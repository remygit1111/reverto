"""Live HIBP integration test — gated on RUN_INTEGRATION_TESTS=1.

This file hits the real ``api.pwnedpasswords.com`` endpoint. It stays
out of the default ``make test`` run (which must stay offline +
deterministic) and only fires when an operator explicitly sets the
env var:

    RUN_INTEGRATION_TESTS=1 .venv/bin/python -m pytest tests/integration/

Rationale: the unit tests in ``tests/test_password_breach.py`` cover
every internal branch with mocks. This file exists to catch one
specific regression — HIBP changing their wire format or endpoint —
which no mock can detect. Run it periodically (quarterly) or
whenever the HIBP module is non-trivially changed.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
))

import pytest

from core import password_breach

_INTEGRATION_GATE = os.environ.get("RUN_INTEGRATION_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _INTEGRATION_GATE,
    reason="Set RUN_INTEGRATION_TESTS=1 to run live HIBP integration tests.",
)


def test_password_literal_is_flagged_pwned():
    """The literal string 'password' has been in HIBP's corpus for
    years with a count in the millions. If this test starts failing,
    either (a) HIBP removed it (extremely unlikely), or (b) our
    module broke the protocol. Either case warrants investigation."""
    result = asyncio.run(password_breach.is_password_pwned("password"))
    assert result is True


def test_known_random_string_is_not_flagged():
    """A long randomly-typed string should not appear in HIBP. If
    this starts reporting True, the suspect is a false-positive in
    our parsing, not a real breach of this string."""
    result = asyncio.run(
        password_breach.is_password_pwned(
            "pwnchk_definitely_not_a_real_password_7f2a9d0c",
        ),
    )
    assert result is False
