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


def test_closed_deals_table_has_dca_column():
    """The bot-detail Deals tab's Closed Deals table renders a
    column-driven table from the ``CLOSED_DEALS_COLUMNS`` array. A
    new ``DCA`` column was added between ``close_price`` and
    ``pnl_btc`` so operators can see how many DCA orders each closed
    deal used (excludes base order). The data comes from
    ``d.dca_count`` which paper.state_io.deal_to_dict serialises
    from the engine's ``PaperDeal.dca_count`` property.

    A future refactor that drops the column entry, renames the key,
    or moves it past ``pnl_btc`` will fail this test before any
    operator notices missing data on the live page.
    """
    js = _APP_JS.read_text(encoding="utf-8")

    # Carve out the CLOSED_DEALS_COLUMNS array literal so the
    # assertions only inspect the column-driven config, not other
    # uses of the same strings (e.g. backtest UI).
    arr_start = js.index("const CLOSED_DEALS_COLUMNS")
    arr_end = js.index("];", arr_start) + 2
    arr_src = js[arr_start:arr_end]

    # Column is present, both as the dictionary ``key`` and the
    # column ``label`` ("DCA" appears in the user-facing header).
    assert "key: 'dca_count'" in arr_src, (
        "CLOSED_DEALS_COLUMNS missing the 'dca_count' column entry"
    )
    assert "label: 'DCA'" in arr_src, (
        "DCA column header label missing"
    )
    # Data binding: the cell renderer must pull from d.dca_count.
    assert "d.dca_count" in arr_src

    # Order matters: dca_count should sit between close_price and
    # pnl_btc so the table reads naturally
    # (entry → close → DCAs used → PnL).
    close_idx = arr_src.index("key: 'close_price'")
    dca_idx   = arr_src.index("key: 'dca_count'")
    pnl_idx   = arr_src.index("key: 'pnl_btc'")
    assert close_idx < dca_idx < pnl_idx, (
        "DCA column must sit between close_price and pnl_btc; got "
        f"close@{close_idx}, dca@{dca_idx}, pnl@{pnl_idx}"
    )


def test_bot_card_does_not_render_open_deal_preview():
    """The bot-card on the Overview / Bots tab used to render the
    first three open deals as a label-free row of
    "deal_id  entry_price  pnl" beneath the stats grid, with a
    "+N more deals" overflow line. Operator feedback: the row was
    confusing — no headers, awkward placement between the stats
    grid and the action-buttons. The OPEN DEALS stat already
    surfaces the count; full deal detail lives on the Active Deals
    top-nav tab and on each bot's detail-page Deals tab. Removed
    in cleanup/bot-card-remove-deal-preview.

    This regression guard pins the three preview-only CSS classes
    + the JS render-helper variable names so a future PR that
    re-introduces the row gets caught at CI rather than ending up
    in front of the operator again. ``.deal-id-cell`` and
    ``.muted-cell`` are intentionally NOT in the dead-list — both
    are still used by the active-deals + closed-deals tables.
    """
    css = _STYLE_CSS.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")

    # Preview-only CSS classes must be gone.
    for cls in (".bot-card-deals", ".bot-card-deal-row", ".more-deals-row"):
        assert cls + " " not in css, (
            f"Preview CSS {cls} resurfaced (with-space). "
            f"See cleanup/bot-card-remove-deal-preview rationale."
        )
        assert cls + "{" not in css, (
            f"Preview CSS {cls} resurfaced (no-space variant)."
        )
        assert cls + ":" not in css, (
            f"Preview CSS {cls} resurfaced via a pseudo-class."
        )

    # Shared CSS classes (used by other tables) must stay.
    assert ".deal-id-cell" in css, (
        "shared .deal-id-cell removed by accident — still used by "
        "the Active Deals table"
    )
    assert ".muted-cell" in css, (
        "shared .muted-cell removed by accident — still used by "
        "Active Deals + Closed Deals tables"
    )

    # JS render-helper for the preview must be gone. ``openDealsHtml``
    # was the unique variable name for the preview-builder; killing
    # it here catches a copy-paste resurrection that just renames the
    # wrapper class.
    assert "openDealsHtml" not in js, (
        "openDealsHtml builder resurfaced in renderBotCard"
    )
    # Class-string usages should also be gone.
    assert 'class="bot-card-deals"' not in js
    assert 'class="bot-card-deal-row"' not in js
    assert 'class="more-deals-row"' not in js


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


