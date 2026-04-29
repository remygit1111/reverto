"""Regression guard for audit r1-031 — audit-log JSONL dual-write.

``_audit`` emits:
  * the legacy pipe-delimited line to logs/audit.log
  * a JSONL record to logs/audit.jsonl
  * (when ``user_id`` is passed) a second JSONL record to
    logs/<user_id>/audit.jsonl
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core import paths  # noqa: E402
from web import app as webapp  # noqa: E402


@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    """Redirect LOG_DIR + paths.BASE_DIR so audit writes land under
    tmp. Covers both the global audit.jsonl path (LOG_DIR) and the
    per-user split path (paths.user_logs_dir)."""
    monkeypatch.setattr(webapp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    return tmp_path


def _read_last_jsonl(path):
    content = path.read_text().strip().splitlines()
    assert content, f"{path} is empty"
    return json.loads(content[-1])


def test_audit_writes_jsonl_alongside_pipe(tmp_logs):
    webapp._audit("test_action", "test_slug", "session:alice")
    jsonl = tmp_logs / "audit.jsonl"
    assert jsonl.exists()
    entry = _read_last_jsonl(jsonl)
    assert entry["action"] == "test_action"
    assert entry["slug"] == "test_slug"
    assert entry["user"] == "session:alice"
    # user_id absent in legacy-style call; request_id is the
    # context-var sentinel since we're outside an HTTP request.
    assert entry["user_id"] is None
    assert entry["request_id"] == "-"


def test_audit_per_user_split_when_user_id_given(tmp_logs):
    webapp._audit(
        "bot_start", "rsi_test", "session:alice", user_id=42,
    )
    global_jsonl = tmp_logs / "audit.jsonl"
    user_jsonl = tmp_logs / "logs" / "42" / "audit.jsonl"
    assert global_jsonl.exists()
    assert user_jsonl.exists()
    g = _read_last_jsonl(global_jsonl)
    u = _read_last_jsonl(user_jsonl)
    assert g == u
    assert g["user_id"] == 42


def test_audit_global_still_fires_when_user_split_fails(
    tmp_logs, monkeypatch,
):
    # Force user_logs_dir to raise so we can verify the global
    # write still lands — per-user is best-effort.
    def _boom(_uid):
        raise OSError("simulated failure")
    monkeypatch.setattr(paths, "user_logs_dir", _boom)
    webapp._audit("x", "y", "session:bob", user_id=7)
    global_jsonl = tmp_logs / "audit.jsonl"
    assert global_jsonl.exists()
    entry = _read_last_jsonl(global_jsonl)
    assert entry["user_id"] == 7


# ── Hotfix: route handlers must propagate user_id ──────────────────────────


def test_auth_login_audit_fires_per_user_split(tmp_logs):
    """Hotfix guard: the auth_login audit call must land in
    logs/<uid>/audit.jsonl so per-user split actually triggers.
    Drives the full login flow end-to-end and then checks the
    per-user file exists + contains the login entry.
    """
    from fastapi.testclient import TestClient
    from core import user_store

    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, "hotfix-pw-r1031-login")

    client = TestClient(webapp.app)
    r = client.post(
        "/auth/login",
        json={"username": "admin", "password": "hotfix-pw-r1031-login"},
    )
    assert r.status_code == 200, r.text

    user_jsonl = tmp_logs / "logs" / str(admin.id) / "audit.jsonl"
    assert user_jsonl.exists(), (
        "per-user audit.jsonl not written — route handler didn't "
        "propagate user_id into _audit()"
    )
    entry = _read_last_jsonl(user_jsonl)
    assert entry["action"] == "auth_login"
    assert entry["user_id"] == admin.id


# ── Phase-A wrap-up: ip + result fields ────────────────────────────────────


def test_audit_emits_ip_and_result_fields(tmp_logs):
    """Phase-A wrap-up: every audit record must carry the new ``ip``
    and ``result`` fields. Class-of-issue guard: a future refactor
    that drops either field from the entry dict regresses observability
    for the failed-attempt path (result="denied") and for IP-aware
    incident triage (ip).
    """
    webapp._audit("test_action", "slug", "session:alice")
    jsonl = tmp_logs / "audit.jsonl"
    entry = _read_last_jsonl(jsonl)
    assert "ip" in entry
    assert "result" in entry
    # No request was passed → ip is None; default result is "ok".
    assert entry["ip"] is None
    assert entry["result"] == "ok"


def test_audit_extracts_ip_from_x_forwarded_for(tmp_logs):
    """``_extract_client_ip`` MUST prefer the leftmost X-Forwarded-For
    entry over the direct socket address (r1-004 trust model: the
    reverse proxy overwrites XFF, so the leftmost hop is the real
    client). Regression guard against accidentally trusting
    ``request.client.host`` when XFF is present.
    """
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(b"x-forwarded-for", b"203.0.113.42, 10.0.0.1")],
        "client": ("10.0.0.1", 50000),
    }
    request = Request(scope)
    webapp._audit(
        "auth_login", "alice", "session:alice",
        user_id=1, request=request, result="ok",
    )
    jsonl = tmp_logs / "audit.jsonl"
    entry = _read_last_jsonl(jsonl)
    assert entry["ip"] == "203.0.113.42"
    assert entry["result"] == "ok"


def test_audit_records_denied_result_for_failed_attempts(tmp_logs):
    """Failed admin attempts (e.g. non-admin hitting /api/emergency-
    stop) must surface in the audit trail with ``result="denied"``,
    not just as a portal-log warning. Pin the contract so future
    callsites that miss the kwarg get caught.
    """
    webapp._audit(
        "emergency_stop", "-", "session:bob",
        user_id=2, result="denied",
    )
    jsonl = tmp_logs / "audit.jsonl"
    entry = _read_last_jsonl(jsonl)
    assert entry["action"] == "emergency_stop"
    assert entry["result"] == "denied"
    assert entry["user_id"] == 2


# ── Audit rhav2-001: file permissions on audit log writes ──────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file mode bits — Windows ACLs use a different model",
)
def test_audit_jsonl_files_chmod_0640_on_create(tmp_logs):
    """Audit rhav2-001 (RHA-v2, LOW): newly-created audit.jsonl
    files must land at mode 0o640 — owner read/write, group read,
    no world bits — so they're not readable by other users on a
    multi-tenant host. Pre-fix the umask of whoever ran the portal
    determined the mode (often 0o022 → 0o644, world-readable).
    """
    webapp._audit(
        "perm_check", "-", "session:alice", user_id=99,
    )
    global_jsonl = tmp_logs / "audit.jsonl"
    user_jsonl = tmp_logs / "logs" / "99" / "audit.jsonl"
    assert global_jsonl.exists()
    assert user_jsonl.exists()
    # Mask off the file-type bits — only the permission bits matter.
    assert (global_jsonl.stat().st_mode & 0o777) == 0o640, (
        f"global audit.jsonl mode = "
        f"{oct(global_jsonl.stat().st_mode & 0o777)}, expected 0o640"
    )
    assert (user_jsonl.stat().st_mode & 0o777) == 0o640, (
        f"per-user audit.jsonl mode = "
        f"{oct(user_jsonl.stat().st_mode & 0o777)}, expected 0o640"
    )
