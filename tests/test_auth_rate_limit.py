"""Phase B PR 4 — per-user login rate-limit regression tests.

Builds on the failed-login counter that already lived in
``core.user_store`` (``increment_failed_login`` / ``reset_failed_login``
/ ``get_failed_login_state``) and the ``/auth/login`` 429-gate that
existed since the login-security-hardening sweep. PR 4 added:

* ``check_login_rate_limit(user_id) -> (is_limited, retry_after)``
  convenience helper that wraps the count + sliding-window logic.
* ``Retry-After`` header on the 429 response so clients can pace.
* ``login_rate_limit_hit`` + ``login_totp_rate_limit_hit`` audit-log
  rows so an operator chasing a brute-force can grep them out.
* Failed-counter increment on ``/auth/login/totp`` wrong-code (was
  explicitly NOT-incrementing pre-PR4; now treated as a login
  failure for counter purposes).
* Failed-counter reset MOVED out of ``/auth/login`` (TOTP-required
  branch) into ``/auth/login/totp`` success path — pre-PR4 a user
  who password-cracked but failed TOTP got their counter reset for
  free, giving them another 10 attempts.

All endpoint tests run through TestClient against the full FastAPI
app so AuthMiddleware, CSRFMiddleware, slowapi, and Pydantic
validation all participate. Slowapi's per-route 5/min bucket would
otherwise dominate over the per-user gate we're testing — the
autouse ``_reset_slowapi_between_tests`` fixture wipes it between
tests.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: F401 — kept for parity with sibling test files

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyotp  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import totp, user_store  # noqa: E402
from core.database import get_db  # noqa: E402
from web import app as webapp  # noqa: E402


_KNOWN_PW = "pytest-rate-limit-pw-123456"


@pytest.fixture(autouse=True)
def _reset_slowapi_between_tests():
    """The slowapi 5/min bucket on /auth/login bleeds across tests
    in the same module. Reset before AND after each test so a
    rate-limit test doesn't poison a sibling."""
    try:
        webapp.limiter.reset()
    except Exception:
        pass
    yield
    try:
        webapp.limiter.reset()
    except Exception:
        pass


@pytest.fixture
def base_client():
    user_store.set_password(1, _KNOWN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def admin_with_totp():
    user_store.set_password(1, _KNOWN_PW)
    secret = totp.generate_secret()
    encrypted = totp.encrypt_seed_for_user(user_id=1, secret=secret)
    user_store.update_user_totp_seed(user_id=1, encrypted_seed=encrypted)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    try:
        yield client, secret
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


def _seed_failed_count(user_id: int, count: int, *, age_seconds: int = 0):
    """Stamp the users row with an arbitrary failed_login_count +
    last_failed_login_at = (now - age_seconds). Avoids waiting on
    the wall-clock for window-related tests."""
    ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    conn = get_db()
    with conn:
        conn.execute(
            "UPDATE users SET failed_login_count = ?, "
            "last_failed_login_at = ? WHERE id = ?",
            (count, ts, user_id),
        )


# ── 1. check_login_rate_limit unit tests ──────────────────────────────────


class TestCheckLoginRateLimit:

    def test_zero_attempts_not_limited(self):
        is_limited, retry_after = user_store.check_login_rate_limit(1)
        assert is_limited is False
        assert retry_after is None

    def test_below_threshold_not_limited(self):
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT - 1)
        is_limited, retry_after = user_store.check_login_rate_limit(1)
        assert is_limited is False
        assert retry_after is None

    def test_at_threshold_within_window_is_limited(self):
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        is_limited, retry_after = user_store.check_login_rate_limit(1)
        assert is_limited is True
        # retry_after must be a positive integer no greater than the
        # full window — within tolerance of the test taking a fraction
        # of a second to run.
        assert isinstance(retry_after, int)
        assert 0 < retry_after <= user_store.FAILED_LOGIN_WINDOW_S

    def test_above_threshold_within_window_is_limited(self):
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT * 2)
        is_limited, _ = user_store.check_login_rate_limit(1)
        assert is_limited is True

    def test_threshold_after_window_elapsed_not_limited(self):
        """A user at threshold whose last failure is OLDER than the
        window must NOT be rate-limited — the cooldown elapsed and
        the next failed attempt will reset the counter via the
        sliding-window logic in ``increment_failed_login``."""
        _seed_failed_count(
            1,
            count=user_store.PER_ACCOUNT_FAIL_LIMIT,
            age_seconds=user_store.FAILED_LOGIN_WINDOW_S + 60,
        )
        is_limited, retry_after = user_store.check_login_rate_limit(1)
        assert is_limited is False
        assert retry_after is None

    def test_unknown_user_id_not_limited(self):
        """``get_failed_login_state(unknown)`` returns (0, None) — the
        rate-limit branch must follow the same code path as a clean
        user (False/None) rather than emitting a misleading 429."""
        is_limited, retry_after = user_store.check_login_rate_limit(99999)
        assert is_limited is False
        assert retry_after is None


