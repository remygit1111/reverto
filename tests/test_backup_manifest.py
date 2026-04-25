"""Regression test for backup-script manifest format — audit r3-008.

``scripts/backup.sh`` writes a ``MANIFEST.txt`` next to the snapshot
that operators read at restore time (``scripts/restore.sh`` echoes the
manifest contents during the plan-confirmation step). The manifest
must include the SQLite ``PRAGMA user_version`` of the backed-up DB
so an operator restoring an older snapshot onto newer code (or vice
versa) sees the schema-version mismatch BEFORE confirming.

The test is end-to-end: it spawns ``scripts/backup.sh`` in a temp
directory containing a fixture DB with a known ``user_version``,
then parses the resulting MANIFEST.txt and asserts the value.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# Path to the real backup script. The test invokes it as a subprocess
# so the bash semantics, path resolution, and external-tool dispatch
# (sqlite3 CLI vs Python fallback) all run as in production.
_BACKUP_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
)


@pytest.fixture
def isolated_reverto_root(tmp_path, monkeypatch):
    """Carve out a tmp directory that looks like a Reverto checkout
    just enough for backup.sh to run end-to-end. Real backup.sh does
    `cd "$(dirname "$0")/.."` so we copy the script into
    ``tmp_path/scripts/`` and run it from there.

    The script needs:
      * ``logs/reverto.db`` — the source DB.
      * ``logs/`` writable for the cron-log redirect (we don't use it
        but the dir must exist).
      * write access to ``backups/`` (created by the script itself).
    """
    # Copy backup.sh into the tmp tree so its `dirname/..` resolves
    # to tmp_path, not the real reverto repo.
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(_BACKUP_SCRIPT, scripts_dir / "backup.sh")
    (scripts_dir / "backup.sh").chmod(0o755)

    # logs/ holds the source DB.
    (tmp_path / "logs").mkdir()
    return tmp_path


def _seed_db(path: Path, schema_version: int) -> None:
    """Create a tiny SQLite DB at ``path`` with PRAGMA user_version
    set to ``schema_version``. Real backup.sh does an online ``.backup``
    so the source needs to be a valid SQLite file."""
    conn = sqlite3.connect(path)
    try:
        # PRAGMA user_version takes a literal int; can't be parameterised.
        conn.execute(f"PRAGMA user_version = {schema_version}")
        # One non-empty table so the .backup API has something to copy
        # — empty databases work too but a real schema is closer to
        # production shape.
        conn.execute("CREATE TABLE _smoke (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def _run_backup(reverto_root: Path) -> Path:
    """Invoke the copied backup.sh script. Returns the path to the
    fresh backup directory it produced."""
    result = subprocess.run(
        ["bash", str(reverto_root / "scripts" / "backup.sh")],
        cwd=reverto_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"backup.sh failed with rc={result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    backups = sorted((reverto_root / "backups").iterdir())
    # Filter out non-date dirs (none expected on first run).
    backups = [b for b in backups if re.match(r"^20\d\d-", b.name)]
    assert backups, "backup.sh produced no dated backup directory"
    return backups[-1]


def test_manifest_includes_schema_version(isolated_reverto_root):
    """Audit r3-008 — MANIFEST.txt must carry a ``Schema version: <int>``
    line so restore-time inspection surfaces schema drift."""
    db_path = isolated_reverto_root / "logs" / "reverto.db"
    _seed_db(db_path, schema_version=7)

    backup_dir = _run_backup(isolated_reverto_root)
    manifest_text = (backup_dir / "MANIFEST.txt").read_text()

    assert "Schema version:" in manifest_text, (
        f"MANIFEST.txt missing 'Schema version:' line. Full content:\n"
        f"{manifest_text}"
    )

    # Extract the value and confirm it parses as the expected integer.
    match = re.search(
        r"^Schema version:\s*(\S+)\s*$",
        manifest_text,
        re.MULTILINE,
    )
    assert match, (
        f"'Schema version:' line malformed: {manifest_text!r}"
    )
    assert match.group(1) == "7", (
        f"expected Schema version=7, got {match.group(1)!r}; "
        f"manifest:\n{manifest_text}"
    )


def test_manifest_schema_version_for_fresh_db(isolated_reverto_root):
    """A fresh SQLite DB has ``PRAGMA user_version = 0`` by default.
    The manifest must carry ``Schema version: 0`` (NOT ``unknown``)
    when the probe succeeds — distinguishes "schema is genuinely v0"
    from "probe failed and we guessed"."""
    db_path = isolated_reverto_root / "logs" / "reverto.db"
    _seed_db(db_path, schema_version=0)

    backup_dir = _run_backup(isolated_reverto_root)
    manifest_text = (backup_dir / "MANIFEST.txt").read_text()

    match = re.search(
        r"^Schema version:\s*(\S+)\s*$",
        manifest_text,
        re.MULTILINE,
    )
    assert match, (
        f"'Schema version:' line malformed: {manifest_text!r}"
    )
    assert match.group(1) == "0", (
        f"expected Schema version=0 (fresh DB default), got "
        f"{match.group(1)!r}; manifest:\n{manifest_text}"
    )
