"""Tests for core/paths.py — the multi-tenant filesystem helper.

Every helper is exercised through a monkey-patched ``core.paths.BASE_DIR``
pointing at tmp_path so the real repository tree is never touched.
The helpers auto-create parents with mkdir(exist_ok=True), so the
assertions focus on (a) the path string is correct and (b) secret
dirs (keys/, credentials/<uid>/) land at mode 0700.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths


@pytest.fixture(autouse=True)
def _sandboxed_base(tmp_path, monkeypatch):
    """Redirect BASE_DIR → tmp_path. Every helper reads BASE_DIR lazily
    (no module-level caching), so this one knob sandboxes the whole
    tree."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    yield tmp_path


# ── Path shape ──────────────────────────────────────────────────────────────


class TestPathShape:
    """Every helper must produce the documented layout under BASE_DIR."""

    def test_bot_yaml_path(self, _sandboxed_base):
        p = paths.bot_yaml_path(1, "rsi_test")
        assert p == _sandboxed_base / "config" / "bots" / "1" / "rsi_test.yaml"

    def test_bot_log_path(self, _sandboxed_base):
        p = paths.bot_log_path(1, "rsi_test")
        assert p == _sandboxed_base / "logs" / "1" / "rsi_test.log"

    def test_bot_state_path(self, _sandboxed_base):
        p = paths.bot_state_path(1, "rsi_test")
        assert p == _sandboxed_base / "logs" / "1" / "rsi_test.state.json"

    def test_bot_pid_path(self, _sandboxed_base):
        p = paths.bot_pid_path(1, "rsi_test")
        assert p == _sandboxed_base / "logs" / "1" / "pids" / "rsi_test.pid"

    def test_bot_manual_trigger_path(self, _sandboxed_base):
        p = paths.bot_manual_trigger_path(1, "rsi_test")
        assert p == (
            _sandboxed_base / "logs" / "1" / "rsi_test.manual_trigger"
        )

    def test_user_fernet_key_path(self, _sandboxed_base):
        p = paths.user_fernet_key_path(1)
        assert p == _sandboxed_base / "keys" / "1.key"

    def test_uuid_creds_path(self, _sandboxed_base):
        uid_uuid = "deadbeef" * 4
        p = paths.uuid_creds_path(1, uid_uuid)
        assert p == _sandboxed_base / "credentials" / "1" / f"{uid_uuid}.enc"

    def test_user_ids_partition_cleanly(self, _sandboxed_base):
        """Two users land in separate trees — the core multi-tenant
        isolation invariant for filesystem paths."""
        assert paths.bot_yaml_path(1, "shared") != paths.bot_yaml_path(
            2, "shared",
        )
        assert paths.user_fernet_key_path(1) != paths.user_fernet_key_path(2)
        uid_uuid = "deadbeef" * 4
        assert paths.uuid_creds_path(1, uid_uuid) != paths.uuid_creds_path(
            2, uid_uuid,
        )


# ── Directory creation + permissions ───────────────────────────────────────


class TestDirCreation:
    """Helpers must create parents on demand so callers never have to
    `mkdir(parents=True)` by hand."""

    def test_user_bots_dir_created(self, _sandboxed_base):
        d = paths.user_bots_dir(1)
        assert d.exists() and d.is_dir()

    def test_user_logs_dir_created(self, _sandboxed_base):
        d = paths.user_logs_dir(1)
        assert d.exists() and d.is_dir()

    def test_user_pid_dir_created(self, _sandboxed_base):
        d = paths.user_pid_dir(1)
        assert d.exists() and d.is_dir()
        # Parent logs/<uid>/ must also exist.
        assert d.parent.exists()

    def test_user_credentials_dir_is_0700(self, _sandboxed_base):
        d = paths.user_credentials_dir(1)
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, f"credentials dir mode {oct(mode)}, expected 0700"

    def test_credentials_parent_dir_is_0700(self, _sandboxed_base):
        """Audit v24 LOW #4 regression guard: the PARENT ``credentials/``
        directory must also be 0700, not just the per-user children.
        Pre-fix, ``Path.mkdir(parents=True)`` created the parent with
        the system umask (0755 typically) because the ``mode`` argument
        only applies to the leaf. A 0755 parent leaks the user-id
        listing to any local user — the per-user content stays
        encrypted but the tenant set itself becomes visible."""
        paths.user_credentials_dir(1)
        parent = _sandboxed_base / "credentials"
        mode = parent.stat().st_mode & 0o777
        assert mode == 0o700, (
            f"credentials/ parent mode {oct(mode)}, expected 0o700 "
            f"(v24 LOW #4 regressed)"
        )

    def test_user_keys_dir_is_0700(self, _sandboxed_base):
        d = paths.user_keys_dir()
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, f"keys dir mode {oct(mode)}, expected 0700"


