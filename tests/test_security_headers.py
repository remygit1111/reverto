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
