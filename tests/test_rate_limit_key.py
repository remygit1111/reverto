"""Regression guard for audit r1-004 — X-Forwarded-For rate-limit key.

``_rate_limit_key_func`` replaces slowapi's default ``get_remote_address``
and honours the leftmost entry in an X-Forwarded-For chain so every
client behind a reverse proxy lands in its own bucket instead of
sharing the proxy's IP.

The tests use a minimal ``SimpleNamespace`` as the Request stand-in —
the key function reads ``request.headers.get(...)`` and eventually
``get_remote_address(request)`` on the fallback path. Starlette's
``get_remote_address`` reads ``request.client.host`` so we mimic
both attributes.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import _rate_limit_key_func  # noqa: E402


def _fake_request(xff: str | None, client_host: str = "10.0.0.1"):
    headers = {}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return SimpleNamespace(
        headers=SimpleNamespace(get=headers.get),
        client=SimpleNamespace(host=client_host),
    )


def test_prefers_leftmost_xff_entry():
    # Typical Caddy chain: original client, first proxy, inner proxy.
    req = _fake_request("203.0.113.7, 10.0.0.1, 10.0.0.2")
    assert _rate_limit_key_func(req) == "203.0.113.7"


def test_strips_surrounding_whitespace():
    # A misconfigured proxy that preserves spaces around the comma
    # must not produce a key with leading/trailing whitespace.
    req = _fake_request("  1.2.3.4  , 5.6.7.8")
    assert _rate_limit_key_func(req) == "1.2.3.4"


def test_falls_back_to_remote_address_without_xff():
    # No X-Forwarded-For header → use the direct connection IP via
    # slowapi's get_remote_address (reads request.client.host).
    req = _fake_request(xff=None, client_host="198.51.100.42")
    assert _rate_limit_key_func(req) == "198.51.100.42"


def test_empty_xff_falls_back_to_remote_address():
    # Header present but blank — defensive: still fall back instead
    # of returning an empty-string bucket key (which would collapse
    # every such request into one shared bucket).
    req = _fake_request(xff="", client_host="198.51.100.99")
    assert _rate_limit_key_func(req) == "198.51.100.99"