# ── Idempotence ────────────────────────────────────────────────────────────


class TestIdempotence:
    """Calling the same helper twice must not raise — the Phase-2 boot
    path relies on repeated mkdirs during the registry-refresh loop."""

    def test_user_bots_dir_twice(self, _sandboxed_base):
        a = paths.user_bots_dir(1)
        b = paths.user_bots_dir(1)
        assert a == b and a.exists()

    def test_user_keys_dir_twice(self, _sandboxed_base):
        a = paths.user_keys_dir()
        b = paths.user_keys_dir()
        assert a == b

    def test_user_credentials_dir_twice(self, _sandboxed_base):
        a = paths.user_credentials_dir(1)
        b = paths.user_credentials_dir(1)
        assert a == b


# ── ensure_secret_file_mode ────────────────────────────────────────────────


class TestSecretFileMode:

    def test_chmod_0600(self, tmp_path):
        f = tmp_path / "secret"
        f.write_bytes(b"shh")
        paths.ensure_secret_file_mode(f)
        mode = f.stat().st_mode & 0o777
        assert mode == 0o600

    def test_missing_file_is_noop(self, tmp_path):
        """Best-effort — a missing file must not raise OSError out
        of the helper. Callers use it right after a .tmp → dst
        rename where the path may briefly not exist."""
        paths.ensure_secret_file_mode(tmp_path / "does-not-exist")


# ── Cross-module smoke: credentials land under the sandboxed tree ──────────


class TestIntegrationWithCredentials:
    """A sanity check that monkey-patching BASE_DIR actually redirects
    the credentials module's writes — without this, tests elsewhere
    would silently write to the real keys/ + credentials/ trees."""

    def test_save_keys_lands_under_sandbox(self, _sandboxed_base, monkeypatch):
        from core import credentials

        # credentials.py caches its own _BASE_DIR at import time;
        # sandbox redirects it so keys/ + credentials/ land in tmp.
        monkeypatch.setattr(credentials, "_BASE_DIR", _sandboxed_base)

        uid_uuid = "deadbeef" * 4
        credentials.save_keys_by_uuid(
            uid_uuid, "bitget", "ak", "sc",
            user_id=7,
        )
        enc = paths.uuid_creds_path(7, uid_uuid)
        key = paths.user_fernet_key_path(7)

        assert enc.exists()
        assert key.exists()
        assert str(enc).startswith(str(_sandboxed_base))
        assert str(key).startswith(str(_sandboxed_base))


# ── Regression — relative path computation ─────────────────────────────────


class TestRelativeToBase:
    """After the Phase-2 migration, ``BotInfo.config_file`` stores
    config/bots/<user_id>/<slug>.yaml as a path relative to BASE_DIR.
    This mirrors what the registry loader produces — tests pinned the
    old flat shape, so re-verify the new one here."""

    def test_yaml_is_relative_to_base(self, _sandboxed_base):
        full = paths.bot_yaml_path(1, "rsi_test")
        rel = full.relative_to(_sandboxed_base)
        assert rel == Path("config/bots/1/rsi_test.yaml")


# ── PT-v4-FS-002: chmod-failure visibility ─────────────────────────────────


class TestEnsureDirChmodVisibility:
    """Pre-fix, ``_ensure_dir`` swallowed ``OSError`` from chmod with a
    bare ``except: pass``. Operators had no way to notice that a
    secret directory was sitting at the wrong mode. Post-fix, the
    failure logs at WARNING with the path + intended mode + error so
    the boot log surfaces permission drift."""

    def test_chmod_failure_logs_warning(
        self, _sandboxed_base, monkeypatch, caplog,
    ):
        target = _sandboxed_base / "warned"

        def boom(path, mode):
            raise OSError("simulated EPERM")

        monkeypatch.setattr(paths.os, "chmod", boom)

        with caplog.at_level(logging.WARNING, logger="core.paths"):
            paths._ensure_dir(target, mode=0o700)

        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "chmod" in m and str(target) in m and "simulated EPERM" in m
            for m in msgs
        ), msgs

    def test_chmod_success_no_warning(self, _sandboxed_base, caplog):
        with caplog.at_level(logging.WARNING, logger="core.paths"):
            paths._ensure_dir(_sandboxed_base / "ok")

        assert not any(
            "chmod" in r.getMessage() for r in caplog.records
        )


# ── PT-v4-FS-002: symlink refusal vs warn-and-continue ─────────────────────


