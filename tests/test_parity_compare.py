"""Tests for scripts/parity_compare.py.

Covers the matching algorithm, flag detection, aggregate math and the
markdown + json renderers end-to-end. The DB-backed helpers use a
tmp_path SQLite via core.database.set_db_path so the real reverto.db is
never touched.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import get_db, init_db, set_db_path  # noqa: E402
from scripts.parity_compare import (  # noqa: E402
    DEFAULT_MATCH_WINDOW_S,
    Pair,
    _aggregates,
    compute_flags,
    match_deals,
    render_json,
    render_markdown,
    main as parity_main,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point core.database at a per-test SQLite file so seeded rows
    never leak between tests (and the real logs/reverto.db is
    untouched)."""
    db_file = tmp_path / "parity.db"
    set_db_path(db_file)
    init_db()
    yield db_file


def _seed_deal(
    bot_slug: str,
    deal_id: str,
    opened_at: datetime,
    *,
    closed_at: datetime | None = None,
    initial_price: float = 80_000.0,
    pnl_pct: float | None = None,
    status: str = "closed",
    close_reason: str = "take_profit",
    exit_trigger: dict | None = None,
    dca_orders: int = 0,
):
    """Raw INSERT — bypasses PaperDeal/save_deal so tests stay compact
    and don't depend on the full engine type surface. Uses user_id=1
    (the seeded admin) throughout, matching parity_compare's default."""
    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO deals (
                id, user_id, bot_slug, bot_name, side, status, close_reason,
                opened_at, closed_at, initial_price, avg_entry,
                close_price, total_size, leverage, pnl_btc, pnl_pct,
                peak_price, entry_trigger, exit_trigger
            ) VALUES (?, 1, ?, ?, 'long', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                deal_id, bot_slug, bot_slug, status, close_reason,
                opened_at.isoformat(),
                closed_at.isoformat() if closed_at else None,
                initial_price, initial_price,
                initial_price + 100 if closed_at else None,
                0.001,
                0.000001 if pnl_pct else None,
                pnl_pct,
                initial_price + 100,
                None,
                json.dumps(exit_trigger) if exit_trigger else None,
            ),
        )
        for i in range(dca_orders):
            conn.execute(
                """
                INSERT INTO orders (
                    id, user_id, deal_id, bot_slug, order_number, order_type,
                    price, size, fee_btc, placed_at
                ) VALUES (?, 1, ?, ?, ?, 'dca', ?, ?, 0, ?)
                """,
                (
                    f"{deal_id}-dca-{i}",
                    deal_id, bot_slug, i + 2,
                    initial_price - (i + 1) * 200,
                    0.0005,
                    opened_at.isoformat(),
                ),
            )


T0 = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)


def _deal(slug: str, _id: str, t_offset_s: int = 0, **kw) -> dict:
    """In-memory deal dict that matches the shape _load_bot_deals emits.
    Used by pure-function tests that don't need the DB round-trip."""
    base = dict(
        id=_id,
        opened_at=T0 + timedelta(seconds=t_offset_s),
        closed_at=None,
        initial_price=80_000.0,
        avg_entry=80_000.0,
        close_price=None,
        pnl_btc=None,
        pnl_pct=None,
        dca_count=0,
        entry_trigger={},
        exit_trigger={},
        close_reason=None,
        status="open",
    )
    base.update(kw)
    return base


# ── match_deals ─────────────────────────────────────────────────────────────

