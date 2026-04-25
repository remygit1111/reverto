"""Regression guard for SecurityHeadersMiddleware — audit r1-075 HSTS.

The middleware attaches CSP + X-Frame-Options + HSTS headers. HSTS
is scheme-gated so an operator running over plain http://localhost
doesn't end up with a browser stuck in forced-HTTPS state. These
tests pin both the emit and the no-emit paths.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from web.app import app  # noqa: E402


def test_hsts_header_emitted_on_https():
    # TestClient with an https:// base_url puts scheme=https in the
    # ASGI scope so ``request.url.scheme == "https"`` resolves true
    # inside the middleware without needing a real TLS terminator.
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/health")
    assert "Strict-Transport-Security" in r.headers
    assert "max-age=31536000" in r.headers["Strict-Transport-Security"]
    assert "includeSubDomains" in r.headers["Strict-Transport-Security"]


def test_hsts_header_absent_on_http():
    # Default base_url is http://testserver — HSTS must stay off
    # so ``make start`` on localhost doesn't trap the operator in
    # a forced-HTTPS redirect loop.
    client = TestClient(app)
    r = client.get("/health")
    assert "Strict-Transport-Security" not in r.headers


# ── r1-076: CSP connect-src wildcards ──────────────────────────────────────


def test_csp_no_ws_wildcard():
    """Audit r1-076: ws:/wss: wildcards removed — 'self' covers
    same-origin WS endpoints and matches the request scheme."""
    client = TestClient(app)
    r = client.get("/health")
    csp = r.headers.get("Content-Security-Policy", "")
    # Find the connect-src directive specifically — 'ws:' as a
    # substring of the whole CSP would false-positive on other
    # directives if we ever added one with 'ws' in it.
    directives = {
        part.strip().split(" ", 1)[0]: part.strip()
        for part in csp.split(";") if part.strip()
    }
    connect_src = directives.get("connect-src", "")
    assert "ws:" not in connect_src, f"connect-src contains ws: {connect_src!r}"
    assert "wss:" not in connect_src, f"connect-src contains wss: {connect_src!r}"


def test_csp_still_allows_self_and_unpkg():
    """connect-src must retain 'self' (same-origin WS + fetch) and
    unpkg.com (lightweight-charts + gridstack sourcemaps)."""
    client = TestClient(app)
    r = client.get("/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "connect-src 'self'" in csp
    assert "https://unpkg.com" in csp


def test_server_header_stripped_by_middleware():
    """Audit r3-003 (defense-in-depth) — SecurityHeadersMiddleware
    deletes any ``Server`` header that reaches it. TestClient bypasses
    uvicorn's H11 serialisation, so this only validates the
    middleware-side belt-and-braces guard. The PRIMARY fix lives in
    ``uvicorn.Config(server_header=False)`` — see the next test.
    """
    client = TestClient(app)
    r = client.get("/health")
    # Case-insensitive header-name view (Starlette/uvicorn casing
    # varies across versions).
    header_names_lower = {k.lower() for k in r.headers.keys()}
    assert "server" not in header_names_lower, (
        f"Server header should be stripped by middleware; got: "
        f"{[k for k in r.headers.keys() if k.lower() == 'server']}"
    )


def test_uvicorn_config_suppresses_server_header():
    """Audit r3-003 (primary fix) — uvicorn's ``Server: uvicorn``
    response header is injected at the H11 protocol layer, AFTER any
    Starlette middleware. The only place to suppress it is the
    ``uvicorn.Config(...)`` call. This test introspects the source
    of ``run_portal`` to assert ``server_header=False`` is passed —
    a future regression that drops the kwarg gets caught at CI time
    rather than discovered via ``curl -I https://reverto.bot/`` post-
    deploy.
    """
    import inspect
    from web import app as webapp

    source = inspect.getsource(webapp.run_portal)
    assert "server_header=False" in source, (
        "uvicorn.Config(...) must include server_header=False to "
        "suppress the 'Server: uvicorn' fingerprint at the protocol "
        "layer. Middleware-side strip is defense-in-depth only and "
        "doesn't run before uvicorn's H11 serialisation."
    )


def test_permissions_policy_header_present():
    """Audit pd-011 — Permissions-Policy must deny every browser
    sensor / device API. A trading portal has no legit use for
    camera/microphone/geolocation/payment etc., so an XSS or
    compromised third-party script can't prompt the user either.
    """
    client = TestClient(app)
    r = client.get("/health")
    pp = r.headers.get("Permissions-Policy", "")
    assert pp, "Permissions-Policy header missing"
    for directive in (
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "payment=()",
        "usb=()",
    ):
        assert directive in pp, (
            f"Permissions-Policy missing {directive!r}: got {pp!r}"
        )
