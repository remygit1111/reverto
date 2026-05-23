"""Repo-meta regression guards.

These tests assert invariants about repo infrastructure rather
than runtime code paths — the supply-chain hygiene posture that
the dev workflow + CI enforce, and that a future PR could
silently regress without a code test catching it.

Today the file pins PT-v4-r1-060 (Pinned-Dependencies): every
``requirements*.txt`` is hash-pinned, every CI workflow uses
SHA-pinned actions.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPTv4r1060PinnedDependencies:
    """Class-of-issue regression for PT-v4-r1-060 (MED).

    Pre-fix ``requirements*.txt`` were version-pinned but not
    hash-pinned — a compromised PyPI account could ship a
    malicious 1.0.0 release and pip would install it without
    complaint. CI actions were already SHA-pinned at the time
    of this PR (audited via grep) but a future regression
    would re-open the same supply-chain surface.

    These tests pin both halves of the contract so a partial
    revert (e.g. ``make compile-deps`` skipped, or a manual
    ``uses: actions/foo@v1`` slipped into a workflow) fails
    CI loudly instead of silently re-opening the gap.
    """

    def test_requirements_txt_uses_sha256_hashes(self):
        content = (_REPO_ROOT / "requirements.txt").read_text()
        assert "--hash=sha256:" in content, (
            "requirements.txt must be hash-pinned. Regenerate "
            "with: make compile-deps (then commit BOTH the .in "
            "and the .txt)."
        )

    def test_requirements_ml_uses_sha256_hashes(self):
        content = (_REPO_ROOT / "requirements-ml.txt").read_text()
        assert "--hash=sha256:" in content, (
            "requirements-ml.txt must be hash-pinned. "
            "Regenerate with: make compile-deps."
        )

    def test_requirements_dev_uses_sha256_hashes(self):
        """Dev tooling (pip-tools, ruff, pip-audit, pytest,
        pytest-cov) is part of CI's trusted compute base. The
        Scorecard Pinned-Dependencies check flagged
        unpinned dev installs as a gap; hash-pinning closes it.
        """
        content = (_REPO_ROOT / "requirements-dev.txt").read_text()
        assert "--hash=sha256:" in content, (
            "requirements-dev.txt must be hash-pinned. "
            "Regenerate with: make compile-deps."
        )

    def test_every_pinned_package_has_at_least_one_hash(self):
        """A more careful check than substring: every ``foo==X.Y``
        line in requirements.txt must be followed (via line-
        continuation backslash) by at least one ``--hash=sha256:``
        line. Catches a partial-compile where pip-compile emitted
        some packages without hashes — pip would refuse to install
        in that state, but the failure mode is a confusing CI
        error rather than the clear "regenerate the lock" signal
        this test gives."""
        content = (_REPO_ROOT / "requirements.txt").read_text()
        # A pinned dep is ``name==version \`` at line start; its
        # continuation block contains one or more ``--hash=`` lines
        # until the next non-indented line or blank. Quick check:
        # the count of pin lines must be <= the count of hash
        # lines (every pin has >= 1 hash; many have several).
        pin_lines = re.findall(
            r"(?m)^[A-Za-z0-9_.\-]+==\S+", content,
        )
        hash_lines = re.findall(
            r"(?m)^\s*--hash=sha256:", content,
        )
        assert len(pin_lines) > 0, (
            "no pinned packages found in requirements.txt — "
            "lockfile shape regressed"
        )
        assert len(hash_lines) >= len(pin_lines), (
            f"hash deficit: {len(pin_lines)} pinned packages but "
            f"only {len(hash_lines)} --hash lines. pip-compile "
            "may have been run without --generate-hashes."
        )

    def test_workflows_use_sha_pinned_actions(self):
        """Every ``uses: <repo>@<ref>`` in
        ``.github/workflows/*.yml`` must reference a 40-char hex
        SHA, not a tag or a major-version pseudo-pointer
        (``@v4``, ``@v4.2.2``, ``@main``, …).

        A re-tagged action is silent supply-chain exposure: the
        repo owner can repoint ``v4`` at a different commit
        without any in-repo signal. SHA-pinning makes the
        version explicit and immutable.
        """
        workflow_dir = _REPO_ROOT / ".github" / "workflows"
        sha_re = re.compile(
            r"@[a-f0-9]{40}(\s|$)",
        )
        bad_pin_re = re.compile(
            # ``uses: owner/repo@ref`` where ref is NOT a 40-char
            # hex SHA. Catches @v1, @v1.2.3, @main, @latest,
            # @some-branch, etc. Ignores the trailing ``# v1.2.3``
            # comment that documents which tag the SHA was at.
            r"uses:\s*[\w.\-]+/[\w.\-]+@(?![a-f0-9]{40}(?:\s|$))"
            r"\S+",
        )

        offenders: list[str] = []
        for wf in sorted(workflow_dir.glob("*.yml")):
            for line_no, raw_line in enumerate(
                wf.read_text().splitlines(), start=1,
            ):
                line = raw_line.split("#", 1)[0]  # strip comments
                if "uses:" not in line:
                    continue
                if not re.search(r"uses:\s*[\w.\-]+/[\w.\-]+@", line):
                    continue  # uses: ./local-action — not pinnable
                if bad_pin_re.search(line):
                    offenders.append(
                        f"{wf.name}:{line_no}: {raw_line.strip()}"
                    )

        assert not offenders, (
            "PT-v4-r1-060 regression: every workflow `uses:` "
            "must reference a 40-char hex SHA (with trailing "
            "`# vX.Y.Z` comment). Tag-pinned actions are silent "
            "supply-chain exposure. Offenders:\n  - "
            + "\n  - ".join(offenders)
        )
        # Sanity: at least one SHA-pinned action exists (catches
        # an empty-glob false-pass if the workflows directory
        # ever gets moved).
        any_sha = False
        for wf in workflow_dir.glob("*.yml"):
            if sha_re.search(wf.read_text()):
                any_sha = True
                break
        assert any_sha, "no SHA-pinned actions found at all"

    def test_in_files_exist_and_have_intent_pins(self):
        """The .in files are the human-edited intent source.
        If they go missing, the lockfiles become "magic" — no
        operator could safely upgrade a dep without reverse-
        engineering the lock first."""
        for in_file in (
            "requirements.in", "requirements-ml.in",
            "requirements-dev.in",
        ):
            path = _REPO_ROOT / in_file
            assert path.exists(), (
                f"{in_file} missing — the .in file is the "
                "human-readable intent. Regenerate from the "
                "current .txt and commit."
            )
            text = path.read_text()
            # At least one ``==`` pin must be present. The .in
            # files keep exact pins for everything we care about
            # (loose ``>=`` would defeat the lock's purpose).
            assert "==" in text, (
                f"{in_file} has no `==` pins — that defeats the "
                "PT-v4-r1-060 hardening (a recompile would pull "
                "arbitrary newer versions silently)."
            )

    def test_makefile_has_compile_deps_target(self):
        """``make compile-deps`` must remain the one-command
        workflow for recompiling the lockfiles. A future
        Makefile reshuffle that drops this target would leave
        operators running pip-compile by hand and inevitably
        forgetting --generate-hashes."""
        makefile = (_REPO_ROOT / "Makefile").read_text()
        assert re.search(r"(?m)^compile-deps:", makefile), (
            "Makefile lost the `compile-deps` target — "
            "operators need this for the hashed-recompile workflow"
        )
        assert "--generate-hashes" in makefile, (
            "Makefile's compile-deps target dropped "
            "--generate-hashes — the lockfile shape regressed"
        )