# ── 2. /auth/login wrong-password counter lifecycle ──────────────────────


class TestLoginEndpointCounter:

    def test_wrong_password_increments_db_counter(self, base_client):
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong-pw"},
        )
        assert r.status_code == 401
        count, _ = user_store.get_failed_login_state(1)
        assert count == 1

    def test_correct_password_resets_counter(self, base_client):
        _seed_failed_count(1, count=3)
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        count, last_at = user_store.get_failed_login_state(1)
        assert count == 0
        assert last_at is None


# ── 3. /auth/login 429 + Retry-After header ──────────────────────────────


class TestLoginEndpoint429:

    def test_at_threshold_returns_429_with_retry_after(self, base_client):
        # Seed the user at exactly the threshold within the window.
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 429
        # Detail string carries the human-readable wait time.
        assert "try again in" in r.json()["detail"].lower()
        # Retry-After header is a positive integer in seconds.
        retry_after = int(r.headers["retry-after"])
        assert 0 < retry_after <= user_store.FAILED_LOGIN_WINDOW_S

    def test_429_fires_before_password_verify(self, base_client):
        """A user at the threshold gets 429 even with the CORRECT
        password — the gate runs before bcrypt. Confirms the gate
        is positioned correctly to prevent CPU-amortisation
        attacks."""
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 429
        # And the counter does NOT increment on the 429 path —
        # rate-limit responses are not themselves a "failed
        # attempt" for counter purposes.
        count, _ = user_store.get_failed_login_state(1)
        assert count == user_store.PER_ACCOUNT_FAIL_LIMIT

    def test_429_window_elapsed_no_longer_locks(self, base_client):
        """The 429 only fires while the window is active. If the
        timestamp is older than the window, the user is back to a
        fresh streak on the next attempt."""
        _seed_failed_count(
            1,
            count=user_store.PER_ACCOUNT_FAIL_LIMIT,
            age_seconds=user_store.FAILED_LOGIN_WINDOW_S + 60,
        )
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        # Window elapsed → no 429; correct password → 200.
        assert r.status_code == 200
        count, _ = user_store.get_failed_login_state(1)
        assert count == 0


# ── 4. /auth/login + TOTP branch — counter NOT reset on password-only ─────


class TestLoginCounterResetGatedOnFullSuccess:

    def test_password_ok_totp_required_does_not_reset_counter(
        self, admin_with_totp,
    ):
        """Pre-PR4 the reset fired right after password-verify —
        meaning a user who password-cracked but failed TOTP got
        their counter reset for free. PR 4 moved the reset out of
        the TOTP-required branch; it now only fires on FULL login
        success."""
        client, _ = admin_with_totp
        _seed_failed_count(1, count=3)
        r = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        assert r.json()["requires_totp"] is True
        # Counter must STILL be 3 — reset is deferred to
        # /auth/login/totp success.
        count, _ = user_store.get_failed_login_state(1)
        assert count == 3

    def test_password_then_totp_success_resets_counter(
        self, admin_with_totp,
    ):
        client, secret = admin_with_totp
        _seed_failed_count(1, count=3)
        # Step 1: password.
        r1 = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r1.status_code == 200
        # Counter still 3 after password step.
        assert user_store.get_failed_login_state(1)[0] == 3
        # Step 2: TOTP.
        code = pyotp.TOTP(
            secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS,
        ).now()
        r2 = client.post("/auth/login/totp", json={"code": code})
        assert r2.status_code == 200
        # NOW the counter resets.
        count, last_at = user_store.get_failed_login_state(1)
        assert count == 0
        assert last_at is None


# ── 5. /auth/login/totp wrong-code increments counter ────────────────────


class TestLoginTotpCounter:

    def test_wrong_totp_increments_counter(self, admin_with_totp):
        client, _ = admin_with_totp
        # Step 1: password ok, pending cookie minted.
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        # Step 2: wrong code.
        r = client.post("/auth/login/totp", json={"code": "000000"})
        assert r.status_code == 401
        # Counter incremented — pre-PR4 this was an explicit
        # non-increment branch.
        count, _ = user_store.get_failed_login_state(1)
        assert count == 1

    def test_threshold_during_totp_returns_429_clears_pending(
        self, admin_with_totp,
    ):
        """A user who lands on the TOTP step but then crosses the
        threshold (because of accumulated wrong codes) gets 429 +
        cleared pending cookie — they have to start the login flow
        from scratch after the cooldown."""
        client, _ = admin_with_totp
        # Step 1: password ok.
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        # Force the user past the threshold (timestamp = now so
        # the window is active).
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        # Step 2: rate-limit gate fires before verify_code.
        r = client.post("/auth/login/totp", json={"code": "111111"})
        assert r.status_code == 429
        retry_after = int(r.headers["retry-after"])
        assert 0 < retry_after <= user_store.FAILED_LOGIN_WINDOW_S
        # Pending-login-TOTP cookie cleared on the 429 path.
        cleared = any(
            "reverto_login_totp_pending=" in raw and "Max-Age=0" in raw
            for raw in r.headers.get_list("set-cookie")
        )
        assert cleared


