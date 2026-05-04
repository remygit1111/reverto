"""Tests for ``core.paths.purge_bot`` — PT-v4-FS-001.

The helper removes every (user_id, bot_slug)-scoped artefact except
the YAML. Pre-fix DELETE /api/bots/{slug} only unlinked the YAML,
which let the engine rehydrate from a leftover state.json when the
bot was recreated under the same slug — inheriting balance, open
deals, drawdown peak and PnL history.

These tests pin the contract:
  * Every expected file type is removed.
  * DB rows for the target (user, slug) are gone.
  * YAML is intentionally NOT removed.
  * Audit history (logs/audit.log etc.) is intentionally NOT removed.
  * Other users / other slugs are isolated.
  * DB step is transactional (partial-failure rolls back).
  * The summary dict matches the documented shape.
  * Idempotent on a second call.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths  # noqa: E402
from core.database import get_db  # noqa: E402


# ── Sandbox helpers ────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect ``core.paths.BASE_DIR`` to a tmp tree so every helper
    in core/paths writes inside the sandbox. The autouse
    ``_isolate_reverto_db`` fixture in tests/conftest.py already
    points the SQLite DB at a tmp file, so we get DB isolation for
    free.
    """
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    return tmp_path


def _seed_files(user_id: int, slug: str) -> dict[str, Path]:
    """Create every file purge_bot is expected to remove. Returns a
    dict of {label: path} so individual assertions can be precise
    about which file disappeared.
    """
    p: dict[str, Path] = {
        "state":   paths.bot_state_path(user_id, slug),
        "lock":    paths.bot_state_lock_path(user_id, slug),
        "trigger": paths.bot_manual_trigger_path(user_id, slug),
        "pid":     paths.bot_pid_path(user_id, slug),
        "ml":      paths.user_ml_results_path(user_id, slug),
        "log":     paths.bot_log_path(user_id, slug),
    }
    for path in p.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed")

    # Rotated logs.
    logs = paths.user_logs_dir(user_id)
    for n in (1, 2, 3):
        rotated = logs / f"{slug}.log.{n}"
        rotated.write_text(f"rotated {n}")
        p[f"log_{n}"] = rotated

    # Sentinel files (3 actions × 1 deal id each is enough).
    deal_id = "202604191342-0001"
    for action in ("edit", "close", "cancel"):
        sentinel = logs / f"{slug}.deal_{action}_{deal_id}"
        sentinel.write_text("seed")
        p[f"sentinel_{action}"] = sentinel

    return p


def _seed_yaml(user_id: int, slug: str) -> Path:
    yaml_path = paths.bot_yaml_path(user_id, slug)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text("bot:\n  name: " + slug + "\n")
    return yaml_path


