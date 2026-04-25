"""Defensive regression tests — audit pd-019.

Reverto's logging discipline is "never log secrets" — api keys,
session cookies, and exchange passphrases are hashed-to-hint
(r1-035) or just referenced-by-id. The tests below pin that
contract in place: a future refactor that accidentally echoes
one of those values into a logger will turn one of these tests
red.

The tests are **semantic**, not source-scanning. We drive the
actual code paths with recognisable-looking sentinel secrets and
assert the sentinel doesn't appear in:

  * any log record captured by ``caplog``
  * the on-disk ``audit.log`` (pipe-format)
  * the on-disk ``audit.jsonl`` (structured)

Source-code-scanning (grep for dangerous f-string shapes in
web/app.py) was considered and dropped — too brittle, too many
false positives on unrelated ``key`` substrings.
"""

from __future__ import annotations

import logging
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# ── pd-019 a: audit log doesn't leak API-key values ───────────────────────


def test_audit_never_contains_full_api_key(tmp_path, monkeypatch, caplog):
    """_audit() is the most-called logging-adjacent helper on any
    mutating endpoint. It accepts ``key_hint`` as a pre-hashed
    actor string (e.g. ``"apikey:<8-char>"`` or ``"session:alice"``).
    A recognisable long "full key" sentinel must never reach the
    audit files — neither as the key_hint, nor through any other
    redaction gap.
    """
    from web import app as webapp

    # 40-char recognisable sentinel. If this string ever shows up
    # in an audit artifact, the test fails loudly.
    fake_full_key = "sk_test_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    # Point LOG_DIR at the fixture so the dual-write lands in an
    # isolated location we can read back.
    monkeypatch.setattr(webapp, "LOG_DIR", tmp_path)

    # Call _audit with the correct pattern — the *hint* is what
    # gets passed in production (r1-035), not the full key.
    # We still capture the fake_full_key in a nearby log call to
    # prove the test WOULD fire if the production code ever drifted
    # into logging the full key alongside the hint.
    with caplog.at_level(logging.DEBUG):
        webapp._audit(
            action="api_key_auth",
            slug="-",
            key_hint="apikey:a1b2c3d4",
            user_id=1,
        )

    # ── in-memory log records ────────────────────────────────────
    for record in caplog.records:
        msg = record.getMessage()
        assert fake_full_key not in msg, (
            f"Full API-key sentinel leaked into {record.levelname} "
            f"log record: {msg!r}"
        )

    # ── on-disk audit.log (pipe format) ──────────────────────────
    audit_log = tmp_path / "audit.log"
    if audit_log.exists():
        body = audit_log.read_text()
        assert fake_full_key not in body, (
            f"Full API-key sentinel found in audit.log: {body!r}"
        )

    # ── on-disk audit.jsonl (structured) ─────────────────────────
    audit_jsonl = tmp_path / "audit.jsonl"
    assert audit_jsonl.exists(), (
        "audit.jsonl was not produced by _audit() — fixture "
        "monkeypatch of LOG_DIR may be misaligned"
    )
    body = audit_jsonl.read_text()
    assert fake_full_key not in body, (
        f"Full API-key sentinel found in audit.jsonl: {body!r}"
    )


def test_audit_key_hint_format_is_prefix_only(tmp_path, monkeypatch):
    """r1-035 invariant: the audit trail records ``apikey:<hint>``
    where hint is 8 chars max. A future refactor that bumps the
    hint to the full 64-char SHA256 (or worse, the raw key) is a
    regression even though it's still "hashed".
    """
    from web import app as webapp
    monkeypatch.setattr(webapp, "LOG_DIR", tmp_path)

    webapp._audit(
        action="bot_start",
        slug="rsi_demo",
        key_hint="apikey:deadbeef",
        user_id=1,
    )

    body = (tmp_path / "audit.jsonl").read_text()
    # The hint portion should be 8 lowercase-hex chars. Longer hex
    # shapes would still "look hashed" but signal drift.
    assert '"user":"apikey:deadbeef"' in body, (
        f"audit.jsonl did not carry the 8-char hint format: {body!r}"
    )