class TestEnsureDirSymlinkPolicy:
    """Symlinks at security-critical directory paths are a
    permission-drift / deploy-error / symlink-attack signal.
    ``refuse_symlinks=True`` (keys/, credentials/) must raise so
    secrets never land on a redirected path; default warn-only keeps
    operator deploy-pain from blocking portal startup for
    non-critical dirs."""

    def test_refuse_symlinks_raises_on_symlink(self, _sandboxed_base):
        # Make the link target a real directory so the only thing
        # different from a normal happy path is the symlink itself.
        target = _sandboxed_base / "real_target"
        target.mkdir()
        link = _sandboxed_base / "linked"
        link.symlink_to(target)

        with pytest.raises(RuntimeError) as ei:
            paths._ensure_dir(link, mode=0o700, refuse_symlinks=True)

        msg = str(ei.value)
        assert "symlink" in msg.lower()
        assert str(link) in msg

    def test_warn_symlinks_continues(
        self, _sandboxed_base, caplog,
    ):
        target = _sandboxed_base / "real_target"
        target.mkdir()
        link = _sandboxed_base / "linked"
        link.symlink_to(target)

        with caplog.at_level(logging.WARNING, logger="core.paths"):
            result = paths._ensure_dir(link)

        assert result == link
        assert any(
            "symlink" in r.getMessage().lower()
            for r in caplog.records
        )

    def test_user_keys_dir_refuses_symlink(self, _sandboxed_base):
        # Pre-create keys/ as a symlink to a different location so the
        # helper must refuse rather than proceed.
        elsewhere = _sandboxed_base / "elsewhere"
        elsewhere.mkdir()
        (_sandboxed_base / "keys").symlink_to(elsewhere)

        with pytest.raises(RuntimeError):
            paths.user_keys_dir()

    def test_user_credentials_dir_refuses_symlink_at_parent(
        self, _sandboxed_base,
    ):
        # The parent ``credentials/`` is also security-critical — a
        # symlink there could redirect every user's encrypted blob.
        elsewhere = _sandboxed_base / "elsewhere_creds"
        elsewhere.mkdir()
        (_sandboxed_base / "credentials").symlink_to(elsewhere)

        with pytest.raises(RuntimeError):
            paths.user_credentials_dir(1)

    def test_user_credentials_dir_refuses_symlink_at_user_leaf(
        self, _sandboxed_base,
    ):
        # Per-user leaf: someone could swap ``credentials/<uid>/`` for
        # a link pointing into another user's tree. Refuse there too.
        (_sandboxed_base / "credentials").mkdir(mode=0o700)
        elsewhere = _sandboxed_base / "elsewhere_user"
        elsewhere.mkdir()
        (_sandboxed_base / "credentials" / "1").symlink_to(elsewhere)

        with pytest.raises(RuntimeError):
            paths.user_credentials_dir(1)

    def test_user_logs_dir_does_not_refuse_symlinks(
        self, _sandboxed_base, caplog,
    ):
        """Non-security-critical helpers stay at warn-and-continue —
        operator deploy-pain (e.g. ``logs/<uid>/`` symlinked to a
        larger partition) must not block portal startup. The leaf
        path must itself be a symlink for ``_ensure_dir`` to see it
        (``is_symlink`` checks the exact path, not its parents)."""
        (_sandboxed_base / "logs").mkdir()
        elsewhere = _sandboxed_base / "logs_elsewhere"
        elsewhere.mkdir()
        (_sandboxed_base / "logs" / "1").symlink_to(elsewhere)

        with caplog.at_level(logging.WARNING, logger="core.paths"):
            result = paths.user_logs_dir(1)

        # Did not raise — symlinks at non-critical paths warn only.
        assert result == _sandboxed_base / "logs" / "1"
        assert any(
            "symlink" in r.getMessage().lower()
            for r in caplog.records
        )


# ── PUB-v1-001: centralised path-leaf validation ──────────────────────────


# Inputs that must be rejected by _validate_safe_leaf. Each is a
# leaf string that, if interpolated verbatim into a path, would
# either escape the user-scoped directory or land on an
# unexpected file. The regex ^[A-Za-z0-9_-]+$ rejects every one;
# the separate "" / ".." / separator branches give a clearer
# error message for the common cases.
_UNSAFE_LEAVES = [
    "",                       # empty
    "..",                     # parent-directory token
    "../etc/passwd",          # classic path traversal
    "/etc/passwd",            # absolute path
    "sub/dir",                # embedded forward slash
    "sub\\dir",               # embedded backslash
    "with space",             # space — outside the safe class
    "with.dot",               # dot — outside the safe class (this
                              # is the realistic one: a slug that
                              # survives Starlette's [^/]+ converter
                              # but is not slugify output)
    "with%2Fslash",           # percent-encoded slash (% rejected)
    "trailing.",              # trailing dot
    ".hidden",                # leading dot
    "tab\tchar",              # control character
]


