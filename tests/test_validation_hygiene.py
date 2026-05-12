"""Regression guards for the validation-hygiene cluster.

Closes pd-007 (slug regex asymmetry as design-intent), pd-009
(chart-route exception split), r2-007 (POSIX rename docstring),
r2-010 (API-key format validator), r2-011 (restore.sh schema-
version compatibility check).

The tests are a mix of:
* contract pins (the slug-regex test asserts the deliberate
  asymmetry survives a future "harmonise" temptation),
* helper-function unit tests (API-key validator),
* docstring-content guards (r2-007 + the slug-regex design note),
* shell-script-content guards (r2-011 restore validator).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── pd-007: slug regex asymmetry as design-intent ─────────────────────────


class TestSlugRegexDesignIntent:
    """pd-007 — the project deliberately uses two slug regexes with
    non-identical character classes. Pre-fix the asymmetry was
    undocumented; a future "consistency" PR could have widened
    ``_SLUG_RE`` to match ``_BOT_SLUG_RE`` and silently broken the
    on-disk filesystem layout for any slug entered with mixed case.
    Both contracts are now pinned + documented.
    """

    def test_slug_re_is_post_lowercase_narrow(self):
        """``_SLUG_RE`` is applied AFTER lowercasing inside
        ``slugify()`` — its narrow ``[^a-z0-9_]`` charset is
        deliberate. Widening it would change the on-disk filesystem
        layout for every mixed-case input."""
        from web.app import _SLUG_RE

        # Charset is post-lowercase: lowercase + digit + underscore
        # only; anything else (uppercase, hyphen, special chars) is
        # stripped. Applied raw (without first lowercasing) to a
        # mixed-case input it strips both the uppercase chars AND
        # the hyphen — that's the wrong behaviour for unfiltered
        # input, which is exactly why slugify() lowercases first.
        # ("Bot-One" → strip B, -, O → "otne".)
        assert _SLUG_RE.sub("", "Bot-One") == "otne", (
            "pd-007: _SLUG_RE pre-fix narrow charset must remain — "
            "uppercase + hyphen are stripped because slugify "
            "lowercases first. Widening this regex changes the "
            "on-disk filesystem layout for every existing slug."
        )
        # After the deliberate lowercase that slugify() does first,
        # the same input becomes "bot-one"; the regex then strips
        # only the hyphen, leaving "botone".
        assert _SLUG_RE.sub("", "bot-one") == "botone"

    def test_bot_slug_re_accepts_mixed_case_and_hyphen(self):
        """``_BOT_SLUG_RE`` (URL-path validator) accepts the wider
        ``[A-Za-z0-9_-]`` superset because it sees slugs that may
        have been generated outside ``slugify()`` — by YAML import
        of a legacy hyphenated config, by direct on-disk operator
        edit, or by tests that bypass the wizard. Validator's job
        is purely "is this URL-safe and not a path-traversal".
        """
        from web.app import _BOT_SLUG_RE

        assert _BOT_SLUG_RE.match("Bot-One"), (
            "pd-007: _BOT_SLUG_RE must accept mixed-case + hyphen — "
            "tightening it would break URL paths for any legacy "
            "hyphenated slug."
        )
        assert _BOT_SLUG_RE.match("bot_one_2")
        assert _BOT_SLUG_RE.match("BOT123")
        # Path-traversal + special chars rejected.
        assert not _BOT_SLUG_RE.match("../etc/passwd")
        assert not _BOT_SLUG_RE.match("bot/one")
        assert not _BOT_SLUG_RE.match("bot one")  # space
        assert not _BOT_SLUG_RE.match("")

    def test_slugify_lowercases_before_narrow_strip(self):
        """The two-stage pipeline (lowercase → narrow strip) is the
        contract that justifies the regex asymmetry. If a future
        refactor removes the lowercasing step, the narrow regex
        becomes inconsistent with itself, not just with
        _BOT_SLUG_RE."""
        from web.app import slugify

        assert slugify("Bot One") == "bot_one"
        assert slugify("BOT-NAME-2") == "botname2"
        assert slugify("Hello World") == "hello_world"

    def test_design_note_present_in_app_py(self):
        """The pd-007 design note above the regex declarations must
        survive — it explains why the asymmetry exists. A future
        edit that removes the comment would re-open pd-007 even if
        the regex bodies are untouched."""
        app_py = (_REPO_ROOT / "web" / "app.py").read_text(
            encoding="utf-8",
        )
        assert "pd-007 design note" in app_py, (
            "pd-007: the design-note comment block above _SLUG_RE / "
            "_BOT_SLUG_RE must remain so a future reader does not "
            "assume the asymmetry is drift and harmonise the two."
        )


# ── pd-009: chart-route exception split ───────────────────────────────────


class TestChartRouteExceptionSplit:
    """pd-009 — chart routes used a single ``except Exception`` per
    fetch site, which lost type/message granularity in operator log
    triage and treated transient failures (NetworkError) the same as
    permanent ones (BadSymbol, AuthenticationError). The split now
    routes by ccxt class:

    * ``ccxt.NetworkError`` / ``asyncio.TimeoutError`` → 503
    * ``ccxt.BadSymbol`` → 400
    * other ``ccxt.ExchangeError`` → 502 (existing behaviour)
    * other ``Exception`` → 500
    """

    def _read_chart_py(self):
        return (_REPO_ROOT / "web" / "routes" / "chart.py").read_text(
            encoding="utf-8",
        )

    def test_chart_imports_ccxt(self):
        """The except-branches reference ``ccxt.NetworkError`` etc —
        the import must be present at module-level."""
        chart = self._read_chart_py()
        assert re.search(
            r"^import ccxt\b", chart, re.MULTILINE,
        ), "pd-009: chart.py must import ccxt to use its exception types"

    def test_network_error_branch_returns_503(self):
        """At least one of the chart-route try-blocks must catch
        ``ccxt.NetworkError`` and raise a 503 — pre-fix the same
        fetch failure was indistinguishable from a 502 permanent
        error."""
        chart = self._read_chart_py()
        assert "ccxt.NetworkError" in chart, (
            "pd-009: ccxt.NetworkError branch missing from chart.py"
        )
        assert "status_code=503" in chart, (
            "pd-009: chart.py must raise 503 on transient failures"
        )

    def test_bad_symbol_branch_returns_400(self):
        """``ccxt.BadSymbol`` is a client-error — caller asked for a
        pair the exchange does not list. 400 makes the wrong
        request visible instead of hiding it as a 502."""
        chart = self._read_chart_py()
        assert "ccxt.BadSymbol" in chart, (
            "pd-009: ccxt.BadSymbol branch missing from chart.py"
        )

    def test_unexpected_exception_returns_500_with_full_trace(self):
        """The catchall must be reserved for genuinely unexpected
        errors and must use ``logger.exception`` (full traceback)
        rather than ``logger.error`` (no traceback)."""
        chart = self._read_chart_py()
        # 500-status raises must exist somewhere.
        assert "status_code=500" in chart, (
            "pd-009: chart.py must surface 500 for unexpected errors "
            "(distinct from 502 ccxt permanent errors)"
        )
        # logger.exception must outnumber raw logger.error inside
        # chart.py's catchall pattern.
        assert chart.count("logger.exception") >= 3, (
            "pd-009: each fetch-block catchall must use "
            "logger.exception so the operator gets the full traceback"
        )

    def test_each_branch_has_distinct_logger_call(self):
        """Each ccxt-class branch logs distinctly — pre-fix all
        failures landed in one ``logger.exception`` line, making
        log-grep useless to distinguish transient from permanent."""
        chart = self._read_chart_py()
        # Transient warnings go through logger.warning (not exception)
        # so the traceback noise is reserved for the permanent /
        # unexpected paths.
        assert "logger.warning" in chart, (
            "pd-009: transient (NetworkError) branch must use "
            "logger.warning, not logger.exception — the traceback is "
            "noise on a known-transient class."
        )


# ── r2-007: POSIX atomic-rename docstring ─────────────────────────────────


class TestStateIoPosixDocstring:
    """r2-007 — the .tmp + Path.replace() pattern relies on POSIX
    atomic rename. Pre-fix the dependency was undocumented, so a
    future operator deploying onto NFS or Windows would have no
    warning that the atomicity contract weakens."""

    def test_write_docstring_mentions_posix(self):
        from paper.state_io import StateIO

        doc = StateIO.write.__doc__ or ""
        assert "POSIX" in doc, (
            "r2-007: StateIO.write docstring must mention POSIX so "
            "the atomicity contract is grep-discoverable."
        )

    def test_write_docstring_lists_non_posix_caveats(self):
        """The docstring must enumerate the layouts where the
        guarantee weakens — NFS, Windows, FUSE — so an operator
        diagnosing an exotic crash mode can spot the assumption."""
        from paper.state_io import StateIO

        doc = (StateIO.write.__doc__ or "").lower()
        # At least two of the three known caveat classes must surface.
        caveats = ["nfs", "windows", "fuse"]
        present = [c for c in caveats if c in doc]
        assert len(present) >= 2, (
            f"r2-007: docstring must enumerate non-POSIX caveats. "
            f"Found only {present}; expected at least 2 of {caveats}."
        )

    def test_write_docstring_references_audit_id(self):
        from paper.state_io import StateIO

        doc = StateIO.write.__doc__ or ""
        assert "r2-007" in doc, (
            "r2-007: docstring must reference the finding ID for "
            "grep-ability when chasing the audit trail."
        )


# ── r2-010 (REMOVED): API-key format validator ────────────────────────────
#
# The TestApiKeyFormatValidator class was deleted alongside the
# ``_validate_api_key_format`` helper in core/credentials.py. The
# heuristic regex rejected legitimate Bitget keys after Bitget's
# "bg_" prefix rollout (underscores aren't in [A-Za-z0-9]). Defence
# now lives in (1) Pydantic length bounds at the route layer and
# (2) the test-connection endpoint that round-trips a real
# authenticated call.


# ── r2-011: restore.sh schema-version compatibility check ─────────────────


class TestRestoreSchemaVersionCheck:
    """r2-011 — backup.sh stamps ``Schema version: <N>`` into the
    MANIFEST (audit r3-008), but pre-fix restore.sh just *displayed*
    the manifest without comparing the backup version against the
    running code's expected ``SCHEMA_VERSION``. Restoring a forward-
    version backup is incorrect; this test pins that restore.sh now
    refuses such restores."""

    def _read_restore_sh(self):
        return (_REPO_ROOT / "scripts" / "restore.sh").read_text(
            encoding="utf-8",
        )

    def test_restore_reads_backup_manifest_schema_version(self):
        sh = self._read_restore_sh()
        assert "BACKUP_SCHEMA_VERSION" in sh, (
            "r2-011: restore.sh must read the backup MANIFEST's "
            "Schema-version line into a variable for comparison."
        )
        # Awk-extraction line that grabs the value.
        assert "Schema version:" in sh, (
            "r2-011: restore.sh must reference the literal "
            "MANIFEST line label so a future MANIFEST format change "
            "is caught here."
        )

    def test_restore_resolves_current_schema_version_from_python(self):
        """The compatibility check must consult the running code's
        ``core.database.SCHEMA_VERSION`` so a stale shell-side
        constant cannot drift."""
        sh = self._read_restore_sh()
        assert "from core.database import SCHEMA_VERSION" in sh, (
            "r2-011: restore.sh must read SCHEMA_VERSION from the "
            "running code, not a hardcoded number."
        )

    def test_restore_refuses_future_schema_with_exit_1(self):
        """A backup taken from a newer code-version is refused with
        exit 1 — silently restoring would leave the DB in a state
        the running code does not understand."""
        sh = self._read_restore_sh()
        # The future-version branch must contain "exit 1".
        assert re.search(
            r"newer than the current code expects.*?exit 1",
            sh, re.DOTALL,
        ), (
            "r2-011: restore.sh must exit 1 on a forward-version "
            "backup, not just print a warning and continue."
        )

    def test_restore_warns_on_missing_manifest_version(self):
        """Pre-r3-008 backups have no Schema-version line — restore
        warns but does not block, matching the docstring promise."""
        sh = self._read_restore_sh()
        assert "Pre-r3-008" in sh or "no schema-version stamp" in sh, (
            "r2-011: restore.sh must explicitly handle the "
            "no-stamp case (older backup) with a clear warning."
        )

    def test_restore_audit_id_present(self):
        """The audit ID must surface in the restore.sh comment block
        so a future grep for the finding lands here."""
        sh = self._read_restore_sh()
        assert "r2-011" in sh, (
            "r2-011: restore.sh comment block must reference the "
            "finding ID for grep-discoverability."
        )
