"""Regression guard for audit v1 r1-041 — ``_state_mtimes`` keyed on
``(user_id, slug)`` instead of slug alone.

Two users with the same slug (multi-tenant Phase 2 allows this) must
each get their own mtime-cache entry; otherwise user A's state-file
change would suppress user B's change-detection on the next iteration
of ``watch_state_files``.

The test drives one iteration of the watcher with ``asyncio.sleep``
patched to raise, so the ``while True`` loop runs its body once and
then unwinds. Two fake bots share a slug but differ on ``user_id``;
both must land in the cache under distinct tuple keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from web import app as web_app  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeBot:
    """Stand-in for ``BotInfo`` with the watcher's tiny read surface.

    The watcher touches ``state_file.exists()``, ``state_file.stat()
    .st_mtime``, ``read_state()``, ``user_id`` and ``slug``. Nothing
    else — so we can avoid constructing a full BotInfo.
    """

    def __init__(self, user_id: int, slug: str, state_file):
        self.user_id = user_id
        self.slug = slug
        self.state_file = state_file

    def read_state(self) -> dict:
        return {"bot_name": self.slug, "user_id": self.user_id}


class _StopIteration(Exception):
    """Private sentinel so pytest doesn't confuse this with StopIteration."""


def test_state_mtimes_keyed_on_user_and_slug(tmp_path, monkeypatch):
    # Two bots sharing a slug under different users — the exact
    # multi-tenant scenario r1-041 flagged.
    sf_a = tmp_path / "user1.rsi_test.state.json"
    sf_b = tmp_path / "user2.rsi_test.state.json"
    sf_a.write_text(json.dumps({"bot_name": "rsi_test"}))
    sf_b.write_text(json.dumps({"bot_name": "rsi_test"}))

    bot_a = _FakeBot(user_id=1, slug="rsi_test", state_file=sf_a)
    bot_b = _FakeBot(user_id=2, slug="rsi_test", state_file=sf_b)

    async def _fake_all():
        return [bot_a, bot_b]

    monkeypatch.setattr(web_app.registry, "all", _fake_all)

    broadcasts: list[tuple[int | None, dict]] = []

    async def _fake_broadcast(payload, target_user_id=None):
        broadcasts.append((target_user_id, json.loads(payload)))

    monkeypatch.setattr(
        web_app.state_broadcaster, "broadcast", _fake_broadcast,
    )

    # _compute_summary reads a list of state dicts; bypass the real
    # implementation to keep the test focused on mtime-keying.
    monkeypatch.setattr(
        web_app, "_compute_summary", lambda snapshot: {"bots": len(snapshot)},
    )

    # Break out after the first full iteration by raising from sleep.
    async def _fake_sleep(_delay):
        raise _StopIteration

    monkeypatch.setattr(web_app.asyncio, "sleep", _fake_sleep)

    # Fresh cache so a prior test's entries don't mask the regression.
    web_app._state_mtimes.clear()

    with pytest.raises(_StopIteration):
        _run(web_app.watch_state_files())

    # Both users must have their own cache key — the regression would
    # leave exactly one entry under the shared slug.
    assert (1, "rsi_test") in web_app._state_mtimes
    assert (2, "rsi_test") in web_app._state_mtimes
    assert len(web_app._state_mtimes) == 2

    # Both users must have received a bot_state broadcast (plus one
    # summary frame each). Regression path would drop user B's frame.
    bot_state_targets = [
        uid for uid, body in broadcasts if body.get("type") == "bot_state"
    ]
    assert sorted(bot_state_targets) == [1, 2]
