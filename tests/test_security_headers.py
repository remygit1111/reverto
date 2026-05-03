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


def test_csp_inline_script_hash_matches_index_html():
    """The auth-checked safety-net inline script in index.html
    is whitelisted via a SHA-256 hash on ``script-src``. If the
    script content drifts away from the hash (e.g. someone tweaked
    whitespace, renamed a variable), the browser will silently
    refuse to execute it and the page can stay hidden when app.js
    fails to load. This test fails fast at CI time so the drift is
    caught before deploy.

    Two assertions:
      1. The hash in the live CSP header matches the hash baked
         into ``web/app.py``'s ``_INLINE_SCRIPT_CSP_HASH`` constant.
      2. That hash is the actual SHA-256(base64) of the inline
         script content currently shipping in
         ``web/static/index.html``.
    """
    import base64
    import hashlib
    import re
    from pathlib import Path

    from web import app as webapp

    expected_hash = webapp._INLINE_SCRIPT_CSP_HASH
    assert expected_hash.startswith("sha256-"), (
        f"_INLINE_SCRIPT_CSP_HASH must use the CSP ``sha256-…`` form; "
        f"got {expected_hash!r}"
    )

    # 1) Hash is in the live CSP header on script-src.
    client = TestClient(app)
    r = client.get("/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert f"'{expected_hash}'" in csp, (
        f"CSP script-src missing inline-script hash. CSP was: {csp!r}"
    )

    # 2) Hash matches the actual script bytes shipping in index.html.
    index_html = (
        Path(__file__).resolve().parent.parent
        / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    # Find the first inline <script> block — that's the auth-checked
    # safety-net (the only inline script in this file). Capture
    # exactly the bytes between the closing ``>`` of the open tag
    # and the opening ``<`` of the close tag — that's what browsers
    # hash for CSP.
    m = re.search(r"<script>(.*?)</script>", index_html, flags=re.DOTALL)
    assert m is not None, (
        "no inline <script> block found in index.html — CSP hash is "
        "now whitelisting nothing"
    )
    script_body = m.group(1)
    digest = hashlib.sha256(script_body.encode("utf-8")).digest()
    computed = "sha256-" + base64.b64encode(digest).decode("ascii")
    assert computed == expected_hash, (
        f"Inline-script hash drift!\n"
        f"  Expected (in web/app.py): {expected_hash}\n"
        f"  Computed (from index.html): {computed}\n"
        f"  → If you modified the inline script, regenerate the "
        f"hash in web/app.py (see _INLINE_SCRIPT_CSP_HASH comment "
        f"for the one-liner)."
    )


def test_csp_keeps_unsafe_inline_off_for_scripts():
    """Belt-and-suspenders: even with the inline-script hash added,
    ``'unsafe-inline'`` must NOT slip into ``script-src``. The whole
    point of the per-script hash is to keep the strict posture — a
    future PR that accidentally adds ``'unsafe-inline'`` would defeat
    the entire mechanism."""
    client = TestClient(app)
    r = client.get("/health")
    csp = r.headers.get("Content-Security-Policy", "")
    # Walk the directives so a global substring hit on
    # ``style-src 'unsafe-inline'`` (which is allowed; r1-076) does
    # not false-positive.
    directives = {
        part.strip().split(" ", 1)[0]: part.strip()
        for part in csp.split(";") if part.strip()
    }
    script_src = directives.get("script-src", "")
    assert "'unsafe-inline'" not in script_src, (
        f"script-src must not allow 'unsafe-inline' — defeats the "
        f"per-script hash whitelist. script-src was: {script_src!r}"
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


def test_permissions_policy_identical_across_caddy_and_fastapi():
    """Audit PT-v4-NW-007: ensures Permissions-Policy allowlists
    stay identical across Caddy vhosts and FastAPI middleware.
    If they drift, this test fails.

    Three sources of truth must remain byte-identical:
      1. ops/caddy/Caddyfile reverto.bot vhost ``header { ... }``
      2. ops/caddy/Caddyfile app.reverto.bot vhost ``header { ... }``
      3. FastAPI SecurityHeadersMiddleware response header

    The Caddyfile is parsed as text — we look for the
    ``Permissions-Policy "..."`` line inside each vhost block and
    extract the quoted value. A drift between any two values
    (whitespace, ordering, content) fails the test with a clear
    diff.
    """
    import re
    from pathlib import Path

    caddyfile = (
        Path(__file__).resolve().parent.parent
        / "ops" / "caddy" / "Caddyfile"
    ).read_text(encoding="utf-8")

    # Split the file into per-vhost blocks. A vhost starts with
    # ``<host> {`` at column 0 and closes with ``}`` at column 0.
    # Crude but adequate — the Caddyfile is small and hand-maintained.
    def _extract_vhost_block(host: str) -> str:
        pattern = re.compile(
            rf"^{re.escape(host)} \{{\n(.*?)^\}}$",
            re.DOTALL | re.MULTILINE,
        )
        m = pattern.search(caddyfile)
        assert m is not None, (
            f"vhost {host!r} not found in Caddyfile — parity test "
            f"cannot proceed"
        )
        return m.group(1)

    def _extract_permissions_policy(block: str, host: str) -> str:
        # Match the line:    Permissions-Policy "<value>"
        # Allow leading tabs/spaces; capture everything between the
        # outer quotes.
        m = re.search(
            r'^\s*Permissions-Policy\s+"([^"]*)"',
            block,
            re.MULTILINE,
        )
        assert m is not None, (
            f"Permissions-Policy directive missing from {host} "
            f"vhost block"
        )
        return m.group(1)

    marketing_pp = _extract_permissions_policy(
        _extract_vhost_block("reverto.bot"), "reverto.bot",
    )
    app_pp = _extract_permissions_policy(
        _extract_vhost_block("app.reverto.bot"), "app.reverto.bot",
    )

    client = TestClient(app)
    r = client.get("/health")
    fastapi_pp = r.headers.get("Permissions-Policy", "")
    assert fastapi_pp, "FastAPI Permissions-Policy header missing"

    # Three-way parity. Build a small diff message when they drift
    # so the failure is easy to triage without re-grepping the file.
    sources = {
        "Caddy reverto.bot": marketing_pp,
        "Caddy app.reverto.bot": app_pp,
        "FastAPI middleware": fastapi_pp,
    }
    distinct = set(sources.values())
    assert len(distinct) == 1, (
        "Permissions-Policy drift across the three sources:\n"
        + "\n".join(f"  {name}: {value!r}" for name, value in sources.items())
    )
