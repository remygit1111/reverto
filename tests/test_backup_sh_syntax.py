"""Regression tests for the scripts/backup.sh hardening PR
(PT-v4-EI-001 / EI-002 / EI-003).

Two layers:

* **Static** — `bash -n` parse + class-of-issue presence checks so a
  future edit that strips the EXIT trap, the concurrent-run flock,
  or the rotation-coordinated credentials copy fails loudly here
  instead of silently regressing the monitoring/atomicity contract.
* **Integration** — spawn the real `backup.sh` in an isolated tmp
  tree (same harness shape as `test_backup_manifest.py`) and prove
  the EI-002 trap actually stamps `.last_error` on a non-DB-missing
  failure, and that the EI-003 guard refuses a concurrent run
  *without* a false-positive `.last_error` stamp.

The integration tests set ``REVERTO_BACKUP_LOCK`` to a tmp path so
they never contend with the production ``/var/lock/reverto-backup.lock``
or with each other.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

_BACKUP_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
)


# ── Static checks ──────────────────────────────────────────────────────────


def test_backup_sh_syntax_valid():
    """backup.sh must pass `bash -n` (syntax-only parse)."""
    result = subprocess.run(
        ["bash", "-n", str(_BACKUP_SCRIPT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_backup_sh_has_exit_trap():
    """PT-v4-EI-002 regression: a `trap ... EXIT` must be registered
    so every non-zero exit stamps backups/.last_error."""
    text = _BACKUP_SCRIPT.read_text()
    assert "trap " in text and " EXIT" in text, (
        "PT-v4-EI-002 regression: scripts/backup.sh missing EXIT trap"
    )
    assert ".last_error" in text, (
        "PT-v4-EI-002 regression: EXIT trap no longer writes .last_error"
    )


def test_backup_sh_has_concurrent_run_guard():
    """PT-v4-EI-003 regression: a flock-based single-run guard must
    be present."""
    text = _BACKUP_SCRIPT.read_text()
    assert "flock" in text, (
        "PT-v4-EI-003 regression: scripts/backup.sh missing flock guard"
    )


def test_backup_sh_credentials_copy_is_rotation_coordinated():
    """PT-v4-EI-001 regression.

    NOTE on the deviation from the finding's literal suggested fix:
    the finding proposed "flock matching the lock the rotation flow
    already takes". The rotation in core/credentials.py does NOT lock
    credentials/<uid>/ — it takes an fcntl LOCK_EX|LOCK_NB advisory
    lock on an EPHEMERAL per-user sentinel keys/<uid>.key.lock
    (touch-on-entry / unlink-on-exit). There is no single stable lock
    path, core/credentials.py is out of scope for this PR, and a
    unilateral bash flock would be false safety. The implemented
    mitigation instead (a) waits, bounded, for any in-progress
    rotation to release its sentinel, then (b) stages the copy to a
    .tmp dir and atomically renames. This test pins those two
    properties rather than a (wrong) literal flock-before-cp.
    """
    lines = _BACKUP_SCRIPT.read_text().splitlines()
    text = "\n".join(lines)

    # (a) rotation-idle wait helper exists, references the rotation's
    #     own per-user sentinel, and is invoked before the copy.
    assert "_wait_for_rotation_idle" in text, (
        "PT-v4-EI-001 regression: rotation-idle wait helper removed"
    )
    assert "keys/*.key.lock" in text, (
        "PT-v4-EI-001 regression: no longer keys off the rotation "
        "sentinel keys/<uid>.key.lock"
    )

    # (b) credentials copy is staged + atomically renamed (no direct
    #     `cp -r credentials <dest>` that a restore could see half-done).
    assert "_stage_copy credentials" in text, (
        "PT-v4-EI-001 regression: credentials copy is no longer staged"
    )
    assert 'mv "${_tmp}" "${_dest}"' in text, (
        "PT-v4-EI-001 regression: staged copy no longer atomically "
        "renamed into place"
    )

    # The wait must be called before the credentials stage-copy.
    wait_call = next(
        i for i, ln in enumerate(lines)
        if ln.strip() == "_wait_for_rotation_idle"
    )
    cred_copy = next(
        i for i, ln in enumerate(lines)
        if "_stage_copy credentials" in ln
    )
    assert wait_call < cred_copy, (
        "PT-v4-EI-001 regression: rotation-idle wait must run before "
        "the credentials stage-copy"
    )


# ── Integration harness ────────────────────────────────────────────────────


def _has_flock_binary() -> bool:
    return shutil.which("flock") is not None


@pytest.fixture
def sandbox(tmp_path):
    """A tmp tree backup.sh can run in. Real backup.sh does
    `cd "$(dirname "$0")/.."`, so the copied script's parent-of-parent
    becomes the working root. Lock file is isolated via
    REVERTO_BACKUP_LOCK so these tests never touch /var/lock or race
    each other."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(_BACKUP_SCRIPT, scripts_dir / "backup.sh")
    (scripts_dir / "backup.sh").chmod(0o755)
    (tmp_path / "logs").mkdir()
    return tmp_path


