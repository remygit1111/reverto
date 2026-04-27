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


def test_bot_control_buttons_have_state_aware_logic():
    """Action buttons (Start/Stop/Restart) on the bot-detail page must
    render enabled/disabled based on the derived bot lifecycle state,
    with hover-tooltips on disabled buttons explaining the no-op.

    Asserted shape:
    - Helper function ``updateBotControlButtons`` exists.
    - The helper handles at least the 'running' / 'stopped' / 'error'
      / 'unknown' states (extensible — soft-stop PR will add a
      'stopped_with_deals' entry, structurally easy).
    - Disabled-state hint copy is present so a refactor that drops
      the explanation regresses here.
    - The CSS ``.hbtn:disabled`` rule keeps ``cursor: not-allowed``
      and ``opacity`` so disabled buttons read as non-interactive,
      and does NOT use ``pointer-events: none`` (which would suppress
      the native title-tooltip on hover).
    """
    js = _APP_JS.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")

    # Helper exists and is wired up.
    assert "updateBotControlButtons" in js
    # Each documented state has a branch.
    assert "running" in js
    assert "stopped" in js
    assert "error" in js
    assert "unknown" in js
    # Tooltip hints (verbatim copy) — guard against silent removals.
    assert "Bot is already running" in js
    assert "Bot is already stopped" in js
    assert "Cannot restart a stopped bot" in js
    assert "Bot is in error state" in js

    # CSS: disabled state must mute the button without disabling
    # pointer events (otherwise the title-tooltip never fires).
    assert ".hbtn:disabled" in css
    assert "cursor: not-allowed" in css
    # The bt-pagination rule uses pointer-events: none; the action-
    # button rule must not — assert the disabled hbtn rule does NOT
    # carry that property within its block.
    hbtn_disabled_block_start = css.index(".hbtn:disabled")
    hbtn_disabled_block_end = css.index("}", hbtn_disabled_block_start)
    hbtn_disabled_block = css[hbtn_disabled_block_start:hbtn_disabled_block_end]
    assert "pointer-events: none" not in hbtn_disabled_block, (
        "disabled hbtn must keep pointer-events on so the native "
        "title-tooltip fires on hover"
    )


def test_wizard_save_error_triggers_scrollintoview():
    """``nbShowError`` (the central save-error renderer for the bot
    wizard) must scroll the banner into view after revealing it.

    Reason: the banner sits at the top of a long form. Operators who
    Save from the bottom otherwise see no feedback that validation
    failed, because the banner is off-screen above. ``scrollIntoView``
    with ``behavior: 'smooth'`` handles the in-view case as a no-op,
    so it is safe on every save attempt.
    """
    js = _APP_JS.read_text(encoding="utf-8")

    # Carve out nbShowError's body and assert the scroll lives there.
    fn_start = js.index("function nbShowError(")
    fn_end = js.index("\nfunction ", fn_start + 1)
    fn_body = js[fn_start:fn_end]

    assert "scrollIntoView" in fn_body, (
        "nbShowError must call scrollIntoView after un-hiding the "
        "error banner — operators at the bottom of the form would "
        "otherwise miss the validation feedback"
    )
    assert "smooth" in fn_body, (
        "the scroll should be smooth — abrupt jumps look like a bug"
    )


def test_wizard_review_placeholder_no_pydantic_traceback():
    """The wizard's Review-step live preview falls back to
    ``_nbRenderValidationError`` while the form is still incomplete.
    The previous implementation interpolated the raw Pydantic error
    string into the UI, leaking text like "validation error for
    BotConfig … input_value='' … https://errors.pydantic.dev/…".

    The placeholder must now be a friendly, generic message and the
    raw cause must be funnelled through console.warn for debugging.
    """
    js = _APP_JS.read_text(encoding="utf-8")

    # Carve out _nbRenderValidationError's body.
    fn_start = js.index("function _nbRenderValidationError(")
    fn_end = js.index("\nfunction ", fn_start + 1)
    fn_body = js[fn_start:fn_end]

    # Old leaky pattern is gone.
    assert "Config analysis unavailable" not in fn_body, (
        "old pattern leaked the raw Pydantic message — must be "
        "replaced with a generic friendly placeholder"
    )
    assert "${safeText(msg)}" not in fn_body, (
        "msg must not be interpolated into the placeholder; it "
        "belongs in console.warn for developer debugging only"
    )

    # New friendly placeholder + console-side debug funnel are in place.
    assert "Fill in required fields" in fn_body
    assert "console.warn" in fn_body