class TestMatchDeals:

    def test_simple_pair_within_window(self):
        p = [_deal("p", "P-1", 0)]
        le = [_deal("l", "L-1", 10)]
        pairs, unp, unl = match_deals(p, le)
        assert len(pairs) == 1
        assert pairs[0].paper["id"] == "P-1"
        assert pairs[0].live["id"] == "L-1"
        assert pairs[0].delta_s == 10
        assert unp == [] and unl == []

    def test_prefers_closest_candidate(self):
        """Two live candidates inside the window — greedy picks the
        nearest, not the first encountered."""
        p = [_deal("p", "P-1", 0)]
        le = [_deal("l", "L-far", 80), _deal("l", "L-near", 5)]
        pairs, *_ = match_deals(p, le)
        assert pairs[0].live["id"] == "L-near"

    def test_respects_window(self):
        """Delta > window → no match; deal surfaces as unmatched."""
        p = [_deal("p", "P-1", 0)]
        le = [_deal("l", "L-1", DEFAULT_MATCH_WINDOW_S + 30)]
        pairs, unp, unl = match_deals(p, le)
        assert pairs == []
        assert len(unp) == 1 and len(unl) == 1

    def test_no_double_use(self):
        """Two paper deals near one live deal — first claims it, second
        stays unmatched. Prevents shadow-inflation of match rate."""
        p = [_deal("p", "P-1", 0), _deal("p", "P-2", 6)]
        le = [_deal("l", "L-1", 3)]
        pairs, unp, unl = match_deals(p, le)
        assert len(pairs) == 1
        assert pairs[0].paper["id"] == "P-1"
        assert [x["id"] for x in unp] == ["P-2"]
        assert unl == []

    def test_empty_inputs(self):
        assert match_deals([], []) == ([], [], [])

    def test_all_unmatched(self):
        p = [_deal("p", "P-1", 0), _deal("p", "P-2", 10_000)]
        le = [_deal("l", "L-1", 1_000_000)]
        pairs, unp, unl = match_deals(p, le)
        assert pairs == []
        assert {d["id"] for d in unp} == {"P-1", "P-2"}
        assert [d["id"] for d in unl] == ["L-1"]


# ── Flags ───────────────────────────────────────────────────────────────────

class TestFlagDetection:

    def test_timing_warn_fires_above_threshold(self):
        pair = Pair(
            paper=_deal("p", "P-1", 0),
            live=_deal("l", "L-1", 35),
            delta_s=35,
        )
        assert "timing_warn" in compute_flags(pair)

    def test_timing_warn_quiet_below_threshold(self):
        pair = Pair(
            paper=_deal("p", "P-1", 0),
            live=_deal("l", "L-1", 20),
            delta_s=20,
        )
        assert "timing_warn" not in compute_flags(pair)

    def test_price_warn_fires_on_bp_delta(self):
        """+15 bp relative delta (80_000 → 80_120) crosses the 10 bp
        threshold. Basis-point math: 120/80000 = 15 bp."""
        pair = Pair(
            paper=_deal("p", "P-1", 0, initial_price=80_000.0),
            live=_deal("l", "L-1", 5,  initial_price=80_120.0),
            delta_s=5,
        )
        flags = compute_flags(pair)
        assert "price_warn" in flags
        assert "timing_warn" not in flags

    def test_pnl_warn_fires_on_pp_delta(self):
        pair = Pair(
            paper=_deal("p", "P-1", 0, pnl_pct=1.0, status="closed"),
            live=_deal("l", "L-1", 5, pnl_pct=1.8, status="closed"),
            delta_s=5,
        )
        assert "pnl_warn" in compute_flags(pair)

    def test_exit_mismatch_only_when_both_closed(self):
        """Half-closed pairs should NOT fire exit_mismatch — that's an
        unmatched-timing artefact, not an exit-logic bug."""
        half = Pair(
            paper=_deal(
                "p", "P-1", 0, status="closed",
                exit_trigger={"type": "price_tp"},
            ),
            live=_deal(
                "l", "L-1", 5, status="open",
                exit_trigger={},
            ),
            delta_s=5,
        )
        assert "exit_mismatch" not in compute_flags(half)

        both = Pair(
            paper=_deal(
                "p", "P-1", 0, status="closed",
                exit_trigger={"type": "price_tp"},
            ),
            live=_deal(
                "l", "L-1", 5, status="closed",
                exit_trigger={"type": "indicator_tp"},
            ),
            delta_s=5,
        )
        assert "exit_mismatch" in compute_flags(both)

    def test_dca_mismatch_above_threshold(self):
        pair = Pair(
            paper=_deal("p", "P-1", 0, dca_count=1),
            live=_deal("l", "L-1", 5, dca_count=5),
            delta_s=5,
        )
        assert "dca_mismatch" in compute_flags(pair)

    def test_no_flags_on_identical_pair(self):
        pair = Pair(
            paper=_deal("p", "P-1", 0, pnl_pct=1.0, status="closed",
                       exit_trigger={"type": "price_tp"}),
            live=_deal("l", "L-1", 5, pnl_pct=1.0, status="closed",
                       exit_trigger={"type": "price_tp"}),
            delta_s=5,
        )
        assert compute_flags(pair) == []