def _run(root: Path, lock_path: Path):
    env = dict(os.environ)
    env["REVERTO_BACKUP_LOCK"] = str(lock_path)
    return subprocess.run(
        ["bash", str(root / "scripts" / "backup.sh")],
        cwd=root, capture_output=True, text=True, env=env, check=False,
    )


@pytest.mark.skipif(not _has_flock_binary(), reason="flock(1) not available")
def test_ei002_trap_stamps_last_error_on_non_db_missing_failure(sandbox):
    """PT-v4-EI-002: a corrupt source DB makes the sqlite3/.backup
    step fail — a path that pre-fix exited silently. The EXIT trap
    must stamp backups/.last_error with a timestamp + exit code."""
    # Present-but-corrupt DB → online-backup fails (CLI or py fallback).
    (sandbox / "logs" / "reverto.db").write_bytes(b"this is not a sqlite db")

    result = _run(sandbox, sandbox / "bk.lock")

    assert result.returncode != 0, (
        f"expected non-zero exit on corrupt DB; got 0\n{result.stdout}"
    )
    last_error = sandbox / "backups" / ".last_error"
    assert last_error.is_file(), (
        "PT-v4-EI-002 regression: backups/.last_error NOT stamped on a "
        f"sqlite-backup failure.\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    body = last_error.read_text()
    assert "backup.sh exited" in body, (
        f".last_error present but missing exit-code stamp: {body!r}"
    )


@pytest.mark.skipif(not _has_flock_binary(), reason="flock(1) not available")
def test_ei003_concurrent_run_refused_without_false_last_error(sandbox):
    """PT-v4-EI-003: when the lock is already held, backup.sh must
    exit non-zero AND must NOT stamp .last_error — the EXIT trap is
    registered *after* the flock guard precisely so a correctly
    declined concurrent run does not raise a false monitoring alarm.
    """
    lock_path = sandbox / "bk.lock"
    # Hold the lock from this process for the duration of the run.
    holder = open(lock_path, "w")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        # A valid DB so the ONLY possible failure is the lock guard.
        import sqlite3
        conn = sqlite3.connect(sandbox / "logs" / "reverto.db")
        conn.execute("CREATE TABLE _smoke (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        result = _run(sandbox, lock_path)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert result.returncode != 0, (
        "PT-v4-EI-003 regression: concurrent run was NOT refused "
        f"(rc=0).\nstdout:{result.stdout}"
    )
    assert "already running" in result.stderr, (
        f"expected 'already running' decline message; got: {result.stderr!r}"
    )
    last_error = sandbox / "backups" / ".last_error"
    assert not last_error.exists(), (
        "PT-v4-EI-003/EI-002 interaction regression: a correctly "
        "declined concurrent run must NOT stamp .last_error (the EXIT "
        "trap is armed only after the flock guard). Found: "
        f"{last_error.read_text() if last_error.exists() else ''!r}"
    )
