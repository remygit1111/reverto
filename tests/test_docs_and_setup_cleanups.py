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


def test_readme_status_section_lists_phase_a_and_b():
    """rha-012: pre-fix README:4 said "Phase-1 live-trading scaffold"
    but README:74 said ``make live`` was "refused until Phase 3 lands"
    — a reader had to reconcile the two. Post-fix the README opens
    with a tightened tagline (the runner refuses real orders until
    Phase-3) AND a Status section that enumerates Foundation +
    Phase-3a + Phase A/B + Phase-3 prep + Phase C+ as separate
    milestones, so the live-mode-refusal is no longer a contradiction
    against an earlier optimistic claim."""
    readme = _read("README.md")
    assert "## Status" in readme, (
        "rha-012: README must carry a top-level ## Status section "
        "listing the actual phase milestones."
    )
    # Phase A + Phase B were both feature-complete by 2026-04-29
    # and must surface in the Status section so the README does
    # not undercount delivered work.
    assert "Phase A" in readme, (
        "rha-012: README Status section must mention Phase A "
        "(foundation wrap-up — feature-complete)."
    )
    assert "Phase B" in readme, (
        "rha-012: README Status section must mention Phase B "
        "(TOTP + per-user rate limit + cookie posture — feature-"
        "complete)."
    )
    # The tagline at the top must explicitly clarify that the
    # live-mode runner refuses real orders, so a reader hitting
    # README:1-5 and README:74 sees consistent claims.
    head = readme[: readme.find("## Status")]
    assert "refuses real orders" in head.lower() or "refused until" in head.lower(), (
        "rha-012: opening tagline must explicitly note that the "
        "Phase-1 live-trading scaffold's runner refuses real orders "
        "until Phase-3 lands — otherwise README:4 contradicts the "
        "Commands block at README:74."
    )


def test_requirements_ml_comments_are_english():
    """rha-015: pre-fix line 1 of ``requirements-ml.txt`` was Dutch
    ("ML dependencies — niet nodig voor paper/live trading"). The
    surrounding ecosystem (requirements.txt, pyproject, every other
    dep file) is English; the lone Dutch comment was an outlier.
    Translate to English while keeping the v26-26 audit reference
    intact."""
    contents = _read("requirements-ml.txt")
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
        f"rha-015: requirements-ml.txt still contains Dutch fragments: "
        f"{leftover}. Comment block must be English to match the rest "
        "of the dependency-file ecosystem."
    )
    # The audit-cross-reference must survive translation.
    assert "v26-26" in contents, (
        "rha-015: the v26-26 audit cross-reference in the comment "
        "block must survive the NL→EN translation."
    )


def test_phase3_md_strikethrough_on_obsolete_auth_json_claims():
    """v26-24: pre-fix the body of ``docs/phase-3.md`` §2 read in
    operative present tense ("password hashes blijven in
    `.auth.json`") even though Phase-3a moved everything into the
    ``users`` table. The audit's remediation said "prune or
    strike through". Body now carries explicit ``~~ ... ~~``
    markdown strikethrough on the worst-offender claims plus
    inline (v26-24) historical notes, so a reader skipping the
    section-level "Historical note" header still gets an
    unmistakable visual signal that the bullet is obsolete."""
    phase3 = _read("docs/phase-3.md")
    # The two body claims that Phase-3a most directly contradicts:
    # the §2 paragraph saying password hashes stay in .auth.json,
    # and the §2 paragraph saying logs/.auth.json gets per-user-
    # split. Both must now sit inside ~~ strikethrough markers.
    assert "~~password hashes + session-epoch blijven in" in phase3, (
        "v26-24: §2 'password hashes blijven in .auth.json' bullet "
        "must be wrapped in ~~ ... ~~ strikethrough — pre-fix it "
        "read as operative present-tense, contradicting Phase-3a."
    )
    assert "~~De huidige `logs/.auth.json`" in phase3, (
        "v26-24: §2 'De huidige logs/.auth.json wordt gesplitst' "
        "bullet must be wrapped in ~~ ... ~~ strikethrough — "
        "pre-fix it described a credential-split that Phase-3a "
        "made unnecessary."
    )
    # Inline (v26-24: historical) markers must surface so a grep
    # for the audit ID lands on the body claims, not just the
    # section headers.
    assert "_(v26-24:" in phase3, (
        "v26-24: at least one inline italic '(v26-24: historical "
        "...)' note must surface inside §2 so the audit-trail "
        "marker is body-level, not only header-level."
    )


def test_security_model_documents_dependency_pinning_acceptance():
    """r1-061: SaaS-readiness flagged transitive deps as non-blocking
    in CI. ACCEPTED-by-design for the current single-tenant deploy;
    pin the policy section + acceptance language so a future
    "tighten everything" sweep doesn't quietly delete the rationale
    and re-open the finding."""
    sec_model = _read("docs/security-model.md")
    # Section header.
    assert "Dependency-pinning policy" in sec_model, (
        "r1-061: security-model.md must carry a 'Dependency-pinning "
        "policy' section so the acceptance-by-design rationale is "
        "discoverable from the threat-model document, not buried "
        "in commit-message history."
    )
    # Explicit r1-061 + ACCEPTED-by-design framing.
    assert "r1-061" in sec_model, (
        "r1-061: the policy section must cross-reference the "
        "finding ID for grep-ability."
    )
    assert "ACCEPTED-by-design" in sec_model, (
        "r1-061: the acceptance must use the explicit "
        "'ACCEPTED-by-design' label so a tracker query can "
        "distinguish it from open / deferred findings."
    )
    # Phase-4 trigger for re-evaluation.
    assert "Phase-4" in sec_model or "Phase 4" in sec_model, (
        "r1-061: the acceptance must name a concrete trigger for "
        "re-evaluation (multi-tenant rollout) so 'ACCEPTED-by-"
        "design' does not silently drift into 'forgotten'."
    )
