"""Regression guard for r1-067 — ``watch_state_files`` must not block
the asyncio event loop while reading bot state files.

Pre-fix: ``state = bot.read_state()`` ran synchronously inside the
loop, doing file I/O + JSON parsing + a possible silent-exit-reconcile
state-file rewrite. With N bots in the registry the per-iteration
latency was ``N × read_time``; every other coroutine on the loop
(WS frame ack, /api/* request handler, broadcaster fan-out) was
delayed by that same amount.

Post-fix: ``await asyncio.to_thread(bot.read_state)`` pushes the I/O
onto the default thread pool. The loop stays responsive even when
the underlying read is artificially slow, and a slow read on bot N
no longer head-of-lines bot N+1's broadcast (the per-bot path runs
serially but each await yields control).

These tests pin the contract:
  * ``read_state`` is invoked via ``asyncio.to_thread`` (not
    directly on the loop).
  * Other coroutines progress while a per-bot read is in flight.
  * Multi-bot summary reads run concurrently (``asyncio.gather``)
    so a slow read on bot A doesn't delay bot B's read.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from web import app as web_app  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _SlowBot:
    """Stand-in for ``BotInfo``. ``read_state`` blocks for a fixed
    duration on the calling thread so we can observe whether the
    asyncio loop is blocked or running concurrent work."""

    def __init__(self, user_id: int, slug: str, state_file, sleep_s: float = 0.05):
        self.user_id = user_id
        self.slug = slug
        self.state_file = state_file
        self.sleep_s = sleep_s
        self.read_threads: list[int] = []
        self.lock = threading.Lock()

    def read_state(self) -> dict:
        # Record which OS thread executed this read so the test can
        # assert it ran off-loop (i.e. on a thread pool worker).
        with self.lock:
            self.read_threads.append(threading.get_ident())
        # Real I/O latency — the whole point of asyncio.to_thread.
        import time as _time
        _time.sleep(self.sleep_s)
        return {
            "bot_name": self.slug,
            "user_id": self.user_id,
            "running": False,
        }


class _StopIteration(Exception):
    """Private sentinel so pytest doesn't confuse this with builtin
    StopIteration. The watcher's ``while True`` is broken by raising
    from the per-iteration sleep — same pattern as the existing
    test_watch_state_files_per_user.py uses."""


@pytest.fixture
def _watch_loop_setup(tmp_path, monkeypatch):
    """Wire two slow bots into the watcher and stub broadcaster +
    summary so the test focuses on the threading contract.

    Returns (bots, broadcasts) so the calling test can drive the
    loop and inspect side effects.
    """
    sf_a = tmp_path / "user1_slow_a.state.json"
    sf_b = tmp_path / "user1_slow_b.state.json"
    sf_a.write_text(json.dumps({"bot_name": "slow_a"}))
    sf_b.write_text(json.dumps({"bot_name": "slow_b"}))

    bot_a = _SlowBot(user_id=1, slug="slow_a", state_file=sf_a, sleep_s=0.05)
    bot_b = _SlowBot(user_id=1, slug="slow_b", state_file=sf_b, sleep_s=0.05)

    async def _fake_all():
        return [bot_a, bot_b]

    monkeypatch.setattr(web_app.registry, "all", _fake_all)

    broadcasts: list[dict] = []

    async def _fake_broadcast(payload, target_user_id=None):
        broadcasts.append({
            "target": target_user_id, "body": json.loads(payload),
        })

    monkeypatch.setattr(
        web_app.state_broadcaster, "broadcast", _fake_broadcast,
    )
    monkeypatch.setattr(
        web_app, "_compute_summary",
        lambda snapshot: {"bots": len(snapshot)},
    )

    # Only short-circuit the watcher's own end-of-iteration 2.0s
    # sleep — other coroutines on the loop (test tickers etc.) must
    # be allowed to call asyncio.sleep normally. Real asyncio.sleep
    # is preserved via a captured reference; the fake checks the
    # delay and raises only on the watcher's signature 2.0s value.
    _real_sleep = web_app.asyncio.sleep

    async def _fake_sleep(delay):
        if delay == 2.0:
            raise _StopIteration
        await _real_sleep(delay)

    monkeypatch.setattr(web_app.asyncio, "sleep", _fake_sleep)
    web_app._state_mtimes.clear()

    return bot_a, bot_b, broadcasts


# ── Threading contract ─────────────────────────────────────────────────────


class TestReadStateOffloaded:
    """``read_state`` must execute on a thread pool worker, NOT on the
    event-loop thread. Pre-fix the call was synchronous on the loop."""

    def test_read_state_runs_off_loop(self, _watch_loop_setup):
        bot_a, bot_b, _broadcasts = _watch_loop_setup

        loop_thread_id: dict[str, int] = {}

        async def _capture_then_run():
            loop_thread_id["main"] = threading.get_ident()
            await web_app.watch_state_files()

        with pytest.raises(_StopIteration):
            _run(_capture_then_run())

        loop_id = loop_thread_id["main"]
        # Both bots' read_state was called at least once (per-bot
        # path + summary path = 2 calls each).
        assert bot_a.read_threads, "bot_a.read_state was never called"
        assert bot_b.read_threads, "bot_b.read_state was never called"
        # NONE of those calls executed on the loop thread.
        for thread_id in bot_a.read_threads + bot_b.read_threads:
            assert thread_id != loop_id, (
                f"read_state ran on loop thread {loop_id} — "
                f"asyncio.to_thread offload regression"
            )


class TestSummaryReadsRunConcurrently:
    """The summary path runs ``asyncio.gather`` of N to_thread reads
    so a slow file system on one bot can't head-of-line the rest of
    the user's summary. Wall-clock for the summary block must be
    closer to ``max(sleep_s)`` than ``sum(sleep_s)``."""

    def test_summary_block_finishes_concurrently(self, _watch_loop_setup):
        import time
        bot_a, bot_b, _broadcasts = _watch_loop_setup
        # Force a slower read so the diff between serial and parallel
        # is measurable above pytest noise.
        bot_a.sleep_s = 0.3
        bot_b.sleep_s = 0.3

        # Force the per-bot path to skip the read by pre-seeding
        # mtimes so only the summary path triggers reads. That makes
        # the wall-clock measurement specifically about gather().
        web_app._state_mtimes[(bot_a.user_id, bot_a.slug)] = (
            bot_a.state_file.stat().st_mtime
        )
        web_app._state_mtimes[(bot_b.user_id, bot_b.slug)] = (
            bot_b.state_file.stat().st_mtime
        )

        start = time.monotonic()
        with pytest.raises(_StopIteration):
            _run(web_app.watch_state_files())
        elapsed = time.monotonic() - start

        # Concurrent reads: ~0.3s (max of the two), serial would be
        # ~0.6s. Threshold of 0.55s gives some slack for the rest of
        # the iteration body but still fails a regression to serial.
        assert elapsed < 0.55, (
            f"summary read block took {elapsed:.3f}s — "
            f"likely regressed from gather to serial. "
            f"(bot_a sleep={bot_a.sleep_s}, bot_b sleep={bot_b.sleep_s})"
        )


class TestLoopRemainsResponsive:
    """A coroutine running on the loop alongside the watcher must be
    able to interleave its work with the watcher's slow reads. If the
    loop were blocked, ``ticks_during_watch`` would be 0."""

    def test_other_coroutines_run_during_slow_read(self, _watch_loop_setup):
        bot_a, bot_b, _broadcasts = _watch_loop_setup
        bot_a.sleep_s = 0.2
        bot_b.sleep_s = 0.2

        ticks_during_watch = 0

        async def _ticker(stop_event: asyncio.Event):
            nonlocal ticks_during_watch
            while not stop_event.is_set():
                ticks_during_watch += 1
                await asyncio.sleep(0.01)

        async def _drive():
            stop = asyncio.Event()
            ticker_task = asyncio.create_task(_ticker(stop))
            try:
                await web_app.watch_state_files()
            except _StopIteration:
                pass
            finally:
                stop.set()
                await ticker_task

        _run(_drive())

        # Ballpark: with two 0.2s reads + a summary block, the watcher
        # body takes ~0.5–0.8s. A 10ms-per-tick coroutine should fire
        # at least 10× — anything less means the loop was effectively
        # blocked. Thresholding low (5) gives generous slack for CI.
        assert ticks_during_watch >= 5, (
            f"only {ticks_during_watch} ticks observed during watch — "
            f"loop appears blocked despite asyncio.to_thread"
        )
