"""Phase B PR 1 — TOTP foundation regression tests.

Anchors the contract of every helper in ``core.totp`` plus the
schema-migration that landed ``users.totp_seed_encrypted``. No
endpoint exercises this surface yet (PR 2 wires enrollment, PR 3
wires the login gate) — these tests are the only consumers in PR 1
and will keep the foundation honest as the higher PRs build on it.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import pyotp  # noqa: E402
from cryptography.fernet import InvalidToken  # noqa: E402

from core import credentials, paths  # noqa: E402
from core.database import SCHEMA_VERSION, get_db  # noqa: E402
from core.totp import (  # noqa: E402
    ALGORITHM,
    DIGITS,
    ISSUER,
    PERIOD_SECONDS,
    SECRET_BASE32_LEN,
    SECRET_BITS,
    SKEW_TOLERANCE_WINDOWS,
    decrypt_seed_for_user,
    encrypt_seed_for_user,
    generate_provisioning_uri,
    generate_recovery_token,
    generate_secret,
    verify_code,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Redirect the per-user Fernet keystore to a tmp tree.

    Matches the fixture in tests/test_credentials.py so the encryption
    round-trip tests don't touch the real ``keys/`` directory. The
    autouse DB-isolation fixture from conftest gives us a fresh
    ``logs/reverto.db`` per test independently.
    """
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    return tmp_path


# ── 1. Constants — pinned operator decisions ───────────────────────────────


class TestConstants:
    """The TOTP constants encode the operator decision from
    2026-04-28 (security-model.md sectie 6.1 + 6.2). A future PR
    that drops one of these values to chase compatibility with a
    quirky authenticator app would silently change the security
    contract — pin the values here so it can't happen unnoticed."""

    def test_issuer_is_reverto(self):
        assert ISSUER == "Reverto"

    def test_algorithm_is_sha1(self):
        """RFC 6238 default — widest authenticator-app compatibility."""
        assert ALGORITHM == "SHA1"

    def test_digits_is_six(self):
        assert DIGITS == 6

    def test_period_is_thirty_seconds(self):
        assert PERIOD_SECONDS == 30

    def test_skew_tolerance_is_one_window(self):
        """±30 s — half a window in either direction. Larger
        tolerances widen the brute-force surface; smaller ones break
        users with mild device-clock drift."""
        assert SKEW_TOLERANCE_WINDOWS == 1

    def test_secret_is_rfc6238_minimum(self):
        """160 bits = the RFC 6238 floor."""
        assert SECRET_BITS == 160
        assert SECRET_BASE32_LEN == 32


# ── 2. Secret generation ───────────────────────────────────────────────────


class TestSecretGeneration:

    def test_secret_length_matches_constant(self):
        assert len(generate_secret()) == SECRET_BASE32_LEN

    def test_secret_uses_base32_alphabet(self):
        """RFC 4648 base32: A-Z + 2-7, no padding. Authenticator apps
        reject anything outside this alphabet on enrollment."""
        secret = generate_secret()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
        assert set(secret).issubset(allowed)

    def test_secrets_are_unique(self):
        """Two consecutive calls must not return the same value.
        Property-style smoke test — collision would indicate the
        underlying RNG is busted, not just a coincidence."""
        seen = {generate_secret() for _ in range(50)}
        assert len(seen) == 50

    def test_secret_is_pyotp_compatible(self):
        """The generated secret must drive pyotp.TOTP without error
        — produce a 6-digit numeric code on demand."""
        secret = generate_secret()
        code = pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD_SECONDS).now()
        assert len(code) == DIGITS
        assert code.isdigit()


# ── 3. Provisioning URI ────────────────────────────────────────────────────