def test_dashboard_fetches_log_errors_not_silently_swallow():
    """``fetchOverview`` and ``fetchDetail`` are the dashboard's hot-
    path readers. RHA-v1 rha-004 found both wrapped in
    ``catch (e) {}`` so any network/parse failure left the UI showing
    stale data with no DevTools breadcrumb. Both must now route
    failures through ``console.warn`` so the staleness-badge in the
    header has a paper trail an operator can correlate with.

    Other ``catch (e) {}`` blocks elsewhere in app.js (logout
    cleanup, WS teardown, localStorage probes) are intentional and
    out of scope for this assertion — we scope the check to the two
    named functions only.
    """
    js = _APP_JS.read_text(encoding="utf-8")

    overview_start = js.index("async function fetchOverview(")
    # Function body ends at the first ``\n}\n`` after the open. Using
    # ``\nfunction `` to bound the slice would over-shoot into the
    # next renderOverview() body and pick up unrelated catch blocks.
    overview_end = js.index("\n}\n", overview_start)
    overview_body = js[overview_start:overview_end]
    assert "console.warn" in overview_body, (
        "fetchOverview must console.warn on failure paths so the "
        "staleness badge in the header has a corresponding DevTools "
        "log line"
    )
    assert "catch (e) {}" not in overview_body, (
        "fetchOverview must not silently swallow errors (RHA-v1 rha-004)"
    )

    detail_start = js.index("async function fetchDetail(")
    detail_end = js.index("\n}\n", detail_start)
    detail_body = js[detail_start:detail_end]
    assert "console.warn" in detail_body, (
        "fetchDetail must console.warn on failure paths"
    )
    assert "catch (e) {}" not in detail_body, (
        "fetchDetail must not silently swallow errors (RHA-v1 rha-004)"
    )


def test_combined_connection_indicator_present():
    """The header connection indicator combines WS state and /api/bots
    fetch staleness into a single dot+label (RHA-v1 rha-004 refined).
    Earlier the dashboard had two separate indicators which could
    show contradictory states ("live" dot next to "disconnected"
    badge). This test pins the consolidated structure so a future
    refactor that re-introduces a separate badge fails fast.

    Asserts:
    - Single dot (#state-ws-dot) + single label (#state-ws-label).
    - No standalone ``staleness-badge`` element / class anywhere.
    - .live-dot CSS gains the new ``.stale`` and ``.disconnected``
      modifiers (the existing ``.connected`` and default states are
      unchanged).
    - JS exposes ``_updateConnectionIndicator`` (the single source of
      truth) and ``_startConnectionTimer`` (the 5s tick), and tracks
      ``_wsConnected`` as the WS-side input.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    # Single combined indicator, no separate staleness-badge.
    assert 'id="state-ws-dot"' in html
    assert 'id="state-ws-label"' in html
    assert 'id="staleness-badge"' not in html, (
        "the separate staleness-badge was rolled back in favour of a "
        "single combined indicator (RHA-v1 rha-004 refined)"
    )
    assert "staleness-badge" not in css, (
        "leftover .staleness-badge CSS from the rolled-back separate "
        "indicator must be removed"
    )

    # New live-dot state classes.
    assert ".live-dot.stale" in css
    assert ".live-dot.disconnected" in css
    assert "@keyframes pulse-slow" in css

    # JS — single update helper + WS-side input mirror.
    assert "_updateConnectionIndicator" in js
    assert "_startConnectionTimer" in js
    assert "_wsConnected" in js
    # Old function names must be gone so a half-finished migration
    # cannot leave dead helpers calling each other.
    assert "_updateStalenessBadge" not in js
    assert "_startStalenessTimer" not in js


def test_staleness_thresholds_defined():
    """The 30s / 90s thresholds and 5s tick interval must exist as
    named constants so they're discoverable from a single grep and
    so a future tuning PR doesn't leave magic numbers scattered
    through the code.
    """
    js = _APP_JS.read_text(encoding="utf-8")

    assert "STALENESS_STALE_SEC" in js
    assert "STALENESS_DISCONNECTED_SEC" in js
    assert "STALENESS_TICK_MS" in js
    # The values themselves — keep these aligned with RHA-v1's spec
    # (30s stale / 90s disconnected). A future operator-tuning PR
    # is welcome to bump them, but the test ratchets the current
    # contract so the change is intentional.
    assert "STALENESS_STALE_SEC = 30" in js
    assert "STALENESS_DISCONNECTED_SEC = 90" in js


def test_initial_load_skeleton_present():
    """RHA-v1 rha-005 — the bot-grid + dashboard stat-grid must show
    skeleton placeholders on initial page-load so a slow first
    fetch does not look like a wedged backend. The skeleton cards
    must be marked with ``skeleton-on-init`` so JS can strip the
    class on first ``_markFetchSuccess``; ``skeleton-on-init`` must
    NOT appear on the bot-grid / stat-grid in any state where it
    persists across refresh polls (caught structurally — the JS
    helper strips, the CSS only animates while the class is
    present).
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    assert "bot-card-skeleton" in html
    assert "skeleton-on-init" in html
    # CSS hooks for the two skeleton flavours.
    assert ".bot-card-skeleton" in css
    assert ".stat-grid.skeleton-on-init .card" in css
    # JS strips the class on success so the pulse stops on the next
    # poll cycle without flicker.
    assert "_markFetchSuccess" in js
    assert "skeleton-on-init" in js


