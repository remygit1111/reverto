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


def test_csp_connect_src_still_allows_self():
    """connect-src must retain 'self' (same-origin WS + fetch).

    Pre-PT-v4-NW-009 this also asserted ``https://unpkg.com``
    in CSP (sourcemap fetches for the CDN-hosted bundles). With
    Lightweight Charts + GridStack now vendored under
    /static/vendor/, no external origin is needed any more; the
    inverted "unpkg.com NOT in csp" assertion lives in
    ``TestPTv4NW009UnpkgRemoved`` below."""
    client = TestClient(app)
    r = client.get("/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "connect-src 'self'" in csp


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


# ── PT-v4-NW-009: drop unpkg.com from CSP, vendor libs under 'self' ────────


class TestPTv4NW009UnpkgRemoved:
    """Class-of-issue regression for PT-v4-NW-009 (INFO).

    Pre-fix the SPA loaded Lightweight Charts and GridStack from
    https://unpkg.com and the CSP allow-listed that CDN in three
    directives (script-src, style-src, connect-src). Even with
    SRI hashes on every <script>/<link> tag, the "trust forever"
    gap remained: a future bump that forgot to regenerate the SRI
    hashes, or a CSP-only callsite that bypassed SRI, would have
    consumed unpkg-served bytes blindly.

    Post-fix both libraries are vendored under
    ``web/static/vendor/`` and served from 'self'. The CSP no
    longer mentions unpkg.com anywhere. The SRI + crossorigin
    attributes were dropped from the tags because they're
    obsolete for same-origin assets.

    Tests below pin:
      1. Every CSP directive — no "unpkg" substring anywhere.
      2. The strict script-src / style-src / connect-src shapes
         survive (defence-in-depth against a partial revert that
         drops the *whole* directive instead of just the
         unpkg.com token).
      3. The vendored libraries are actually present on disk
         (so a future PR that deletes the vendor dir without
         updating index.html gets caught here, not in browser
         console errors at runtime).
      4. index.html no longer LOADS anything from
         https://unpkg.com (a comment explaining the removal is
         allowed to survive).
    """

    def test_csp_contains_no_unpkg_reference(self):
        client = TestClient(app)
        r = client.get("/health")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "unpkg" not in csp, (
            f"PT-v4-NW-009 regression: CSP still allow-lists "
            f"unpkg.com somewhere. CSP was: {csp!r}"
        )

    def test_csp_script_src_strict_self_plus_hash_only(self):
        """script-src must be exactly ``'self' '<INLINE_HASH>'``
        — no external origins at all. The inline-script hash
        whitelist remains (it covers the auth-checked safety-net
        in index.html); nothing else needs to slip through."""
        from web import app as webapp

        client = TestClient(app)
        r = client.get("/health")
        csp = r.headers.get("Content-Security-Policy", "")
        directives = {
            part.strip().split(" ", 1)[0]: part.strip()
            for part in csp.split(";") if part.strip()
        }
        script_src = directives.get("script-src", "")
        # Tokens after the directive name.
        tokens = set(script_src.split()[1:])
        assert tokens == {
            "'self'", f"'{webapp._INLINE_SCRIPT_CSP_HASH}'",
        }, (
            f"PT-v4-NW-009: script-src must be 'self' + inline "
            f"hash only — no external CDN. Got: {script_src!r}"
        )

    def test_csp_style_src_strict_self_plus_unsafe_inline_only(self):
        """style-src loses its unpkg.com entry. 'unsafe-inline'
        stays per the r1-076 carve-out (inline styles across
        chart tooltips, dynamic panel layouts, theme switching)."""
        client = TestClient(app)
        r = client.get("/health")
        csp = r.headers.get("Content-Security-Policy", "")
        directives = {
            part.strip().split(" ", 1)[0]: part.strip()
            for part in csp.split(";") if part.strip()
        }
        style_src = directives.get("style-src", "")
        tokens = set(style_src.split()[1:])
        assert tokens == {"'self'", "'unsafe-inline'"}, (
            f"PT-v4-NW-009: style-src must be 'self' + "
            f"'unsafe-inline' only. Got: {style_src!r}"
        )

    def test_csp_connect_src_strict_self_only(self):
        """connect-src loses its unpkg.com entry (sourcemap
        fetches no longer cross origins). 'self' is sufficient
        for same-origin XHR + WS endpoints."""
        client = TestClient(app)
        r = client.get("/health")
        csp = r.headers.get("Content-Security-Policy", "")
        directives = {
            part.strip().split(" ", 1)[0]: part.strip()
            for part in csp.split(";") if part.strip()
        }
        connect_src = directives.get("connect-src", "")
        tokens = set(connect_src.split()[1:])
        assert tokens == {"'self'"}, (
            f"PT-v4-NW-009: connect-src must be 'self' only. "
            f"Got: {connect_src!r}"
        )

    def test_vendored_libraries_present_on_disk(self):
        """The three vendored asset files must exist where
        index.html points and have non-trivial size. A future PR
        that deleted the vendor dir while leaving the references
        in place would produce 404 spam in the browser console
        at runtime — this test catches that at CI time."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        for relpath, lo, hi in (
            (
                "web/static/vendor/lightweight-charts/"
                "lightweight-charts.standalone.production.js",
                100_000, 1_000_000,
            ),
            (
                "web/static/vendor/gridstack/gridstack-all.js",
                40_000, 500_000,
            ),
            (
                "web/static/vendor/gridstack/gridstack.min.css",
                1_000, 50_000,
            ),
        ):
            p = repo_root / relpath
            assert p.exists(), f"vendored asset missing: {relpath}"
            size = p.stat().st_size
            assert lo < size < hi, (
                f"unexpected size for {relpath}: {size} bytes "
                f"(expected {lo} < size < {hi}). The bundle may "
                "have been replaced with a placeholder."
            )

    def test_index_html_uses_vendored_paths(self):
        """The script + stylesheet refs in index.html must point
        at ``/static/vendor/...``, not at unpkg. A future bump
        that mistakenly re-introduced an unpkg href would break
        immediately because the CSP no longer allows it — but
        the failure mode at runtime would be a blank chart with
        a console CSP-violation, not an actionable test failure.
        Pin the source-of-truth here instead."""
        from pathlib import Path

        index_html = (
            Path(__file__).resolve().parent.parent
            / "web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        # Match the operational URL form (``https://unpkg.com``),
        # not the bare word — the file legitimately carries a
        # comment explaining *why* the unpkg dependency was
        # removed, and that breadcrumb should survive.
        assert "https://unpkg.com" not in index_html, (
            "PT-v4-NW-009 regression: index.html still loads an "
            "asset from https://unpkg.com. Move it to "
            "/static/vendor/ and update the tag."
        )
        # Positive assertion: the vendored paths landed.
        assert (
            "/static/vendor/lightweight-charts/"
            "lightweight-charts.standalone.production.js"
        ) in index_html
        assert "/static/vendor/gridstack/gridstack-all.js" in index_html
        assert "/static/vendor/gridstack/gridstack.min.css" in index_html

    def test_vendored_assets_served_through_static_mount(self):
        """End-to-end check that the static mount actually serves
        the vendored files at the URLs index.html references —
        catches a future "moved web/static" or "renamed mount
        prefix" PR that would 404 every browser hit."""
        client = TestClient(app)
        for url in (
            "/static/vendor/lightweight-charts/"
            "lightweight-charts.standalone.production.js",
            "/static/vendor/gridstack/gridstack-all.js",
            "/static/vendor/gridstack/gridstack.min.css",
        ):
            r = client.get(url)
            assert r.status_code == 200, (
                f"{url} returned {r.status_code} — vendored asset "
                "not reachable through the static mount."
            )
            assert len(r.content) > 1_000, (
                f"{url} returned only {len(r.content)} bytes — "
                "static mount is serving an empty / stub file."
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