def _ensure_user(user_id: int) -> None:
    """Insert a placeholder users row so deals.user_id FK is happy.
    Idempotent: ``INSERT OR IGNORE`` so re-seeding for a second test
    fixture under the same user_id is a no-op."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, role, active) "
        "VALUES (?, ?, 'user', 1)",
        (user_id, f"u{user_id}"),
    )
    conn.commit()


def _seed_db_rows(user_id: int, slug: str) -> dict[str, int]:
    """Insert representative rows into every bot-scoped table so the
    purge has something to delete. Returns row counts per table at
    seed time so assertions can verify "before" counts."""
    _ensure_user(user_id)
    conn = get_db()
    deal_id = f"d-{user_id}-{slug}-1"
    conn.execute(
        "INSERT INTO deals (id, user_id, bot_slug, bot_name, status, "
        "opened_at, initial_price, total_size) "
        "VALUES (?, ?, ?, ?, 'open', '2026-05-04', 80000, 0.001)",
        (deal_id, user_id, slug, slug),
    )
    conn.execute(
        "INSERT INTO orders (id, user_id, deal_id, bot_slug, "
        "order_number, order_type, price, size, placed_at) "
        "VALUES (?, ?, ?, ?, 1, 'base', 80000, 0.001, '2026-05-04')",
        (f"o-{deal_id}", user_id, deal_id, slug),
    )
    conn.execute(
        "INSERT INTO chart_annotations (user_id, bot_slug, type, "
        "timeframe, x1, y1) VALUES (?, ?, 'note', '1h', 1, 80000)",
        (user_id, slug),
    )
    conn.execute(
        "INSERT INTO backtest_runs (user_id, bot_slug, bot_name, "
        "start_date, end_date, timeframe, initial_balance_btc) "
        "VALUES (?, ?, ?, '2026-04-01', '2026-04-30', '1h', 0.1)",
        (user_id, slug, slug),
    )
    conn.commit()
    return {"deals": 1, "orders": 1, "chart_annotations": 1, "backtest_runs": 1}


def _count_db_rows(user_id: int, slug: str) -> dict[str, int]:
    conn = get_db()
    out: dict[str, int] = {}
    for tbl in ("deals", "orders", "chart_annotations", "backtest_runs"):
        # orders join via deal_id — instead use bot_slug column directly.
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {tbl} "
            "WHERE user_id=? AND bot_slug=?",
            (user_id, slug),
        ).fetchone()
        out[tbl] = row["n"]
    return out


# ── Filesystem coverage ────────────────────────────────────────────────────


class TestPurgeRemovesAllKnownFiles:
    """Every file type listed in the docstring must disappear."""

    def test_all_seeded_files_removed(self, sandbox):
        seeded = _seed_files(1, "alpha")
        # YAML NOT seeded for this test — focus is filesystem.
        for label, path in seeded.items():
            assert path.exists(), f"setup error: {label} not seeded"

        summary = paths.purge_bot(1, "alpha")

        for label, path in seeded.items():
            assert not path.exists(), (
                f"{label} ({path}) survived purge — files_failed: "
                f"{summary['files_failed']}"
            )

    def test_files_removed_count_is_accurate(self, sandbox):
        seeded = _seed_files(1, "beta")
        summary = paths.purge_bot(1, "beta")
        # Every seeded file should be counted exactly once.
        assert summary["files_removed"] == len(seeded), (
            f"expected {len(seeded)} removals, got "
            f"{summary['files_removed']}"
        )
        assert summary["files_failed"] == []

    def test_handles_missing_files(self, sandbox):
        """Only some files exist — purge reports only what it really
        removed, no false counts."""
        # Only seed state.json and the active log.
        state = paths.bot_state_path(1, "gamma")
        log = paths.bot_log_path(1, "gamma")
        state.write_text("only-state")
        log.write_text("only-log")

        summary = paths.purge_bot(1, "gamma")
        assert summary["files_removed"] == 2
        assert summary["files_failed"] == []
        assert not state.exists()
        assert not log.exists()


# ── DB coverage ───────────────────────────────────────────────────────────


class TestPurgeRemovesDbRows:
    """Bot-scoped rows in deals / orders / chart_annotations /
    backtest_runs must all go."""

    def test_target_user_slug_rows_removed(self, sandbox):
        before = _seed_db_rows(7, "alpha")
        assert _count_db_rows(7, "alpha") == before

        summary = paths.purge_bot(7, "alpha")

        after = _count_db_rows(7, "alpha")
        assert after == {
            "deals": 0, "orders": 0,
            "chart_annotations": 0, "backtest_runs": 0,
        }
        # Summary records the rowcount per table.
        assert summary["db_rows_removed"]["deals"] == 1
        assert summary["db_rows_removed"]["orders"] == 1
        assert summary["db_rows_removed"]["chart_annotations"] == 1
        assert summary["db_rows_removed"]["backtest_runs"] == 1

    def test_db_step_is_transactional(self, monkeypatch, sandbox):
        """Simulate a failure on the chart_annotations DELETE — the
        transaction must roll back so the previously-deleted deals
        + orders rows are also restored. Pre-fix shape (no
        transaction) would leave deals gone and chart_annotations
        intact, which is the worst kind of half-deletion.

        sqlite3.Connection's C-extension methods can't be patched
        directly, so we proxy through a small wrapper that delegates
        every attribute access to the real connection except
        ``execute`` — that one short-circuits to raise on the
        chart_annotations DELETE.
        """
        _seed_db_rows(9, "tx")
        before = _count_db_rows(9, "tx")
        assert before["deals"] == 1

        real_conn = get_db()

        class _BoomingProxy:
            """Delegates everything except a targeted ``execute``."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=None):
                if (
                    "chart_annotations" in sql.lower()
                    and sql.strip().upper().startswith("DELETE")
                ):
                    raise sqlite3.OperationalError(
                        "simulated mid-purge failure"
                    )
                return (
                    self._inner.execute(sql, params)
                    if params is not None
                    else self._inner.execute(sql)
                )

            def __enter__(self):
                # Ditto: ``with conn:`` opens an implicit transaction
                # — Python's sqlite3.Connection does it via __enter__.
                return self._inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                return self._inner.__exit__(exc_type, exc, tb)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        proxy = _BoomingProxy(real_conn)
        monkeypatch.setattr("core.database.get_db", lambda: proxy)

        summary = paths.purge_bot(9, "tx")

        # Roll-back: every table back to pre-purge counts.
        after = _count_db_rows(9, "tx")
        assert after == before, (
            f"transaction did not roll back; before={before} after={after}"
        )
        # Caller sees the warning so they know to retry.
        assert summary["warnings"], (
            "expected a warning entry recording the DB failure"
        )

    def test_db_failure_does_not_block_filesystem(
        self, monkeypatch, sandbox,
    ):
        """Even when the DB step fails, the filesystem step should
        still proceed — the bot's leftover state files are still
        worth removing so the engine can't rehydrate from them."""
        _seed_db_rows(11, "fs-only")
        seeded = _seed_files(11, "fs-only")

        real_conn = get_db()

        class _AlwaysBoomProxy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=None):
                raise sqlite3.OperationalError("DB temporarily down")

            def __enter__(self):
                return self._inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                return self._inner.__exit__(exc_type, exc, tb)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        monkeypatch.setattr(
            "core.database.get_db", lambda: _AlwaysBoomProxy(real_conn),
        )

        summary = paths.purge_bot(11, "fs-only")

        # DB warnings recorded but files still gone.
        assert summary["warnings"]
        for label, path in seeded.items():
            assert not path.exists(), (
                f"{label} survived purge despite DB failure"
            )


