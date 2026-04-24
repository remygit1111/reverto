"""Regression guard for audit v1 r1-023 — bot subprocess env allowlist.

Before the fix ``start_bot`` / ``start_bot_dry_run`` passed
``os.environ.copy()`` into ``subprocess.Popen`` — every bot inherited
every tenant's ``TELEGRAM_BOT_TOKEN``, ``BITGET_PASSPHRASE``,
``REVERTO_API_KEY``, and any other secret sitting in the portal's
env. These tests pin the ``_bot_subprocess_env`` helper to the small
allowlist the fix introduced: secrets explicitly excluded, a handful
of process-level config entries explicitly included, and the per-user
scoping breadcrumb present.

The helper is a pure function over ``os.environ``; all seven tests
use ``monkeypatch.setenv`` + ``monkeypatch.delenv`` to prepare the
environment and assert against the returned dict without spawning a
real subprocess.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from web.app import _bot_subprocess_env  # noqa: E402


# ── Secret exclusion ──────────────────────────────────────────────────────


class TestSecretsExcluded:
    """Every variable the audit called out as a leak vector must be
    absent from the subprocess env, even when it's set in the portal
    process. Parametrised over the full set so a future addition to
    ``_BOT_ENV_ALLOWLIST`` that accidentally re-opens a gap trips this
    block immediately."""

    @pytest.mark.parametrize("var", [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_CLAUDE_BOT_TOKEN",
        "TELEGRAM_CLAUDE_CHAT_ID",
        "BITGET_PASSPHRASE",
        "REVERTO_API_KEY",
        "REVERTO_SECRET_KEY",
        "REVERTO_DESTRUCTIVE_MIGRATE",
    ])
    def test_secret_env_not_forwarded(self, var, monkeypatch):
        monkeypatch.setenv(var, "leaky-value-should-never-escape")
        env = _bot_subprocess_env(user_id=1)
        assert var not in env, (
            f"{var} was passed to the bot subprocess — r1-023 regression. "
            f"Got env keys: {sorted(env.keys())}"
        )


# ── Whitelist inclusion ───────────────────────────────────────────────────


class TestAllowlistInclusion:
    def test_path_forwarded(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
        env = _bot_subprocess_env(user_id=1)
        assert env.get("PATH") == "/usr/local/bin:/usr/bin:/bin"

    def test_log_level_forwarded(self, monkeypatch):
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "DEBUG")
        env = _bot_subprocess_env(user_id=1)
        assert env.get("REVERTO_LOG_LEVEL") == "DEBUG"

    def test_user_id_breadcrumb_set(self, monkeypatch):
        # Deliberately pass an id > 1 so a future change to default-to-1
        # would trip this assertion.
        env = _bot_subprocess_env(user_id=42)
        assert env.get("REVERTO_BOT_USER_ID") == "42"

    def test_pythonunbuffered_forced_to_one(self, monkeypatch):
        # Even when unset in the parent, the subprocess must see "1" so
        # the log tailer catches output in near-real time.
        monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
        env = _bot_subprocess_env(user_id=1)
        assert env.get("PYTHONUNBUFFERED") == "1"


# ── Edge cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_values_filtered(self, monkeypatch):
        # An env var present but set to an empty string must not surface
        # in the result. The subprocess's ``os.environ`` with "" entries
        # is valid but noisy; the helper strips them.
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "")
        env = _bot_subprocess_env(user_id=1)
        assert "REVERTO_LOG_LEVEL" not in env

    def test_unknown_env_var_not_forwarded(self, monkeypatch):
        # Any env var outside the allowlist should be withheld even if
        # it looks innocuous. Defense against operator slip that drops
        # a secret into a non-standard name.
        monkeypatch.setenv("REVERTO_CUSTOM_UNEXPECTED", "whatever")
        env = _bot_subprocess_env(user_id=1)
        assert "REVERTO_CUSTOM_UNEXPECTED" not in env

    def test_env_contains_only_allowlisted_plus_scoping(self, monkeypatch):
        # Belt-and-braces: set a mix of allowed + disallowed keys and
        # assert the resulting dict's keyset is a subset of the union
        # of the allowlist + ``REVERTO_BOT_USER_ID``.
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("BITGET_PASSPHRASE", "y")
        monkeypatch.setenv("PATH", "/bin")
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "INFO")
        env = _bot_subprocess_env(user_id=7)
        from web.app import _BOT_ENV_ALLOWLIST
        allowed = set(_BOT_ENV_ALLOWLIST) | {"REVERTO_BOT_USER_ID"}
        extras = set(env.keys()) - allowed
        assert not extras, (
            f"Helper returned keys outside the documented allowlist: {extras}"
        )