class TestPUBv1001LeafValidation:
    """Class-of-issue regression for PUB-v1-001 (LOW).

    core/paths.py is the architecture-designated single home for
    user-scoped path construction, but pre-fix it interpolated
    caller-supplied ``slug`` / ``credentials_uuid`` leaves
    verbatim and delegated validation to call sites — applied
    inconsistently (duplicate/import/admin guarded; config-read,
    config-update, export did not).

    Post-fix every leaf-taking helper calls ``_validate_safe_leaf``
    so the module itself is the chokepoint. These tests pin that
    behaviour against a parametrised battery of traversal-shaped
    and out-of-class inputs, and confirm legitimate slug / UUID
    leaves still resolve cleanly.
    """

    # ── The validator itself ───────────────────────────────────

    @pytest.mark.parametrize("bad", _UNSAFE_LEAVES)
    def test_validate_safe_leaf_rejects_unsafe(self, bad):
        with pytest.raises(ValueError, match="testfield"):
            paths._validate_safe_leaf(bad, field="testfield")

    @pytest.mark.parametrize("good", [
        "rsi_test", "rsi-test", "BTC_scalper_v2",
        "9fc8a8ff54104b7eb09cfaf87e5b0d32",  # uuid4().hex shape
        "a",                                  # single char
    ])
    def test_validate_safe_leaf_accepts_safe(self, good):
        # Must not raise.
        paths._validate_safe_leaf(good, field="testfield")

    def test_validate_safe_leaf_rejects_non_string(self):
        with pytest.raises(ValueError, match="non-empty string"):
            paths._validate_safe_leaf(None, field="slug")  # type: ignore[arg-type]

    def test_parent_token_gets_specific_message(self):
        with pytest.raises(ValueError, match="parent-directory"):
            paths._validate_safe_leaf("..", field="slug")

    def test_separator_gets_specific_message(self):
        with pytest.raises(ValueError, match="single path segment"):
            paths._validate_safe_leaf("a/b", field="slug")

    # ── Helpers that take a slug ───────────────────────────────
    #
    # The validator's full input battery is exercised once above
    # (test_validate_safe_leaf_rejects_unsafe). Each helper only
    # needs a single bad-leaf case to prove it actually CALLS the
    # validator — re-running all 12 inputs per helper would just
    # re-test the validator seven times over.

    _SLUG_HELPERS = [
        "bot_yaml_path", "bot_log_path", "bot_state_path",
        "bot_state_lock_path", "bot_pid_path",
        "bot_manual_trigger_path", "user_ml_results_path",
    ]

    @pytest.mark.parametrize("helper", _SLUG_HELPERS)
    def test_slug_helpers_reject_unsafe_slug(
        self, helper, _sandboxed_base,
    ):
        fn = getattr(paths, helper)
        with pytest.raises(ValueError, match="slug"):
            fn(1, "../etc/passwd")

    @pytest.mark.parametrize("helper", _SLUG_HELPERS)
    def test_slug_helpers_accept_canonical_slug(
        self, helper, _sandboxed_base,
    ):
        fn = getattr(paths, helper)
        result = fn(1, "rsi-test")
        # The slug survives into the filename verbatim.
        assert "rsi-test" in result.name
        # And the path stays inside the sandboxed BASE_DIR.
        assert str(result).startswith(str(_sandboxed_base))

    # ── uuid_creds_path ────────────────────────────────────────

    @pytest.mark.parametrize("bad", ["", "..", "../etc/passwd", "a/b"])
    def test_uuid_creds_path_rejects_unsafe_uuid(
        self, bad, _sandboxed_base,
    ):
        # Distinct from the slug helpers: the error must name the
        # ``credentials_uuid`` field, not ``slug``.
        with pytest.raises(ValueError, match="credentials_uuid"):
            paths.uuid_creds_path(1, bad)

    def test_uuid_creds_path_accepts_canonical_uuid(
        self, _sandboxed_base,
    ):
        uuid_hex = "9fc8a8ff54104b7eb09cfaf87e5b0d32"
        result = paths.uuid_creds_path(1, uuid_hex)
        assert result.name == f"{uuid_hex}.enc"
        assert str(result).startswith(str(_sandboxed_base))

    # ── purge_bot fast-fail ────────────────────────────────────

    def test_purge_bot_rejects_unsafe_slug_before_db_step(
        self, _sandboxed_base,
    ):
        """purge_bot runs its DB-delete transaction before the
        leaf-helper calls. The up-front guard must reject a bad
        slug before any DB work happens — so a ValueError, not a
        partial purge summary, comes back."""
        with pytest.raises(ValueError, match="slug"):
            paths.purge_bot(1, "../escape")

    def test_purge_bot_accepts_canonical_slug(self, _sandboxed_base):
        # A well-formed slug with nothing on disk + no DB rows is a
        # clean no-op that returns the summary dict (does not raise).
        summary = paths.purge_bot(1, "rsi-test")
        assert summary["files_removed"] == 0
        assert isinstance(summary["files_failed"], list)
