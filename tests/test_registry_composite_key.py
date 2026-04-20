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
        """Bij DB-failure met een geldige cache moeten we N stale
        cycles lang de cache hergebruiken — niet fail-open terugvallen
        op integer-name matching, niet meteen fail-closed leeg teruggeven.
        """
        import web.app as webapp
        _seed_user(2, "bob")
        _write_bot_yaml(sandbox_registry, 1, "legit", "Legit")
        _write_bot_yaml(sandbox_registry, 2, "bobs_bot", "Bob")

        # Prime de cache met één succesvolle scan.
        reg = BotRegistry()
        assert webapp._cached_active_users == {1, 2}

        # Patch core.user.get_active_user_ids op raise-pad.
        def _boom():
            raise RuntimeError("DB temporarily unavailable")
        monkeypatch.setattr("core.user.get_active_user_ids", _boom)

        caplog.clear()
        with caplog.at_level("WARNING", logger="web.app"):
            for _ in range(3):
                # Force elke iteratie door de DB-path heen — zonder
                # deze reset short-circuit de happy-path cache
                # (_CACHE_TTL_S=30s) en worden de fail-closed counters
                # nooit aangeraakt. Dit test-scenario valideert juist
                # het DB-failure gedrag, niet de cache.
                webapp._cache_last_refresh_ts = 0.0
                result = reg._scan_user_dirs()
                # Elke stale-cycle levert nog steeds (1, …) en (2, …).
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
        # Geen ERROR binnen het venster — dat is juist het verschil
        # met fail-closed mode.
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors == []

    def test_db_failure_returns_empty_after_max_stale_refreshes(
        self, sandbox_registry, monkeypatch, caplog,
    ):
        """Na ``_MAX_STALE_REFRESHES`` mislukte pogingen flip we naar
        fail-closed: returnt [], ERROR log."""
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
            # _MAX_STALE_REFRESHES=5; de 6e call moet fail-closed gaan.
            # Force DB-path per iteratie — de 30s happy-path cache zou
            # anders 5 van de 6 iteraties wegoptimaliseren.
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
        # Counter stays incremented — de volgende DB-success reset 'em.
        assert webapp._db_failure_count > webapp._MAX_STALE_REFRESHES

    def test_db_success_after_failure_resets_counter(
        self, sandbox_registry, monkeypatch,
    ):
        """Na een herstelde DB-call moet de counter terug naar 0 en
        de cache vernieuwd zijn. Zonder reset zou de registry
        permanent in stale-mode blijven zitten na een hickup."""
        import web.app as webapp
        _seed_user(2, "bob")
        reg = BotRegistry()

        # Twee mislukte scans. Force DB-path — binnen TTL (30s) zou
        # de happy-path cache anders de failure stilletjes wegslikken.
        def _boom():
            raise RuntimeError("hiccup")
        monkeypatch.setattr("core.user.get_active_user_ids", _boom)
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()
        assert webapp._db_failure_count == 2

        # Herstel de DB-call en scan opnieuw.
        monkeypatch.undo()
        # Seed user 2 opnieuw omdat monkeypatch.undo() de fixture-
        # installatie niet verwijdert maar onze monkeypatch wel.
        # (sandbox_registry zelf blijft gelden.)
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

        # Patch get_active_user_ids VOORDAT BotRegistry() de
        # eerste scan doet — anders primt __init__ de cache alsnog.
        def _boom():
            raise RuntimeError("DB not ready")
        monkeypatch.setattr("core.user.get_active_user_ids", _boom)

        _write_bot_yaml(sandbox_registry, 1, "would_be_orphan_if_fail_open", "X")

        caplog.clear()
        with caplog.at_level("ERROR", logger="web.app"):
            reg = BotRegistry()
            result = reg._scan_user_dirs()

        assert result == [], (
            "fail-open regressie — lege cache moet meteen [] returnen"
        )
        error_msgs = [
            r.message for r in caplog.records
            if r.levelname == "ERROR" and "no prior cache" in r.message
        ]
        assert error_msgs, (
            f"expected ERROR met 'no prior cache', got "
            f"{[r.message for r in caplog.records if r.levelname == 'ERROR']}"
        )


# ── Audit v25 Finding #7: orphan warning log dedup ─────────────────────────


