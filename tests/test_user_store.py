"""Tests voor core.user_store — DB-backed user + auth helpers.

Pin de contract van verify_password, set_password, bump_session_epoch
en get_session_epoch. De DB is per-test ge-isoleerd via de autouse
``_isolate_reverto_db`` fixture in conftest.py, dus elke test start
met een verse admin-seed en een NULL password_hash.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt
import pytest

from core import user_store
from core.database import get_db


_KNOWN_PW = "pytest-known-password-123"


class TestUserStoreReads:
    """Read-side helpers re-exported from core.user."""

    def test_get_user_by_id_returns_seeded_admin(self):
        admin = user_store.get_user_by_id(1)
        assert admin is not None
        assert admin.username == "admin"
        assert admin.role == "admin"
        assert admin.active is True
        # Fresh seed — epoch starts at 0.
        assert admin.session_epoch == 0

    def test_get_user_by_id_returns_none_for_unknown(self):
        assert user_store.get_user_by_id(999) is None

    def test_get_user_by_username_returns_seeded_admin(self):
        admin = user_store.get_user_by_username("admin")
        assert admin is not None
        assert admin.id == 1

    def test_get_user_by_username_returns_none_for_unknown(self):
        assert user_store.get_user_by_username("does-not-exist") is None


class TestVerifyPassword:
    """verify_password fails closed on every failure mode — no
    information leak that would let an attacker enumerate usernames
    or distinguish between "wrong password" and "no such user".
    """

    def test_succeeds_with_correct_credentials(self):
        user_store.set_password(1, _KNOWN_PW)
        user = user_store.verify_password("admin", _KNOWN_PW)
        assert user is not None
        assert user.id == 1

    def test_fails_with_wrong_password(self):
        user_store.set_password(1, _KNOWN_PW)
        assert user_store.verify_password("admin", "wrong") is None

    def test_fails_with_null_hash(self):
        """Fresh seed — admin has no password yet. Login must fail
        closed, not fall back to "no password = accept anything"."""
        assert user_store.verify_password("admin", "anything") is None

    def test_fails_with_nonexistent_user(self):
        assert user_store.verify_password("no-such-user", "pw") is None

    def test_fails_with_inactive_user(self):
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (username, active, role) "
                "VALUES ('frozen', 0, 'user')",
            )
        user_store.set_password(
            user_store.get_user_by_username("frozen").id, _KNOWN_PW,
        )
        # Even with the correct password, inactive users cannot log in.
        assert user_store.verify_password("frozen", _KNOWN_PW) is None

    def test_fails_gracefully_on_malformed_hash(self):
        """A row with a malformed password_hash (e.g. someone typed
        a plain string into the DB) must not crash — bcrypt.checkpw
        raises ValueError, and verify_password translates that to a
        generic None."""
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = 'not-a-bcrypt-hash' "
                "WHERE id = 1",
            )
        assert user_store.verify_password("admin", "anything") is None

    def test_verify_password_unknown_user_runs_bcrypt(self):
        """Audit pt-101: an unknown-username verify must still run a
        bcrypt round-trip (~50ms+ on rounds=12) so an attacker timing
        the response can't tell "no such user" from "wrong password".
        We assert a generous lower bound — exact wall time is host-
        dependent — but anything below 50ms means the dummy-bcrypt
        path was skipped and the fix has regressed."""
        import time as _time

        # Warm up bcrypt — first call on a fresh interpreter pays
        # one-time import + hash compile costs that would otherwise
        # show up in this measurement.
        user_store.verify_password("warmup-noop-user", "warmup")

        start = _time.perf_counter()
        result = user_store.verify_password("definitely-no-such-user", "pw")
        elapsed_ms = (_time.perf_counter() - start) * 1000
        assert result is None
        # rounds=12 bcrypt is ~150-300ms on commodity hardware.
        # 50ms guards against accidental short-circuit ("if user is
        # None: return None") regressions while staying well clear
        # of CI flakiness on slow runners.
        assert elapsed_ms >= 50, (
            f"unknown-user verify returned in {elapsed_ms:.1f}ms — "
            "dummy-bcrypt path appears to have been skipped"
        )

    def test_verify_password_timing_parity(self):
        """Audit pt-101: the unknown-user path and the known-user-
        wrong-password path should take comparable time so a remote
        attacker comparing two response timings can't separate them.
        We assert ratio < 2.0 (i.e. one path no more than 2x the
        other) — exact equality is unattainable with a real-world
        clock + GC + Python overhead, but a 100ms-vs-300ms gap
        (the pre-fix shape) would blow well past this threshold."""
        import time as _time

        user_store.set_password(1, _KNOWN_PW)

        # Warm up — first bcrypt call after import pays one-time costs.
        user_store.verify_password("admin", "warmup-wrong-pw")
        user_store.verify_password("warmup-unknown-user", "pw")

        # Sample multiple times — single-shot timings on a noisy host
        # can have 2x jitter even with no code change. Three samples,
        # take the median, compare medians.
        def _median_ms(username: str, pw: str) -> float:
            samples = []
            for _ in range(3):
                t0 = _time.perf_counter()
                user_store.verify_password(username, pw)
                samples.append((_time.perf_counter() - t0) * 1000)
            samples.sort()
            return samples[1]  # median of 3

        known_wrong_ms = _median_ms("admin", "wrong-password-xxx")
        unknown_ms = _median_ms("totally-unknown-user", "pw")

        ratio = max(known_wrong_ms, unknown_ms) / min(
            known_wrong_ms, unknown_ms,
        )
        assert ratio < 2.0, (
            f"timing parity broken: known-wrong={known_wrong_ms:.1f}ms, "
            f"unknown={unknown_ms:.1f}ms, ratio={ratio:.2f}"
        )


class TestSetPassword:
    def test_updates_hash(self):
        assert user_store.set_password(1, _KNOWN_PW) is True
        conn = get_db()
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = 1",
        ).fetchone()
        # Stored hash must be a valid bcrypt verification target.
        assert bcrypt.checkpw(
            _KNOWN_PW.encode("utf-8"), row["password_hash"].encode("utf-8"),
        )

    def test_returns_false_on_unknown_user(self):
        assert user_store.set_password(999, "whatever") is False

    def test_overwrites_previous_hash(self):
        user_store.set_password(1, "first-password-123")
        user_store.set_password(1, "second-password-456")
        assert user_store.verify_password("admin", "first-password-123") is None
        assert user_store.verify_password("admin", "second-password-456") is not None


class TestSessionEpoch:
    def test_bump_increments(self):
        assert user_store.get_session_epoch(1) == 0
        assert user_store.bump_session_epoch(1) == 1
        assert user_store.bump_session_epoch(1) == 2
        assert user_store.get_session_epoch(1) == 2

    def test_bump_is_per_user(self):
        """Bumping user 1's epoch must not affect any other user.
        Pre-Phase-3a the epoch was a single integer in .auth.json
        — logging out one user invalidated everyone's cookies. The
        DB-per-row design fixes that."""
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (username, role) VALUES ('bob', 'user')",
            )
        bob = user_store.get_user_by_username("bob")
        assert user_store.get_session_epoch(bob.id) == 0

        user_store.bump_session_epoch(1)  # admin's epoch
        assert user_store.get_session_epoch(1) == 1
        assert user_store.get_session_epoch(bob.id) == 0

    def test_get_returns_zero_for_unknown_user(self):
        assert user_store.get_session_epoch(999) == 0

    def test_bump_returns_value_atomically(self):
        """Audit v26-11: bump_session_epoch gebruikt sinds v26-11
        een ``UPDATE ... RETURNING session_epoch`` statement, zodat
        de post-update waarde in één SQL-call terugkomt. Pre-fix
        was UPDATE gevolgd door aparte SELECT, met een race-window
        waarin twee threads dezelfde SELECT-waarde zouden lezen.

        Deze test dekt de functionele kant (retour = post-UPDATE
        waarde); de concurrency-garantie zit in het RETURNING
        statement zelf.
        """
        assert user_store.get_session_epoch(1) == 0
        # Elke bump moet zijn eigen unieke waarde retourneren.
        v1 = user_store.bump_session_epoch(1)
        v2 = user_store.bump_session_epoch(1)
        v3 = user_store.bump_session_epoch(1)
        assert v1 == 1
        assert v2 == 2
        assert v3 == 3
        # En de DB matcht met de laatste return.
        assert user_store.get_session_epoch(1) == 3

    def test_bump_for_unknown_user_is_noop(self):
        """No row matches — UPDATE does nothing, return value is 0.
        Callers that use this for cleanup shouldn't crash."""
        assert user_store.bump_session_epoch(999) == 0


