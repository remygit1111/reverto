"""Tests for core.telegram_config_store — link tokens + telegram_configs CRUD."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core import telegram_config_store  # noqa: E402
from core.database import get_db  # noqa: E402


def _seed_user(uid: int = 2, name: str = "bob_tg") -> int:
    """Add a non-admin user so cross-user tests have someone to
    compare against. The autouse fixture already seeds user_id=1
    via init_db()."""
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (id, username, role) VALUES (?, ?, 'user')",
            (uid, name),
        )
    return uid


class TestCreateLinkToken:

    def test_token_is_32_hex_chars(self):
        token = telegram_config_store.create_link_token(1)
        assert isinstance(token, str)
        assert len(token) == 32
        # Round-trip — only [0-9a-f].
        int(token, 16)

    def test_replaces_prior_unused_token(self):
        first = telegram_config_store.create_link_token(1)
        second = telegram_config_store.create_link_token(1)
        assert first != second
        # The first token row should have been deleted.
        conn = get_db()
        rows = conn.execute(
            "SELECT token FROM telegram_link_tokens WHERE user_id = 1",
        ).fetchall()
        assert [r["token"] for r in rows] == [second]

    def test_does_not_touch_used_token(self):
        first = telegram_config_store.create_link_token(1)
        # Mark used.
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE telegram_link_tokens SET used_at = ? "
                "WHERE token = ?",
                (datetime.now(timezone.utc).isoformat(), first),
            )
        second = telegram_config_store.create_link_token(1)
        # The used token is preserved (audit trail) — only the
        # unused one (if any) gets replaced.
        rows = conn.execute(
            "SELECT token FROM telegram_link_tokens "
            "WHERE user_id = 1 ORDER BY token",
        ).fetchall()
        tokens = {r["token"] for r in rows}
        assert first in tokens
        assert second in tokens


class TestConsumeLinkToken:

    def test_happy_path_returns_user_id(self):
        token = telegram_config_store.create_link_token(1)
        user_id = telegram_config_store.consume_link_token(token, "999")
        assert user_id == 1
        # The row is now used.
        conn = get_db()
        row = conn.execute(
            "SELECT used_at FROM telegram_link_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        assert row["used_at"] is not None
        # And a telegram_configs row was upserted.
        config = telegram_config_store.get_config(1)
        assert config is not None
        assert config["chat_id"] == "999"

    def test_already_used_returns_none(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        # Second consume of the same token.
        assert telegram_config_store.consume_link_token(token, "999") is None

    def test_unknown_token_returns_none(self):
        assert telegram_config_store.consume_link_token(
            "deadbeef" * 4, "999",
        ) is None

    def test_expired_token_returns_none(self):
        token = telegram_config_store.create_link_token(1)
        # Backdate expiry to 1 second ago.
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE telegram_link_tokens SET expires_at = ? "
                "WHERE token = ?",
                (past, token),
            )
        assert telegram_config_store.consume_link_token(token, "999") is None

    def test_preserves_existing_notify_on_on_relink(self):
        # First link → DEFAULT_NOTIFY_ON.
        token1 = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token1, "999")
        # Operator customises.
        telegram_config_store.update_notify_on(1, ["entry", "tp_hit"])
        # Relink — the new chat_id should land but the notify_on
        # preference stays.
        token2 = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token2, "888")
        cfg = telegram_config_store.get_config(1)
        assert cfg["chat_id"] == "888"
        assert sorted(cfg["notify_on"]) == ["entry", "tp_hit"]

    def test_cross_user_tokens_isolated(self):
        _seed_user(2)
        token1 = telegram_config_store.create_link_token(1)
        token2 = telegram_config_store.create_link_token(2)
        telegram_config_store.consume_link_token(token2, "user2-chat")
        # User 1's token is untouched.
        cfg1 = telegram_config_store.get_config(1)
        cfg2 = telegram_config_store.get_config(2)
        assert cfg1 is None
        assert cfg2["chat_id"] == "user2-chat"
        # And user 1's token can still be consumed independently.
        assert telegram_config_store.consume_link_token(
            token1, "user1-chat",
        ) == 1


class TestCleanupExpiredTokens:

    def test_deletes_only_expired(self):
        # Fresh token.
        fresh = telegram_config_store.create_link_token(1)
        # Expired token from a different user.
        _seed_user(2)
        expired_token = "ab" * 16
        conn = get_db()
        past = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        with conn:
            conn.execute(
                "INSERT INTO telegram_link_tokens "
                "(token, user_id, created_at, expires_at) "
                "VALUES (?, 2, ?, ?)",
                (expired_token, past, past),
            )
        deleted = telegram_config_store.cleanup_expired_tokens()
        assert deleted == 1
        # Fresh one still there.
        remaining = conn.execute(
            "SELECT token FROM telegram_link_tokens",
        ).fetchall()
        assert [r["token"] for r in remaining] == [fresh]

    def test_idempotent_on_empty_table(self):
        assert telegram_config_store.cleanup_expired_tokens() == 0


class TestGetConfig:

    def test_returns_none_when_not_connected(self):
        assert telegram_config_store.get_config(1) is None

    def test_returns_full_row_after_consume(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        cfg = telegram_config_store.get_config(1)
        assert cfg["chat_id"] == "999"
        assert cfg["user_id"] == 1
        # DEFAULT_NOTIFY_ON applied on a first-time connect.
        assert "entry" in cfg["notify_on"]
        assert "error" in cfg["notify_on"]


class TestUpdateNotifyOn:

    def test_validates_event_types(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        with pytest.raises(ValueError):
            telegram_config_store.update_notify_on(1, ["entry", "bogus"])

    def test_returns_false_when_no_row(self):
        # User 2 never connected.
        _seed_user(2)
        assert telegram_config_store.update_notify_on(2, ["entry"]) is False

    def test_dedupes_preserving_order(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        telegram_config_store.update_notify_on(
            1, ["entry", "tp_hit", "entry"],
        )
        cfg = telegram_config_store.get_config(1)
        assert cfg["notify_on"] == ["entry", "tp_hit"]


class TestIsConnected:

    def test_false_initially(self):
        assert telegram_config_store.is_connected(1) is False

    def test_true_after_consume(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        assert telegram_config_store.is_connected(1) is True


class TestDisconnect:

    def test_returns_true_on_existing_row(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        assert telegram_config_store.disconnect(1) is True
        assert telegram_config_store.get_config(1) is None

    def test_returns_false_when_no_row(self):
        assert telegram_config_store.disconnect(1) is False


class TestAllConnectedUserIds:

    def test_empty_returns_empty(self):
        assert telegram_config_store.all_connected_user_ids() == []

    def test_returns_every_connected_user(self):
        _seed_user(2)
        t1 = telegram_config_store.create_link_token(1)
        t2 = telegram_config_store.create_link_token(2)
        telegram_config_store.consume_link_token(t1, "a")
        telegram_config_store.consume_link_token(t2, "b")
        assert telegram_config_store.all_connected_user_ids() == [1, 2]


class TestTouchLastMessageAt:

    def test_updates_timestamp(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        telegram_config_store.touch_last_message_at(1)
        cfg = telegram_config_store.get_config(1)
        assert cfg["last_message_at"] is not None


class TestNotifyOnRoundtripJSON:

    def test_stored_as_json_array(self):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "999")
        conn = get_db()
        row = conn.execute(
            "SELECT notify_on FROM telegram_configs WHERE user_id = 1",
        ).fetchone()
        # The store writes JSON — round-trip parse confirms format.
        parsed = json.loads(row["notify_on"])
        assert isinstance(parsed, list)



# ───────────────────────────────────────────────────────────────────
# Regression tests for PUB-v1-003: no token prefix in logs
# ───────────────────────────────────────────────────────────────────
#
# consume_link_token() previously logged a token[:8] prefix on two
# failure paths (lookup miss, unparseable expires_at). These tests
# ensure no portion of the raw bearer token appears in log output,
# even when those failure paths fire.


class TestPubV1_003_NoTokenInLogs:
    """PUB-v1-003 regression: consume_link_token must not log any
    portion of the bearer token, even on failure paths."""

    def test_lookup_miss_does_not_log_token_prefix(self, caplog):
        """Miss-path must use sha256 correlator, not token prefix."""
        unknown_token = "a" * 32  # 32-char hex, never inserted

        with caplog.at_level("INFO"):
            result = telegram_config_store.consume_link_token(
                unknown_token, "12345",
            )

        assert result is None
        full_log = " ".join(rec.message for rec in caplog.records)
        # No portion of the raw bearer in logs
        assert unknown_token not in full_log
        assert unknown_token[:8] not in full_log
        # The non-reversible correlator IS expected to appear
        import hashlib
        expected = hashlib.sha256(unknown_token.encode()).hexdigest()[:12]
        assert expected in full_log

    def test_unparseable_expires_at_does_not_log_token_prefix(
        self, caplog,
    ):
        """Parse-fail-path must use user_id, not token prefix.

        Inserts a token row directly with a malformed expires_at so
        consume_link_token reaches the line-148 ValueError handler.
        """
        distinctive_token = "b" * 32
        target_user_id = 1  # The autouse fixture seeds this user

        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO telegram_link_tokens "
                "(token, user_id, expires_at, used_at) "
                "VALUES (?, ?, ?, ?)",
                (distinctive_token, target_user_id, "not-a-date", None),
            )

        with caplog.at_level("WARNING"):
            result = telegram_config_store.consume_link_token(
                distinctive_token, "99999",
            )

        assert result is None
        full_log = " ".join(rec.message for rec in caplog.records)
        # No portion of the raw bearer in logs
        assert distinctive_token not in full_log
        assert distinctive_token[:8] not in full_log
        # The user_id IS expected to appear
        assert str(target_user_id) in full_log
