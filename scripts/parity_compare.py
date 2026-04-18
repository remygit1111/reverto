#!/usr/bin/env python3
"""Parity compare — side-by-side diff of a paper bot vs a live-dry bot.

Use case: an operator runs two bots with identical strategy config — one
in mode=paper and one in mode=live with --dry-run. After ≥1 week, this
script pairs their deals by open-time proximity and surfaces where the
two engines disagree. The Phase-1 decision to promote to real-order
execution hinges on paper being a faithful proxy for live-dry.

Output: Markdown report to stdout (or ``--output <file>``). JSON mode
via ``--json`` for pipeline consumption. Summary metrics land on stderr
so a caller can ``| tee report.md`` while still reading the verdict.

The script is intentionally side-effect-free against the DB — it only
SELECTs via core.deal_store.get_deals + a small orders aggregate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import correlation, mean, median
from typing import Optional

# Allow `python scripts/parity_compare.py` invocation without --module.
_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from core.database import get_db, init_db  # noqa: E402
from core.deal_store import get_deals  # noqa: E402


# ── Config knobs ──────────────────────────────────────────────────────────────

DEFAULT_MATCH_WINDOW_S = 120
# Flag thresholds. Kept as module-level constants so a test can pin the
# exact numbers without having to wire them through the CLI.
TIMING_WARN_S     = 30
PRICE_WARN_BP     = 10       # 10 basis points = 0.10 %
PNL_WARN_PP       = 0.5      # 0.5 percentage points
DCA_MISMATCH_MIN  = 2        # flag when |paper_dca − live_dca| > 2


# ── Data plumbing ─────────────────────────────────────────────────────────────

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Tolerate the couple of ISO flavours the engine writes (with or
    without timezone, with or without microseconds). Return None on
    malformed input so the caller can skip the deal instead of
    crashing the whole compare."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Legacy rows (pre-tz-aware engine) are naive UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _dca_counts_by_deal(bot_slug: str) -> dict[str, int]:
    """Single-query aggregate of DCA order counts per deal for a bot.

    One query is cheaper than N per-deal round-trips; the result is a
    {deal_id: count_of_dca_orders} dict that the caller consults while
    building pair metrics.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT deal_id, COUNT(*) AS n
        FROM orders
        WHERE bot_slug = ? AND order_type = 'dca'
        GROUP BY deal_id
        """,
        (bot_slug,),
    ).fetchall()
    return {row["deal_id"]: int(row["n"]) for row in rows}


def _load_bot_deals(slug: str, since: Optional[datetime]) -> list[dict]:
    """Pull deals + DCA counts for one bot. Filters by opened_at >= since
    in Python so we don't have to re-shape the deal_store SQL."""
    raw = get_deals(bot_slug=slug, limit=100_000)
    dca = _dca_counts_by_deal(slug)

    out: list[dict] = []
    for d in raw:
        opened = _parse_iso(d.get("opened_at"))
        if opened is None:
            continue
        if since is not None and opened < since:
            continue
        closed = _parse_iso(d.get("closed_at"))
        out.append({
            "id":            d["id"],
            "opened_at":     opened,
            "closed_at":     closed,
            "initial_price": d.get("initial_price"),
            "avg_entry":     d.get("avg_entry"),
            "close_price":   d.get("close_price"),
            "pnl_btc":       d.get("pnl_btc"),
            "pnl_pct":       d.get("pnl_pct"),
            "dca_count":     dca.get(d["id"], 0),
            "entry_trigger": d.get("entry_trigger") or {},
            "exit_trigger":  d.get("exit_trigger") or {},
            "close_reason":  d.get("close_reason"),
            "status":        d.get("status"),
        })
    out.sort(key=lambda x: x["opened_at"])
    return out


# ── Matching ──────────────────────────────────────────────────────────────────

@dataclass
class Pair:
    paper: dict
    live: dict
    delta_s: float
    flags: list[str] = field(default_factory=list)


