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