class TestProvisioningURI:

    def test_uri_has_otpauth_scheme(self):
        uri = generate_provisioning_uri(generate_secret(), "alice")
        assert uri.startswith("otpauth://totp/")

    def test_uri_carries_secret(self):
        secret = generate_secret()
        uri = generate_provisioning_uri(secret, "alice")
        assert f"secret={secret}" in uri

    def test_uri_carries_issuer_query_param(self):
        """Some authenticator apps read the path-prefix issuer, others
        read the query-param. Both must be present for cross-app
        compatibility."""
        uri = generate_provisioning_uri(generate_secret(), "alice")
        assert "issuer=Reverto" in uri

    def test_uri_carries_issuer_path_prefix(self):
        uri = generate_provisioning_uri(generate_secret(), "alice")
        # pyotp formats as otpauth://totp/<issuer>:<account>?...
        assert "/Reverto:" in uri

    def test_uri_includes_username(self):
        uri = generate_provisioning_uri(generate_secret(), "alice")
        assert "alice" in uri

    def test_uri_round_trip_via_pyotp_parser(self):
        """Strongest-form check: the URI we build must round-trip
        through pyotp.parse_uri back into a TOTP object that produces
        the same code as the original secret."""
        secret = generate_secret()
        uri = generate_provisioning_uri(secret, "alice")
        parsed = pyotp.parse_uri(uri)
        assert parsed.secret == secret
        assert parsed.digits == DIGITS
        assert parsed.interval == PERIOD_SECONDS


# ── 4. Code verification ───────────────────────────────────────────────────


class TestCodeVerification:

    def test_current_code_accepts(self):
        secret = generate_secret()
        code = pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD_SECONDS).now()
        assert verify_code(secret, code) is True

    def test_wrong_code_rejects(self):
        """A code that's not the current one (and not within ±1
        window) must be refused."""
        secret = generate_secret()
        # 000000 is virtually never the live code; the test is
        # statistically-flaky-by-design at ~1-in-1M odds. If it ever
        # fires, the regression file in our hands is the lottery ticket.
        assert verify_code(secret, "000000") is False

    def test_non_digit_code_rejects(self):
        secret = generate_secret()
        assert verify_code(secret, "abcdef") is False
        assert verify_code(secret, "12345a") is False
        assert verify_code(secret, "12 456") is False

    def test_short_code_rejects(self):
        secret = generate_secret()
        assert verify_code(secret, "12345") is False

    def test_long_code_rejects(self):
        secret = generate_secret()
        assert verify_code(secret, "1234567") is False

    def test_empty_secret_rejects(self):
        assert verify_code("", "123456") is False

    def test_empty_code_rejects(self):
        assert verify_code(generate_secret(), "") is False

    def test_previous_window_accepts_via_skew_tolerance(self):
        """A code from 30 s ago (one window back) must still verify
        — that's the clock-drift compensation. Use pyotp.at(t) for
        deterministic time control rather than time.sleep."""
        secret = generate_secret()
        previous = pyotp.TOTP(
            secret, digits=DIGITS, interval=PERIOD_SECONDS,
        ).at(time.time() - PERIOD_SECONDS)
        assert verify_code(secret, previous) is True

    def test_next_window_accepts_via_skew_tolerance(self):
        """A code from 30 s in the future also verifies. The skew
        tolerance is symmetric — covers a fast-running device clock
        as well as a slow-running one."""
        secret = generate_secret()
        next_window = pyotp.TOTP(
            secret, digits=DIGITS, interval=PERIOD_SECONDS,
        ).at(time.time() + PERIOD_SECONDS)
        assert verify_code(secret, next_window) is True

    def test_far_past_window_rejects(self):
        """A code from three windows back (90 s ago) is outside
        ±SKEW_TOLERANCE_WINDOWS and must be rejected."""
        secret = generate_secret()
        too_old = pyotp.TOTP(
            secret, digits=DIGITS, interval=PERIOD_SECONDS,
        ).at(time.time() - PERIOD_SECONDS * 3)
        assert verify_code(secret, too_old) is False


# ── 5. Encryption round-trip ───────────────────────────────────────────────


