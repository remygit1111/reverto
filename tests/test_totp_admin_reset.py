"""Tests for ``scripts/totp_admin_reset.py`` (audit pt-150).

The wrapper closes the audit-trail gap on the runbook-documented
SQL recovery procedure. The forensic contract is:

* Every successful reset writes a ``totp_admin_reset`` JSONL line
  to both ``logs/audit.jsonl`` (global) and
  ``logs/<user_id>/audit.jsonl`` (per-user split).
* The audit row is written BEFORE the DB UPDATE — failure-mode
  choice: a false-positive entry is preferable to a silent change.
* Validation refuses on user-not-found and on user-without-TOTP.
* The ``--reason`` field is captured verbatim in the JSONL row.

These tests import the script's ``main()`` directly + monkeypatch
``sys.argv`` rather than spawning a subprocess. Subprocess-based
testing would need to propagate the autouse ``_isolate_reverto_db``
fixture's tmp DB path through env-vars or a full IPC dance — the
import-path keeps the suite simple and lets pytest's monkeypatch
machinery isolate state cleanly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# scripts/ isn't a package — add it to sys.path so we can import
# the script the same way tests/test_setup_admin.py does.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import totp_admin_reset  # noqa: E402

from core import user_store  # noqa: E402
from core.database import get_db  # noqa: E402


_FAKE_ENCRYPTED_SEED = "fake-fernet-blob-for-test"


@pytest.fixture
def admin_with_totp():
    """Seed admin user with a fake encrypted TOTP seed so the
    wrapper's ``user.totp_enabled`` check passes. The exact blob
    contents do not matter — totp_enabled is just a NULL-check —
    so we use a sentinel rather than running a real Fernet
    encryption."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None, "admin row missing — fixture invariant"
    user_store.update_user_totp_seed(admin.id, _FAKE_ENCRYPTED_SEED)
    # Sanity: re-read so the test's view of the row matches what
    # the script will see.
    admin = user_store.get_user_by_username("admin")
    assert admin.totp_enabled is True
    return admin


