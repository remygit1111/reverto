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


def test_detail_context_bar_present():
    """The bot detail page must render bot identity (name + meta) and
    the sub-nav inside a single ``.detail-context-bar`` strip.
    Asserts the structural pieces exist so a refactor that splits them
    apart again — re-introducing the floating-h1 problem — fails fast.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    # Wrapper + the two textual sinks openBot()/fetchDetail() write to.
    assert 'class="detail-context-bar"' in html
    assert 'id="bot-name-display"' in html
    assert 'id="bot-meta-display"' in html

    # CSS for the bar and its children must exist (so removing the
    # styling ratchets a CSS-rule-removal review, not a silent
    # un-styling regression).
    assert ".detail-context-bar" in css
    assert ".detail-bot-name" in css
    assert ".detail-bot-meta" in css

    # JS must populate both elements — name from slug/bot_name, meta
    # composed from b.mode / b.pair / b.exchange.
    assert "bot-name-display" in js
    assert "bot-meta-display" in js
