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
from web.app import BotInfo, BotRegistry, _reset_user_dirs_cache


_MINIMAL_YAML = {
    "bot": {
        "name": "Composite Key Test",
        "mode": "paper",
        "exchange_account_id": 1,
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


@pytest.fixture(autouse=True)
def _reset_scan_cache():
    """Reset the module-level fail-closed cache before AND after every
    test so state doesn't leak across tests (Paths from tmp_path A
    would otherwise sit in the dedup set while tmp_path B scans run).
    """
    _reset_user_dirs_cache()
    yield
    _reset_user_dirs_cache()


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
        """Seed only user 1 (default). Drop a config/bots/999/ yaml.
        Registry must not pick this up and must log a WARNING."""
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
        """Positive-path guard: the default admin user (id=1) keeps
        working. If we fail here we accidentally choked off the only
        production user."""
        _write_bot_yaml(sandbox_registry, 1, "rsi", "Admin's RSI")

        reg = BotRegistry()
        assert (1, "rsi") in reg._bots

    def test_mixed_orphan_and_valid_dirs(self, sandbox_registry, caplog):
        """Realistic scenario: the operator has a valid user 1 dir
        AND an orphan 777 dir. Only 1 must land in the registry;
        777 gets a WARNING."""
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
        """A user with active=0 must be treated like a
        non-existent user. The cross-check filter must require
        active=1, not just ID existence. Otherwise an operator who
        deactivates a tenant could still see bots for that tenant
        appear in the registry."""
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


# ── Audit v25 Finding #1: fail-closed fallback bij DB-failure ──────────────


class TestUserDirScanCacheFallback:
    """Pre-fix viel _scan_user_dirs bij get_active_user_ids()-failure
    terug op integer-name-only matching — elke orphan dir kwam er
    weer door. Post-fix: last-known-good cache, herbruikbaar tot
    ``_MAX_STALE_REFRESHES`` achter elkaar falen, dan fail-closed.
    """

    def test_db_success_refreshes_cache(self, sandbox_registry):
        """Succesvolle DB-call vult de module-cache en reset de
        failure-counter. Zonder deze invariant kan een eerder
        mislukte scan voor altijd blijven hangen in stale-mode."""
        import web.app as webapp
        _seed_user(2, "bob")
        reg = BotRegistry()
        reg._scan_user_dirs()

        assert webapp._cached_active_users == {1, 2}
        assert webapp._db_failure_count == 0

    def test_db_failure_uses_cache_within_window(
        self, sandbox_registry, monkeypatch, caplog,
    ):
        """On a DB failure with a valid cache we must reuse the
        cache for N stale cycles — not fail-open back to
        integer-name matching, not fail-closed empty straight away.
        """
        import web.app as webapp
        _seed_user(2, "bob")
        _write_bot_yaml(sandbox_registry, 1, "legit", "Legit")
        _write_bot_yaml(sandbox_registry, 2, "bobs_bot", "Bob")

        # Prime the cache with a single successful scan.
        reg = BotRegistry()
        assert webapp._cached_active_users == {1, 2}

        # Patch core.user.get_active_user_ids onto a raise path.
        def _boom():
            raise RuntimeError("DB temporarily unavailable")
        monkeypatch.setattr("core.user.get_active_user_ids", _boom)

        caplog.clear()
        with caplog.at_level("WARNING", logger="web.app"):
            for _ in range(3):
                # Force every iteration through the DB path —
                # without this reset the happy-path cache
                # (_CACHE_TTL_S=30s) short-circuits and the
                # fail-closed counters are never touched. This test
                # scenario specifically validates the DB-failure
                # behaviour, not the cache.
                webapp._cache_last_refresh_ts = 0.0
                result = reg._scan_user_dirs()
                # Each stale cycle still yields (1, …) and (2, …).
                uids = {uid for uid, _dir in result}
                assert uids == {1, 2}

        assert webapp._db_failure_count == 3
        stale_msgs = [
            r.message for r in caplog.records
            if r.levelname == "WARNING" and "stale cycles" in r.message
        ]
        assert len(stale_msgs) == 3, (
            f"expected 3 stale-cycle WARNINGs, got {stale_msgs}"
        )
        # No ERROR within the window — that's the very difference
        # from fail-closed mode.
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors == []

    def test_db_failure_returns_empty_after_max_stale_refreshes(
        self, sandbox_registry, monkeypatch, caplog,
    ):
        """After ``_MAX_STALE_REFRESHES`` failed attempts we flip
        to fail-closed: returns [], logs ERROR."""
        import web.app as webapp
        _write_bot_yaml(sandbox_registry, 1, "legit", "Legit")
        reg = BotRegistry()
        assert webapp._cached_active_users == {1}

        monkeypatch.setattr(
            "core.user.get_active_user_ids",
            lambda: (_ for _ in ()).throw(RuntimeError("DB down")),
        )

        caplog.clear()
        with caplog.at_level("ERROR", logger="web.app"):
            result = None
            # _MAX_STALE_REFRESHES=5; the 6th call must go
            # fail-closed. Force the DB path per iteration —
            # otherwise the 30s happy-path cache would optimise
            # 5 of the 6 iterations away.
            for _ in range(webapp._MAX_STALE_REFRESHES + 1):
                webapp._cache_last_refresh_ts = 0.0
                result = reg._scan_user_dirs()

        assert result == [], (
            f"expected empty list after exhausted staleness, got {result}"
        )
        error_msgs = [
            r.message for r in caplog.records
            if r.levelname == "ERROR"
            and "fail-closed" in r.message
        ]
        assert error_msgs, (
            f"expected ERROR with 'fail-closed', got "
            f"{[r.message for r in caplog.records]}"
        )
        # Counter stays incremented — the next DB success resets it.
        assert webapp._db_failure_count > webapp._MAX_STALE_REFRESHES

    def test_db_success_after_failure_resets_counter(
        self, sandbox_registry, monkeypatch,
    ):
        """After a recovered DB call the counter must reset to 0
        and the cache must be refreshed. Without the reset the
        registry would stay in stale mode permanently after a
        hiccup."""
        import web.app as webapp
        _seed_user(2, "bob")
        reg = BotRegistry()

        # Two failed scans. Force the DB path — within TTL (30s)
        # the happy-path cache would otherwise silently swallow
        # the failure.
        def _boom():
            raise RuntimeError("hiccup")
        monkeypatch.setattr("core.user.get_active_user_ids", _boom)
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()
        assert webapp._db_failure_count == 2

        # Recover the DB call and scan again.
        monkeypatch.undo()
        # Seed user 2 again because monkeypatch.undo() doesn't
        # remove the fixture installation but does remove our
        # monkeypatch. (sandbox_registry itself stays active.)
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()

        assert webapp._db_failure_count == 0
        assert webapp._cached_active_users == {1, 2}

    def test_db_failure_with_no_prior_cache_fails_closed_immediately(
        self, sandbox_registry, monkeypatch, caplog,
    ):
        """Boot-scenario: DB nog niet ready (of registry freshly
        re-initialised na een reset) + get_active_user_ids() faalt.
        Zonder cache moeten we meteen fail-closed — niet fail-open
        fallback op integer-name matching."""
        import web.app as webapp
        # Zorg dat de cache écht leeg is voor deze test.
        _reset_user_dirs_cache()
        assert webapp._cached_active_users is None

        # Patch get_active_user_ids BEFORE BotRegistry() does the
        # first scan — otherwise __init__ primes the cache anyway.
        def _boom():
            raise RuntimeError("DB not ready")
        monkeypatch.setattr("core.user.get_active_user_ids", _boom)

        _write_bot_yaml(sandbox_registry, 1, "would_be_orphan_if_fail_open", "X")

        caplog.clear()
        with caplog.at_level("ERROR", logger="web.app"):
            reg = BotRegistry()
            result = reg._scan_user_dirs()

        assert result == [], (
            "fail-open regression — empty cache must return [] immediately"
        )
        error_msgs = [
            r.message for r in caplog.records
            if r.levelname == "ERROR" and "no prior cache" in r.message
        ]
        assert error_msgs, (
            f"expected ERROR with 'no prior cache', got "
            f"{[r.message for r in caplog.records if r.levelname == 'ERROR']}"
        )


# ── Audit v25 Finding #7: orphan warning log dedup ─────────────────────────


class TestOrphanLogDedup:
    """Pre-fix: every 5-second registry refresh that hit an orphan
    dir re-logged a WARNING. One typo = 720 WARNINGs/hour. Post-fix:
    only log if the orphan is NEW since the last scan. Disappeared
    and re-introduced orphans are logged again so operator drift
    stays visible.
    """

    def test_orphan_logged_once_across_scans(
        self, sandbox_registry, caplog,
    ):
        """5 scans against the same orphan = 1 WARNING. Not 5."""
        _write_bot_yaml(sandbox_registry, 1, "legit", "OK")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan")

        # The first scan happens in ``BotRegistry.__init__``;
        # capture from that point on so the total count across all
        # 5 scans matches (first + 4 manual).
        with caplog.at_level("WARNING", logger="web.app"):
            reg = BotRegistry()  # scan 1
            for _ in range(4):
                reg._scan_user_dirs()  # scans 2–5

        warnings_999 = [
            r.message for r in caplog.records
            if r.levelname == "WARNING"
            and "orphan" in r.message.lower()
            and "999" in r.message
        ]
        assert len(warnings_999) == 1, (
            f"expected exactly 1 WARNING for 999 across 5 scans, got "
            f"{len(warnings_999)}: {warnings_999}"
        )

    def test_new_orphan_logged_on_subsequent_scan(
        self, sandbox_registry, caplog,
    ):
        """Scan 1: 999 logged. Scan 2 (after adding 888): 888
        logged, 999 NOT logged again."""
        _write_bot_yaml(sandbox_registry, 1, "legit", "OK")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan 999")

        reg = BotRegistry()  # first scan in __init__ logs 999.
        caplog.clear()

        _write_bot_yaml(sandbox_registry, 888, "ghost2", "Orphan 888")

        with caplog.at_level("WARNING", logger="web.app"):
            reg._scan_user_dirs()

        orphan_msgs = [
            r.message for r in caplog.records
            if r.levelname == "WARNING" and "orphan" in r.message.lower()
        ]
        assert any("888" in m for m in orphan_msgs), (
            f"new orphan 888 must be logged, got {orphan_msgs}"
        )
        assert not any("999" in m for m in orphan_msgs), (
            f"existing orphan 999 must NOT re-log, got {orphan_msgs}"
        )

    def test_removed_orphan_relogged_if_recreated(
        self, sandbox_registry, caplog,
    ):
        """Life cycle of an orphan: log → disappear → log again
        when it comes back. Guarantees that operator drift stays
        visible without a temporarily removed dir going silent
        forever."""
        import shutil as _shutil
        _write_bot_yaml(sandbox_registry, 1, "legit", "OK")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan 999")

        # Scan 1 — 999 is logged.
        reg = BotRegistry()

        # Operator cleans up 999.
        _shutil.rmtree(sandbox_registry / "config" / "bots" / "999")

        caplog.clear()
        with caplog.at_level("WARNING", logger="web.app"):
            reg._scan_user_dirs()  # Scan 2 — no orphan any more.

        # Operator puts it back.
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan 999 take 2")

        with caplog.at_level("WARNING", logger="web.app"):
            reg._scan_user_dirs()  # Scan 3 — 999 is new again.

        orphan_msgs = [
            r.message for r in caplog.records
            if r.levelname == "WARNING" and "orphan" in r.message.lower()
            and "999" in r.message
        ]
        assert len(orphan_msgs) == 1, (
            f"expected 999 to re-log exactly once after recreation, got "
            f"{len(orphan_msgs)}: {orphan_msgs}"
        )


# ── Audit v25 Finding #6: happy-path DB-cache TTL ──────────────────────────


class TestUserDirScanCacheTTL:
    """Finding #6: before the fix ``_scan_user_dirs`` ran a fresh
    ``get_active_user_ids()`` DB call on every scan (≈ every 5 s
    per portal), even if nothing in the users table changed. The
    ``_CACHE_TTL_S`` gate (30 s) now reuses the cached set across
    scans — the DB-failure fail-closed paths above
    (TestUserDirScanCacheFallback) are only reached once the cache
    expires or is missing.
    """

    def test_cache_hit_within_ttl_skips_db_call(
        self, sandbox_registry, monkeypatch,
    ):
        """Two scans within TTL = one DB call. Without the TTL gate
        the counter would tick to 2 (and every bot tick would make
        an extra query)."""
        import web.app as webapp
        _seed_user(2, "bob")

        calls = {"n": 0}
        from core import user as _user_mod
        orig = _user_mod.get_active_user_ids

        def _counting():
            calls["n"] += 1
            return orig()
        monkeypatch.setattr("core.user.get_active_user_ids", _counting)

        reg = BotRegistry()
        # BotRegistry() already does 1 scan via __init__ →
        # _refresh_locked. Zero the counter so that path is not
        # counted in the assertion.
        initial = calls["n"]
        reg._scan_user_dirs()
        reg._scan_user_dirs()
        assert calls["n"] == initial, (
            f"expected cache-hits to skip DB; counter went {initial} → {calls['n']}"
        )

    def test_cache_refresh_after_ttl_expires(
        self, sandbox_registry, monkeypatch,
    ):
        """After TTL expiry the next scan is a cache miss and hits
        the DB again. Simulated by rewinding the timestamp."""
        import web.app as webapp
        _seed_user(2, "bob")

        calls = {"n": 0}
        from core import user as _user_mod
        orig = _user_mod.get_active_user_ids

        def _counting():
            calls["n"] += 1
            return orig()
        monkeypatch.setattr("core.user.get_active_user_ids", _counting)

        reg = BotRegistry()
        initial = calls["n"]
        reg._scan_user_dirs()  # cache-hit
        # Simulate TTL expiry by rewinding the timestamp.
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()  # cache-miss → DB call
        assert calls["n"] == initial + 1, (
            f"expected exactly one DB call after TTL expiry; counter "
            f"went {initial} → {calls['n']}"
        )

    def test_cache_reset_helper_also_clears_timestamp(
        self, sandbox_registry,
    ):
        """``_reset_user_dirs_cache()`` must also zero the TTL
        timestamp — otherwise a test fixture that clears the cache
        would still see the next scan as a cache hit and not touch
        the DB."""
        import web.app as webapp
        _seed_user(2, "bob")

        reg = BotRegistry()  # primes the cache + timestamp
        assert webapp._cache_last_refresh_ts > 0
        assert webapp._cached_active_users == {1, 2}

        _reset_user_dirs_cache()
        assert webapp._cache_last_refresh_ts == 0.0
        assert webapp._cached_active_users is None

        # A subsequent scan must then be guaranteed to hit the DB
        # (cache is empty, so the TTL check skips on
        # `_cached_active_users is None` and the try/except DB
        # path is traversed).
        reg._scan_user_dirs()
        assert webapp._cached_active_users == {1, 2}