@pytest.fixture
def admin_without_totp():
    """Admin row exists but TOTP is NOT enabled. Used to exercise
    the validation-refusal path."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    # Belt-and-braces: explicitly clear in case a previous test
    # left a seed behind (though _isolate_reverto_db should give
    # us a fresh DB).
    user_store.update_user_totp_seed(admin.id, None)
    return admin


@pytest.fixture
def audit_log_dir(tmp_path, monkeypatch):
    """Redirect the script's ``_LOG_DIR`` + ``paths.user_logs_dir``
    to a tmp directory so audit-log writes land where the test can
    inspect them, without touching the real ``logs/`` tree."""
    from core import paths as core_paths

    monkeypatch.setattr(totp_admin_reset, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(core_paths, "BASE_DIR", tmp_path)
    return tmp_path


def _run_main(*argv: str) -> int:
    """Invoke ``totp_admin_reset.main`` with the given CLI args.
    Returns the exit code."""
    sys_argv = ["totp_admin_reset.py", *argv]
    old_argv = sys.argv
    try:
        sys.argv = sys_argv
        return totp_admin_reset.main()
    finally:
        sys.argv = old_argv


# ── Validation paths ──────────────────────────────────────────────────────


class TestValidationRefusal:
    """The wrapper must refuse on missing user / missing TOTP. A
    silent no-op or a misleading success message would defeat the
    pt-150 audit-trail contract — operators rely on the script's
    own output to confirm the reset actually happened."""

    def test_user_not_found_returns_1_and_no_audit(
        self, audit_log_dir, capsys,
    ):
        rc = _run_main(
            "--username", "nonexistent_user", "--yes",
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err.lower()
        # No audit line should have landed.
        global_audit = audit_log_dir / "audit.jsonl"
        assert not global_audit.exists() or not global_audit.read_text(), (
            "validation-refusal path must not emit an audit row"
        )

    def test_user_without_totp_returns_1_and_no_audit(
        self, admin_without_totp, audit_log_dir, capsys,
    ):
        rc = _run_main("--username", "admin", "--yes")
        assert rc == 1
        err = capsys.readouterr().err
        assert "totp" in err.lower()
        # Same: no audit row for a refused reset.
        global_audit = audit_log_dir / "audit.jsonl"
        assert not global_audit.exists() or not global_audit.read_text()


# ── Confirmation prompt ───────────────────────────────────────────────────


class TestConfirmationPrompt:
    """Without ``--yes`` the script prompts the operator. A typo'd
    response (anything other than the literal 'yes') must cancel."""

    def test_typed_yes_proceeds(
        self, admin_with_totp, audit_log_dir, monkeypatch, capsys,
    ):
        responses = iter(["yes"])
        monkeypatch.setattr(
            "builtins.input", lambda *_a, **_k: next(responses),
        )
        rc = _run_main("--username", "admin")
        assert rc == 0
        out = capsys.readouterr().out
        assert "TOTP reset" in out

    def test_typed_no_cancels(
        self, admin_with_totp, audit_log_dir, monkeypatch, capsys,
    ):
        responses = iter(["no"])
        monkeypatch.setattr(
            "builtins.input", lambda *_a, **_k: next(responses),
        )
        rc = _run_main("--username", "admin")
        assert rc == 1
        out = capsys.readouterr().out
        assert "Cancelled" in out
        # Cancellation produces no audit row + no DB change.
        global_audit = audit_log_dir / "audit.jsonl"
        assert not global_audit.exists() or not global_audit.read_text()
        # DB still has the seed.
        admin = user_store.get_user_by_username("admin")
        assert admin.totp_enabled is True

    def test_yes_flag_skips_prompt(
        self, admin_with_totp, audit_log_dir, monkeypatch,
    ):
        # If input() is called we want the test to fail loudly —
        # --yes must NOT consult stdin.
        def _fail_input(*_a, **_k):
            pytest.fail(
                "--yes must skip the prompt; input() was called",
            )

        monkeypatch.setattr("builtins.input", _fail_input)
        rc = _run_main("--username", "admin", "--yes")
        assert rc == 0


# ── Audit-write-before-DB-update ordering (the pt-150 core) ────────────────


class TestAuditTrailOrdering:
    """The pt-150 forensic contract: every successful reset writes
    a ``totp_admin_reset`` audit-row BEFORE the DB UPDATE. A future
    refactor that reverses the order would re-open the finding —
    these tests pin the contract."""

    def test_global_audit_jsonl_gets_one_new_line(
        self, admin_with_totp, audit_log_dir,
    ):
        global_audit = audit_log_dir / "audit.jsonl"
        before = (
            global_audit.read_text().splitlines()
            if global_audit.exists() else []
        )

        rc = _run_main("--username", "admin", "--yes")
        assert rc == 0

        after = global_audit.read_text().splitlines()
        assert len(after) == len(before) + 1, (
            "exactly one new line must land in audit.jsonl"
        )
        new_entry = json.loads(after[-1])
        assert new_entry["action"] == "totp_admin_reset"
        assert new_entry["slug"] == "admin"
        assert new_entry["user_id"] == admin_with_totp.id
        assert new_entry["result"] == "ok"

    def test_per_user_audit_jsonl_also_gets_the_line(
        self, admin_with_totp, audit_log_dir,
    ):
        """Per-user split mirrors the dual-write contract that
        web/app.py::_audit produces — so per-tenant audit pulls
        (logs/<user_id>/audit.jsonl) include CLI-side resets the
        same way they include in-portal totp_disabled events."""
        rc = _run_main("--username", "admin", "--yes")
        assert rc == 0

        # The per-user split lives under logs/<user_id>/audit.jsonl
        # via paths.user_logs_dir(uid). The audit_log_dir fixture
        # rewires paths.BASE_DIR to tmp.
        user_audit = audit_log_dir / "logs" / str(admin_with_totp.id) / "audit.jsonl"
        assert user_audit.exists(), (
            f"per-user audit.jsonl missing at {user_audit}"
        )
        lines = user_audit.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "totp_admin_reset"

    def test_reason_recorded_verbatim(
        self, admin_with_totp, audit_log_dir,
    ):
        rc = _run_main(
            "--username", "admin",
            "--reason", "lost phone",
            "--yes",
        )
        assert rc == 0
        last = audit_log_dir.joinpath("audit.jsonl").read_text().splitlines()[-1]
        entry = json.loads(last)
        assert entry["reason"] == "lost phone", (
            "pt-150: --reason must be recorded verbatim in the "
            "audit row so a future investigator can read the "
            "operator's context."
        )

    def test_default_reason_is_admin_recovery(
        self, admin_with_totp, audit_log_dir,
    ):
        """Operators who forget --reason get a placeholder rather
        than an empty / null field, so the audit-row is never
        ambiguous."""
        rc = _run_main("--username", "admin", "--yes")
        assert rc == 0
        last = audit_log_dir.joinpath("audit.jsonl").read_text().splitlines()[-1]
        entry = json.loads(last)
        assert entry["reason"] == "admin_recovery"

    def test_audit_emitted_before_db_update(
        self, admin_with_totp, audit_log_dir, monkeypatch,
    ):
        """Hard pin on the ordering: monkeypatch
        ``update_user_totp_seed`` to raise. The audit row must
        already be on disk at the moment the DB update is
        attempted, so a crash mid-reset still leaves a (false-
        positive) audit trail. The reverse ordering would leave a
        silent successful reset with no trail — the failure mode
        we explicitly chose to avoid in pt-150's design."""
        global_audit = audit_log_dir / "audit.jsonl"
        captured = {"audit_existed_at_db_call": None}

        original_update = user_store.update_user_totp_seed

        def _spy_update(uid, blob):
            # Snapshot whether the audit row is already on disk
            # at the moment the DB write is attempted.
            captured["audit_existed_at_db_call"] = (
                global_audit.exists()
                and bool(global_audit.read_text().strip())
            )
            return original_update(uid, blob)

        monkeypatch.setattr(
            "totp_admin_reset.update_user_totp_seed",
            _spy_update,
        )

        rc = _run_main("--username", "admin", "--yes")
        assert rc == 0
        assert captured["audit_existed_at_db_call"] is True, (
            "pt-150: audit row must be on disk BEFORE the DB "
            "update — the wrapper's failure-mode choice. A future "
            "refactor that reverses this ordering re-opens pt-150."
        )


# ── DB state after reset ───────────────────────────────────────────────────


class TestDbStateAfterReset:
    """After a successful reset, ``users.totp_seed_encrypted`` is
    NULL in the DB and the user row's ``totp_enabled`` reads
    False. This is the OUTCOME the audit row records; tests pin
    that the wrapper actually performs the DB write rather than
    only emitting the audit."""

    def test_totp_seed_cleared_after_reset(
        self, admin_with_totp, audit_log_dir,
    ):
        rc = _run_main("--username", "admin", "--yes")
        assert rc == 0
        admin = user_store.get_user_by_username("admin")
        assert admin.totp_enabled is False
        # And the raw column is NULL.
        conn = get_db()
        row = conn.execute(
            "SELECT totp_seed_encrypted FROM users WHERE id = ?",
            (admin.id,),
        ).fetchone()
        assert row["totp_seed_encrypted"] is None
