"""Tests for web.app.lifespan — background-task lifecycle.

Regression-guards the shutdown path: before this fix the lifespan
handler spawned ``tail_logs`` and ``watch_state_files`` but never
cancelled them, so uvicorn's graceful-shutdown path hung waiting for
``asyncio.sleep`` inside the tasks' while-True loops. stop.sh's 5s
grace period would expire and SIGKILL fired on every ``make restart``.

The suite patches the two background-task coroutines with stubs that
surface their lifecycle (started / cancelled / wedged) so assertions
don't depend on file-system state the real tasks poll.

No pytest-asyncio dependency — driven through ``asyncio.run`` to
match the pattern in tests/test_password_breach.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from web import app as web_app


def _run(coro):
    return asyncio.run(coro)


class _StubTask:
    """Replacement coroutine for tail_logs / watch_state_files that
    records whether it was started and whether it saw a CancelledError.
    """

    def __init__(self, wedge: bool = False):
        self.started = asyncio.Event()
        self.cancelled = False
        self.wedge = wedge

    async def __call__(self):
        self.started.set()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            if self.wedge:
                # Swallow the cancellation and keep sleeping — mimics a
                # task whose loop body ignores CancelledError. Uses a
                # shielded sleep so the surrounding gather() genuinely
                # times out rather than completing instantly.
                await asyncio.shield(asyncio.sleep(10))
            raise


def _install_stubs(monkeypatch, tail=None, watch=None):
    tail = tail if tail is not None else _StubTask()
    watch = watch if watch is not None else _StubTask()
    monkeypatch.setattr(web_app, "tail_logs", tail)
    monkeypatch.setattr(web_app, "watch_state_files", watch)
    return tail, watch


def test_lifespan_spawns_background_tasks(monkeypatch):
    """Startup half spawns both background tasks before yielding."""
    tail, watch = _install_stubs(monkeypatch)

    async def _drive():
        async with web_app.lifespan(web_app.app):
            # Both coroutines must have been scheduled and started.
            await asyncio.wait_for(tail.started.wait(), timeout=1.0)
            await asyncio.wait_for(watch.started.wait(), timeout=1.0)

    _run(_drive())


def test_lifespan_cancels_tasks_on_shutdown(monkeypatch):
    """Shutdown half cancels both tasks and waits for them to return."""
    tail, watch = _install_stubs(monkeypatch)

    async def _drive():
        async with web_app.lifespan(web_app.app):
            await asyncio.wait_for(tail.started.wait(), timeout=1.0)
            await asyncio.wait_for(watch.started.wait(), timeout=1.0)
        # Exiting the context manager runs the shutdown half. Both stubs
        # must have observed CancelledError by the time we return.
        assert tail.cancelled is True
        assert watch.cancelled is True

    _run(_drive())


def test_lifespan_timeout_logs_warning(monkeypatch, caplog):
    """A task that refuses to exit on cancel must not block shutdown.

    The stub swallows CancelledError and sleeps past the 2s cap, so
    ``asyncio.wait_for`` must trip and the warning must surface in the
    portal log.
    """
    wedged = _StubTask(wedge=True)
    tail, watch = _install_stubs(monkeypatch, tail=wedged)

    # Monkeypatch the timeout down so the test finishes quickly.
    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(fut, timeout):
        return await real_wait_for(fut, timeout=0.2)

    monkeypatch.setattr(web_app.asyncio, "wait_for", _fast_wait_for)

    async def _drive():
        with caplog.at_level(logging.WARNING, logger=web_app.logger.name):
            async with web_app.lifespan(web_app.app):
                await asyncio.wait_for(wedged.started.wait(), timeout=1.0)
                await asyncio.wait_for(watch.started.wait(), timeout=1.0)

    _run(_drive())

    assert any(
        "Background task cancellation timed out" in rec.getMessage()
        for rec in caplog.records
    ), "expected timeout warning in portal log"
