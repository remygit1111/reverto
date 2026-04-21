"""Tests for scripts/setup_admin.py.

Audit v26-07 regression guard: empty ``REVERTO_ADMIN_PW`` must fall
back to the interactive prompt instead of silently failing the
length check. The DB is isolated per test via the autouse
``_isolate_reverto_db`` fixture in conftest.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# scripts/ isn't a package; add it to sys.path so we can import the
# script as a module the same way tests/test_wipe_deals.py does.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import setup_admin  # noqa: E402


class TestSetupAdminEnvVarHandling:

    def test_empty_env_var_falls_back_to_prompt(self, monkeypatch, capsys):
        """Audit v26-07: REVERTO_ADMIN_PW="" pre-fix bypassed the
        interactive prompt and then failed the length-check with a
        confusing error. Post-fix: empty string is treated as unset
        and getpass is invoked."""
        monkeypatch.setenv("REVERTO_ADMIN_PW", "")

        calls = {"getpass": 0}

        def _fake_getpass(prompt=""):
            calls["getpass"] += 1
            # Return a valid password on every getpass call.
            return "valid-prompt-password-long-enough"

        monkeypatch.setattr(setup_admin.getpass, "getpass", _fake_getpass)

        exit_code = setup_admin.main()

        assert exit_code == 0
        assert calls["getpass"] == 2, (
            "empty env-var must fall back to two getpass calls "
            "(new password + confirm), not silently fail"
        )

    def test_unset_env_var_falls_back_to_prompt(self, monkeypatch):
        """Parity check: unset env-var also prompts. Same code path
        as the empty-string case post-fix, different trigger."""
        monkeypatch.delenv("REVERTO_ADMIN_PW", raising=False)

        calls = {"getpass": 0}

        def _fake_getpass(prompt=""):
            calls["getpass"] += 1
            return "valid-prompt-password-long-enough"

        monkeypatch.setattr(setup_admin.getpass, "getpass", _fake_getpass)

        exit_code = setup_admin.main()

        assert exit_code == 0
        assert calls["getpass"] == 2

    def test_env_var_short_password_refused(self, monkeypatch):
        """A too-short env-var password still hits the length check
        (no interactive prompt, direct rejection). Covers the
        non-empty-but-short branch."""
        monkeypatch.setenv("REVERTO_ADMIN_PW", "short")

        # getpass must NOT be called — env-var is non-empty.
        def _fail_getpass(prompt=""):
            pytest.fail("getpass should not be invoked for non-empty env-var")

        monkeypatch.setattr(setup_admin.getpass, "getpass", _fail_getpass)

        exit_code = setup_admin.main()
        assert exit_code == 1

    def test_env_var_happy_path(self, monkeypatch):
        """Non-empty env-var with sufficient length → success without
        prompting. Sanity check that the common case still works."""
        monkeypatch.setenv("REVERTO_ADMIN_PW", "sufficiently-long-env-password")

        def _fail_getpass(prompt=""):
            pytest.fail("getpass should not be invoked for non-empty env-var")

        monkeypatch.setattr(setup_admin.getpass, "getpass", _fail_getpass)

        exit_code = setup_admin.main()
        assert exit_code == 0

    def test_prompt_passwords_mismatch_rejected(self, monkeypatch):
        """Interactive path: if the two getpass inputs differ, the
        script exits 1 with 'passwords don't match'."""
        monkeypatch.delenv("REVERTO_ADMIN_PW", raising=False)

        responses = iter(["first-password", "second-password"])

        def _fake_getpass(prompt=""):
            return next(responses)

        monkeypatch.setattr(setup_admin.getpass, "getpass", _fake_getpass)

        exit_code = setup_admin.main()
        assert exit_code == 1
