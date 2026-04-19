"""Tests for BotRegistry composite-key (user_id, slug) semantics.

Phase-2 the registry stopped using slug as a globally-unique key —
two users can now own a bot with the same slug name. These tests
pin the isolation invariant so a future refactor can't silently
collapse back to slug-only and leak data across users.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import database, paths
from web.app import BotInfo, BotRegistry


_MINIMAL_YAML = {
    "bot": {
        "name": "Composite Key Test",
        "mode": "paper",
        "exchange": "bitget",
        "pair": "BTC/USD",
        "contract_type": "inverse_perpetual",
        "leverage": {"enabled": False, "size": 1},
        "dca": {
            "base_order_size": 0.001,
            "max_orders": 3,
            "order_spacing_pct": 2.5,
            "multiplier": 1.0,
        },
        "entry": {"indicators": []},
        "take_profit": {"target_pct": 3.0},
        "stop_loss": {"type": "fixed", "pct": 5.0},
    }
}


@pytest.fixture
def sandbox_registry(tmp_path, monkeypatch):
    """Point BASE_DIR at tmp_path AND at a fresh SQLite DB so the
    Phase-2 cross-check (``_scan_user_dirs`` → ``users`` table) has
    something to look at. The default init_db() seed gives us user
    id=1 (admin); tests that need additional users call
    ``_seed_user()`` inside their body."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    # The BotRegistry module-level constants are bound to the real
    # paths at import time. Rebind them on the `web.app` module so
    # the _scan_user_dirs loop walks tmp_path/config/bots/.
    import web.app as webapp
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setattr(webapp, "CONFIG_DIR", tmp_path / "config" / "bots")
    monkeypatch.setattr(webapp, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(webapp, "PID_DIR", tmp_path / "logs" / "pids")
    # Per-test DB so get_active_user_ids() has a predictable set.
    database.set_db_path(tmp_path / "registry_test.db")
    database.init_db()
    yield tmp_path
    database.close_db()


def _seed_user(user_id: int, username: str, active: int = 1) -> None:
    """Insert an extra users row alongside the seeded admin (id=1).
    Used by tests that register bots under user_ids other than 1."""
    conn = database.get_db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, username, active) VALUES (?, ?, ?)",
            (user_id, username, active),
        )


def _write_bot_yaml(base: Path, user_id: int, slug: str, name: str) -> None:
    user_dir = base / "config" / "bots" / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(_MINIMAL_YAML)
    payload["bot"] = {**payload["bot"], "name": name}
    (user_dir / f"{slug}.yaml").write_text(yaml.safe_dump(payload))


class TestCompositeKey:

    def test_two_users_same_slug_do_not_collide(self, sandbox_registry):
        """The whole point of Phase 2: user 1 and user 2 can own a
        bot named ``rsi_test`` without stepping on each other."""
        _seed_user(2, "bob")
        _write_bot_yaml(sandbox_registry, 1, "rsi_test", "User 1's RSI")
        _write_bot_yaml(sandbox_registry, 2, "rsi_test", "User 2's RSI")

        reg = BotRegistry()
        bot_a = asyncio.run(reg.get(1, "rsi_test"))
        bot_b = asyncio.run(reg.get(2, "rsi_test"))
        assert bot_a is not None and bot_a.user_id == 1
        assert bot_b is not None and bot_b.user_id == 2
        # Different BotInfo objects — the registry keeps them apart.
        assert bot_a is not bot_b

    def test_all_filters_by_user(self, sandbox_registry):
        _seed_user(2, "bob")
        _write_bot_yaml(sandbox_registry, 1, "alpha", "A1")
        _write_bot_yaml(sandbox_registry, 1, "beta", "B1")
        _write_bot_yaml(sandbox_registry, 2, "gamma", "G2")

        reg = BotRegistry()
        all_1 = asyncio.run(reg.all(user_id=1))
        all_2 = asyncio.run(reg.all(user_id=2))
        all_none = asyncio.run(reg.all())

        assert {b.slug for b in all_1} == {"alpha", "beta"}
        assert {b.slug for b in all_2} == {"gamma"}
        assert len(all_none) == 3

    def test_cross_user_lookup_returns_none(self, sandbox_registry):
        """Registry.get(user_id=2, slug=) must NOT find a bot that
        belongs to user 1 — even if no user 2 bot with that slug exists."""
        _seed_user(2, "bob")
        _write_bot_yaml(sandbox_registry, 1, "only_mine", "Mine")
        reg = BotRegistry()
        assert asyncio.run(reg.get(1, "only_mine")) is not None
        assert asyncio.run(reg.get(2, "only_mine")) is None

    def test_begin_start_scoped_per_user(self, sandbox_registry):
        """Claiming the start-slot for (1, slug) must not block (2, slug)."""
        _seed_user(2, "bob")
        _write_bot_yaml(sandbox_registry, 1, "shared", "One")
        _write_bot_yaml(sandbox_registry, 2, "shared", "Two")
        reg = BotRegistry()

        async def _run():
            ok1 = await reg.begin_start(1, "shared")
            ok2 = await reg.begin_start(2, "shared")
            # Second attempt on same (1, shared) must fail.
            ok1b = await reg.begin_start(1, "shared")
            # Releases only clear their own pair.
            await reg.end_start(1, "shared")
            ok1c = await reg.begin_start(1, "shared")
            return ok1, ok2, ok1b, ok1c

        ok1, ok2, ok1b, ok1c = asyncio.run(_run())
        assert ok1 is True and ok2 is True
        assert ok1b is False  # still claimed
        assert ok1c is True   # re-claimable after end_start


