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

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
