"""Source-grep regression guards on the marketing/ static assets.

These checks lock in the few invariants that have load-bearing
behavior beyond CSS aesthetics:

* The theme-toggle button exists on every visitor-facing page
  and carries an aria-label so screen readers can announce it.
* The flash-of-wrong-theme prevention script (now externalized
  to /js/theme-init.js for strict-CSP compliance) is referenced
  in <head> BEFORE the stylesheet, with no defer/async — moving
  it after, deferring it, or removing it reintroduces the
  dark-to-light flicker on initial paint.
* The light-theme override block exists in marketing.css so
  the toggle has somewhere to flip to.

We pin these because the marketing site has no test framework
of its own; these source-greps are the cheapest catch for an
accidental removal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MARKETING = _REPO_ROOT / "marketing"


# Pages where the toggle MUST be visible (top-nav-bearing
# visitor pages). maintenance.html is intentionally excluded —
# it's a 5xx fallback with minimal chrome (no nav), so a toggle
# would be out of place there.
_TOGGLE_PAGES = ("index.html", "roadmap.html", "changelog.html")

# Pages where the flash-prevention <script> must run BEFORE the
# stylesheet load. All four pages get it because all four
# render against the same CSS and would flash without it.
_THEME_PAGES = ("index.html", "roadmap.html", "changelog.html",
                "maintenance.html")


def _read(page: str) -> str:
    return (_MARKETING / page).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", _TOGGLE_PAGES)
def test_theme_toggle_button_present(page):
    html = _read(page)
    assert 'id="theme-toggle-btn"' in html, (
        f"{page} missing theme-toggle-btn — visitors lose the "
        "ability to switch between light and dark."
    )
    assert 'aria-label=' in html, (
        f"{page}: theme-toggle button must carry an aria-label "
        "so screen readers can announce it."
    )


@pytest.mark.parametrize("page", _THEME_PAGES)
def test_theme_init_script_runs_before_stylesheet(page):
    html = _read(page)
    # The external /js/theme-init.js sets
    # document.documentElement.dataset.theme synchronously from
    # localStorage; the stylesheet read of [data-theme="light"]
    # then matches without any flicker.
    script_pos = html.find('theme-init.js')
    css_pos = html.find('marketing.css')
    assert script_pos != -1, (
        f"{page} missing the theme-init.js script reference — "
        "light-theme visitors will see a flash of dark on "
        "initial paint."
    )
    assert css_pos != -1, f"{page} missing the marketing.css link"
    assert script_pos < css_pos, (
        f"{page}: theme-init.js <script> must appear BEFORE the "
        "stylesheet <link> so document.documentElement.dataset."
        "theme is set by the time CSS resolves [data-theme=...]."
    )


@pytest.mark.parametrize("page", _THEME_PAGES)
def test_theme_init_script_is_synchronous(page):
    # defer/async would let the stylesheet evaluate before
    # data-theme is set, reintroducing the flash. Pin against
    # both attributes appearing on the theme-init.js tag.
    html = _read(page)
    for line in html.splitlines():
        if 'theme-init.js' in line:
            assert 'defer' not in line, (
                f"{page}: theme-init.js must not have defer — "
                "it would defeat the flash-prevention purpose."
            )
            assert 'async' not in line, (
                f"{page}: theme-init.js must not have async — "
                "it would defeat the flash-prevention purpose."
            )
            break
    else:
        pytest.fail(
            f"{page}: theme-init.js script tag not found — "
            "covered separately by the runs-before-stylesheet "
            "test, but flagged here too for clarity."
        )


def test_theme_init_file_exists_with_iife():
    init_file = _MARKETING / "js" / "theme-init.js"
    assert init_file.exists(), (
        "marketing/js/theme-init.js missing — the four "
        "marketing HTMLs reference it from <head>."
    )
    content = init_file.read_text(encoding="utf-8")
    assert 'localStorage.getItem' in content, (
        "theme-init.js missing localStorage.getItem call — "
        "the IIFE wouldn't read the user's theme choice."
    )
    assert 'reverto-theme' in content, (
        "theme-init.js missing the 'reverto-theme' key — the "
        "render.js toggle writes to that key, so reading any "
        "other key would desync the two."
    )
    assert 'data-theme' in content or 'dataset.theme' in content, (
        "theme-init.js doesn't appear to set data-theme — "
        "without it, CSS [data-theme=\"light\"] never matches."
    )


def test_marketing_css_has_light_theme_block():
    css = (_MARKETING / "css" / "marketing.css").read_text(
        encoding="utf-8",
    )
    assert '[data-theme="light"]' in css, (
        "marketing.css missing the [data-theme=\"light\"] override "
        "block — the toggle button has nothing to flip the page "
        "to."
    )