# ── Aggregates ──────────────────────────────────────────────────────────────

class TestAggregates:

    def test_empty_pairs_returns_empty_dict(self):
        assert _aggregates([]) == {}

    def test_basic_stats(self):
        pairs = [
            Pair(
                paper=_deal("p", "P-1", 0, initial_price=80_000, pnl_pct=1.0),
                live=_deal("l", "L-1", 5, initial_price=80_010, pnl_pct=1.1),
                delta_s=5,
            ),
            Pair(
                paper=_deal("p", "P-2", 100, initial_price=80_100, pnl_pct=-0.5),
                live=_deal("l", "L-2", 115, initial_price=80_100, pnl_pct=-0.4),
                delta_s=15,
            ),
        ]
        agg = _aggregates(pairs)
        assert agg["mean_timing_delta_s"] == pytest.approx(10)
        assert agg["max_timing_delta_s"] == 15
        # Both price deltas ≥ 0, mean > 0.
        assert agg["mean_price_delta_bp"] > 0
        # Live PnL in both cases is higher by 0.1 pp, so mean diff is +0.1.
        assert agg["mean_pnl_delta_pp"] == pytest.approx(0.1, abs=1e-9)


# ── End-to-end via DB fixture ──────────────────────────────────────────────

class TestReportGeneration:
    """End-to-end: seed deals, run main(), inspect output. Uses
    capsys + tmp_path to capture stdout/stderr and a file path."""

    def _seed_happy_path(self):
        """Two matched pairs plus one paper-only extra."""
        _seed_deal(
            "paper_bot", "PAP-1", T0,
            closed_at=T0 + timedelta(minutes=30),
            initial_price=80_000.0, pnl_pct=0.8,
            exit_trigger={"type": "price_tp"},
            dca_orders=1,
        )
        _seed_deal(
            "live_bot", "LIV-1", T0 + timedelta(seconds=12),
            closed_at=T0 + timedelta(minutes=33),
            initial_price=80_020.0, pnl_pct=0.7,
            exit_trigger={"type": "price_tp"},
            dca_orders=1,
        )
        _seed_deal(
            "paper_bot", "PAP-2", T0 + timedelta(hours=2),
            closed_at=T0 + timedelta(hours=3),
            initial_price=79_000.0, pnl_pct=-0.3,
            exit_trigger={"type": "stop_loss"},
            dca_orders=3,
        )
        _seed_deal(
            "live_bot", "LIV-2", T0 + timedelta(hours=2, seconds=40),
            closed_at=T0 + timedelta(hours=3, minutes=1),
            initial_price=79_010.0, pnl_pct=-0.2,
            exit_trigger={"type": "stop_loss"},
            dca_orders=2,
        )
        # Paper-only orphan (no live counterpart within window).
        _seed_deal(
            "paper_bot", "PAP-3", T0 + timedelta(hours=10),
            closed_at=T0 + timedelta(hours=10, minutes=20),
            initial_price=79_500.0, pnl_pct=0.2,
        )

    def test_markdown_report_has_all_sections(self, capsys, tmp_path):
        self._seed_happy_path()
        rc = parity_main([
            "--paper", "paper_bot",
            "--live", "live_bot",
            "--since", "2026-04-18",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "# Parity Compare Report" in out
        assert "## Summary" in out
        assert "## Flags" in out
        assert "## Unmatched paper deals" in out
        assert "Match rate" in out
        # At least one of our pairs is flagged (LIV-2 is 40s off).
        assert "timing_warn" in out

    def test_json_output_is_valid(self, capsys):
        self._seed_happy_path()
        rc = parity_main([
            "--paper", "paper_bot",
            "--live", "live_bot",
            "--json",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["paper_slug"] == "paper_bot"
        assert payload["live_slug"] == "live_bot"
        assert payload["paper_total"] == 3
        assert payload["live_total"] == 2
        assert len(payload["pairs"]) == 2
        assert len(payload["unmatched_paper"]) == 1
        assert "aggregates" in payload

    def test_output_file(self, tmp_path):
        self._seed_happy_path()
        out_file = tmp_path / "report.md"
        rc = parity_main([
            "--paper", "paper_bot",
            "--live", "live_bot",
            "--output", str(out_file),
        ])
        assert rc == 0
        text = out_file.read_text()
        assert "# Parity Compare Report" in text

    def test_empty_db_graceful(self, capsys):
        """No deals in either bot → report renders with 0s, no crash."""
        rc = parity_main([
            "--paper", "nope_paper",
            "--live",  "nope_live",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Paper deals      | 0" in out
        assert "Live deals       | 0" in out
        assert "Matched pairs    | 0" in out

    def test_custom_window(self, capsys):
        """Pairs that match with window=120 must STOP matching at
        window=10 — PAP-2/LIV-2 is 40s apart."""
        self._seed_happy_path()
        rc = parity_main([
            "--paper", "paper_bot",
            "--live", "live_bot",
            "--window", "10",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        # With window=10 only LIV-1 (12s from PAP-1) is outside window,
        # so actually NO pairs match. Adjust expectation: expect 0 pairs.
        assert "Matched pairs    | 0" in out

    def test_stderr_summary_line(self, capsys):
        self._seed_happy_path()
        parity_main(["--paper", "paper_bot", "--live", "live_bot"])
        err = capsys.readouterr().err
        assert "parity:" in err
        assert "match_rate" in err


# ── Renderer smoke tests (pure functions) ──────────────────────────────────

class TestRenderers:

    def test_markdown_empty_inputs(self):
        out = render_markdown(
            "paper", "live", since=None,
            paper_deals=[], live_deals=[], pairs=[],
            unmatched_paper=[], unmatched_live=[],
            window_s=120,
        )
        assert "Paper deals      | 0" in out
        assert "Live deals       | 0" in out
        # No unmatched sections when both lists are empty.
        assert "Unmatched paper deals" not in out

    def test_json_schema_keys(self):
        body = render_json(
            "paper", "live", since=None,
            paper_deals=[], live_deals=[], pairs=[],
            unmatched_paper=[], unmatched_live=[],
            window_s=120,
        )
        payload = json.loads(body)
        for key in (
            "paper_slug", "live_slug", "window_s",
            "pairs", "unmatched_paper", "unmatched_live",
            "flag_counts", "aggregates",
        ):
            assert key in payload

    def test_empty_state_interpretation(self):
        """Both bots with 0 deals must render the "no deals yet"
        message, not the misleading "Low parity" branch."""
        body = render_markdown(
            "paper", "live", since=None,
            paper_deals=[], live_deals=[], pairs=[],
            unmatched_paper=[], unmatched_live=[],
            window_s=120,
        )
        assert "No deals yet in the period" in body
        # "Low parity" must NOT appear — that's the old behaviour we
        # explicitly guarded against.
        assert "Low parity" not in body