def match_deals(
    paper_deals: list[dict],
    live_deals: list[dict],
    window_s: int = DEFAULT_MATCH_WINDOW_S,
) -> tuple[list[Pair], list[dict], list[dict]]:
    """Greedy nearest-neighbour match by open time.

    Each paper deal consumes at most one live deal, chosen as the
    unclaimed live deal whose opened_at is closest (absolute delta).
    Live deals outside the window on any paper candidate are
    unmatched. The algorithm is O(N*M) which is fine for the
    1–10k-deal volumes this tool targets; a sweep-line variant is
    trivial if throughput becomes a concern.
    """
    pairs: list[Pair] = []
    used_live: set[str] = set()

    for p in paper_deals:
        best: Optional[dict] = None
        best_delta = float("inf")
        for live_d in live_deals:
            if live_d["id"] in used_live:
                continue
            delta = abs(
                (live_d["opened_at"] - p["opened_at"]).total_seconds()
            )
            if delta <= window_s and delta < best_delta:
                best = live_d
                best_delta = delta
        if best is not None:
            pairs.append(Pair(paper=p, live=best, delta_s=best_delta))
            used_live.add(best["id"])

    matched_paper_ids = {pair.paper["id"] for pair in pairs}
    unmatched_paper = [p for p in paper_deals if p["id"] not in matched_paper_ids]
    unmatched_live = [le for le in live_deals if le["id"] not in used_live]
    return pairs, unmatched_paper, unmatched_live


# ── Per-pair flags + metrics ──────────────────────────────────────────────────

def _entry_price(deal: dict) -> Optional[float]:
    """Prefer initial_price (deterministic, base order), fall back to
    avg_entry when initial is missing (shouldn't happen but defensive)."""
    v = deal.get("initial_price")
    if v is None:
        v = deal.get("avg_entry")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _exit_trigger_kind(trigger: dict | None) -> str:
    """Normalise the free-form exit_trigger dict into a single string
    we can compare across engines. Falls back to the close_reason
    column for engines that didn't populate the JSON."""
    if isinstance(trigger, dict):
        for key in ("type", "kind", "reason"):
            val = trigger.get(key)
            if isinstance(val, str) and val:
                return val
    return "—"


def _price_delta_bp(p_price: Optional[float], l_price: Optional[float]) -> Optional[float]:
    """Return the live-over-paper price delta in basis points
    (1 bp = 0.01 %). None when either side is missing or paper is 0."""
    if p_price is None or l_price is None or p_price == 0:
        return None
    return (l_price - p_price) / p_price * 10_000.0


def compute_flags(pair: Pair) -> list[str]:
    """Per-pair flags. See module-level threshold constants."""
    flags: list[str] = []
    if pair.delta_s > TIMING_WARN_S:
        flags.append("timing_warn")

    bp = _price_delta_bp(_entry_price(pair.paper), _entry_price(pair.live))
    if bp is not None and abs(bp) > PRICE_WARN_BP:
        flags.append("price_warn")

    p_pnl, l_pnl = pair.paper.get("pnl_pct"), pair.live.get("pnl_pct")
    if p_pnl is not None and l_pnl is not None:
        if abs(float(p_pnl) - float(l_pnl)) > PNL_WARN_PP:
            flags.append("pnl_warn")

    p_exit = _exit_trigger_kind(pair.paper.get("exit_trigger"))
    l_exit = _exit_trigger_kind(pair.live.get("exit_trigger"))
    # Only flag when BOTH sides are closed — a half-open pair is an
    # unmatched-timing thing, not a genuine exit disagreement.
    if (
        pair.paper.get("status") == "closed"
        and pair.live.get("status") == "closed"
        and p_exit != l_exit
    ):
        flags.append("exit_mismatch")

    if abs(pair.paper.get("dca_count", 0) - pair.live.get("dca_count", 0)) > DCA_MISMATCH_MIN:
        flags.append("dca_mismatch")
    return flags


# ── Aggregates + interpretation ───────────────────────────────────────────────

def _aggregates(pairs: list[Pair]) -> dict:
    if not pairs:
        return {}
    deltas = [pair.delta_s for pair in pairs]
    price_bps = [
        bp for pair in pairs
        if (bp := _price_delta_bp(
            _entry_price(pair.paper), _entry_price(pair.live)
        )) is not None
    ]
    pnl_diffs = [
        float(pair.live["pnl_pct"]) - float(pair.paper["pnl_pct"])
        for pair in pairs
        if pair.paper.get("pnl_pct") is not None
        and pair.live.get("pnl_pct") is not None
    ]
    paper_pnls = [
        float(pair.paper["pnl_pct"]) for pair in pairs
        if pair.paper.get("pnl_pct") is not None
        and pair.live.get("pnl_pct") is not None
    ]
    live_pnls = [
        float(pair.live["pnl_pct"]) for pair in pairs
        if pair.paper.get("pnl_pct") is not None
        and pair.live.get("pnl_pct") is not None
    ]

    agg = {
        "mean_timing_delta_s":   mean(deltas),
        "median_timing_delta_s": median(deltas),
        "max_timing_delta_s":    max(deltas),
    }
    if price_bps:
        agg["mean_price_delta_bp"] = mean(price_bps)
        agg["median_price_delta_bp"] = median(price_bps)
    if pnl_diffs:
        agg["mean_pnl_delta_pp"] = mean(pnl_diffs)
        agg["median_pnl_delta_pp"] = median(pnl_diffs)
    # correlation needs ≥ 2 data points AND non-zero variance on both
    # sides. Guard both so a uniformly-flat PnL dataset doesn't throw.
    if len(paper_pnls) >= 10 and len(set(paper_pnls)) > 1 and len(set(live_pnls)) > 1:
        try:
            agg["pnl_correlation"] = correlation(paper_pnls, live_pnls)
        except Exception:
            pass
    return agg