def test_no_dead_css_classes_resurface():
    """RHA-v1 rha-009 verified five orphan CSS classes (.amb,
    .btn-delete, .deal-trigger-badge, .active-deals-header,
    .bt-history-panel) and they were removed in
    cleanup/rha-009-rha-010-dead-code. The RHA verification was a
    4-way grep across CSS / HTML / JS / quoted strings — re-adding
    one of these classes without re-running that verification almost
    certainly means the operator just resurrected a dead artifact.

    If a future PR genuinely needs one of these names, change it to
    something that is not on this rejected list, or add an explicit
    comment explaining why the audit verdict has flipped.
    """
    css = _STYLE_CSS.read_text(encoding="utf-8")
    dead_classes = [
        ".amb",
        ".btn-delete",
        ".deal-trigger-badge",
        ".active-deals-header",
        ".bt-history-panel",
    ]
    for cls in dead_classes:
        # Match both ``.foo {`` and ``.foo{`` selector openings so a
        # minified or unspaced re-introduction is still caught.
        assert f"{cls} {{" not in css, (
            f"Dead CSS class {cls} resurfaced (with-space). "
            f"Re-run RHA-v1 rha-009 verification before re-adding."
        )
        assert f"{cls}{{" not in css, (
            f"Dead CSS class {cls} resurfaced (no-space variant)."
        )
        # Pseudo-class / modifier variants on the same root.
        assert f"{cls}:" not in css, (
            f"Dead CSS class {cls} resurfaced via a pseudo-class "
            f"or pseudo-element."
        )
        assert f"{cls}." not in css, (
            f"Dead CSS class {cls} resurfaced via a chained class "
            f"modifier (e.g. {cls}.something)."
        )


def test_fmtDateNL_stays_removed():
    """RHA-v1 rha-010 — ``fmtDateNL`` had exactly one grep hit (its
    own definition) and was removed in cleanup/rha-009-rha-010-dead-
    code. Re-introducing it without first finding a real call site
    would mean re-adding dead code.

    Catches both ``function fmtDateNL`` and the const/let/var/arrow
    assignment shapes so a refactor that swaps function-decl style
    can't slip the helper back in unnoticed.
    """
    js = _APP_JS.read_text(encoding="utf-8")
    assert "function fmtDateNL" not in js, (
        "fmtDateNL re-added as a function declaration — verify it is "
        "actually called somewhere first (RHA-v1 rha-010 found 0 "
        "call sites)."
    )
    assert "const fmtDateNL" not in js
    assert "let fmtDateNL" not in js
    assert "var fmtDateNL" not in js
    # Catches ``fmtDateNL = (ts) => {...}`` arrow-style and any
    # assignment to a property of the same name.
    assert "fmtDateNL =" not in js
    assert "fmtDateNL:" not in js


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