# ── Failed-login tracking (v6 schema) ───────────────────────────────────

class TestFailedLoginTracking:
    """increment_failed_login / reset_failed_login / get_failed_login_state
    back the per-account rate-limit + exponential backoff + anomaly
    logging in /auth/login. Tests pin the sliding-window + atomic-
    RETURNING behaviour.
    """

    def test_increment_failed_login_increments(self):
        """Fresh user → first failure produces count=1."""
        assert user_store.increment_failed_login(1) == 1

    def test_increment_failed_login_persists(self):
        """Two increments in the same window → counter reaches 2,
        both at the DB layer and via the public getter."""
        user_store.increment_failed_login(1)
        user_store.increment_failed_login(1)
        count, last_at = user_store.get_failed_login_state(1)
        assert count == 2
        assert last_at is not None

    def test_increment_records_timestamp(self):
        """``last_failed_login_at`` is populated on every increment
        so the sliding window has a reference point."""
        before = datetime.now(UTC)
        user_store.increment_failed_login(1)
        after = datetime.now(UTC)

        _, last_at = user_store.get_failed_login_state(1)
        assert last_at is not None
        # Window [before, after] is the valid wall-time band.
        assert before.timestamp() - 1 <= last_at.timestamp() <= after.timestamp() + 1

    def test_reset_failed_login_clears(self):
        """Increment a few times, reset, confirm counter is 0 and
        timestamp is NULL. Matches the "successful login clears the
        counter" contract the /auth/login handler relies on."""
        for _ in range(5):
            user_store.increment_failed_login(1)
        assert user_store.reset_failed_login(1) is True
        count, last_at = user_store.get_failed_login_state(1)
        assert count == 0
        assert last_at is None

    def test_reset_failed_login_unknown_user(self):
        """UPDATE affects 0 rows → function returns False."""
        assert user_store.reset_failed_login(999) is False

    def test_get_failed_login_state_fresh_user(self):
        """Seeded admin with no failures → (0, None)."""
        count, last_at = user_store.get_failed_login_state(1)
        assert count == 0
        assert last_at is None

    def test_get_failed_login_state_unknown_user(self):
        """Unknown user_id — don't crash, return (0, None) like a
        fresh user. Matches ``get_session_epoch``'s defensive
        fallback."""
        count, last_at = user_store.get_failed_login_state(999)
        assert count == 0
        assert last_at is None

    def test_increment_unknown_user_returns_zero(self):
        """No row → nothing to UPDATE → return 0. Matches
        ``bump_session_epoch``'s contract."""
        assert user_store.increment_failed_login(999) == 0

    def test_sliding_window_resets_stale_counter(self):
        """A prior failure older than ``FAILED_LOGIN_WINDOW_S`` must
        not count — the next increment starts fresh at 1.
        Simulates "yesterday's typo shouldn't gate today's login"."""
        # Seed an old failure directly via SQL so the test doesn't
        # depend on wall-clock advancing.
        stale_ts = (datetime.now(UTC) - timedelta(
            seconds=user_store.FAILED_LOGIN_WINDOW_S + 60,
        )).isoformat()
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE users SET failed_login_count = 7, "
                "last_failed_login_at = ? WHERE id = 1",
                (stale_ts,),
            )
        # Next increment sees the stale timestamp → fresh streak.
        assert user_store.increment_failed_login(1) == 1

    def test_sliding_window_continues_recent_streak(self):
        """Failures within the window accumulate normally."""
        recent_ts = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE users SET failed_login_count = 4, "
                "last_failed_login_at = ? WHERE id = 1",
                (recent_ts,),
            )
        assert user_store.increment_failed_login(1) == 5
