#!/usr/bin/env python3
"""Idempotent importer for the audit/pentest findings tracker.

Usage:

    .venv/bin/python3 scripts/seed_audit_findings.py
        [--seed data/findings_seed.yaml]
        [--quiet]

Reads a YAML file with the canonical seed shape and inserts each
finding via ``audit_findings_store.upsert_finding``. The store's
INSERT OR IGNORE semantics make re-runs safe — operator edits made
through the admin UI to ``status`` / ``notes`` / ``resolution_ref``
are never overwritten by a subsequent seed pass.

Exit codes:
    0 — success (with summary printed to stdout unless --quiet)
    1 — YAML parse error or schema validation error
    2 — DB layer error (table missing, etc.)

The portal lifespan invokes this implicitly on first boot when the
``audit_findings`` table is empty (see web.app._maybe_seed_audit_findings).
Direct invocation is for re-imports after a YAML update.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# Ensure the project root is on sys.path so ``core.*`` imports work
# whether the script is launched from the project root or elsewhere.
_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT))

from core import audit_findings_store  # noqa: E402
from core.database import init_db  # noqa: E402

DEFAULT_SEED_PATH = _ROOT / "data" / "findings_seed.yaml"


def load_seed(path: Path) -> list[dict]:
    """Parse the YAML file and return the ``findings:`` list.

    Raises ValueError with operator-readable detail on malformed
    input so the CLI exit-code path can route to stderr.
    """
    if not path.is_file():
        raise ValueError(f"seed file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in {path}: {e}")
    if not isinstance(raw, dict) or "findings" not in raw:
        raise ValueError(
            f"seed file {path} must be a mapping with a top-level "
            "'findings:' list"
        )
    items = raw["findings"]
    if not isinstance(items, list):
        raise ValueError("'findings' must be a list")
    return items


def import_seed(items: list[dict], quiet: bool = False) -> tuple[int, int]:
    """Apply each entry through ``upsert_finding``. Returns
    ``(inserted, skipped)``.

    Per-entry ValueErrors are caught and logged; the import does not
    abort on a single bad row so the seed can be progressively
    cleaned up even if some entries are malformed.
    """
    inserted = 0
    skipped = 0
    bad = 0
    for entry in items:
        try:
            ok = audit_findings_store.upsert_finding(
                finding_id=entry["finding_id"],
                source_doc=entry["source_doc"],
                severity=entry["severity"],
                status=entry["status"],
                title=entry["title"],
                description=entry.get("description") or "",
                resolution_ref=entry.get("resolution_ref"),
                notes=entry.get("notes") or "",
            )
        except (KeyError, ValueError) as e:
            bad += 1
            if not quiet:
                print(
                    f"  [skip-bad] {entry.get('finding_id', '?')}: {e}",
                    file=sys.stderr,
                )
            continue
        if ok:
            inserted += 1
        else:
            skipped += 1
    if not quiet:
        total = inserted + skipped + bad
        print(
            f"Seed import: {inserted} inserted, {skipped} already-present, "
            f"{bad} malformed (of {total} entries).",
        )
    return inserted, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--seed",
        type=Path,
        default=DEFAULT_SEED_PATH,
        help=f"YAML seed file (default: {DEFAULT_SEED_PATH})",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="suppress summary output",
    )
    args = parser.parse_args()

    try:
        items = load_seed(args.seed)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        # init_db is idempotent — safe to call on every import to
        # ensure the audit_findings table exists.
        init_db()
        import_seed(items, quiet=args.quiet)
    except Exception as e:
        print(f"DB error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
