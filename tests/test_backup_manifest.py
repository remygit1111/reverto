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


# ── PT-v4-EI-004 — backup must capture per-bot state + configs ──────────────


def _seed_state_files(reverto_root: Path) -> list[Path]:
    """Create representative ``logs/<uid>/<slug>.state.json`` files
    so the backup test can verify they survive into the tarball.
    Returns the list of seeded source paths (relative to root) for
    cross-reference in assertions.
    """
    sources: list[Path] = []
    for uid in (1, 7):
        user_dir = reverto_root / "logs" / str(uid)
        user_dir.mkdir(parents=True, exist_ok=True)
        state_path = user_dir / f"bot_a_{uid}.state.json"
        state_path.write_text(
            '{"bot_name": "bot_a_'
            + str(uid)
            + '", "balance_btc": 0.1}\n'
        )
        sources.append(state_path)
    return sources


def _seed_bot_yamls(reverto_root: Path) -> list[Path]:
    """Create representative ``config/bots/<uid>/<slug>.yaml`` files
    matching the production layout."""
    sources: list[Path] = []
    for uid in (1, 7):
        bots_dir = reverto_root / "config" / "bots" / str(uid)
        bots_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = bots_dir / f"bot_a_{uid}.yaml"
        yaml_path.write_text("bot:\n  name: bot_a_" + str(uid) + "\n")
        sources.append(yaml_path)
    return sources


def test_backup_includes_state_json_files(isolated_reverto_root):
    """PT-v4-EI-004: per-bot state.json files must land in the backup
    tarball at the same logs/<uid>/<slug>.state.json layout so a
    restore can drop them straight back into the source tree."""
    db_path = isolated_reverto_root / "logs" / "reverto.db"
    _seed_db(db_path, schema_version=4)
    seeded = _seed_state_files(isolated_reverto_root)

    backup_dir = _run_backup(isolated_reverto_root)

    # Each seeded state file must exist under the backup at the same
    # relative path. ``cp --parents`` in backup.sh preserves the
    # logs/<uid>/ structure inside BACKUP_DIR.
    for src in seeded:
        rel = src.relative_to(isolated_reverto_root)
        assert (backup_dir / rel).exists(), (
            f"backup missing {rel}; backup tree:\n"
            + "\n".join(
                str(p.relative_to(backup_dir))
                for p in backup_dir.rglob("*") if p.is_file()
            )
        )
        # Content round-trips byte-for-byte.
        assert (backup_dir / rel).read_text() == src.read_text()


def test_backup_includes_config_bots_dir(isolated_reverto_root):
    """PT-v4-EI-004: config/bots/<uid>/<slug>.yaml must land in the
    backup so a restored DB doesn't reference YAML files that aren't
    on disk."""
    db_path = isolated_reverto_root / "logs" / "reverto.db"
    _seed_db(db_path, schema_version=4)
    seeded = _seed_bot_yamls(isolated_reverto_root)

    backup_dir = _run_backup(isolated_reverto_root)

    backed_up_bots = backup_dir / "config" / "bots"
    assert backed_up_bots.is_dir(), (
        "config/bots/ missing from backup; backup tree:\n"
        + "\n".join(
            str(p.relative_to(backup_dir))
            for p in backup_dir.rglob("*") if p.is_file()
        )
    )
    for src in seeded:
        rel = src.relative_to(isolated_reverto_root / "config")
        assert (backup_dir / "config" / rel).exists(), (
            f"backup missing config/{rel}"
        )
        assert (backup_dir / "config" / rel).read_text() == src.read_text()


def test_backup_skips_state_section_when_no_bots(isolated_reverto_root):
    """A fresh install has no logs/<uid>/ subdirs and no config/bots/
    directory. The backup must still succeed — the new EI-004 logic
    short-circuits both copies and produces a backup with just the DB
    + manifest, exactly as before. Regression guard so a future
    refactor doesn't make the new copies mandatory."""
    db_path = isolated_reverto_root / "logs" / "reverto.db"
    _seed_db(db_path, schema_version=4)

    backup_dir = _run_backup(isolated_reverto_root)
    assert (backup_dir / "reverto.db").exists()
    assert (backup_dir / "MANIFEST.txt").exists()
    # No state.json files → no logs/<uid>/ subdirs in backup tree.
    state_jsons = list(backup_dir.rglob("*.state.json"))
    assert state_jsons == [], (
        f"unexpected state.json files in fresh-install backup: "
        f"{state_jsons}"
    )
    assert not (backup_dir / "config" / "bots").exists(), (
        "config/bots/ should not exist in backup when source has none"
    )
