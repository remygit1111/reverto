"""Regression guard for audit r1-034 — request-id tracing.

``RequestIdMiddleware`` mints (or accepts) a 12-char request id
for every HTTP request, exposes it via the ``_request_id_ctx``
contextvar during the request lifecycle, and writes it back on
the response as ``X-Request-Id``.
"""

from __future__ import annotations

import os
import re
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from web.app import app, current_request_id  # noqa: E402


def test_request_id_header_set_in_response():
    client = TestClient(app)
    r = client.get("/health")
    req_id = r.headers.get("X-Request-Id")
    assert req_id is not None, "missing X-Request-Id on response"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", req_id), (
        f"request id {req_id!r} failed the safe-char shape check"
    )


def test_request_id_echoes_safe_inbound_header():
    client = TestClient(app)
    r = client.get("/health", headers={"X-Request-Id": "abc123DEF-45"})
    assert r.headers.get("X-Request-Id") == "abc123DEF-45"


def test_request_id_rejects_unsafe_inbound_header():
    # Control chars / newlines / long strings → middleware must
    # ignore and mint a fresh id so an attacker can't inject
    # \\r\\n into log lines via the header.
    client = TestClient(app)
    r = client.get("/health", headers={"X-Request-Id": "bad\nhdr"})
    new_id = r.headers.get("X-Request-Id")
    assert new_id != "bad\nhdr"
    assert new_id is not None
    assert "\n" not in new_id


def test_current_request_id_default_outside_request():
    # Direct call outside an HTTP request returns the sentinel
    # so background tasks and module-import code can safely log
    # without special-casing.
    assert current_request_id() == "-"