def _interpretation(
    match_rate: float,
    agg: dict,
    flag_counts: dict,
    total_paper: int = 0,
    total_live: int = 0,
) -> list[str]:
    """Deterministic, thresholded one-liners. Caller turns these into
    bullets; empty list = nothing noteworthy."""
    out: list[str] = []

    # Empty-state guard: when neither bot has produced any deals in
    # the period, the match-rate branches below all collapse to
    # "Low parity" which is misleading — there's nothing to compare.
    # Surface a dedicated message so the operator knows to keep
    # collecting data rather than chase a phantom divergence.
    if total_paper == 0 and total_live == 0:
        return [
            "No deals yet in the period — wait for bots to generate "
            "data before drawing conclusions.",
        ]

    if match_rate >= 0.85:
        out.append(
            "High parity — paper engine is a faithful proxy for live timing."
        )
    elif match_rate >= 0.60:
        out.append(
            "Partial parity — many deals line up but a non-trivial fraction "
            "is engine-exclusive. Investigate unmatched tables."
        )
    else:
        out.append(
            "Low parity — most deals have no counterpart. The two bots are "
            "making meaningfully different decisions."
        )

    mt = agg.get("mean_timing_delta_s")
    if mt is not None and mt > 20:
        out.append(
            f"Noticeable timing drift (mean {mt:.1f}s) — check price-feed "
            "cache TTL and main_paper vs main_live poll intervals."
        )

    corr = agg.get("pnl_correlation")
    if corr is not None:
        if corr >= 0.90:
            out.append(
                f"PnL correlation {corr:.2f} — strategy outcomes track "
                "tightly. Phase-3 preparation is justified."
            )
        elif corr < 0.70:
            out.append(
                f"PnL correlation only {corr:.2f} — paper PnL does not "
                "predict live PnL reliably. Do not extrapolate."
            )

    exit_mis = flag_counts.get("exit_mismatch", 0)
    total = sum(flag_counts.get("_pair_total", [0])) if isinstance(
        flag_counts.get("_pair_total"), list
    ) else flag_counts.get("_pair_total", 0)
    if total and exit_mis / total > 0.10:
        out.append(
            f"Exit logic divergence — {exit_mis}/{total} pairs exited on "
            "different triggers. Investigate exit-trigger evaluation."
        )
    return out


# ── Renderers ─────────────────────────────────────────────────────────────────

def _fmt_num(v, spec: str = ".2f") -> str:
    if v is None:
        return "—"
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "—"


