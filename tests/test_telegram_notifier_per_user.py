"""Tests for the per-user TelegramNotifier wiring + safety overrides."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core import telegram_config_store  # noqa: E402


def _make_notifier(monkeypatch, user_id: int = 1):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    from notifications.telegram import TelegramNotifier
    return TelegramNotifier(user_id=user_id)


def _stub_send(notifier):
    """Drop-in stub that captures every send body without httpx I/O."""
    captured: list[str] = []
    notifier.send = lambda msg: captured.append(msg)
    return captured


class TestConstruction:

    def test_missing_token_raises(self, monkeypatch):
        # Force os.getenv to report no token — monkeypatch.delenv
        # isn't enough because notifications.telegram calls
        # load_dotenv() at import time and the .env on the dev
        # machine already populates TELEGRAM_BOT_TOKEN.
        import notifications.telegram as tg_mod
        monkeypatch.setattr(tg_mod.os, "getenv", lambda key, default=None: None)
        from notifications.telegram import TelegramNotifier
        with pytest.raises(ValueError):
            TelegramNotifier(user_id=1)

    def test_unconnected_user_disables_send(self, monkeypatch):
        n = _make_notifier(monkeypatch, user_id=1)
        assert n._enabled is False
        assert n.chat_id is None

    def test_connected_user_enables_send(self, monkeypatch):
        # Connect user 1 first.
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        n = _make_notifier(monkeypatch, user_id=1)
        assert n._enabled is True
        assert n.chat_id == "999"

    def test_chat_id_override_path(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        from notifications.telegram import TelegramNotifier
        n = TelegramNotifier(chat_id_override="123")
        assert n._enabled is True
        assert n.chat_id == "123"


class TestGracefulDegradation:

    def test_unconnected_notify_entry_silent(self, monkeypatch):
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_entry("MyBot", "BTC/USD", 60000.0, 0.001, 1)
        assert cap == []

    def test_unconnected_notify_error_silent(self, monkeypatch):
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_error("MyBot", "boom")
        assert cap == []


class TestSafetyOverrides:

    def test_error_always_sent_when_connected(self, monkeypatch):
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        # Lock notify_on to just ["startup"] — error is NOT in the
        # list yet must still fire.
        telegram_config_store.update_notify_on(1, ["startup"])
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_error("MyBot", "boom")
        assert len(cap) == 1
        assert "Error" in cap[0]

    def test_liq_warn_always_sent_when_connected(self, monkeypatch):
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        telegram_config_store.update_notify_on(1, ["startup"])
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_liquidation_warning(
            "MyBot", "BTC/USD", 60000.0, 50000.0, 5.0,
        )
        assert len(cap) == 1
        assert "LIQUIDATION" in cap[0]

    def test_shutdown_always_sent_when_connected(self, monkeypatch):
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        telegram_config_store.update_notify_on(1, ["startup"])
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_shutdown("MyBot")
        assert len(cap) == 1

    def test_safety_override_does_not_send_when_unconnected(
        self, monkeypatch,
    ):
        # The safety override only kicks in for *connected* users —
        # an unconnected user still no-ops the entire surface.
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_error("MyBot", "boom")
        n.notify_liquidation_warning(
            "MyBot", "BTC/USD", 60000.0, 50000.0, 5.0,
        )
        assert cap == []


class TestNotifyOnRespectsUserPref:

    def test_unticked_non_safety_event_suppressed(self, monkeypatch):
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        telegram_config_store.update_notify_on(1, ["startup"])
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        # tp_hit is NOT a safety event and NOT in notify_on.
        n.notify_take_profit("MyBot", "BTC/USD", 60000.0, 0.001, 1.0)
        assert cap == []

    def test_ticked_event_sends(self, monkeypatch):
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        telegram_config_store.update_notify_on(1, ["tp_hit"])
        n = _make_notifier(monkeypatch, user_id=1)
        cap = _stub_send(n)
        n.notify_take_profit("MyBot", "BTC/USD", 60000.0, 0.001, 1.0)
        assert len(cap) == 1

    def test_explicit_notify_on_override(self, monkeypatch):
        # Caller passes notify_on=[] explicitly — the safety override
        # still kicks in for ERROR, but tp_hit stays off.
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        from notifications.telegram import TelegramNotifier
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        n = TelegramNotifier(user_id=1, notify_on=[])
        cap = _stub_send(n)
        n.notify_take_profit("MyBot", "BTC/USD", 60000.0, 0.001, 1.0)
        n.notify_error("MyBot", "boom")
        assert len(cap) == 1  # only the error
        assert "Error" in cap[0]


class TestUserIdRequired:

    def test_construction_without_user_or_override_raises(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        from notifications.telegram import TelegramNotifier
        with pytest.raises(ValueError):
            TelegramNotifier()


class TestSendTouchesLastMessageAt:

    def test_touch_called_on_successful_send(self, monkeypatch):
        t = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(t, "999")
        # Stub the real httpx call to a 200.
        import notifications.telegram as tg_mod
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setattr(
            tg_mod.httpx, "post",
            lambda *a, **kw: MagicMock(status_code=200, text="{}"),
        )
        from notifications.telegram import TelegramNotifier
        TelegramNotifier(user_id=1).send("hello")
        # last_message_at should now be populated.
        cfg = telegram_config_store.get_config(1)
        assert cfg["last_message_at"] is not None