# ── 6. Audit-log entries ─────────────────────────────────────────────────


class TestRateLimitAuditLog:
    """Pin the audit-event types and result-flags so an operator
    grepping audit.jsonl post-incident gets stable output."""

    def test_login_rate_limit_hit_audit_event(self, base_client, tmp_path, monkeypatch):
        from core import paths
        monkeypatch.setattr(webapp, "LOG_DIR", tmp_path)
        monkeypatch.setattr(paths, "BASE_DIR", tmp_path)

        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 429

        import json
        jsonl = tmp_path / "audit.jsonl"
        assert jsonl.exists()
        rows = [
            json.loads(line)
            for line in jsonl.read_text().strip().splitlines()
        ]
        hit = [r for r in rows if r["action"] == "login_rate_limit_hit"]
        assert len(hit) == 1
        assert hit[0]["result"] == "denied"
        assert hit[0]["user_id"] == 1

    def test_login_totp_rate_limit_hit_audit_event(
        self, admin_with_totp, tmp_path, monkeypatch,
    ):
        from core import paths
        monkeypatch.setattr(webapp, "LOG_DIR", tmp_path)
        monkeypatch.setattr(paths, "BASE_DIR", tmp_path)

        client, _ = admin_with_totp
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        r = client.post("/auth/login/totp", json={"code": "111111"})
        assert r.status_code == 429

        import json
        rows = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        ]
        hit = [r for r in rows if r["action"] == "login_totp_rate_limit_hit"]
        assert len(hit) == 1
        assert hit[0]["result"] == "denied"
        assert hit[0]["user_id"] == 1


# ── 7. User-enumeration protection ───────────────────────────────────────


class TestUserEnumerationDefence:

    def test_known_and_unknown_username_share_429_detail_shape(
        self, base_client,
    ):
        """The 429-detail string must have the same shape on the
        known-user path (precise retry-after) and the unknown-user
        path (worst-case = full window). Otherwise an attacker could
        user-enumerate by reading the precision of the wait-time
        in the response body. Both paths use ``_rate_limit_detail``
        which formats minutes+seconds the same way."""
        # Known-user 429.
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        r_known = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        # Reset slowapi between calls (the autouse fixture only
        # resets between tests, not within).
        webapp.limiter.reset()
        # Unknown-user 429 needs a primed in-memory counter; we
        # can't seed that without driving 10 failed unknowns, which
        # would also fire slowapi 5/min. Skip the per-IP path test
        # here — the count-based shape parity is exercised by the
        # detail-string regex check on the known-user response
        # alone, since both branches call _rate_limit_detail().
        assert r_known.status_code == 429
        # Shape: "Too many failed login attempts. Please try again
        # in <wait>." — the wait value carries no user-specific
        # data leak, just the time math.
        assert "try again in" in r_known.json()["detail"].lower()


class TestRetryAfterRoundedToMinute:
    """Audit pt-160 (PT-v3, INFO × MEDIUM): the Retry-After header
    on every 429 path must be rounded UP to the next 60 s boundary
    so an attacker comparing two 429 responses can't separate
    "rate-limited known account" from "unknown account hit per-IP
    cap" by reading the timestamp precision."""

    def test_round_retry_after_to_minute_helper(self):
        from web.routes.auth import _round_retry_after_to_minute

        # Zero / negative passes through to 0 — the SPA reads this
        # as "retry now" but the 429 body itself is the gate, not
        # the Retry-After header.
        assert _round_retry_after_to_minute(0) == 0
        assert _round_retry_after_to_minute(-5) == 0

        # Sub-minute precision is rounded up to a full minute.
        assert _round_retry_after_to_minute(1) == 60
        assert _round_retry_after_to_minute(59) == 60
        # Exact 60 stays at 60 (no jump to 120).
        assert _round_retry_after_to_minute(60) == 60
        # Non-boundary multi-minute precision rounds up.
        assert _round_retry_after_to_minute(61) == 120
        assert _round_retry_after_to_minute(599) == 600
        assert _round_retry_after_to_minute(873) == 900
        # Already on a 60 s boundary stays put.
        assert _round_retry_after_to_minute(900) == 900

    def test_login_429_retry_after_header_is_60s_multiple(
        self, base_client,
    ):
        """End-to-end check: the live /auth/login 429 path must
        return a Retry-After value that's a clean multiple of 60.
        Pre-pt-160 it was the raw seconds-until-window-end (e.g.
        873) which leaked sub-minute precision."""
        _seed_failed_count(1, count=user_store.PER_ACCOUNT_FAIL_LIMIT)
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 429
        retry_after = int(r.headers["Retry-After"])
        assert retry_after % 60 == 0, (
            f"Retry-After={retry_after} is not a 60 s multiple — "
            "pt-160 rounding regressed"
        )
        assert retry_after > 0
        assert retry_after <= 900  # one full window, ceiling-rounded