def render_markdown(
    paper_slug: str,
    live_slug: str,
    since: Optional[datetime],
    paper_deals: list[dict],
    live_deals: list[dict],
    pairs: list[Pair],
    unmatched_paper: list[dict],
    unmatched_live: list[dict],
    window_s: int,
) -> str:
    """Full markdown report. See module docstring for intended use."""
    flag_counts: dict[str, int] = {
        "timing_warn": 0, "price_warn": 0, "pnl_warn": 0,
        "exit_mismatch": 0, "dca_mismatch": 0,
    }
    for pair in pairs:
        pair.flags = compute_flags(pair)
        for f in pair.flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    flag_counts["_pair_total"] = len(pairs)

    agg = _aggregates(pairs)
    match_rate = (len(pairs) / len(paper_deals)) if paper_deals else 0.0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    since_str = since.date().isoformat() if since else "all time"

    lines: list[str] = []
    lines.append("# Parity Compare Report")
    lines.append("")
    lines.append(f"**Paper bot:** `{paper_slug}`  ")
    lines.append(f"**Live bot:** `{live_slug}`  ")
    lines.append(f"**Period:** {since_str} → {now}  ")
    lines.append(f"**Matching window:** {window_s}s")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Paper deals      | {len(paper_deals)} |")
    lines.append(f"| Live deals       | {len(live_deals)} |")
    lines.append(f"| Matched pairs    | {len(pairs)} |")
    lines.append(f"| Match rate       | {match_rate * 100:.1f}% |")
    lines.append(f"| Unmatched paper  | {len(unmatched_paper)} |")
    lines.append(f"| Unmatched live   | {len(unmatched_live)} |")
    lines.append("")

    if agg:
        lines.append("| Aggregate | Value |")
        lines.append("|---|---|")
        lines.append(
            f"| Mean entry timing Δ   | {_fmt_num(agg.get('mean_timing_delta_s'), '.2f')}s |"
        )
        lines.append(
            f"| Median entry timing Δ | {_fmt_num(agg.get('median_timing_delta_s'), '.2f')}s |"
        )
        lines.append(
            f"| Max entry timing Δ    | {_fmt_num(agg.get('max_timing_delta_s'), '.2f')}s |"
        )
        if "mean_price_delta_bp" in agg:
            lines.append(
                f"| Mean entry price Δ    | {_fmt_num(agg['mean_price_delta_bp'], '+.2f')} bp |"
            )
        if "mean_pnl_delta_pp" in agg:
            lines.append(
                f"| Mean PnL Δ (live − paper) | {_fmt_num(agg['mean_pnl_delta_pp'], '+.3f')} pp |"
            )
        if "pnl_correlation" in agg:
            lines.append(
                f"| PnL correlation       | {_fmt_num(agg['pnl_correlation'], '.3f')} |"
            )
        lines.append("")

    lines.append("## Flags")
    lines.append("")
    lines.append(
        f"- timing_warn (> {TIMING_WARN_S}s): {flag_counts['timing_warn']} pairs"
    )
    lines.append(
        f"- price_warn (> {PRICE_WARN_BP}bp): {flag_counts['price_warn']} pairs"
    )
    lines.append(
        f"- pnl_warn (> {PNL_WARN_PP}pp): {flag_counts['pnl_warn']} pairs"
    )
    lines.append(f"- exit_mismatch: {flag_counts['exit_mismatch']} pairs")
    lines.append(
        f"- dca_mismatch (Δdca > {DCA_MISMATCH_MIN}): {flag_counts['dca_mismatch']} pairs"
    )
    lines.append("")

    if unmatched_paper:
        lines.append(f"## Unmatched paper deals ({len(unmatched_paper)})")
        lines.append("")
        lines.append("| ID | Opened | Entry | Close reason |")
        lines.append("|---|---|---|---|")
        for d in unmatched_paper[:50]:
            lines.append(
                f"| {d['id']} | {d['opened_at'].isoformat(timespec='seconds')} "
                f"| {_fmt_num(d.get('initial_price'), '.2f')} "
                f"| {d.get('close_reason') or 'open'} |"
            )
        if len(unmatched_paper) > 50:
            lines.append(f"| … | +{len(unmatched_paper) - 50} more | | |")
        lines.append("")

    if unmatched_live:
        lines.append(f"## Unmatched live deals ({len(unmatched_live)})")
        lines.append("")
        lines.append("| ID | Opened | Entry | Close reason |")
        lines.append("|---|---|---|---|")
        for d in unmatched_live[:50]:
            lines.append(
                f"| {d['id']} | {d['opened_at'].isoformat(timespec='seconds')} "
                f"| {_fmt_num(d.get('initial_price'), '.2f')} "
                f"| {d.get('close_reason') or 'open'} |"
            )
        if len(unmatched_live) > 50:
            lines.append(f"| … | +{len(unmatched_live) - 50} more | | |")
        lines.append("")

    flagged = [pair for pair in pairs if pair.flags]
    if flagged:
        lines.append(f"## Flagged pairs ({len(flagged)})")
        lines.append("")
        for pair in flagged[:30]:
            p, live_d = pair.paper, pair.live
            p_price = _entry_price(p)
            l_price = _entry_price(live_d)
            bp = _price_delta_bp(p_price, l_price)
            lines.append(
                f"### {p['id']} ↔ {live_d['id']}  "
                f"({', '.join(pair.flags)})"
            )
            lines.append("")
            lines.append(
                f"- Paper opened: {p['opened_at'].isoformat(timespec='seconds')} "
                f"@ {_fmt_num(p_price, '.2f')}"
            )
            lines.append(
                f"- Live opened:  {live_d['opened_at'].isoformat(timespec='seconds')} "
                f"@ {_fmt_num(l_price, '.2f')}"
            )
            lines.append(f"- Timing Δ: {pair.delta_s:.1f}s")
            if bp is not None:
                lines.append(
                    f"- Price Δ: {_fmt_num(bp, '+.1f')} bp"
                )
            p_pnl = p.get("pnl_pct")
            l_pnl = live_d.get("pnl_pct")
            if p_pnl is not None and l_pnl is not None:
                lines.append(
                    f"- Paper PnL: {_fmt_num(p_pnl, '+.3f')}%  "
                    f"Live PnL: {_fmt_num(l_pnl, '+.3f')}%"
                )
            lines.append(
                f"- DCA: paper={p.get('dca_count', 0)} / live={live_d.get('dca_count', 0)}"
            )
            lines.append(
                f"- Exit: paper={_exit_trigger_kind(p.get('exit_trigger'))} "
                f"/ live={_exit_trigger_kind(live_d.get('exit_trigger'))}"
            )
            lines.append("")
        if len(flagged) > 30:
            lines.append(f"_+{len(flagged) - 30} more flagged pairs (truncated)._")
            lines.append("")

    interp = _interpretation(
        match_rate, agg, flag_counts,
        total_paper=len(paper_deals), total_live=len(live_deals),
    )
    if interp:
        lines.append("## Interpretation")
        lines.append("")
        for item in interp:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines)