class TestOrphanLogDedup:
    """Pre-fix: elke 5-seconden registry-refresh die een orphan dir
    tegenkwam logde opnieuw een WARNING. Eén typo = 720
    WARNINGs/uur. Post-fix: alleen loggen als de orphan NIEUW is
    sinds de vorige scan. Verdwenen + heringevoerde orphans worden
    wél opnieuw gelogd zodat operator-drift niet onzichtbaar is.
    """

    def test_orphan_logged_once_across_scans(
        self, sandbox_registry, caplog,
    ):
        """5 scans op dezelfde orphan = 1 WARNING. Niet 5."""
        _write_bot_yaml(sandbox_registry, 1, "legit", "OK")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan")

        # De eerste scan gebeurt in ``BotRegistry.__init__``; capture
        # vanaf dat moment zodat de totaal-telling over alle 5 scans
        # klopt (eerste + 4 handmatige).
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
        """Scan 1: 999 gelogd. Scan 2 (na toevoegen van 888): 888
        gelogd, 999 NIET opnieuw."""
        _write_bot_yaml(sandbox_registry, 1, "legit", "OK")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan 999")

        reg = BotRegistry()  # eerste scan in __init__ logt 999.
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
        """Life-cycle van een orphan: loggen → verdwijnen → opnieuw
        loggen als 'ie terugkomt. Garandeert dat operator-drift
        zichtbaar blijft zonder dat een tijdelijk verwijderde dir
        voor altijd stil blijft."""
        import shutil as _shutil
        _write_bot_yaml(sandbox_registry, 1, "legit", "OK")
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan 999")

        # Scan 1 — 999 wordt gelogd.
        reg = BotRegistry()

        # Operator ruimt 999 op.
        _shutil.rmtree(sandbox_registry / "config" / "bots" / "999")

        caplog.clear()
        with caplog.at_level("WARNING", logger="web.app"):
            reg._scan_user_dirs()  # Scan 2 — geen orphan meer.

        # Operator plaatst 'm opnieuw.
        _write_bot_yaml(sandbox_registry, 999, "ghost", "Orphan 999 take 2")

        with caplog.at_level("WARNING", logger="web.app"):
            reg._scan_user_dirs()  # Scan 3 — 999 is weer nieuw.

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
    """Finding #6: vóór de fix draaide ``_scan_user_dirs`` elke scan
    (≈ elke 5 s per portal) een verse ``get_active_user_ids()`` DB-call,
    ook als niets in de users-tabel veranderde. De ``_CACHE_TTL_S``
    gate (30 s) hergebruikt nu de cached set tussen scans — de
    DB-failure fail-closed paden hierboven (TestUserDirScanCacheFallback)
    worden pas bereikt als de cache expired of ontbreekt.
    """

    def test_cache_hit_within_ttl_skips_db_call(
        self, sandbox_registry, monkeypatch,
    ):
        """Twee scans binnen TTL = één DB-call. Zonder TTL-gate telt
        de counter door naar 2 (en doet elke bot-tick een extra query)."""
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
        # BotRegistry() doet al 1 scan via __init__ → _refresh_locked.
        # Nul de counter zodat dit pad niet meetelt in de assertion.
        initial = calls["n"]
        reg._scan_user_dirs()
        reg._scan_user_dirs()
        assert calls["n"] == initial, (
            f"expected cache-hits to skip DB; counter went {initial} → {calls['n']}"
        )

    def test_cache_refresh_after_ttl_expires(
        self, sandbox_registry, monkeypatch,
    ):
        """Na TTL-expiry is de volgende scan een cache-miss en raakt
        de DB weer. Gesimuleerd door de timestamp terug te zetten."""
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
        # Simuleer TTL-expiry door de timestamp terug in de tijd te zetten.
        webapp._cache_last_refresh_ts = 0.0
        reg._scan_user_dirs()  # cache-miss → DB call
        assert calls["n"] == initial + 1, (
            f"expected exactly one DB call after TTL expiry; counter "
            f"went {initial} → {calls['n']}"
        )

    def test_cache_reset_helper_also_clears_timestamp(
        self, sandbox_registry,
    ):
        """``_reset_user_dirs_cache()`` moet ook de TTL-timestamp nullen
        — anders zou een test-fixture die de cache clear'd de volgende
        scan nog steeds als een cache-hit zien en de DB niet raken."""
        import web.app as webapp
        _seed_user(2, "bob")

        reg = BotRegistry()  # primt de cache + timestamp
        assert webapp._cache_last_refresh_ts > 0
        assert webapp._cached_active_users == {1, 2}

        _reset_user_dirs_cache()
        assert webapp._cache_last_refresh_ts == 0.0
        assert webapp._cached_active_users is None

        # Een volgende scan moet dan gegarandeerd de DB raken (cache is
        # leeg, dus de TTL-check slaat over op `_cached_active_users is
        # None` en de try/except DB-path wordt doorlopen).
        reg._scan_user_dirs()
        assert webapp._cached_active_users == {1, 2}