# ── Preservation guarantees ───────────────────────────────────────────────


class TestPurgePreservesYaml:
    """YAML-last principle: purge_bot never touches the YAML so a
    partial-failure leaves the bot re-deletable for retry."""

    def test_yaml_survives_purge(self, sandbox):
        yaml_path = _seed_yaml(1, "delta")
        _seed_files(1, "delta")
        paths.purge_bot(1, "delta")
        assert yaml_path.exists(), (
            "YAML must survive purge — caller is responsible for the "
            "final unlink (see route handler)"
        )


class TestPurgePreservesAuditHistory:
    """Audit history is permanent. The audit log files live under
    ``logs/`` (not under ``logs/<uid>/``) so they're naturally
    out-of-scope for a per-user-per-slug purge — but pin the
    invariant so a future schema change that introduces per-bot
    audit storage doesn't accidentally start wiping audit rows."""

    def test_audit_log_file_outside_scope(self, sandbox):
        # Place a fake top-level audit.log to verify purge doesn't
        # walk outside logs/<uid>/.
        audit_log = sandbox / "logs" / "audit.log"
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        audit_log.write_text("audit history")
        _seed_files(1, "epsilon")
        paths.purge_bot(1, "epsilon")
        assert audit_log.exists()
        assert audit_log.read_text() == "audit history"


# ── Isolation ─────────────────────────────────────────────────────────────


class TestPurgeUserIsolation:
    """User A's purge must not touch user B's identical-slug bot."""

    def test_other_users_state_intact(self, sandbox):
        a_files = _seed_files(1, "shared")
        b_files = _seed_files(2, "shared")
        _seed_db_rows(1, "shared")
        _seed_db_rows(2, "shared")

        paths.purge_bot(1, "shared")

        # User A: gone.
        for label, path in a_files.items():
            assert not path.exists(), f"user 1 / {label} survived"
        assert _count_db_rows(1, "shared") == {
            "deals": 0, "orders": 0,
            "chart_annotations": 0, "backtest_runs": 0,
        }
        # User B: untouched.
        for label, path in b_files.items():
            assert path.exists(), f"user 2 / {label} was wrongly removed"
        assert _count_db_rows(2, "shared") == {
            "deals": 1, "orders": 1,
            "chart_annotations": 1, "backtest_runs": 1,
        }


class TestPurgeSlugIsolation:
    """Same user, different slugs — only the target slug goes."""

    def test_other_slugs_state_intact(self, sandbox):
        foo_files = _seed_files(1, "foo")
        bar_files = _seed_files(1, "bar")
        _seed_db_rows(1, "foo")
        _seed_db_rows(1, "bar")

        paths.purge_bot(1, "foo")

        for label, path in foo_files.items():
            assert not path.exists(), f"foo / {label} survived"
        assert _count_db_rows(1, "foo") == {
            "deals": 0, "orders": 0,
            "chart_annotations": 0, "backtest_runs": 0,
        }
        for label, path in bar_files.items():
            assert path.exists(), f"bar / {label} was wrongly removed"
        assert _count_db_rows(1, "bar") == {
            "deals": 1, "orders": 1,
            "chart_annotations": 1, "backtest_runs": 1,
        }


# ── Idempotency + summary shape ───────────────────────────────────────────


class TestPurgeIdempotent:
    """Running purge twice in a row must not error and the second
    call must report zero removals."""

    def test_second_call_is_clean(self, sandbox):
        _seed_files(1, "twice")
        _seed_db_rows(1, "twice")
        first = paths.purge_bot(1, "twice")
        assert first["files_removed"] > 0

        second = paths.purge_bot(1, "twice")
        assert second["files_removed"] == 0
        assert second["files_failed"] == []
        # Every DB rowcount is 0 — nothing left to delete.
        assert all(v == 0 for v in second["db_rows_removed"].values())


class TestPurgeReturnSummary:
    """Pin the dict shape so callers (route handler + tests) can
    rely on the keys being present even on the no-op path."""

    def test_returned_keys(self, sandbox):
        summary = paths.purge_bot(1, "no-such-bot")
        assert set(summary.keys()) == {
            "files_removed", "files_failed",
            "db_rows_removed", "warnings",
        }
        assert isinstance(summary["files_removed"], int)
        assert isinstance(summary["files_failed"], list)
        assert isinstance(summary["db_rows_removed"], dict)
        assert isinstance(summary["warnings"], list)