def render_json(
    paper_slug: str,
    live_slug: str,
    since: Optional[datetime],
    paper_deals: list[dict],
    live_deals: list[dict],
    pairs: list[Pair],
    unmatched_paper: list[dict],
    unmatched_live: list[dict],
    window_s: int,
) -> str:
    """Same data, machine-readable shape. Timestamps as ISO strings."""
    for pair in pairs:
        pair.flags = compute_flags(pair)
    flag_counts: dict[str, int] = {}
    for pair in pairs:
        for f in pair.flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    def _deal(deal: dict) -> dict:
        return {
            "id":            deal["id"],
            "opened_at":     deal["opened_at"].isoformat(),
            "closed_at":     deal["closed_at"].isoformat() if deal.get("closed_at") else None,
            "initial_price": deal.get("initial_price"),
            "pnl_pct":       deal.get("pnl_pct"),
            "dca_count":     deal.get("dca_count", 0),
            "status":        deal.get("status"),
            "exit_trigger":  _exit_trigger_kind(deal.get("exit_trigger")),
        }

    payload = {
        "paper_slug": paper_slug,
        "live_slug": live_slug,
        "since": since.isoformat() if since else None,
        "window_s": window_s,
        "paper_total": len(paper_deals),
        "live_total": len(live_deals),
        "pairs": [
            {
                "paper": _deal(pair.paper),
                "live": _deal(pair.live),
                "delta_s": pair.delta_s,
                "flags": pair.flags,
            }
            for pair in pairs
        ],
        "unmatched_paper": [_deal(d) for d in unmatched_paper],
        "unmatched_live":  [_deal(d) for d in unmatched_live],
        "flag_counts": flag_counts,
        "aggregates": _aggregates(pairs),
    }
    return json.dumps(payload, indent=2, default=str)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = _parse_iso(value)
    if dt is None:
        raise SystemExit(f"--since: invalid ISO date/datetime: {value!r}")
    return dt


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a paper bot's deals vs a live-dry bot's deals and "
            "highlight divergence. Side-effect-free (SELECT-only)."
        ),
    )
    parser.add_argument("--paper", required=True, help="Paper bot slug")
    parser.add_argument("--live", required=True, help="Live (dry-run) bot slug")
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date/datetime cut-off (e.g. 2026-04-18). Default: all time.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_MATCH_WINDOW_S,
        help=f"Match window in seconds. Default: {DEFAULT_MATCH_WINDOW_S}.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of Markdown.",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Write report to this file. Default: stdout.",
    )
    args = parser.parse_args(argv)

    init_db()
    since = _parse_since(args.since)

    paper_deals = _load_bot_deals(args.paper, since)
    live_deals  = _load_bot_deals(args.live, since)

    pairs, unm_p, unm_l = match_deals(paper_deals, live_deals, args.window)

    renderer = render_json if args.json else render_markdown
    body = renderer(
        args.paper, args.live, since,
        paper_deals, live_deals, pairs, unm_p, unm_l,
        args.window,
    )

    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
    else:
        print(body)

    match_rate = (len(pairs) / len(paper_deals)) if paper_deals else 0.0
    sys.stderr.write(
        f"parity: paper={len(paper_deals)} live={len(live_deals)} "
        f"pairs={len(pairs)} match_rate={match_rate * 100:.1f}%\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