def test_login_error_div_outside_both_login_forms():
    """``#login-error`` must sit OUTSIDE both ``#login-form`` and
    ``#login-totp-form`` so it stays visible across the 2-step
    login flow. PR 3 follow-up: pre-fix the div lived inside
    ``#login-form`` and was dragged into display:none with its
    parent when ``_showLoginTotpForm()`` swapped the forms — wrong-
    TOTP-code attempts produced no UI feedback.

    Pin the DOM-structure here: error-div opens AFTER both form
    closing tags. Tests the source order in index.html, not the
    runtime DOM, but that's sufficient — innerHTML/string-build
    paths in app.js do not produce these IDs.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")

    # Locate the open-tag of each form and the closing </form> that
    # belongs to it. .find() returns the first match; the password
    # form opens first so its </form> is the first one after.
    login_form_open = html.find('id="login-form"')
    assert login_form_open >= 0, "missing #login-form in index.html"
    login_form_close = html.find("</form>", login_form_open)
    assert login_form_close > login_form_open

    totp_form_open = html.find('id="login-totp-form"')
    assert totp_form_open >= 0, "missing #login-totp-form in index.html"
    totp_form_close = html.find("</form>", totp_form_open)
    assert totp_form_close > totp_form_open

    # There must be exactly one #login-error in the document, and it
    # must sit AFTER both form closing tags. A duplicate inside
    # either form would defeat the visibility fix.
    error_open = html.find('id="login-error"')
    assert error_open >= 0, "missing #login-error in index.html"
    assert html.find('id="login-error"', error_open + 1) == -1, (
        "duplicate #login-error in index.html — the visibility fix "
        "needs a single shared div outside both forms"
    )
    assert error_open > login_form_close, (
        "#login-error sits inside #login-form. When _showLoginTotpForm "
        "hides #login-form to reveal the TOTP step, this error-div "
        "would be hidden too. Move it after both </form> tags."
    )
    assert error_open > totp_form_close, (
        "#login-error sits inside #login-totp-form. It would be hidden "
        "during the password step. Move it after both </form> tags."
    )


# ── TOTP modal hygiene cluster (RHA-v2 rhav2-003..007) ────────────────────


def _slice_modal_card(html: str, modal_id: str) -> str:
    """Return the substring from ``id="modal_id"`` to the matching
    ``</div></div>`` boundary. Heuristic — works because TOTP modals
    are simple `.modal-overlay > .modal-card > content` shape and
    the SPA never nests a third-level modal inside one of them."""
    start = html.find(f'id="{modal_id}"')
    assert start >= 0, f"missing modal id={modal_id!r} in index.html"
    # Cut at the next sibling-modal opener as a safety boundary.
    return html[start:start + 3500]


class TestTotpModalAriaDialog:
    """rhav2-004: TOTP modals must declare role=dialog + aria-modal +
    aria-labelledby. Pre-fix the modals announced as plain divs to
    screen readers, no focus-trap hint, no titled label."""

    def test_enroll_modal_has_aria_dialog_attrs(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        block = _slice_modal_card(html, "totp-enroll-modal")
        assert 'role="dialog"' in block, (
            "rhav2-004: #totp-enroll-modal .modal-card must declare "
            "role='dialog' for screen-reader announce."
        )
        assert 'aria-modal="true"' in block
        assert 'aria-labelledby="totp-enroll-title"' in block
        # The labelledby target must exist.
        assert 'id="totp-enroll-title"' in block

    def test_disable_modal_has_aria_dialog_attrs(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        block = _slice_modal_card(html, "totp-disable-modal")
        assert 'role="dialog"' in block
        assert 'aria-modal="true"' in block
        assert 'aria-labelledby="totp-disable-title"' in block
        assert 'id="totp-disable-title"' in block


class TestTotpModalEscClosesViaCleanup:
    """rhav2-003 + rhav2-005: pressing Escape on a TOTP modal must
    route through the typed close-helpers (clear typed password,
    clear secret + QR), not the global fallback that just removes
    the .show class. Pinning the wiring here: the Cancel buttons
    must carry data-action="close" so the global Escape handler
    finds them and dispatches a click — which in turn fires the
    cleanup logic in _closeTotpEnrollModal / _closeTotpDisableModal."""

    def test_enroll_cancel_button_has_data_action_close(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        block = _slice_modal_card(html, "totp-enroll-modal")
        # The Cancel button line must carry data-action="close" so the
        # global Esc handler recognises it as the close-target.
        assert 'id="totp-enroll-cancel"' in block
        # Restrict the search to the line containing the Cancel id so
        # we know the data-action attribute is on THAT button.
        cancel_line = next(
            line for line in block.splitlines()
            if 'id="totp-enroll-cancel"' in line
        )
        assert 'data-action="close"' in cancel_line, (
            "rhav2-005: #totp-enroll-cancel needs data-action='close' "
            "so the global Esc handler routes through "
            "_closeTotpEnrollModal — pre-fix Esc removed only the "
            ".show class, leaving the rendered QR + secret in the DOM."
        )

    def test_disable_cancel_button_has_data_action_close(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        block = _slice_modal_card(html, "totp-disable-modal")
        assert 'id="totp-disable-cancel"' in block
        cancel_line = next(
            line for line in block.splitlines()
            if 'id="totp-disable-cancel"' in line
        )
        assert 'data-action="close"' in cancel_line, (
            "rhav2-003: #totp-disable-cancel needs data-action='close' "
            "so the global Esc handler routes through "
            "_closeTotpDisableModal — pre-fix Esc left the typed "
            "password value in the DOM."
        )


class TestTotpEnrollDoubleClickProtection:
    """rhav2-006: _startTotpEnrollment must disable the trigger
    button while the /auth/totp/setup fetch is in-flight to prevent
    a double-click from minting two pending-secret cookies in rapid
    succession (last-write-wins on the server-side cookie)."""

    def test_start_enrollment_disables_button_during_fetch(self):
        js = _APP_JS.read_text(encoding="utf-8")
        # Locate the function body and assert button-disable + restore
        # both appear within it.
        start = js.find("async function _startTotpEnrollment(")
        assert start >= 0
        # Body extends until the next top-level function or async
        # function declaration.
        end_a = js.find("\nasync function ", start + 1)
        end_b = js.find("\nfunction ", start + 1)
        end = min(x for x in (end_a, end_b) if x > 0)
        body = js[start:end]
        assert "btn.disabled = true" in body, (
            "rhav2-006: _startTotpEnrollment must set btn.disabled=true "
            "before the fetch to prevent re-entry."
        )
        # Reset must run in finally so a thrown error doesn't leave
        # the button permanently disabled.
        assert "finally" in body
        assert "btn.disabled = false" in body


class TestTotpSecretCopyButton:
    """rhav2-007: the manual-entry secret needs a one-click copy
    button. Clipboard API + selection fallback for browsers without
    permission. 32-char base32 is awkward to type and ambiguous to
    read — a copy button removes the friction."""

    def test_copy_button_present_in_html(self):
        html = _INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="totp-secret-copy-btn"' in html
        assert 'id="totp-secret-copy-feedback"' in html

    def test_copy_handler_uses_clipboard_with_selection_fallback(self):
        js = _APP_JS.read_text(encoding="utf-8")
        assert "_copyTotpSecret" in js, (
            "rhav2-007: _copyTotpSecret handler missing"
        )
        # Body must reference both the clipboard API + the
        # range-selection fallback so users without permission can
        # still finish the enrollment.
        start = js.find("async function _copyTotpSecret(")
        assert start >= 0
        end = js.find("\nfunction ", start + 1)
        body = js[start:end if end > 0 else len(js)]
        assert "navigator.clipboard.writeText" in body
        assert "createRange" in body

    def test_copy_button_wired_in_handlers_init(self):
        js = _APP_JS.read_text(encoding="utf-8")
        # The wireup function must reference the copy-button id so
        # the click event is bound at init time.
        assert "totp-secret-copy-btn" in js
        # And the wire-up must call _copyTotpSecret as the listener.
        wireup_start = js.find("function _wireTotpUiHandlers(")
        assert wireup_start >= 0
        end = js.find("\nfunction ", wireup_start + 1)
        wireup_body = js[wireup_start:end if end > 0 else len(js)]
        assert "_copyTotpSecret" in wireup_body


class TestModalAccessibility:
    """rha-006 + rha-007 + rhav2-004 broader sweep.

    Three concerns share a single wireup pass in app.js:

    * rha-006 — auto-focus the right element when a modal opens, and
      restore focus to the trigger button when it closes.
    * rha-007 — Tab / Shift-Tab cycle within the modal, never escape
      to the page underneath.
    * rhav2-004 broader sweep — every ``modal-overlay`` carries the
      ARIA dialog triple (``role="dialog"`` + ``aria-modal="true"`` +
      ``aria-labelledby``), not just the TOTP modals that landed in
      ``fix/totp-modal-hygiene-cluster``.

    Tests are source-grep checks: there is no JS harness, so we
    assert the helpers exist + are wired + the markup carries the
    attributes. Behaviour is verified end-to-end by an operator-side
    smoke test (Tab cycles, Esc restores) — see PR description.
    """

    def test_all_modals_have_aria_dialog_attributes(self):
        """rhav2-004: every .modal-overlay's inner .modal-card carries
        ``role="dialog"`` + ``aria-modal="true"`` + ``aria-labelledby``.
        Pre-fix only the two TOTP modals had them; this guard makes
        sure a future modal-add can't silently regress the broader
        sweep."""
        import re

        html = _INDEX_HTML.read_text(encoding="utf-8")
        modal_ids = re.findall(
            r'<div\s+id="([^"]+-modal)"\s+class="modal-overlay"',
            html,
        )
        # Floor-guard: 13 modals at the time of writing
        # (api-key, profile, 2× totp, settings, deal-edit,
        #  wizard-backtest, sweep, finding-detail, emergency-stop,
        #  bulk-stop, bulk-restart, cl-edit). Any future add gets
        # caught by the loop below; the floor only catches a
        # regression that drops modals from the markup.
        assert len(modal_ids) >= 11, (
            f"Expected at least 11 .modal-overlay elements; "
            f"found {len(modal_ids)}: {modal_ids}"
        )

        for modal_id in modal_ids:
            anchor = f'<div id="{modal_id}" class="modal-overlay">'
            start = html.find(anchor)
            assert start != -1
            # Slice up to the next opening modal-overlay (or end of
            # file) so we only assert against this modal's block,
            # not bleed into the next one.
            next_modal = html.find(
                '<div id="', start + len(anchor),
            )
            block = html[start:next_modal if next_modal > 0 else len(html)]

            assert 'role="dialog"' in block, (
                f"Modal {modal_id} missing role=\"dialog\" on its "
                "modal-card. rhav2-004 broader sweep regression."
            )
            assert 'aria-modal="true"' in block, (
                f"Modal {modal_id} missing aria-modal=\"true\" on "
                "its modal-card. rhav2-004 broader sweep regression."
            )
            assert "aria-labelledby=" in block, (
                f"Modal {modal_id} missing aria-labelledby on its "
                "modal-card. rhav2-004 broader sweep regression."
            )

    def test_focus_helper_present_in_app_js(self):
        """rha-006: ``_focusFirstElementInModal`` exists and waits one
        animation frame so a still-laying-out modal does not get
        focused before it is visible (browsers silently no-op a focus
        on display:none)."""
        js = _APP_JS.read_text(encoding="utf-8")
        assert "_focusFirstElementInModal" in js, (
            "rha-006: _focusFirstElementInModal helper missing."
        )
        # Locate the function body so the requestAnimationFrame
        # check is scoped — a stray rAF elsewhere should not
        # vacuously pass this test.
        start = js.find("function _focusFirstElementInModal(")
        assert start >= 0
        end = js.find("\nfunction ", start + 1)
        body = js[start:end if end > 0 else len(js)]
        assert "requestAnimationFrame" in body, (
            "rha-006: auto-focus must defer one frame via "
            "requestAnimationFrame so the modal is laid out before "
            "we focus into it."
        )

    def test_focus_trap_helper_present_in_app_js(self):
        """rha-007: ``_trapFocusInModal`` exists and handles BOTH
        Tab and Shift-Tab cycling. A trap that only handles forward-
        Tab leaks focus on Shift-Tab from the first element."""
        js = _APP_JS.read_text(encoding="utf-8")
        assert "_trapFocusInModal" in js, (
            "rha-007: _trapFocusInModal helper missing."
        )
        start = js.find("function _trapFocusInModal(")
        assert start >= 0
        end = js.find("\nfunction ", start + 1)
        body = js[start:end if end > 0 else len(js)]
        assert "shiftKey" in body, (
            "rha-007: focus-trap must inspect shiftKey to handle "
            "Shift-Tab wrapping at the first focusable element."
        )
        # Tab + the wrap-direction logic must both be present.
        assert "'Tab'" in body or '"Tab"' in body
        assert "preventDefault" in body, (
            "rha-007: trap must preventDefault on the Tab event "
            "that wraps, otherwise the browser also moves focus."
        )

    def test_focus_trap_wired_to_modals_at_init(self):
        """rha-007: ``_wireAllModalFocusTraps`` runs once on
        DOMContentLoaded so every modal in ``index.html`` gets its
        MutationObserver before the user can open one."""
        js = _APP_JS.read_text(encoding="utf-8")
        assert "_wireAllModalFocusTraps" in js, (
            "rha-007: _wireAllModalFocusTraps wireup function missing."
        )
        # Must be called from the DOMContentLoaded handler so the
        # observers exist before the first modal-show. Locate the
        # init block and assert the call-site is inside it.
        init_start = js.find("document.addEventListener('DOMContentLoaded'")
        assert init_start >= 0
        init_end = js.find("\n});", init_start)
        init_body = js[init_start:init_end if init_end > 0 else len(js)]
        assert "_wireAllModalFocusTraps()" in init_body, (
            "rha-007: _wireAllModalFocusTraps must be called from the "
            "DOMContentLoaded handler."
        )

    def test_focus_restore_via_weakmap_of_triggers(self):
        """rha-006 part 2: closing a modal returns focus to the
        button that opened it. ``_modalTriggers`` (a WeakMap so a
        removed modal's trigger is GC-eligible) records the trigger
        when the MutationObserver detects the show, and
        ``_restoreFocusAfterModalClose`` re-focuses it on hide."""
        js = _APP_JS.read_text(encoding="utf-8")
        assert "_modalTriggers" in js, (
            "rha-006: _modalTriggers WeakMap missing — focus-restore "
            "has no place to record the trigger."
        )
        assert "WeakMap" in js, (
            "rha-006: trigger registry must be a WeakMap so a "
            "detached modal element does not pin its trigger."
        )
        assert "_restoreFocusAfterModalClose" in js, (
            "rha-006: _restoreFocusAfterModalClose helper missing — "
            "the close path has no way to re-focus the trigger."
        )
        # The restore helper must guard against a trigger that has
        # since been removed from the DOM (e.g. modal opened a
        # confirmation that re-rendered the surrounding UI). Pre-
        # guard, focusing a detached element silently no-ops AND
        # leaves the user with focus on document.body.
        start = js.find("function _restoreFocusAfterModalClose(")
        assert start >= 0
        end = js.find("\nfunction ", start + 1)
        body = js[start:end if end > 0 else len(js)]
        assert "document.body.contains" in body, (
            "rha-006: focus-restore must check document.body.contains "
            "before focusing the trigger — a re-rendered UI may have "
            "detached it."
        )
