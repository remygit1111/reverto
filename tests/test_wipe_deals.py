"""Tests for scripts/wipe_deals.py — state.json reset alongside DB wipe.

Pins the per-file reset logic, the pid-liveness safety gate, and the
batch walker. The full main() flow (DB DELETE + VACUUM) is not
exercised here — that path is covered by operator-run integration
only; these tests focus on the file-system state logic that would
otherwise be easy to regress silently.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the script as a module so we can hit its internal helpers.
# scripts/ isn't a package, so we add it to sys.path first.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import wipe_deals  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sandboxed_base(tmp_path, monkeypatch):
    """Redirect wipe_deals.BASE at tmp_path so logs/<uid>/ lookups all
    resolve under the per-test sandbox. Matches the pattern used by
    tests/test_paths.py."""
    monkeypatch.setattr(wipe_deals, "BASE", tmp_path)
    yield tmp_path


def _write_state(path: Path, **overrides) -> dict:
    """Write a minimal but realistic state.json at ``path`` and return
    the dict that was written. Fields chosen to match what PaperEngine
    actually produces in _write_state so the reset test asserts against
    production-shaped input."""
    data: dict = {
        "bot_name":            "test_bot",
        "mode":                "paper",
        "exchange":            "bitget",
        "pair":                "BTC/USD",
        "running":             True,
        "current_price":       80_000.0,
        "schedule_open":       True,
        "has_trading_windows": False,
        "balance_btc":         0.085,      # < initial; will get reset up
        "initial_balance_btc": 0.1,
        "total_pnl_btc":       -0.015,
        "win_rate":            42.0,
        "open_deals_count":    2,
        "closed_deals_count":  5,
        "fees_paid_btc":       0.0003,
        "started_at":          "2026-04-19T07:00:00+00:00",
        "updated_at":          "2026-04-19T13:42:00+00:00",
        "open_deals": [
            {"id": "202604191342-0001", "bot_name": "test_bot"},
            {"id": "202604191342-0002", "bot_name": "test_bot"},
        ],
        "closed_deals": [
            {"id": "202604191300-0042"},
        ],
        "indicators":          {"rsi_14": 42.5},
        "drawdown_guard":      {"peak_value": 0.12, "triggered": False},
        "paused_by_drawdown":  False,
        "paused_by_clock_skew": False,
    }
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ── 1. Single-file reset logic ──────────────────────────────────────────────


class TestResetStateFile:

    def test_wipe_resets_state_json_fields(self, tmp_path):
        """Deal-tracking fields reset to their "fresh start" values;
        unrelated fields untouched; backup landed at the expected path."""
        state = tmp_path / "bot.state.json"
        original = _write_state(state)

        backup = wipe_deals._reset_state_file(state)

        out = json.loads(state.read_text(encoding="utf-8"))
        # Reset fields
        assert out["balance_btc"] == original["initial_balance_btc"]
        assert out["total_pnl_btc"] == 0
        assert out["win_rate"] == 0.0
        assert out["open_deals_count"] == 0
        assert out["closed_deals_count"] == 0
        assert out["fees_paid_btc"] == 0
        assert out["open_deals"] == []
        assert out["closed_deals"] == []
        # Preserved fields
        assert out["bot_name"] == original["bot_name"]
        assert out["mode"] == original["mode"]
        assert out["exchange"] == original["exchange"]
        assert out["pair"] == original["pair"]
        assert out["started_at"] == original["started_at"]
        assert out["running"] == original["running"]
        assert out["drawdown_guard"] == original["drawdown_guard"]
        assert out["indicators"] == original["indicators"]
        # Backup exists and holds the pre-wipe data.
        assert backup.exists()
        backup_data = json.loads(backup.read_text(encoding="utf-8"))
        assert backup_data["open_deals_count"] == 2
        assert backup_data["balance_btc"] == pytest.approx(0.085)

    def test_wipe_falls_back_to_default_balance_if_initial_missing(
        self, tmp_path,
    ):
        """Older state.json files may not carry initial_balance_btc —
        reset then falls back to the engine's 0.1 BTC default so the
        field always ends up with a sensible number."""
        state = tmp_path / "bot.state.json"
        data = _write_state(state)
        data.pop("initial_balance_btc")
        state.write_text(json.dumps(data, indent=2), encoding="utf-8")

        wipe_deals._reset_state_file(state)

        out = json.loads(state.read_text(encoding="utf-8"))
        assert out["balance_btc"] == pytest.approx(0.1)

    def test_wipe_does_not_materialise_absent_closed_deals_key(
        self, tmp_path,
    ):
        """If a state.json genuinely lacks ``closed_deals`` the wipe
        must leave it absent — adding a new key would change the
        file's shape in a way the engine never wrote."""
        state = tmp_path / "bot.state.json"
        data = _write_state(state)
        data.pop("closed_deals")
        state.write_text(json.dumps(data, indent=2), encoding="utf-8")

        wipe_deals._reset_state_file(state)
        out = json.loads(state.read_text(encoding="utf-8"))
        assert "closed_deals" not in out

    def test_wipe_backup_filename_is_deterministic(self, tmp_path):
        """Backup always lands at ``<path>.state.json.pre_wipe_backup``.
        Deterministic naming is what lets a repeated wipe overwrite
        the backup safely — no timestamped accumulation."""
        state = tmp_path / "mybot.state.json"
        _write_state(state)

        backup = wipe_deals._reset_state_file(state)

        expected = tmp_path / "mybot.state.json.pre_wipe_backup"
        assert backup == expected
        assert backup.exists()

    def test_wipe_backup_is_overwrite_safe(self, tmp_path):
        """Running wipe twice on the same file must succeed — the
        second run overwrites the first backup. This makes "oops run
        it again" a safe operator action."""
        state = tmp_path / "bot.state.json"
        _write_state(state)

        backup1 = wipe_deals._reset_state_file(state)
        # Poke the (now-reset) state.json so the second wipe is different.
        _write_state(state, balance_btc=0.042)
        backup2 = wipe_deals._reset_state_file(state)

        assert backup1 == backup2
        # Backup reflects the intermediate value (0.042), not the first.
        backup_data = json.loads(backup2.read_text(encoding="utf-8"))
        assert backup_data["balance_btc"] == pytest.approx(0.042)