class TestBotInfoPathScoping:

    def test_paths_partition_per_user(self, sandbox_registry):
        info_a = BotInfo(user_id=1, slug="x", config_file="config/bots/1/x.yaml")
        info_b = BotInfo(user_id=2, slug="x", config_file="config/bots/2/x.yaml")
        assert info_a.state_file != info_b.state_file
        assert info_a.log_file != info_b.log_file
        assert info_a.pid_file != info_b.pid_file
        assert info_a.manual_trigger_file != info_b.manual_trigger_file
        # And all of them carry the right user_id in the path segment.
        assert "/1/" in str(info_a.state_file)
        assert "/2/" in str(info_b.state_file)


class TestIgnoresNonNumericSubdirs:
    """Only integer-named subdirs of config/bots/ count as users.
    Legacy backup folders (e.g. 'backup_20260101') or operator-
    placed directories must be silently skipped."""

    def test_non_integer_dir_ignored(self, sandbox_registry):
        _write_bot_yaml(sandbox_registry, 1, "good", "OK")
        # Plant a stray directory that must NOT register as a user.
        stray = sandbox_registry / "config" / "bots" / "backup_snapshot"
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "ghost.yaml").write_text("bot: {}")

        reg = BotRegistry()
        all_bots = asyncio.run(reg.all())
        slugs = {b.slug for b in all_bots}
        assert "good" in slugs
        assert "ghost" not in slugs


# ── Audit v24 MEDIUM #2: users-table cross-check ───────────────────────────


class TestOrphanUserDirs:
    """_scan_user_dirs verifieert nu dat een integer-named subdir
    matcht met een active row in de users tabel. Orphan dirs
    (operator-fout, stale state, deactivated user) worden gelogd
    als WARNING met 'orphan' in de message en geskipped."""

    def test_orphan_integer_dir_is_skipped(self, sandbox_registry, caplog):
        """Seed alleen user 1 (default). Drop config/bots/999/ yaml.
        Registry mag dit niet oppakken + moet een WARNING loggen."""
        _write_bot_yaml(sandbox_registry, 1, "legit", "Legit one")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan")

        with caplog.at_level("WARNING", logger="web.app"):
            reg = BotRegistry()

        assert (999, "ghost") not in reg._bots
        assert (1, "legit") in reg._bots

        warnings = [
            r.message for r in caplog.records
            if r.levelname == "WARNING" and "orphan" in r.message.lower()
        ]
        assert any("999" in w for w in warnings), (
            f"expected 'orphan' warning mentioning 999, got {warnings}"
        )

    def test_existing_user_dir_still_scanned(self, sandbox_registry):
        """Positive-path guard: de default admin user (id=1) blijft
        gewoon werken. Als we hier falen hebben we per ongeluk de
        enige productie-user afgeknepen."""
        _write_bot_yaml(sandbox_registry, 1, "rsi", "Admin's RSI")

        reg = BotRegistry()
        assert (1, "rsi") in reg._bots

    def test_mixed_orphan_and_valid_dirs(self, sandbox_registry, caplog):
        """Realistisch scenario: operator heeft een valide user 1
        dir én een orphan 777 dir. Alleen 1 mag in de registry
        landen; 777 krijgt een WARNING."""
        _write_bot_yaml(sandbox_registry, 1, "real", "Real")
        _write_bot_yaml(sandbox_registry, 777, "fake", "Orphan")

        with caplog.at_level("WARNING", logger="web.app"):
            reg = BotRegistry()

        assert (1, "real") in reg._bots
        assert (777, "fake") not in reg._bots

        warnings = [
            r.message for r in caplog.records
            if r.levelname == "WARNING" and "orphan" in r.message.lower()
        ]
        assert any("777" in w for w in warnings), (
            f"expected 'orphan' warning mentioning 777, got {warnings}"
        )

    def test_inactive_user_dir_is_treated_as_orphan(
        self, sandbox_registry, caplog,
    ):
        """Een user met active=0 moet net als een niet-bestaande user
        behandeld worden. Het cross-check filter moet op active=1
        staan, niet alleen op ID-bestaan. Anders kan een operator die
        een tenant deactiveert nog steeds bots voor die tenant in de
        registry zien verschijnen."""
        _seed_user(42, "deactivated", active=0)
        _write_bot_yaml(sandbox_registry, 42, "bot", "Dead tenant")

        with caplog.at_level("WARNING", logger="web.app"):
            reg = BotRegistry()

        assert (42, "bot") not in reg._bots
        warnings = [
            r.message for r in caplog.records
            if r.levelname == "WARNING" and "orphan" in r.message.lower()
        ]
        assert any("42" in w for w in warnings)
