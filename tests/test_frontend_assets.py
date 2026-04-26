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

_APP_JS = Path(__file__).resolve().parent.parent / "web" / "static" / "app.js"


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
