"""Regression tests for core.cleanup — audit pd-044.

The atomic-write pattern leaves ``<target>.tmp`` orphans on an
ungraceful crash. ``cleanup_orphaned_tmp_files`` is the startup
hook that sweeps them out. Tests pin the three properties that
matter operationally:

  1. Only ``.tmp`` files are removed; sibling files are preserved.
  2. Missing directories never raise — boot must not fail over an
     uncreated path.
  3. A per-file unlink failure never halts the rest of the sweep.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cleanup import cleanup_orphaned_tmp_files  # noqa: E402


def test_cleanup_removes_tmp_files_recursively(tmp_path):
    """Walk the whole tree, including subdirs — atomic-write sites
    are spread across logs/<uid>/ and credentials/<uid>/ so a
    top-level-only sweep would miss most orphans."""
    (tmp_path / "top.tmp").write_text("x")
    (tmp_path / "keep.json").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.tmp").write_text("x")
    (sub / "keep.log").write_text("x")

    removed = cleanup_orphaned_tmp_files(tmp_path)

    assert removed == 2
    assert not (tmp_path / "top.tmp").exists()
    assert not (sub / "nested.tmp").exists()
    # Siblings untouched — only *.tmp in scope.
    assert (tmp_path / "keep.json").exists()
    assert (sub / "keep.log").exists()


def test_cleanup_handles_missing_dir_silently(tmp_path):
    """Boot must not fail over a not-yet-created directory. The
    helper returns 0 without raising so the lifespan hook can be
    called before any write-site has materialised its dir tree.
    """
    nonexistent = tmp_path / "does-not-exist"
    removed = cleanup_orphaned_tmp_files(nonexistent)
    assert removed == 0
    assert not nonexistent.exists()  # Not created as a side-effect.


def test_cleanup_handles_multiple_directories(tmp_path):
    """Passing multiple dirs walks each one; counts aggregate."""
    d1 = tmp_path / "logs"
    d1.mkdir()
    (d1 / "a.tmp").write_text("x")
    d2 = tmp_path / "credentials"
    d2.mkdir()
    (d2 / "b.tmp").write_text("x")
    (d2 / "c.tmp").write_text("x")

    removed = cleanup_orphaned_tmp_files(d1, d2)

    assert removed == 3
    assert not any(d1.iterdir())
    assert not any(d2.iterdir())


def test_cleanup_continues_on_unlink_failure(tmp_path, monkeypatch):
    """If one file is unreadable/unremovable, the sweep logs at
    DEBUG and moves on. Counter reflects only the files that
    actually came off disk."""
    (tmp_path / "ok1.tmp").write_text("x")
    (tmp_path / "bad.tmp").write_text("x")
    (tmp_path / "ok2.tmp").write_text("x")

    original_unlink = Path.unlink

    def mock_unlink(self, *args, **kwargs):
        if self.name == "bad.tmp":
            raise OSError("simulated permission denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    removed = cleanup_orphaned_tmp_files(tmp_path)

    assert removed == 2
    # The two OK files are gone; the problem child remains.
    assert not (tmp_path / "ok1.tmp").exists()
    assert not (tmp_path / "ok2.tmp").exists()
    assert (tmp_path / "bad.tmp").exists()


def test_cleanup_ignores_tmp_shaped_directories(tmp_path):
    """``foo.tmp/`` as a directory (unlikely but legal on POSIX)
    must be skipped — ``Path.unlink`` on a directory raises
    ``IsADirectoryError`` and we don't want that to masquerade as
    a normal per-file failure."""
    dir_with_tmp_suffix = tmp_path / "weird.tmp"
    dir_with_tmp_suffix.mkdir()
    (tmp_path / "real.tmp").write_text("x")

    removed = cleanup_orphaned_tmp_files(tmp_path)

    assert removed == 1
    assert not (tmp_path / "real.tmp").exists()
    assert dir_with_tmp_suffix.exists() and dir_with_tmp_suffix.is_dir()


def test_cleanup_summary_log_only_when_nonzero(tmp_path, caplog):
    """Silent when nothing is cleaned up — avoids noise on a
    healthy startup that just rebooted the portal cleanly. INFO
    line only when the sweep actually removed something so log-
    readers notice orphan accumulation."""
    # Silent case.
    with caplog.at_level(logging.INFO, logger="core.cleanup"):
        cleanup_orphaned_tmp_files(tmp_path)
    assert not any(
        "orphaned .tmp" in rec.getMessage().lower()
        for rec in caplog.records
    )

    # Non-silent case.
    caplog.clear()
    (tmp_path / "a.tmp").write_text("x")
    with caplog.at_level(logging.INFO, logger="core.cleanup"):
        cleanup_orphaned_tmp_files(tmp_path)
    assert any(
        "cleaned up 1 orphaned" in rec.getMessage().lower()
        for rec in caplog.records
    )
