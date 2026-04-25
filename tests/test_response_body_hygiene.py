"""Class-of-issue regression — audit pd-001 + r2-001.

pd-001 (VPS-1.5 polish) fixed three explicit sites where raw
exception strings leaked into HTTPException ``detail`` fields.
r2-001 (v2 audit) found a fourth site that was missed by that
sweep (``web/routes/bots.py:638``) — same class of bug, just
not in the pd-001 enumeration.

The v2 audit's learning: **class-of-issue fixes need class-of-
issue regression tests**. A site-by-site unit test would have
missed the bot-duplicate endpoint for the same reason the PR
missed it. A grep-level guard catches any future regression
of the same pattern — whether it's a copy-paste into a new
route or a rewrite that re-introduces the leak.

The check is intentionally narrow: flag ``HTTPException(detail=
f"...{e}...")`` (or ``{err}`` / ``{exc}`` / ``{ex}`` /
``{exception}``) shapes. Bare-variable interpolation of
values named like exceptions into a response ``detail`` is the
exact pattern pd-001/r2-001 closed. Other interpolations
(slug, user_id, new_slug, path, etc.) are safe and out of
scope for this check.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Match ``HTTPException(...status_code=5XX...detail=f"...{...<excname>...}...")``
# where <excname> is exactly one of the conventional exception variable
# names used in Reverto's route-layer except-clauses, AND the status
# code is 5XX (server error).
#
# Scope rationale: r2-001 / pd-001 are specifically about **500-class**
# infra leaks — YAMLError line/column, OSError paths, etc. **400-class**
# responses on client-input validation can legitimately surface parser
# error strings to help the user fix their input (e.g.
# ``invalid timestamp: {e}`` on a user-supplied ISO string). Scoping
# the check to 5XX keeps the guard aligned with the actual threat
# model and avoids false positives on validation UX.
#
# Match-shape (audit r3-002): the exception name must appear as a
# **standalone word** (``\b...\b``) ANYWHERE inside the f-string
# interpolation braces. The original pattern required the brace to
# OPEN with the name (``\{(?:e|...)``), which missed function-wrapped
# variants like ``{str(e)[:200]}`` and ``{repr(exc)}`` — three sites
# in chart.py slipped past that pattern. Word-boundary matching keeps
# the false-positive surface tight: ``{user_id}``, ``{slug}``,
# ``{e_count}``, ``{count_e}`` all do NOT contain the names as
# standalone tokens so they don't match.
#
# Format-spec (`:fmt`) and conversion-flag (`!s`/`!r`/`!a`) are
# implicitly accommodated by the open-ended interpolation match.
#
# DOTALL + non-greedy ``.*?`` stops at the first ``)``, which lets
# the pattern span multi-line HTTPException(...) calls.
_EXCEPTION_NAMES = ("e", "err", "exc", "ex", "exception")
_NAME_ALT = "|".join(_EXCEPTION_NAMES)

_HTTP_EXCEPTION_LEAK_RE = re.compile(
    r'HTTPException\s*\(\s*'               # opening of HTTPException(
    r'(?:status_code\s*=\s*)?'             # optional status_code= kwarg prefix
    r'5\d{2}'                              # 5XX status code (500, 502, 503, ...)
    r'[^)]*?'                              # everything else in the call
    r'detail\s*=\s*'                       # detail=
    r'f"[^"]*'                             # opening of f-string
    r'\{[^}"]*?\b(?:' + _NAME_ALT + r')\b' # {...<exception-name>...}
    r'[^}"]*?\}'                           # rest of interpolation + closing brace
    r'[^"]*"',                             # rest + closing "
    re.DOTALL,
)

# Files in scope: the route layer. web/app.py has its own audit
# history (no leaks there at HEAD) but changes are rare and the false-
# positive surface in a 2700-line module isn't worth the coverage.
# Limiting to web/routes/ is tight + high-signal.
_ROUTES_DIR = (
    Path(__file__).resolve().parent.parent / "web" / "routes"
)


def test_routes_layer_has_no_exception_detail_leak():
    """Scan every web/routes/*.py and fail if any route still
    interpolates a bare exception variable into an HTTPException
    detail. The pattern enumerates the conventional names (e, err,
    exc, ex, exception) that Reverto uses in its except-clauses —
    if a future site introduces a different variable name, add it
    to ``_EXCEPTION_NAMES`` above.
    """
    assert _ROUTES_DIR.is_dir(), (
        f"routes dir not found: {_ROUTES_DIR}"
    )

    violations: list[str] = []
    for py in sorted(_ROUTES_DIR.rglob("*.py")):
        if py.name == "__init__.py":
            continue
        text = py.read_text(encoding="utf-8")
        for m in _HTTP_EXCEPTION_LEAK_RE.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            snippet = m.group(0).replace("\n", " ")[:180]
            rel = py.relative_to(_ROUTES_DIR.parent.parent)
            violations.append(f"{rel}:{line_no}: {snippet}")

    assert not violations, (
        "Found HTTPException(detail=f'...{e}...') patterns — these "
        "leak raw exception strings (line/col/snippet for YAMLError, "
        "OSError paths, etc.) to the client. Use the pd-001 / r2-001 "
        "template: logger.exception(...) + generic detail.\n\n"
        + "\n".join(violations)
    )


def test_regex_fires_on_known_bad_patterns():
    """Meta-test: confirm the regex actually catches the shape that
    r2-001 was about. If someone tightens or breaks the regex, this
    test fails and forces them to update the bad-pattern fixture."""
    bad = 'raise HTTPException(status_code=500, detail=f"YAML parse error: {e}")'
    assert _HTTP_EXCEPTION_LEAK_RE.search(bad) is not None

    bad_conv = 'raise HTTPException(500, detail=f"oops: {exc!r}")'
    assert _HTTP_EXCEPTION_LEAK_RE.search(bad_conv) is not None

    bad_503 = (
        'raise HTTPException(status_code=503, '
        'detail=f"upstream down: {err}")'
    )
    assert _HTTP_EXCEPTION_LEAK_RE.search(bad_503) is not None


def test_regex_catches_function_wrapped_exceptions():
    """Audit r3-002: the original regex required the f-string
    interpolation to OPEN with an exception variable name —
    ``\\{(?:e|err|exc|...)``. Three sites in web/routes/chart.py
    used ``f"...{str(e)[:200]}"`` and slipped past, surfacing as
    r3-001. The broadened regex matches the exception name as a
    **standalone word anywhere** inside the interpolation braces,
    so function-wrapped variants are caught.
    """
    cases = [
        # The exact pattern that slipped past the original regex.
        'raise HTTPException(status_code=502, detail=f"x: {str(e)[:200]}")',
        # repr() / format() variants.
        'raise HTTPException(500, detail=f"oops: {repr(exc)}")',
        'raise HTTPException(502, detail=f"down: {format(err)}")',
        # Conversion-flag combined with truncation.
        'raise HTTPException(500, detail=f"err: {str(e)[:50]!r}")',
        # Attribute access on the exception variable.
        'raise HTTPException(503, detail=f"type: {e.__class__.__name__}")',
    ]
    for case in cases:
        assert _HTTP_EXCEPTION_LEAK_RE.search(case) is not None, (
            f"regex failed to catch function-wrapped exception leak: "
            f"{case!r}"
        )


def test_regex_does_not_false_positive_on_safe_interpolations():
    """Interpolations of values that aren't exception-named should
    not trip the check. ``{new_slug}``, ``{user_id}``, ``{path}``
    etc. are safe — they're either known-sanitised or already
    validated by Pydantic / regex upstream.

    Audit r3-002 broadened the regex to catch function-wrapped
    variants. Word-boundary matching (``\\b``) keeps these
    identifier-substring cases from false-positive.
    """
    safe_cases = [
        'HTTPException(status_code=409, detail=f"Bot with slug \'{new_slug}\' already exists")',
        'HTTPException(500, detail=f"Path traversal attempt on {path}")',
        'HTTPException(status_code=503, detail=f"Unknown bot: {slug}")',
        'HTTPException(500, detail=f"count={count}")',
        # Tricky cases: identifiers that contain an exception-name
        # substring but are NOT exception variables. The broadened
        # regex must not flag these via the ``\b`` word-boundaries.
        'HTTPException(500, detail=f"user={user_id}")',           # contains 'er' substring
        'HTTPException(503, detail=f"email={user.email}")',       # 'email' contains 'e'
        'HTTPException(502, detail=f"prev={e_count} next={count_e}")',  # 'e_' / '_e' compound names
        'HTTPException(500, detail=f"text={message}")',           # 'message' contains 'e'
        'HTTPException(503, detail=f"format={timeframe}")',       # 'timeframe' contains 'e'
        'HTTPException(500, detail=f"ext={extension}")',          # 'extension' starts with 'ex'
    ]
    for case in safe_cases:
        assert _HTTP_EXCEPTION_LEAK_RE.search(case) is None, (
            f"false positive on safe interpolation: {case!r}"
        )


def test_regex_does_not_flag_4xx_validation_detail():
    """4XX responses on client-input validation can legitimately
    surface parser errors to the caller (that IS the UX — tell the
    user what they got wrong). Scope the guard to 5XX only."""
    four_xx_cases = [
        'HTTPException(status_code=400, detail=f"Invalid config: {e}")',
        'HTTPException(400, detail=f"invalid timestamp: {e}")',
        'HTTPException(status_code=422, detail=f"parse error: {exc}")',
    ]
    for case in four_xx_cases:
        assert _HTTP_EXCEPTION_LEAK_RE.search(case) is None, (
            f"regex should not flag 4XX validation responses: {case!r}"
        )
