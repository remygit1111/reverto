"""Source-grep regression tests for hand-written frontend behaviour.

The portal ships vanilla-JS in ``web/static/app.js``; there's no JS
test harness, so behaviour we want to keep ratcheted is asserted by
checking the source file for the handler signature.

Currently covered:
- Number-input scroll-blocker: wheel events on a <input type="number">
  that is NOT the active element must call ``preventDefault()`` so the
  browser's wheel-changes-value default doesn't accidentally edit form
  values when the operator scrolls the bot config / wizard pages.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
_APP_JS = _STATIC / "app.js"
_INDEX_HTML = _STATIC / "index.html"
_STYLE_CSS = _STATIC / "style.css"


def test_number_input_wheel_event_blocked_when_unfocused():
    """The wheel handler in app.js must:

    1. listen for ``wheel`` events at the document level,
    2. match ``input[type="number"]`` targets,
    3. compare against ``document.activeElement`` so focused inputs
       keep their native wheel-step behaviour, and
    4. call ``preventDefault()`` — which means ``passive: false`` is
       required when registering the listener.

    A regression that drops any of these (e.g. switching to
    ``passive: true`` and removing ``preventDefault``) would silently
    re-introduce the accidental-value-change bug operators reported.
    """
    assert _APP_JS.is_file(), f"missing {_APP_JS}"
    src = _APP_JS.read_text(encoding="utf-8")

    assert "addEventListener('wheel'" in src, (
        "app.js must register a document-level 'wheel' listener for "
        "the number-input scroll-blocker"
    )
    assert 'input[type="number"]' in src, (
        "wheel handler must scope to <input type=\"number\"> targets"
    )
    assert "document.activeElement" in src, (
        "wheel handler must check document.activeElement so focused "
        "inputs keep their native wheel-step behaviour"
    )
    assert "preventDefault()" in src, (
        "wheel handler must call preventDefault() to block the "
        "browser's default wheel-changes-value behaviour"
    )
    assert "passive: false" in src, (
        "wheel handler must register with { passive: false } — "
        "preventDefault() is a no-op on passive listeners"
    )


def test_legacy_breadcrumb_and_page_title_removed():
    """Two earlier identity-strip experiments were rolled back in
    favour of the unified ``.detail-context-bar``:

    1. ``#page-breadcrumb`` (with the helper classes ``.hdr-sep`` /
       ``.hdr-slug``) — added no navigation value and floated between
       two nav strips.
    2. ``<h1 class="page-title" id="bot-page-title">`` — a naked 24px
       heading with no container or padding context.

    Class-of-issue regression: any future change that re-introduces
    these IDs/classes is caught here. The ``.cl-breadcrumb`` selector
    for admin sub-page back-links is a different element and
    intentionally stays.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    # Earlier breadcrumb experiment.
    assert 'id="page-breadcrumb"' not in html
    assert 'class="page-breadcrumb' not in html
    assert ".page-breadcrumb" not in css
    assert ".hdr-sep" not in css
    assert ".hdr-slug" not in css
    assert "page-breadcrumb" not in js
    assert "hdr-slug" not in js
    assert "hdr-sep" not in js

    # Earlier h1 page-title experiment.
    assert 'id="bot-page-title"' not in html
    assert 'class="page-title"' not in html
    assert ".page-title" not in css
    assert "bot-page-title" not in js


def test_legacy_detail_context_bar_removed():
    """The merged-strip experiment from PR 3 (``.detail-context-bar``
    wrapping bot identity AND the sub-nav into one row) was rolled
    back in PR 4 — name and sub-nav fought for visual weight in the
    same strip. The identity now lives in its own ``.bot-identity``
    block and the sub-nav is back to a standalone strip.

    Class-of-issue regression: any future change that re-introduces
    ``.detail-context-bar`` (or its child helpers) is caught here.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")

    assert 'class="detail-context-bar"' not in html
    assert 'class="detail-bot-info"' not in html
    assert 'class="detail-bot-name"' not in html
    assert 'class="detail-bot-meta"' not in html
    assert 'class="detail-context-divider"' not in html
    assert ".detail-context-bar" not in css
    assert ".detail-bot-info" not in css
    assert ".detail-bot-name" not in css
    assert ".detail-bot-meta" not in css
    assert ".detail-context-divider" not in css


def test_bot_identity_block_present():
    """The bot detail view must render a ``.bot-identity`` block with
    three populated children: a prominent ``#bot-name-display``
    heading, a ``#bot-identity-status`` pill, and a muted
    ``#bot-meta-display`` line. Asserts the structural pieces exist so
    a refactor that re-merges them with the sub-nav (PR 3 pattern) or
    splits them apart again (PR 1/2 patterns) fails fast.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    # Wrapper + the three sinks openBot()/fetchDetail() write to.
    assert 'class="bot-identity"' in html
    assert 'id="bot-name-display"' in html
    assert 'id="bot-identity-status"' in html
    assert 'id="bot-meta-display"' in html

    # CSS for the block and its children must exist.
    assert ".bot-identity" in css
    assert ".bot-identity-name" in css
    assert ".bot-identity-meta" in css

    # JS must populate all three — name from slug/bot_name, meta
    # composed from b.mode / b.pair / b.exchange, and the running
    # status pill toggling between RUNNING and STOPPED.
    assert "bot-name-display" in js
    assert "bot-meta-display" in js
    assert "bot-identity-status" in js


def test_running_status_pill_decoupled_from_detail_controls():
    """The running-status pill used to live inside ``.detail-controls``
    next to Start/Stop/Restart. PR 4 moved it into the bot-identity
    block so the status reads as part of the identity, not part of the
    action row. The CSS selector therefore must NOT scope the pill to
    ``.detail-controls`` — that descendant rule would silently strip
    the styling at the new location.

    The legacy ``#d-running-status`` ID is also gone (the pill now
    lives at ``#bot-identity-status``); the JS render target moved
    accordingly.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    assert 'id="d-running-status"' not in html
    assert "d-running-status" not in js
    assert ".detail-controls .running-status" not in css
    # The general .running-status rule must remain (de-scoped form).
    assert ".running-status" in css
