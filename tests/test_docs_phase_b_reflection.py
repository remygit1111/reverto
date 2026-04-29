"""Regression guard for the docs/phase-b-reflection PR (RHA-v2
findings rhav2-010, rhav2-011, rhav2-012, rhav2-014).

Phase B shipped TOTP / per-user rate limit / session-epoch
semantics in code months before the user-facing docs caught up.
These tests pin the post-reflection content so a future doc-edit
that accidentally drops the relevant sections fails here instead
of silently regressing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel_path: str) -> str:
    return (_REPO_ROOT / rel_path).read_text(encoding="utf-8")


def test_core_docs_mention_totp():
    """rhav2-010 + rhav2-011: README and docs/architecture.md must
    mention TOTP / 2FA after Phase B feature-completion. Pre-fix
    both files predated the TOTP rollout entirely."""
    readme = _read("README.md")
    architecture = _read("docs/architecture.md")

    keywords = ["TOTP", "2FA", "two-factor", "authenticator"]

    readme_hits = [kw for kw in keywords if kw.lower() in readme.lower()]
    arch_hits = [kw for kw in keywords if kw.lower() in architecture.lower()]

    assert readme_hits, (
        "README has no mention of TOTP / 2FA / two-factor / "
        "authenticator. Phase B is feature-complete but the README "
        "still reads pre-Phase-B. rhav2-011 regression."
    )
    assert arch_hits, (
        "docs/architecture.md has no mention of TOTP / 2FA / "
        "two-factor / authenticator. rhav2-010 regression."
    )


def test_session_epoch_policy_documented():
    """rhav2-014: docs/security-model.md must carry an explicit
    session_epoch policy matrix (events that BUMP vs events that
    do NOT bump). Pre-fix the rules lived only in scattered code
    comments — readers had to grep ``bump_session_epoch`` call-
    sites to recover the rationale."""
    sec_model = _read("docs/security-model.md")

    assert "session_epoch" in sec_model, (
        "docs/security-model.md does not mention session_epoch — "
        "the per-user cookie-invalidation primitive is undocumented. "
        "rhav2-014 regression."
    )
    # The matrix uses BUMP / do NOT BUMP language; either casing
    # passes — but at least one explicit bump-vs-no-bump signal
    # must be present so a future edit can't silently drop the
    # matrix while leaving an incidental ``session_epoch`` mention.
    lower = sec_model.lower()
    assert "bump" in lower and "do not bump" in lower, (
        "session_epoch policy matrix missing the explicit "
        "BUMP / do NOT BUMP split. rhav2-014 regression."
    )
    # pt-130 must be cross-referenced so the doc stays anchored to
    # the findings tracker.
    assert "pt-130" in sec_model, (
        "security-model.md does not cross-reference PT-v3 pt-130 — "
        "the documented attack vector that motivated the matrix. "
        "rhav2-014 regression."
    )


def test_runbook_totp_recovery_english():
    """rhav2-012: the TOTP-recovery section in docs/runbook.md was
    written in Dutch in the original Phase B PR 3 deploy notes;
    every other operator section is English. Pin the English
    translation so a future copy-paste from a Dutch source doc
    regresses the file consistency."""
    runbook = _read("docs/runbook.md")

    assert "TOTP recovery" in runbook, (
        "docs/runbook.md no longer contains a 'TOTP recovery' "
        "section. rhav2-012 regression."
    )

    # Slice the TOTP-recovery section out so the assertion does not
    # false-positive on Dutch words that legitimately live in other
    # parts of the runbook (e.g. 'wanneer' inside Schema-migrations).
    start = runbook.find("## TOTP recovery")
    assert start != -1
    end = runbook.find("\n## ", start + 1)
    if end == -1:
        end = len(runbook)
    section = runbook[start:end].lower()

    # Dutch markers that appeared in the original section.
    # 'beveiliging' (security) and 'wanneer gebruiken' (when to
    # use) are unique enough that they would only return as
    # false-positives via a Dutch retranslation.
    dutch_markers = [
        "wanneer gebruiken",
        "beveiliging:",
        "vereist ssh-toegang",
        "verifieer dat user",
    ]
    leftover = [m for m in dutch_markers if m in section]
    assert leftover == [], (
        f"TOTP-recovery section contains Dutch leftover markers: "
        f"{leftover}. rhav2-012 regression — section should be "
        "English to match the rest of the runbook."
    )