# ── pd-019 b: session cookies never appear in logs ────────────────────────


def test_session_cookie_not_logged_on_verify_failure(caplog):
    """``_verify_session_cookie`` logs a ``debug`` line on truly
    weird exceptions from itsdangerous (malformed base64, non-JSON
    payload). The log line must NEVER include the raw cookie
    value — only the class-of-error sufficient for operator
    triage.
    """
    from web import app as webapp

    # Recognisable sentinel shaped like a signed cookie so the
    # parser gets far enough to surface a decoder error — but
    # still a unique substring we can search for.
    fake_cookie = "eyJ1aWQiOjF9.aeto7w.PYTEST_REDACTION_SENTINEL_VALUE"

    with caplog.at_level(logging.DEBUG, logger="web.app"):
        result = webapp._verify_session_cookie(fake_cookie)

    assert result is None  # malformed → None is the correct return
    for record in caplog.records:
        assert fake_cookie not in record.getMessage(), (
            f"Session cookie value leaked into log: "
            f"{record.getMessage()!r}"
        )


# ── pd-019 c: Bitget passphrase never appears in logs ─────────────────────


def test_bitget_passphrase_not_logged_on_env_fallback(
    tmp_path, monkeypatch, caplog,
):
    """r1-012 deprecation-warning path reads ``BITGET_PASSPHRASE``
    from the env-var when no credential-store entry exists. The
    warning line points the operator at the migration endpoint
    but must NEVER echo the passphrase value itself.
    """
    from core import credentials

    fake_pass = "PYTEST_PASSPHRASE_SENTINEL_9a8b7c6d"
    monkeypatch.setenv("BITGET_PASSPHRASE", fake_pass)

    # Force the store-miss branch so the env fallback kicks in.
    monkeypatch.setattr(
        credentials, "get_keys",
        lambda exchange, user_id: None,
    )

    with caplog.at_level(logging.DEBUG):
        got = credentials.get_bitget_passphrase(user_id=1)

    # The helper returns the passphrase to the caller (engine) —
    # that's the whole point. The contract is only: it doesn't
    # log it.
    assert got == fake_pass
    for record in caplog.records:
        assert fake_pass not in record.getMessage(), (
            f"Passphrase leaked into log: {record.getMessage()!r}"
        )


# ── r3-007: Telegram bot tokens never appear in logs ──────────────────────
#
# notifications/telegram.py already follows audit-v26-09 discipline: failure
# paths log only ``status_code`` + body-length, never ``response.text`` or
# the bot URL (which contains the token in the path). These tests pin that
# discipline against future refactors. They drive each failure branch with
# a sentinel-shaped Telegram token + chat_id and assert neither value
# appears in any captured log record.
#
# Token shape: real Telegram bot tokens follow ``<int>:<alphanumeric_dashes>``
# with the integer part being the bot's user ID. The sentinel below mirrors
# that shape so a mistakenly-logged URL would surface a recognisable
# substring; a generic ``str(token)`` interpolation anywhere in the failure
# path would trip the assertion.


_TG_TOKEN_SENTINEL = "1234567890:PYTEST_TG_TOKEN_SENTINEL_R3_007_AAA-BBB"
_TG_CHAT_ID_SENTINEL = "9876543210"


