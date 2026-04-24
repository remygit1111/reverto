"""Regression guard for audit r1-032 + r1-007 username validation.

``core.user_store.validate_username`` is the single-source enforcer
for the safe-char allowlist. These tests pin the accept / reject
matrix so future signup paths (REST endpoint, admin CLI, etc.)
inherit a stable contract.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core.user_store import validate_username  # noqa: E402


class TestUsernameValidation:
    @pytest.mark.parametrize("bad", [
        "bad|user",         # audit-log delimiter
        "has space",        # whitespace
        "\tleading-tab",    # control char
        "trailing-nl\n",    # newline
        "ünicode",          # non-ASCII — we stick to ASCII for now
        "with/slash",       # URL delimiter
        "with\\backslash",  # path separator
        "<script>",         # HTML injection shape
        "",                 # empty
        "a" * 65,           # too long
        None,               # non-string
    ])
    def test_rejects_unsafe_username(self, bad):
        with pytest.raises(ValueError):
            validate_username(bad)

    @pytest.mark.parametrize("good", [
        "admin",
        "bob",
        "Alice_42",
        "carol-test",
        "d.e.f",
        "_underscore",
        "a",                # min length = 1
        "A" * 64,           # max length = 64
    ])
    def test_accepts_safe_username(self, good):
        validate_username(good)  # no raise