# ── 2. Safety gate: refuse if a bot is alive ────────────────────────────────


class TestCheckNoBotsRunning:

    def test_wipe_refuses_if_bot_is_running(
        self, sandboxed_base, monkeypatch,
    ):
        """A live pid-file aborts the gate with SystemExit. State +
        DB are untouched because the gate raises before any
        destructive op runs."""
        base = sandboxed_base
        pid_dir = base / "logs" / "1" / "pids"
        pid_dir.mkdir(parents=True)
        (pid_dir / "rsi_paper_test.pid").write_text("99999")

        # Simulate pid 99999 being alive — the patched os.kill returns
        # None (i.e. doesn't raise). The helper reads success as
        # "process exists".
        monkeypatch.setattr(wipe_deals.os, "kill", lambda pid, sig: None)

        with pytest.raises(SystemExit):
            wipe_deals._check_no_bots_running(base, {1})

    def test_wipe_proceeds_if_pid_file_stale(
        self, sandboxed_base, monkeypatch,
    ):
        """os.kill raising ProcessLookupError means the pid file is
        stale. The gate must tolerate it and let the wipe continue."""
        base = sandboxed_base
        pid_dir = base / "logs" / "1" / "pids"
        pid_dir.mkdir(parents=True)
        (pid_dir / "dead_bot.pid").write_text("12345")

        def _fake_kill(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(wipe_deals.os, "kill", _fake_kill)
        # No raise — gate passes silently.
        wipe_deals._check_no_bots_running(base, {1})

    def test_wipe_tolerates_unparseable_pid_file(
        self, sandboxed_base, monkeypatch,
    ):
        """A pid file with garbage in it (e.g. truncated write during
        bot crash) must NOT block the wipe — the operator can't clean
        up with wipe-deals if this holds the whole script hostage."""
        base = sandboxed_base
        pid_dir = base / "logs" / "1" / "pids"
        pid_dir.mkdir(parents=True)
        (pid_dir / "corrupt.pid").write_text("NOT-A-NUMBER\n")

        # No os.kill call ever made — the try/except short-circuits.
        wipe_deals._check_no_bots_running(base, {1})

    def test_wipe_with_permission_error_treats_as_alive(
        self, sandboxed_base, monkeypatch,
    ):
        """os.kill raising PermissionError means the process exists
        but belongs to another OS user — we stay conservative and
        refuse the wipe."""
        base = sandboxed_base
        pid_dir = base / "logs" / "1" / "pids"
        pid_dir.mkdir(parents=True)
        (pid_dir / "foreign.pid").write_text("1")

        def _fake_kill(pid, sig):
            raise PermissionError()

        monkeypatch.setattr(wipe_deals.os, "kill", _fake_kill)
        with pytest.raises(SystemExit):
            wipe_deals._check_no_bots_running(base, {1})


# ── 3. Batch walker ─────────────────────────────────────────────────────────


class TestWipeStateFiles:

    def test_wipe_skips_if_no_state_files(self, sandboxed_base):
        """logs/<uid>/ missing → no crash, zero files reset."""
        base = sandboxed_base
        # No logs/1/ at all.
        n = wipe_deals._wipe_state_files(base, {1})
        assert n == 0

    def test_wipe_skips_empty_logs_dir(self, sandboxed_base):
        """logs/<uid>/ exists but contains no *.state.json — 0 reset,
        and the real log files that happen to live in the same dir
        (e.g. bot.log) are untouched."""
        base = sandboxed_base
        logs_1 = base / "logs" / "1"
        logs_1.mkdir(parents=True)
        (logs_1 / "random.log").write_text("some log content")

        n = wipe_deals._wipe_state_files(base, {1})
        assert n == 0
        # Non-state file untouched.
        assert (logs_1 / "random.log").read_text() == "some log content"

    def test_wipe_resets_all_state_files_across_users(self, sandboxed_base):
        """Multi-user case: user 1 has 2 state files, user 2 has 1.
        All 3 get reset + 3 backups land next to them."""
        base = sandboxed_base
        for uid, slugs in [(1, ["a", "b"]), (2, ["c"])]:
            for slug in slugs:
                _write_state(base / "logs" / str(uid) / f"{slug}.state.json")

        n = wipe_deals._wipe_state_files(base, {1, 2})
        assert n == 3

        for uid, slugs in [(1, ["a", "b"]), (2, ["c"])]:
            for slug in slugs:
                state = base / "logs" / str(uid) / f"{slug}.state.json"
                backup = state.with_suffix(".json.pre_wipe_backup")
                assert state.exists() and backup.exists()
                out = json.loads(state.read_text(encoding="utf-8"))
                assert out["open_deals"] == []
                assert out["open_deals_count"] == 0


class TestConcurrentWipeLock:
    """Audit v25 Finding #9 — twee gelijktijdige ``wipe-deals`` calls
    mochten niet kunnen interleaven. _wipe_lock neemt exclusieve
    fcntl.flock; een parallelle poging raised RuntimeError met
    'already in progress'.
    """

    def test_concurrent_wipe_is_blocked_by_flock(self, sandboxed_base):
        """Terwijl proces A binnen ``with _wipe_lock(base)`` zit, moet
        proces B afketsen op BlockingIOError → RuntimeError. We
        simuleren dit met twee geneste context-managers binnen één
        proces — fcntl.flock is advisory per-file-descriptor, dus de
        tweede open() + LOCK_EX|LOCK_NB trippt net zoals vanuit een
        tweede proces zou gebeuren.
        """
        base = sandboxed_base
        with wipe_deals._wipe_lock(base):
            # A houdt de lock; B moet nu afketsen. We wikkelen het in
            # een pytest.raises zodat de tweede context-manager niet
            # onbedoeld onze eigen lock alsnog weet te grijpen.
            with pytest.raises(RuntimeError, match="already in progress"):
                with wipe_deals._wipe_lock(base):
                    pytest.fail("second lock should not be reachable")
        # Na release: lock file bestaat nog (flock is advisory) maar is
        # weer acquireerbaar.
        lock_path = base / wipe_deals._WIPE_LOCK_FILE
        assert lock_path.exists()
        with wipe_deals._wipe_lock(base):
            pass  # acquire + release succeeds again
