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

    def test_env_var_short_password_refused(self, monkeypatch, capsys):
        """Audit v26-03: length check uses the centralised
        PASSWORD_MIN_LENGTH (12). An 11-char password would have
        passed the pre-fix ≥10 check; it must now be rejected."""
        monkeypatch.setenv("REVERTO_ADMIN_PW", "elevenchars")

        # getpass must NOT be called — env-var is non-empty.
        def _fail_getpass(prompt=""):
            pytest.fail("getpass should not be invoked for non-empty env-var")

        monkeypatch.setattr(setup_admin.getpass, "getpass", _fail_getpass)

        exit_code = setup_admin.main()
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "at least 12 characters" in err

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


class TestPasswordPolicyCentralised:
    """Audit v26-03: password-length policy lives in
    ``core.user_store.PASSWORD_MIN_LENGTH``. Both setup_admin and the
    change-password route must reference that exact constant — not a
    local copy that can drift."""

    def test_setup_admin_imports_shared_constant(self):
        """setup_admin must read the same constant exposed by
        core.user_store; a local fallback would re-introduce the v26-03
        drift."""
        from core import user_store

        assert setup_admin.PASSWORD_MIN_LENGTH is user_store.PASSWORD_MIN_LENGTH

    def test_change_password_route_imports_shared_constant(self):
        """The change-password route must reference the same constant;
        a hardcoded literal would re-open the v26-03 drift."""
        from core import user_store
        from web.routes import auth as auth_route

        assert auth_route.PASSWORD_MIN_LENGTH is user_store.PASSWORD_MIN_LENGTH

    def test_policy_value_is_twelve(self):
        """Pin the current value so an accidental downgrade below the
        audit-mandated floor (12) trips this test."""
        from core import user_store

        assert user_store.PASSWORD_MIN_LENGTH == 12
