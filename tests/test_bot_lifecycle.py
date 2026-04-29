"""Bot-lifecycle stability regression tests.

PR ``tweak/bot-lifecycle-stability`` introduces three foundation
mechanisms so the operator-visible bot state stays consistent with
process reality:

1. **Process-group isolation** — bot subprocesses spawn with
   ``start_new_session=True`` (≡ ``preexec_fn=os.setsid``) so a
   systemd cgroup-cleanup on portal-restart cannot mass-kill them.
2. **Heartbeat in state.json** — engines stamp ``last_heartbeat`` on
   every tick. The portal reads it to distinguish a live bot from
   one that exited silently (frozen state.json says ``running: true``
   but the OS process is gone).
3. **Silent-exit reconciliation** — ``BotInfo.read_state`` corrects
   on-disk state when PID is dead OR heartbeat is stale, surfaces
   ``stopped_reason``, and writes back atomically. The startup
   lifespan triggers this for every bot before serving traffic.

These tests pin the contracts so a refactor cannot silently regress
any of the three.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _run(coro):
    """Bridge async test bodies into the synchronous pytest runner.
    The codebase doesn't ship pytest-asyncio; this mirrors the pattern
    in tests/test_lifespan.py, tests/test_broadcasters.py, etc.
    """
    return asyncio.run(coro)

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
os.environ.setdefault("REVERTO_SECRET_KEY", "testkey-for-pytest-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_REPO = Path(__file__).resolve().parent.parent
_WEB_APP = _REPO / "web" / "app.py"
_PAPER_ENGINE = _REPO / "paper" / "paper_engine.py"


# ─── W1: process-group isolation ─────────────────────────────────────────────

def test_bot_subprocess_spawn_uses_setsid():
    """Both bot launchers (paper + live-dry-run) must spawn the
    subprocess with ``start_new_session=True``. That is the modern
    Python equivalent of ``preexec_fn=os.setsid`` — both call
    ``setsid()`` in the child, putting the bot in its own
    process-group so a systemd cgroup-cleanup on portal-restart does
    not mass-kill it.

    A regression that drops the kwarg silently re-introduces the bug
    where bots vanish on every ``systemctl restart reverto``.
    """
    src = _WEB_APP.read_text(encoding="utf-8")

    # Both Popen calls live in start_bot / start_bot_dry_run. Count
    # them so a future third launcher cannot land without the kwarg.
    popen_calls = src.count("subprocess.Popen(")
    assert popen_calls >= 2, (
        f"expected at least 2 subprocess.Popen calls in web/app.py, "
        f"found {popen_calls}"
    )
    setsid_calls = src.count("start_new_session=True")
    assert setsid_calls >= popen_calls, (
        f"every subprocess.Popen call must pass start_new_session=True "
        f"(≡ preexec_fn=os.setsid). Found {setsid_calls} occurrences "
        f"vs {popen_calls} Popen calls."
    )


# ─── W2: heartbeat field in state schema + engine write path ─────────────────

def test_heartbeat_field_in_bot_state_model():
    """``BotStateModel`` must declare ``last_heartbeat`` (and the
    companion ``heartbeat_interval_sec``) so a state.json read does
    not silently drop the field at the validation layer.
    """
    from web.app import BotStateModel

    fields = BotStateModel.model_fields
    assert "last_heartbeat" in fields, (
        "BotStateModel must accept last_heartbeat — silent-exit "
        "detection reads it"
    )
    assert "heartbeat_interval_sec" in fields, (
        "BotStateModel must accept heartbeat_interval_sec so the "
        "portal can scale the staleness threshold to the engine's "
        "tick cadence"
    )
    # The reconcile-side fields must also be schema-known so writing
    # them back through the StateIO path doesn't drop them on the
    # next validate() round-trip.
    assert "stopped_at" in fields
    assert "stopped_reason" in fields


def test_paper_engine_stamps_heartbeat_on_state_write():
    """The engine's ``_write_state`` must include ``last_heartbeat``
    in every snapshot so the portal can detect a silent exit.
    """
    src = _PAPER_ENGINE.read_text(encoding="utf-8")

    # The constant + the field both live in paper_engine.py.
    assert "HEARTBEAT_INTERVAL_SEC" in src
    assert '"last_heartbeat"' in src, (
        "paper_engine._write_state must stamp last_heartbeat in the "
        "state-snapshot dict"
    )
    assert '"heartbeat_interval_sec"' in src


# ─── W3: heartbeat-staleness helper ──────────────────────────────────────────

def test_heartbeat_is_stale_returns_false_for_fresh_heartbeat():
    """A heartbeat younger than the threshold is considered alive."""
    from web.app import _heartbeat_is_stale

    fresh = datetime.now(timezone.utc).isoformat()
    assert _heartbeat_is_stale({"last_heartbeat": fresh}) is False


def test_heartbeat_is_stale_returns_true_for_old_heartbeat():
    """A heartbeat older than the threshold is considered stale."""
    from web.app import HEARTBEAT_STALE_THRESHOLD_SEC, _heartbeat_is_stale

    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=HEARTBEAT_STALE_THRESHOLD_SEC + 30)
    ).isoformat()
    assert _heartbeat_is_stale({"last_heartbeat": old}) is True


def test_heartbeat_is_stale_backwards_compat_no_field():
    """Pre-heartbeat state files have no ``last_heartbeat`` field.
    The helper must NOT flag them as stale — flipping every legacy
    bot to "stopped" on first read after upgrade would be a
    self-inflicted incident.
    """
    from web.app import _heartbeat_is_stale

    assert _heartbeat_is_stale({}) is False
    assert _heartbeat_is_stale({"last_heartbeat": None}) is False
    assert _heartbeat_is_stale({"last_heartbeat": ""}) is False


def test_heartbeat_is_stale_handles_garbled_timestamp():
    """A non-parseable ``last_heartbeat`` must fall back to
    PID-only liveness (return False) rather than crashing the
    state-read path.
    """
    from web.app import _heartbeat_is_stale

    assert _heartbeat_is_stale({"last_heartbeat": "not a date"}) is False
    assert _heartbeat_is_stale({"last_heartbeat": "2026-99-99"}) is False


# ─── W4: silent-exit reconciliation in BotInfo.read_state ────────────────────

@pytest.fixture
def _isolated_bot(tmp_path, monkeypatch):
    """Build a real BotInfo wired to tmp_path so we can poke its
    state file directly. Fakes the registry-side bookkeeping with
    only the bits BotInfo.read_state actually touches.
    """
    from web import app as webapp

    user_id = 9999
    slug = "lifecycle_test_bot"

    # Redirect every paths.* helper to tmp_path so file-mutations
    # are scoped to the test.
    state_dir = tmp_path / "state"
    pid_dir = tmp_path / "pids"
    yaml_dir = tmp_path / "yaml"
    lock_dir = tmp_path / "locks"
    log_dir = tmp_path / "logs"
    for d in (state_dir, pid_dir, yaml_dir, lock_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    state_file = state_dir / f"{slug}.state.json"
    pid_file = pid_dir / f"{slug}.pid"
    yaml_file = yaml_dir / f"{slug}.yaml"
    log_file = log_dir / f"{slug}.log"

    # Minimal YAML so _resolve_yaml_mode doesn't break (it reads the
    # file and returns "" on failure — also fine, but a real file is
    # closer to production).
    yaml_file.write_text(
        "bot:\n  name: lifecycle_test_bot\n  mode: paper\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        webapp.paths, "bot_state_path",
        lambda uid, s: state_file,
    )
    monkeypatch.setattr(
        webapp.paths, "bot_pid_path",
        lambda uid, s: pid_file,
    )
    monkeypatch.setattr(
        webapp.paths, "bot_yaml_path",
        lambda uid, s: yaml_file,
    )
    monkeypatch.setattr(
        webapp.paths, "bot_state_lock_path",
        lambda uid, s: lock_dir / f"{slug}.lock",
    )
    monkeypatch.setattr(
        webapp.paths, "bot_log_path",
        lambda uid, s: log_file,
    )
    monkeypatch.setattr(
        webapp.paths, "bot_manual_trigger_path",
        lambda uid, s: tmp_path / f"{slug}.trigger",
    )

    bot = webapp.BotInfo(
        slug=slug,
        config_file=str(yaml_file),
        user_id=user_id,
    )
    return bot, state_file, pid_file


def test_silent_exit_reconcile_corrects_state_when_pid_dead(_isolated_bot):
    """state.json says running=true but no PID file exists →
    reconcile must flip the on-disk state to ``running=false`` and
    stamp ``stopped_reason=silent_exit``.
    """
    bot, state_file, pid_file = _isolated_bot

    # Write a state-file as the engine would have — running=true,
    # fresh heartbeat. Then "silently kill" the bot by NOT writing
    # the PID file (or writing one for a non-existent PID).
    state_file.write_text(json.dumps({
        "bot_name": "lifecycle_test_bot",
        "running": True,
        "current_price": 50000.0,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    assert not pid_file.exists()

    result = bot.read_state()

    # API response reflects the corrected state.
    assert result["running"] is False
    assert result["stopped_reason"] == "silent_exit"
    assert result["stopped_at"] is not None

    # Disk reflects the corrected state.
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk["running"] is False
    assert on_disk["stopped_reason"] == "silent_exit"
    assert on_disk["current_price"] == 0.0


def test_silent_exit_reconcile_is_idempotent(_isolated_bot):
    """Calling read_state twice on a silent-exit bot must not
    re-write the state file the second time. The second call sees
    ``running=false`` on disk and short-circuits the reconcile gate.
    """
    bot, state_file, pid_file = _isolated_bot

    state_file.write_text(json.dumps({
        "running": True,
        "current_price": 50000.0,
    }), encoding="utf-8")

    # First read reconciles.
    bot.read_state()
    mtime_after_first = state_file.stat().st_mtime_ns
    first_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert first_disk["running"] is False

    # Second read must NOT rewrite — file mtime stays the same.
    bot.read_state()
    mtime_after_second = state_file.stat().st_mtime_ns
    assert mtime_after_first == mtime_after_second, (
        "second read_state on an already-reconciled bot must skip "
        "the disk write — idempotency is required for the lifespan "
        "startup walk that touches every bot"
    )


def test_silent_exit_reconcile_uses_heartbeat_stale_reason(_isolated_bot, monkeypatch):
    """state.json says running=true, PID file points at a live
    process (e.g. the test runner itself), but the heartbeat is
    older than the threshold → reconcile must classify this as
    ``heartbeat_stale`` rather than ``silent_exit``.
    """
    from web.app import HEARTBEAT_STALE_THRESHOLD_SEC

    bot, state_file, pid_file = _isolated_bot

    # PID file points at our own PID (definitely alive).
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    # Heartbeat older than the threshold.
    old_hb = (
        datetime.now(timezone.utc)
        - timedelta(seconds=HEARTBEAT_STALE_THRESHOLD_SEC + 30)
    ).isoformat()
    state_file.write_text(json.dumps({
        "running": True,
        "current_price": 50000.0,
        "last_heartbeat": old_hb,
    }), encoding="utf-8")

    result = bot.read_state()
    assert result["running"] is False
    assert result["stopped_reason"] == "heartbeat_stale"


def test_silent_exit_reconcile_skipped_when_state_says_stopped(_isolated_bot):
    """If on-disk state already says ``running=false``, the reconcile
    gate must short-circuit — there's nothing to correct, and writing
    every read would burn IO on every dashboard tick.
    """
    bot, state_file, _pid_file = _isolated_bot

    state_file.write_text(json.dumps({
        "running": False,
        "current_price": 0.0,
    }), encoding="utf-8")
    mtime_before = state_file.stat().st_mtime_ns

    bot.read_state()
    mtime_after = state_file.stat().st_mtime_ns
    assert mtime_before == mtime_after


# ─── W5: startup reconcile ───────────────────────────────────────────────────

def test_startup_reconcile_helper_exists():
    """The lifespan startup must trigger a reconcile walk before
    serving traffic. Pin the helper's existence so a future refactor
    that drops the lifespan-side trigger fails this test.
    """
    src = _WEB_APP.read_text(encoding="utf-8")
    assert "_reconcile_bot_states_on_startup" in src
    # And it must be called from inside the lifespan handler.
    lifespan_idx = src.index("async def lifespan(")
    lifespan_body = src[lifespan_idx:lifespan_idx + 4000]
    assert "_reconcile_bot_states_on_startup" in lifespan_body, (
        "lifespan() must call _reconcile_bot_states_on_startup() "
        "during startup so silent-exit drift is corrected before "
        "the first API request"
    )


# ─── Schema-version stamp + mismatch detection ───────────────────────────────

def test_state_schema_version_constant_exists():
    """``STATE_SCHEMA_VERSION`` must be a positive integer so the
    portal-side mismatch detection has something concrete to compare
    against. A future bump (when the schema gains another reconcile-
    facing field) is one-line in paper_engine.py.
    """
    from paper.paper_engine import STATE_SCHEMA_VERSION

    assert isinstance(STATE_SCHEMA_VERSION, int)
    assert STATE_SCHEMA_VERSION >= 1


def test_bot_state_includes_schema_version_stamp():
    """The engine's ``_write_state`` must stamp ``state_schema_version``
    in every snapshot — that is the on-disk signal the portal reads
    for mismatch detection.
    """
    src = _PAPER_ENGINE.read_text(encoding="utf-8")

    assert "STATE_SCHEMA_VERSION" in src
    assert '"state_schema_version"' in src, (
        "paper_engine._write_state must include state_schema_version "
        "in the state-snapshot dict"
    )


def test_bot_state_model_accepts_schema_version_field():
    """``BotStateModel`` validates the new field both with a value
    and without — backwards compat for legacy state.json files.
    """
    from web.app import BotStateModel

    m = BotStateModel(state_schema_version=2)
    assert m.state_schema_version == 2

    legacy = BotStateModel()
    assert legacy.state_schema_version is None


def test_mismatch_detection_when_no_version():
    """Bot with ``running=true`` but no ``state_schema_version``
    field is treated as legacy → needs restart so it picks up the
    current code on next portal startup.
    """
    from web.app import _bot_needs_restart

    state = {"running": True}
    assert _bot_needs_restart(state, current_version=2) is True


def test_mismatch_detection_when_version_match():
    """Bot with the portal's current schema version must NOT be
    restarted — that would cause unnecessary churn on every deploy
    that doesn't bump the schema.
    """
    from web.app import _bot_needs_restart

    state = {"running": True, "state_schema_version": 2}
    assert _bot_needs_restart(state, current_version=2) is False


def test_mismatch_detection_when_version_outdated():
    """Bot stamping an older version than the portal currently runs
    is on stale code → restart so the new engine code takes over.
    """
    from web.app import _bot_needs_restart

    state = {"running": True, "state_schema_version": 1}
    assert _bot_needs_restart(state, current_version=2) is True


def test_mismatch_detection_skips_stopped_bots():
    """A bot that is already stopped never needs a schema-mismatch
    restart — silent-exit reconcile already handled it; the auto-
    restart path is reserved for *running* bots on stale code.
    """
    from web.app import _bot_needs_restart

    # Even if the version is wildly different, running=False short-
    # circuits — never burn an auto-restart attempt on an already-
    # stopped bot.
    state = {"running": False, "state_schema_version": 1}
    assert _bot_needs_restart(state, current_version=2) is False
    state_no_version = {"running": False}
    assert _bot_needs_restart(state_no_version, current_version=2) is False


# ─── Bounded auto-restart budget ─────────────────────────────────────────────

@pytest.fixture
def _restart_history_clean():
    """Reset the in-memory restart-history dict around each test so
    one test's attempts do not bleed into another's budget.
    """
    from web import app as webapp

    webapp._BOT_RESTART_HISTORY.clear()
    yield webapp._BOT_RESTART_HISTORY
    webapp._BOT_RESTART_HISTORY.clear()


class _FakeBotForRestart:
    """Minimal stand-in for ``BotInfo`` so the budget logic can be
    exercised without spinning up a real subprocess. Only carries the
    fields ``_attempt_bot_auto_restart`` actually reads.
    """

    def __init__(self, user_id: int, slug: str, state_file=None):
        self.user_id = user_id
        self.slug = slug
        self.state_file = state_file


def test_restart_budget_allows_three_attempts(_restart_history_clean, monkeypatch):
    """First three attempts within the rolling window must all be
    allowed through — the budget is "max 3 in a 5min window," not
    "max 3 ever."
    """
    from web import app as webapp

    calls = []

    async def _fake_restart(user_id, slug):
        calls.append((user_id, slug))
        return {"ok": True, "message": "ok"}

    monkeypatch.setattr(webapp, "restart_bot", _fake_restart)

    bot = _FakeBotForRestart(1, "alpha")
    for _ in range(webapp.RESTART_MAX_ATTEMPTS):
        assert _run(webapp._attempt_bot_auto_restart(bot)) is True

    assert len(calls) == webapp.RESTART_MAX_ATTEMPTS


def test_restart_budget_blocks_fourth_attempt(_restart_history_clean, monkeypatch, tmp_path):
    """The (RESTART_MAX_ATTEMPTS + 1)th attempt within the window
    must be refused, log the give-up, and stamp
    ``stopped_reason="restart_budget_exceeded"`` into state.json.
    """
    from web import app as webapp

    async def _fake_restart(user_id, slug):
        return {"ok": True}

    monkeypatch.setattr(webapp, "restart_bot", _fake_restart)

    state_file = tmp_path / "alpha.state.json"
    state_file.write_text(json.dumps({"running": True}), encoding="utf-8")
    bot = _FakeBotForRestart(1, "alpha", state_file=state_file)

    # Burn the budget.
    for _ in range(webapp.RESTART_MAX_ATTEMPTS):
        assert _run(webapp._attempt_bot_auto_restart(bot)) is True

    # 4th attempt must be refused.
    assert _run(webapp._attempt_bot_auto_restart(bot)) is False

    # state.json must now carry the give-up marker so an operator
    # checking the file (or the UI in a future PR) can see why the
    # portal stopped trying.
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk["stopped_reason"] == "restart_budget_exceeded"
    assert on_disk["stopped_at"] is not None


def test_restart_budget_resets_after_window(_restart_history_clean, monkeypatch):
    """Attempts older than ``RESTART_WINDOW_SECONDS`` must be pruned
    so a bot that has been stable for a day starts with a fresh
    budget after one new failure.
    """
    from web import app as webapp

    async def _fake_restart(user_id, slug):
        return {"ok": True}

    monkeypatch.setattr(webapp, "restart_bot", _fake_restart)

    # Pre-populate history with attempts older than the window.
    bot = _FakeBotForRestart(1, "alpha")
    key = (bot.user_id, bot.slug)
    long_ago = time.time() - (webapp.RESTART_WINDOW_SECONDS + 60)
    webapp._BOT_RESTART_HISTORY[key] = [long_ago, long_ago, long_ago]

    # Despite three "old" attempts, the prune step strips them and
    # this fresh attempt is allowed through.
    assert _run(webapp._attempt_bot_auto_restart(bot)) is True
    # And the history now carries only the new attempt.
    assert len(webapp._BOT_RESTART_HISTORY[key]) == 1


def test_restart_budget_per_bot_isolated(_restart_history_clean, monkeypatch):
    """Budget keying is ``(user_id, slug)`` — exhausting bot A's
    budget must NOT block bot B's first attempt.
    """
    from web import app as webapp

    async def _fake_restart(user_id, slug):
        return {"ok": True}

    monkeypatch.setattr(webapp, "restart_bot", _fake_restart)

    bot_a = _FakeBotForRestart(1, "alpha")
    bot_b = _FakeBotForRestart(1, "beta")

    # Exhaust alpha.
    for _ in range(webapp.RESTART_MAX_ATTEMPTS):
        _run(webapp._attempt_bot_auto_restart(bot_a))
    assert _run(webapp._attempt_bot_auto_restart(bot_a)) is False

    # Beta is unaffected.
    assert _run(webapp._attempt_bot_auto_restart(bot_b)) is True

    # And the per-bot keying really did partition the dict.
    assert (1, "alpha") in webapp._BOT_RESTART_HISTORY
    assert (1, "beta") in webapp._BOT_RESTART_HISTORY


# ── rha-003: _BOT_RESTART_HISTORY explicit growth ceiling ─────────────────


class TestBotRestartHistoryCeiling:
    """rha-003 regression — ``_record_bot_restart`` enforces a per-bot
    absolute size cap on top of the implicit window-prune cap that
    ``_attempt_bot_auto_restart`` already applies.

    The window-prune (``RESTART_MAX_ATTEMPTS=3`` in
    ``RESTART_WINDOW_SECONDS=300``) bounds the steady-state size, but
    a future caller that bypasses the prune step would let the list
    grow unbounded. The explicit ceiling is defence-in-depth so any
    direct mutation of ``_BOT_RESTART_HISTORY`` via the canonical
    helper is bounded regardless of caller discipline.
    """

    def test_record_appends_a_single_entry(self, _restart_history_clean):
        from web import app as webapp

        history = webapp._record_bot_restart(1, "alpha", 100.0)
        assert history == [100.0]
        # And the dict slot is populated.
        assert webapp._BOT_RESTART_HISTORY[(1, "alpha")] == [100.0]

    def test_record_grows_to_ceiling(self, _restart_history_clean):
        """Below the ceiling the list grows naturally."""
        from web import app as webapp

        cap = webapp._BOT_RESTART_HISTORY_MAX_ENTRIES_PER_BOT
        for i in range(cap):
            webapp._record_bot_restart(1, "alpha", float(i))

        history = webapp._BOT_RESTART_HISTORY[(1, "alpha")]
        assert len(history) == cap
        assert history[0] == 0.0
        assert history[-1] == float(cap - 1)

    def test_record_truncates_oldest_above_ceiling(self, _restart_history_clean):
        """Past the cap, oldest entries are dropped — not newest. The
        retention semantic matches a ringbuffer-of-recent-history."""
        from web import app as webapp

        cap = webapp._BOT_RESTART_HISTORY_MAX_ENTRIES_PER_BOT
        # Record 1.5x the cap.
        total = cap + cap // 2
        for i in range(total):
            webapp._record_bot_restart(1, "alpha", float(i))

        history = webapp._BOT_RESTART_HISTORY[(1, "alpha")]
        # Length capped at the ceiling.
        assert len(history) == cap
        # Newest entry preserved (index total-1 in the original sequence).
        assert history[-1] == float(total - 1)
        # Oldest entries beyond cap dropped (indices 0 .. (total-cap)-1).
        # First retained entry is the (total-cap)th original.
        assert history[0] == float(total - cap)

    def test_record_per_bot_isolation(self, _restart_history_clean):
        """Different (user_id, slug) keys keep independent lists; one
        bot's overflow does not push another bot's history."""
        from web import app as webapp

        webapp._record_bot_restart(1, "alpha", 100.0)
        webapp._record_bot_restart(1, "beta", 200.0)
        webapp._record_bot_restart(2, "alpha", 300.0)

        assert webapp._BOT_RESTART_HISTORY[(1, "alpha")] == [100.0]
        assert webapp._BOT_RESTART_HISTORY[(1, "beta")] == [200.0]
        assert webapp._BOT_RESTART_HISTORY[(2, "alpha")] == [300.0]

    def test_record_preserves_list_identity_across_truncate(
        self, _restart_history_clean,
    ):
        """In-place truncation (``del history[:-cap]``) preserves the
        list identity so any caller holding a reference still sees
        the truncated state. Pre-fix a rebind via slice would have
        left the dict's stored list dangling at a different identity."""
        from web import app as webapp

        first = webapp._record_bot_restart(1, "alpha", 0.0)
        cap = webapp._BOT_RESTART_HISTORY_MAX_ENTRIES_PER_BOT
        for i in range(1, cap + 5):
            webapp._record_bot_restart(1, "alpha", float(i))

        # The list returned by the FIRST call must be the same object
        # the dict stores after the truncate.
        assert first is webapp._BOT_RESTART_HISTORY[(1, "alpha")]


# ── rha-014: portal-side docstring counterpart ────────────────────────────


def test_persist_silent_exit_reconcile_docstring_references_rha014():
    """rha-014: the portal-side post-mortem reconcile must carry the
    finding marker + name its engine-side counterpart so the
    deliberate split survives future cleanup passes. The companion
    test for ``StateIO.mark_stopped`` lives in ``test_state_io.py``."""
    from web.app import BotInfo

    doc = BotInfo._persist_silent_exit_reconcile.__doc__ or ""
    assert "rha-014" in doc, (
        "rha-014: _persist_silent_exit_reconcile docstring must "
        "reference the finding for symmetry with mark_stopped."
    )
    assert "mark_stopped" in doc, (
        "rha-014: docstring must name the engine-side counterpart."
    )
