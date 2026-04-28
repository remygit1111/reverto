"""Phase B PR 5 — cookie-posture regression tests.

Pin the per-cookie attribute posture for the four production
cookies Reverto sets:

  * ``reverto_session``            — session-cookie (after /auth/login
    no-TOTP success or /auth/login/totp success).
  * ``reverto_csrf``               — double-submit token (begeleidend
    aan reverto_session). Intentionally NOT HttpOnly so the SPA's JS
    can read it back into the X-CSRF-Token header.
  * ``reverto_totp_pending``       — pending-enrollment (after
    /auth/totp/setup, 10-minute TTL).
  * ``reverto_login_totp_pending`` — pending login-step-2 (after
    /auth/login when the user has TOTP enabled, 2-minute TTL).

A future PR that drops HttpOnly, weakens SameSite, or forgets
Secure on any of these cookies fails immediately here. Closes
v26-22 (cookie-posture, ACCEPTED — turned ACTIVE by this PR).

These tests force the production cookie-flag posture
(``_COOKIE_SECURE = True``, ``_COOKIE_SAMESITE = "strict"``)
explicitly — the standard ``auth_client`` fixture used elsewhere in
the suite flips both to test-friendly values so cookies actually
survive TestClient's plain-HTTP transport. We only inspect the
Set-Cookie header on the response, never need a follow-up
authenticated request, so the production values are safe to use.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import totp, user_store  # noqa: E402
from web import app as webapp  # noqa: E402


_KNOWN_PW = "pytest-cookie-posture-pw-987"


# ── Set-Cookie parser ─────────────────────────────────────────────────────


def parse_set_cookie_attrs(set_cookie_header: str) -> dict:
    """Parse a single Set-Cookie header into a dict of attributes.

    Boolean attributes (HttpOnly, Secure, Partitioned) appear as
    keys with value ``True`` if present and ARE NOT in the dict
    if absent. String attributes (Path, Domain, SameSite, Max-Age,
    Expires) appear with their lowercased value-string. The
    cookie's own ``name`` and ``value`` are folded into the same
    dict for convenience.

    Lowercased keys throughout — the Set-Cookie attribute grammar
    is case-insensitive (RFC 6265 §4.1.1).
    """
    parts = set_cookie_header.split(";")
    name_value = parts[0].strip()
    name, _, value = name_value.partition("=")

    attrs: dict[str, object] = {
        "name": name.strip(),
        "value": value.strip(),
    }
    for part in parts[1:]:
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            attrs[key.strip().lower()] = val.strip()
        else:
            attrs[part.strip().lower()] = True
    return attrs


def find_set_cookie(response, cookie_name: str):
    """Return the parsed-attributes dict for ``cookie_name`` from
    ``response.headers.get_list("set-cookie")``, or ``None`` if no
    Set-Cookie line carries that name. We use the raw header list
    rather than ``response.cookies`` because ``httpx.Cookies`` does
    not expose flags like HttpOnly / Secure (it stores the value-
    pair only)."""
    for raw in response.headers.get_list("set-cookie"):
        attrs = parse_set_cookie_attrs(raw)
        if attrs.get("name") == cookie_name:
            return attrs
    return None


# ── Expected posture (single source of truth) ─────────────────────────────


EXPECTED_COOKIE_POSTURE: dict[str, dict] = {
    "reverto_session": {
        "httponly": True,
        "secure": True,
        "samesite": "strict",
        "path": "/",
    },
    "reverto_csrf": {
        # Intentionally NOT httponly — the SPA must read this in
        # JS to mirror it into the X-CSRF-Token header (double-
        # submit pattern). HttpOnly here would break CSRF defence.
        "httponly": False,
        "secure": True,
        "samesite": "strict",
        "path": "/",
    },
    "reverto_totp_pending": {
        "httponly": True,
        "secure": True,
        "samesite": "strict",
        "path": "/",
    },
    "reverto_login_totp_pending": {
        "httponly": True,
        "secure": True,
        "samesite": "strict",
        "path": "/",
    },
}


def _assert_cookie_posture(response, cookie_name: str) -> None:
    cookie = find_set_cookie(response, cookie_name)
    assert cookie is not None, (
        f"Cookie {cookie_name!r} not found in response Set-Cookie "
        f"headers: {response.headers.get_list('set-cookie')!r}"
    )
    expected = EXPECTED_COOKIE_POSTURE[cookie_name]

    if expected["httponly"]:
        assert cookie.get("httponly") is True, (
            f"{cookie_name}: HttpOnly missing. A cookie that's "
            f"readable from JS leaks to any XSS gadget. Found "
            f"attrs: {sorted(cookie.keys())}."
        )
    else:
        assert "httponly" not in cookie, (
            f"{cookie_name}: HttpOnly is present but must NOT be "
            f"(the double-submit CSRF pattern requires the SPA's "
            f"JS to read this cookie)."
        )

    assert cookie.get("secure") is True, (
        f"{cookie_name}: Secure missing. The cookie can be sent "
        f"over plain HTTP, leaking the credential to any "
        f"network-path observer."
    )

    samesite = (cookie.get("samesite") or "").lower()
    assert samesite == expected["samesite"], (
        f"{cookie_name}: SameSite={samesite!r}, expected "
        f"{expected['samesite']!r}. Weakening SameSite re-opens "
        f"the CSRF surface that the strict policy closes."
    )

    assert cookie.get("path") == expected["path"], (
        f"{cookie_name}: Path={cookie.get('path')!r}, expected "
        f"{expected['path']!r}."
    )

    # Defence-in-depth: no Domain attribute. Reverto runs on a
    # single hostname; broadening the cookie to subdomains
    # (e.g. via Domain=reverto.bot) would let a future subdomain-
    # takeover steal the cookie.
    assert "domain" not in cookie, (
        f"{cookie_name}: Domain attribute set to {cookie.get('domain')!r}. "
        f"Reverto runs from a single hostname — Domain broadens "
        f"the cookie scope to subdomains and is a takeover risk."
    )


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_slowapi_between_tests():
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
def production_posture_client():
    """TestClient with the PRODUCTION cookie-flag posture forced.

    The standard ``auth_client`` fixture flips ``_COOKIE_SECURE`` to
    False + ``_COOKIE_SAMESITE`` to "lax" so cookies survive
    TestClient's plain-HTTP transport — but those are exactly the
    flags the cookie-posture tests need to verify. So this fixture
    forces the production values for the duration of the test.

    Tests using this fixture only inspect the Set-Cookie header on
    the FIRST response — they do not make follow-up requests that
    rely on the cookie being delivered, so the Secure/Strict
    posture is safe to use here.
    """
    user_store.set_password(1, _KNOWN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = True
    webapp._COOKIE_SAMESITE = "strict"
    client = TestClient(webapp.app)
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def admin_with_totp_production_posture():
    """Admin enrolled in TOTP + production cookie-flag posture."""
    user_store.set_password(1, _KNOWN_PW)
    secret = totp.generate_secret()
    encrypted = totp.encrypt_seed_for_user(user_id=1, secret=secret)
    user_store.update_user_totp_seed(user_id=1, encrypted_seed=encrypted)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = True
    webapp._COOKIE_SAMESITE = "strict"
    client = TestClient(webapp.app)
    try:
        yield client, secret
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


# ── 1. parse_set_cookie_attrs unit tests ─────────────────────────────────


class TestParseSetCookieAttrs:
    """Self-tests for the parser — keep it honest as the assertion
    suite below leans on it for every cookie check."""

    def test_basic_name_value(self):
        attrs = parse_set_cookie_attrs("foo=bar")
        assert attrs["name"] == "foo"
        assert attrs["value"] == "bar"

    def test_boolean_attribute_present(self):
        attrs = parse_set_cookie_attrs("foo=bar; HttpOnly; Secure")
        assert attrs["httponly"] is True
        assert attrs["secure"] is True

    def test_string_attribute_lowercased_key(self):
        attrs = parse_set_cookie_attrs("foo=bar; SameSite=Strict; Path=/")
        # Key is lowercased; value preserves case as-served.
        assert attrs["samesite"] == "Strict"
        assert attrs["path"] == "/"

    def test_full_production_shape(self):
        raw = (
            "reverto_session=abc.def; Path=/; Max-Age=86400; "
            "HttpOnly; Secure; SameSite=Strict"
        )
        attrs = parse_set_cookie_attrs(raw)
        assert attrs["name"] == "reverto_session"
        assert attrs["httponly"] is True
        assert attrs["secure"] is True
        assert attrs["samesite"] == "Strict"
        assert attrs["path"] == "/"
        assert attrs["max-age"] == "86400"
        assert "domain" not in attrs

    def test_find_set_cookie_returns_none_for_missing_name(self):
        class _FakeResponse:
            def __init__(self, cookies):
                self.headers = self
                self._cookies = cookies

            def get_list(self, name):
                assert name == "set-cookie"
                return self._cookies

        response = _FakeResponse([
            "first=1; Path=/",
            "second=2; HttpOnly",
        ])
        assert find_set_cookie(response, "third") is None
        assert find_set_cookie(response, "second")["httponly"] is True


# ── 2. reverto_session ────────────────────────────────────────────────────


class TestSessionCookiePosture:

    def test_session_cookie_attrs_after_login(
        self, production_posture_client,
    ):
        r = production_posture_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200, r.text
        _assert_cookie_posture(r, "reverto_session")


# ── 3. reverto_csrf (intentionally NOT HttpOnly) ──────────────────────────


class TestCsrfCookiePosture:

    def test_csrf_cookie_attrs_after_login(
        self, production_posture_client,
    ):
        r = production_posture_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200, r.text
        _assert_cookie_posture(r, "reverto_csrf")

    def test_csrf_cookie_intentionally_not_httponly(
        self, production_posture_client,
    ):
        """The CSRF cookie MUST be JS-readable so the SPA can echo
        it into X-CSRF-Token on every mutating fetch (double-submit
        pattern). HttpOnly here would silently break CSRF defence.
        Pinned as its own test so a "let's just add HttpOnly to all
        cookies" cleanup PR fails with the right diagnostic."""
        r = production_posture_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        cookie = find_set_cookie(r, "reverto_csrf")
        assert cookie is not None
        assert "httponly" not in cookie, (
            "reverto_csrf carries HttpOnly — the SPA can no longer "
            "read it from document.cookie, breaking the double-"
            "submit CSRF pattern that protects every mutating "
            "endpoint."
        )