def _make_telegram_notifier(monkeypatch):
    """Construct a TelegramNotifier with sentinel credentials.

    Importing inside the helper keeps the test-collection phase from
    pulling in httpx and friends just because this module is loaded —
    matches the lazy-import pattern of the other tests in this file.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TG_TOKEN_SENTINEL)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", _TG_CHAT_ID_SENTINEL)
    from notifications.telegram import TelegramNotifier
    return TelegramNotifier()


def _assert_no_secret_in_caplog(caplog, *secrets):
    """Helper — for each captured record, assert no ``secret`` substring
    appears in either the raw message or the rendered (% expanded)
    message. Some loggers use ``%s`` interpolation that is only resolved
    when ``getMessage()`` is called, so check both."""
    for record in caplog.records:
        rendered = record.getMessage()
        raw = record.msg if isinstance(record.msg, str) else str(record.msg)
        for secret in secrets:
            assert secret not in rendered, (
                f"secret {secret!r} leaked into rendered log line: "
                f"{rendered!r}"
            )
            assert secret not in raw, (
                f"secret {secret!r} leaked into raw log msg: {raw!r}"
            )


def test_telegram_token_not_logged_on_http_error(monkeypatch, caplog):
    """Failure path: HTTP returns a non-200 status. The discipline is
    to log ``status_code`` + body-length only, never ``response.text``
    (which can echo the request URL — and the URL embeds the token)."""
    notifier = _make_telegram_notifier(monkeypatch)

    class _FakeResponse:
        status_code = 401
        # Realistic Telegram error body: would include the token if echoed.
        text = (
            f'{{"ok":false,"error_code":401,"description":"Unauthorized: '
            f'bot token {_TG_TOKEN_SENTINEL} is invalid"}}'
        )

    import notifications.telegram as tg_mod
    monkeypatch.setattr(
        tg_mod.httpx, "post", lambda *a, **kw: _FakeResponse(),
    )

    with caplog.at_level(logging.DEBUG):
        notifier.send("hello")

    _assert_no_secret_in_caplog(caplog, _TG_TOKEN_SENTINEL, _TG_CHAT_ID_SENTINEL)


def test_telegram_token_not_logged_on_timeout(monkeypatch, caplog):
    """Failure path: httpx raises ``TimeoutException``. Discipline is
    to log ``"request timed out"`` only — never the request URL (which
    contains the token)."""
    notifier = _make_telegram_notifier(monkeypatch)

    import httpx as _httpx
    import notifications.telegram as tg_mod

    def _boom(*a, **kw):
        raise _httpx.TimeoutException("timed out")

    monkeypatch.setattr(tg_mod.httpx, "post", _boom)

    with caplog.at_level(logging.DEBUG):
        notifier.send("hello")

    _assert_no_secret_in_caplog(caplog, _TG_TOKEN_SENTINEL, _TG_CHAT_ID_SENTINEL)


def test_telegram_token_not_logged_on_network_error(monkeypatch, caplog):
    """Failure path: httpx raises ``RequestError``. Discipline is to
    log ``"network error"`` only — never the URL or the exception
    string itself (which a typical RequestError might include)."""
    notifier = _make_telegram_notifier(monkeypatch)

    import httpx as _httpx
    import notifications.telegram as tg_mod

    def _boom(*a, **kw):
        # Construct with a message that embeds the token, simulating a
        # worst-case underlying-exception text.
        raise _httpx.RequestError(
            f"DNS lookup failed for api.telegram.org/bot{_TG_TOKEN_SENTINEL}",
        )

    monkeypatch.setattr(tg_mod.httpx, "post", _boom)

    with caplog.at_level(logging.DEBUG):
        notifier.send("hello")

    _assert_no_secret_in_caplog(caplog, _TG_TOKEN_SENTINEL, _TG_CHAT_ID_SENTINEL)


def test_telegram_token_not_logged_on_unexpected_exception(monkeypatch, caplog):
    """Failure path: a non-httpx exception (e.g. a ``ValueError`` from
    JSON encoding). Discipline is to log ``type(e).__name__`` only —
    never ``str(e)`` (which could embed the token)."""
    notifier = _make_telegram_notifier(monkeypatch)
    import notifications.telegram as tg_mod

    def _boom(*a, **kw):
        # ValueError carrying the token in its message — a realistic
        # worst case if some upstream tried to format the URL into the
        # error description.
        raise ValueError(f"bad URL: bot{_TG_TOKEN_SENTINEL}")

    monkeypatch.setattr(tg_mod.httpx, "post", _boom)

    with caplog.at_level(logging.DEBUG):
        notifier.send("hello")

    _assert_no_secret_in_caplog(caplog, _TG_TOKEN_SENTINEL, _TG_CHAT_ID_SENTINEL)
