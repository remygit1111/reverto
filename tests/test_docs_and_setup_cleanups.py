"""Regression guards for the docs-and-setup-cleanups PR (RHA-v1
rha-012 + rha-015, v26 v26-24, SaaS-readiness r1-061).

Code-level findings v26-07 (setup_admin empty-PW handling) and
r1-059 (.env.example boot-time cross-validation) were already
implemented in earlier PRs and have full regression coverage in
``tests/test_setup_admin.py`` and
``tests/test_config_validation.py`` respectively. These guards
are doc-content pins to keep the four documentation-side findings
from quietly regressing on a future copy-paste.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel_path: str) -> str:
    return (_REPO_ROOT / rel_path).read_text(encoding="utf-8")



def test_requirements_ml_comments_are_english():
    """rha-015: pre-fix line 1 of ``requirements-ml.txt`` was Dutch
    ("ML dependencies — niet nodig voor paper/live trading"). The
    surrounding ecosystem (requirements.txt, pyproject, every other
    dep file) is English; the lone Dutch comment was an outlier.
    Translate to English while keeping the v26-26 audit reference
    intact.

    PT-v4-r1-060 update: the human-edited comment block moved
    from ``requirements-ml.txt`` (now a pip-compile-generated
    lockfile whose comments are overwritten on every recompile)
    to ``requirements-ml.in`` (the intent file the operator
    edits). The invariants below are checked against BOTH so a
    future re-organisation of the dependency-file layout still
    catches a Dutch-fragment regression in whichever file holds
    the human prose."""
    contents = _read("requirements-ml.in") + _read("requirements-ml.txt")
    # Dutch markers that appeared in the original comment block.
    # If any of these surface again, somebody copy-pasted from
    # the pre-translation source.
    dutch_markers = [
        "niet nodig voor",
        "Installeer met",
        "pint alle shared",
        "Zonder deze constraint kan",
        "draait.",
    ]
    leftover = [m for m in dutch_markers if m in contents]
    assert leftover == [], (
        f"rha-015: requirements-ml.{{in,txt}} still contain "
        f"Dutch fragments: {leftover}. Comment block must be "
        "English to match the rest of the dependency-file "
        "ecosystem."
    )
    # The audit-cross-reference must survive translation. It now
    # lives in requirements-ml.in (the .txt is auto-generated and
    # pip-compile drops free-form .in comments — only the ``-c``
    # constraint line is preserved). Concatenating both above is
    # tolerant to a future reorganisation that moves it back.
    assert "v26-26" in contents, (
        "rha-015: the v26-26 audit cross-reference in the "
        "comment block must survive the NL→EN translation."
    )