class TestEncryptionRoundTrip:

    def test_round_trip_recovers_original_secret(self, tmp_store):
        secret = generate_secret()
        encrypted = encrypt_seed_for_user(user_id=1, secret=secret)
        # Encrypted payload differs from plaintext (smoke test).
        assert encrypted != secret
        # And decryption returns the exact original.
        assert decrypt_seed_for_user(user_id=1, encrypted=encrypted) == secret

    def test_each_user_gets_distinct_ciphertext(self, tmp_store):
        """Same plaintext under user 1 and user 2 produces different
        ciphertexts — that's the per-user key isolation property."""
        secret = generate_secret()
        c1 = encrypt_seed_for_user(user_id=1, secret=secret)
        c2 = encrypt_seed_for_user(user_id=2, secret=secret)
        assert c1 != c2

    def test_user_2_cannot_decrypt_user_1_ciphertext(self, tmp_store):
        """Per-user key isolation: a user-2 key MUST NOT decrypt a
        user-1-encrypted blob. This is the same property exchange
        credentials rely on; we re-assert it for the TOTP path so a
        future refactor to a shared key gets caught here."""
        secret = generate_secret()
        encrypted_for_user_1 = encrypt_seed_for_user(user_id=1, secret=secret)
        # Force user 2's key to materialise on disk.
        encrypt_seed_for_user(user_id=2, secret="dummy")
        with pytest.raises(InvalidToken):
            decrypt_seed_for_user(
                user_id=2, encrypted=encrypted_for_user_1,
            )

    def test_tampered_ciphertext_raises(self, tmp_store):
        """Fernet authenticates the ciphertext — flipping a byte must
        surface as InvalidToken, not return garbage plaintext."""
        secret = generate_secret()
        encrypted = encrypt_seed_for_user(user_id=1, secret=secret)
        # Flip a character in the middle of the blob.
        tampered = encrypted[:-3] + ("A" if encrypted[-3] != "A" else "B") + encrypted[-2:]
        with pytest.raises(InvalidToken):
            decrypt_seed_for_user(user_id=1, encrypted=tampered)


# ── 6. Schema migration — totp_seed_encrypted column ──────────────────────


class TestSchemaTotpColumn:
    """Phase B PR 1 schema delta: ``users.totp_seed_encrypted``
    exists post-migration and reads NULL for the seeded admin row.
    The autouse conftest fixture runs init_db() on a fresh tmp DB
    before each test in this class, so the assertions reflect what
    a freshly-migrated install looks like."""

    def test_schema_version_is_nine(self):
        """SCHEMA_VERSION constant + PRAGMA user_version both
        reflect the v9 bump."""
        conn = get_db()
        stored = conn.execute("PRAGMA user_version").fetchone()[0]
        assert SCHEMA_VERSION == 9
        assert stored == 9

    def test_users_table_has_totp_seed_column(self):
        conn = get_db()
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        assert "totp_seed_encrypted" in cols

    def test_seeded_admin_row_has_null_totp_seed(self):
        """The ``INSERT OR IGNORE`` admin seed in
        ``_SCHEMA_STATEMENTS`` does not specify
        ``totp_seed_encrypted``, so it must default to NULL — i.e.
        a fresh install has zero users enrolled in TOTP, which is
        exactly the operator's intended starting state for Phase B."""
        conn = get_db()
        row = conn.execute(
            "SELECT totp_seed_encrypted FROM users WHERE id = 1",
        ).fetchone()
        assert row is not None
        assert row["totp_seed_encrypted"] is None


# ── 7. User dataclass — totp_enabled property ──────────────────────────────


class TestUserModelTotpEnabled:
    """The ``User.totp_enabled`` property is a thin convenience —
    most callers will read it directly. Pin its truth-table so a
    refactor that changes "is None" to "or empty string" can't slip
    through."""

    def test_totp_enabled_false_when_seed_is_none(self):
        from core.user import User
        u = User(id=1, username="admin")
        assert u.totp_enabled is False

    def test_totp_enabled_true_when_seed_is_set(self):
        from core.user import User
        u = User(id=1, username="admin", totp_seed_encrypted="cipher")
        assert u.totp_enabled is True

    def test_totp_enabled_round_trips_through_get_user_by_id(self):
        """The DB-lookup path must surface ``totp_seed_encrypted``
        on the User dataclass — not silently drop the column."""
        from core import user_store
        admin = user_store.get_user_by_id(1)
        assert admin is not None
        # Fresh DB: admin has not enrolled.
        assert admin.totp_enabled is False
        assert admin.totp_seed_encrypted is None


# ── 8. Recovery-token primitive (reserved for PR 2) ────────────────────────


class TestRecoveryToken:
    """``generate_recovery_token`` is reserved for PR 2's recovery-
    code generation. Pinned in PR 1 so PR 2 inherits the entropy
    contract without re-deciding it."""

    def test_token_is_hex_with_expected_length(self):
        token = generate_recovery_token()
        assert len(token) == 32
        assert all(c in "0123456789abcdef" for c in token)

    def test_tokens_are_unique(self):
        seen = {generate_recovery_token() for _ in range(50)}
        assert len(seen) == 50