# ── 4. reverto_totp_pending (set on /auth/totp/setup) ─────────────────────


class TestTotpEnrollmentCookiePosture:

    def test_totp_enrollment_cookie_attrs(
        self, production_posture_client,
    ):
        # Direct session-cookie set so we don't need a /auth/login
        # round-trip (whose cookie wouldn't survive Secure+Strict
        # over plain HTTP). The /auth/totp/setup endpoint resolves
        # the user via the session cookie we just minted.
        admin = user_store.get_user_by_username("admin")
        production_posture_client.cookies.set(
            "reverto_session",
            webapp._create_session_cookie(admin),
        )
        # CSRFMiddleware needs the matching pair on mutating routes.
        production_posture_client.cookies.set(
            "reverto_csrf", "pytest-csrf",
        )
        production_posture_client.headers["X-CSRF-Token"] = "pytest-csrf"

        r = production_posture_client.post("/auth/totp/setup")
        assert r.status_code == 200, r.text
        _assert_cookie_posture(r, "reverto_totp_pending")


# ── 5. reverto_login_totp_pending (set on /auth/login w/ TOTP) ───────────


class TestTotpLoginCookiePosture:

    def test_totp_login_pending_cookie_attrs(
        self, admin_with_totp_production_posture,
    ):
        client, _secret = admin_with_totp_production_posture
        r = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        assert r.json().get("requires_totp") is True
        _assert_cookie_posture(r, "reverto_login_totp_pending")

    def test_session_cookie_NOT_set_during_totp_pending(
        self, admin_with_totp_production_posture,
    ):
        """The 2FA gate is load-bearing: until /auth/login/totp
        verifies the code, NO session cookie is minted. A bug that
        accidentally sets reverto_session here would let an
        attacker who only has the password bypass the second
        factor entirely."""
        client, _secret = admin_with_totp_production_posture
        r = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        assert find_set_cookie(r, "reverto_session") is None, (
            "reverto_session set during TOTP-pending phase — this "
            "bypasses the 2FA gate. Password alone would grant a "
            "full session."
        )
        # And NO csrf either — that's the begeleidend cookie that
        # only mints alongside the real session.
        assert find_set_cookie(r, "reverto_csrf") is None


# ── 6. Centralised expectation set ────────────────────────────────────────


class TestExpectedPostureCompleteness:
    """Pin the size + key-set of EXPECTED_COOKIE_POSTURE so a future
    PR that adds a new production cookie has to come back and fill
    in the expected attributes here — silently shipping a fifth
    cookie without an entry would otherwise pass the suite without
    the new cookie ever being checked."""

    def test_expected_posture_covers_all_known_cookies(self):
        # If this list grows, EXPECTED_COOKIE_POSTURE must too.
        # The constants in web.app are the authoritative names.
        known_cookie_constants = {
            webapp._SESSION_COOKIE,
            webapp._CSRF_COOKIE,
            webapp._PENDING_TOTP_COOKIE,
            webapp._PENDING_LOGIN_TOTP_COOKIE,
        }
        assert set(EXPECTED_COOKIE_POSTURE.keys()) == known_cookie_constants
