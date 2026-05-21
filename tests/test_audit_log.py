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


def test_auth_logout_emits_audit_event(tmp_logs):
    """Audit PT-v4-AU-004: /auth/logout MUST emit a 'logout' audit
    event so the audit trail captures session lifecycle. Drives the
    full login → logout cycle and asserts the logout entry lands in
    the per-user split file.

    Follows the existing test_cross_tenant_isolation pattern of
    flipping _COOKIE_SECURE=False + _COOKIE_SAMESITE=lax for the
    duration of the test so the TestClient (running over plain
    http://testserver) actually persists the session cookie set by
    the login response. Without this, httpx drops the Secure cookie
    and the logout request lands without a session.
    """
    from fastapi.testclient import TestClient
    from core import user_store

    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, "hotfix-pw-pt-v4-au-004")

    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    try:
        client = TestClient(webapp.app)
        login_r = client.post(
            "/auth/login",
            json={"username": "admin", "password": "hotfix-pw-pt-v4-au-004"},
        )
        assert login_r.status_code == 200, login_r.text
        # Sanity: the session cookie actually landed in the jar.
        assert "reverto_session" in client.cookies, (
            "session cookie missing from TestClient jar after login — "
            "_COOKIE_SECURE flip did not take effect"
        )
        csrf = login_r.json()["csrf_token"]

        logout_r = client.post(
            "/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert logout_r.status_code == 200, logout_r.text
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite

    # Per-user file MUST contain the logout entry.
    user_jsonl = tmp_logs / "logs" / str(admin.id) / "audit.jsonl"
    assert user_jsonl.exists(), (
        "per-user audit.jsonl not written — logout handler didn't "
        "propagate user_id into _audit()"
    )
    # Walk every line so we don't false-fail if other audit events
    # interleaved (totp, etc.). The logout entry is the last one
    # written for this flow.
    lines = [json.loads(line) for line in user_jsonl.read_text().splitlines()]
    logout_entries = [e for e in lines if e["action"] == "logout"]
    assert len(logout_entries) == 1, (
        f"expected exactly one 'logout' audit entry, got {logout_entries!r}"
    )
    logout_entry = logout_entries[0]
    assert logout_entry["user_id"] == admin.id
    assert logout_entry["slug"] == "admin"
    assert logout_entry["result"] == "ok"


def test_auth_logout_no_audit_when_unauthenticated(tmp_logs):
    """Audit PT-v4-AU-004 (no-op path): a stranger hitting /auth/logout
    without a session cookie returns 200 (idempotent) but MUST NOT
    emit an audit event — there's no user identity to attribute and
    the request is effectively a passing-by click. Pinning this so a
    future refactor that audits every logout call doesn't pollute
    audit.jsonl with anonymous logout-noise.
    """
    from fastapi.testclient import TestClient

    client = TestClient(webapp.app)
    r = client.post("/auth/logout")
    assert r.status_code == 200, r.text

    global_jsonl = tmp_logs / "audit.jsonl"
    if not global_jsonl.exists():
        return  # no audit file at all = no logout event, satisfies contract
    lines = [json.loads(line) for line in global_jsonl.read_text().splitlines()]
    logout_entries = [e for e in lines if e["action"] == "logout"]
    assert logout_entries == [], (
        "logout audit fired for an unauthenticated caller — should "
        "be silent on the no-op path"
    )


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


# ── PT-v4-AZ-001: canonical (action, target_id, actor, ...) shape ──────────


def _admin_audit_client():
    """TestClient with the seeded admin authenticated, suitable for
    hitting /api/admin/* endpoints. Mirrors the admin_client fixtures
    in test_changelog_api.py / test_roadmap_routes.py / test_admin_
    marketing_regenerate.py without importing them across modules."""
    from fastapi.testclient import TestClient
    from core import user_store

    admin = user_store.get_user_by_username("admin")
    assert admin is not None, "admin seed missing"
    user_store.set_password(admin.id, "pytest-az001-pw")
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(admin),
    )
    return client, admin


def _find_action(jsonl_path, action):
    """Return the last JSONL entry with action==``action``, or None."""
    if not jsonl_path.exists():
        return None
    for line in reversed(jsonl_path.read_text().splitlines()):
        entry = json.loads(line)
        if entry.get("action") == action:
            return entry
    return None


class TestPTv4AZ001SlugSemantic:
    """Class-of-issue regression for PT-v4-AZ-001 (LOW).

    The canonical _audit(...) signature is
    ``(action, slug, key_hint, user_id=..., *, request, result)``.
    Slug is the TARGET identifier (entry_id, phase_id, "-" for fleet
    actions); key_hint is the ACTOR identifier (``session:<user>``
    produced by ``_request_actor``). Pre-fix the changelog, marketing,
    and roadmap routes swapped these — passing user.username in the
    slug-position and the target id in the key_hint-position — which
    made admin audit-log views read "<username> took action on
    entry: <username>" instead of "<username> took action on entry:
    <id>".

    These tests drive each fixed route end-to-end against a tmp
    audit-log dir and assert that the emitted JSONL row has:
      * slug == target_id (string) — NOT the username
      * user (key_hint) == "session:admin" — actor identity

    Any future regression that re-swaps the arguments fails one of
    these assertions immediately.
    """

    def test_changelog_create_emits_target_id_as_slug(self, tmp_logs):
        client, admin = _admin_audit_client()
        r = client.post(
            "/api/admin/changelog",
            json={
                "title": "PT-v4-AZ-001 regression seed",
                "description": "body",
                "category": "fix",
            },
        )
        assert r.status_code == 201, r.text
        entry_id = r.json()["id"]
        audit = _find_action(
            tmp_logs / "audit.jsonl", "changelog_api_create",
        )
        assert audit is not None, "changelog_api_create audit missing"
        assert audit["slug"] == str(entry_id), (
            f"AZ-001 regression: slug must be the entry_id "
            f"({entry_id!r}), not {audit['slug']!r}. Pre-fix the "
            "callsite swapped slug and key_hint."
        )
        assert audit["slug"] != admin.username, (
            "AZ-001 regression: slug must NOT carry the username — "
            "that's the swap the fix closed."
        )
        assert audit["user"] == "session:admin"
        assert audit["user_id"] == admin.id

    def test_roadmap_create_emits_target_id_as_slug(self, tmp_logs):
        client, admin = _admin_audit_client()
        r = client.post(
            "/api/admin/roadmap",
            json={
                "phase_key": "az001-seed",
                "display_name": "AZ-001 regression seed",
                "summary": "body",
                "status": "pending",
                "sort_order": 999,
            },
        )
        assert r.status_code == 201, r.text
        phase_id = r.json()["id"]
        audit = _find_action(
            tmp_logs / "audit.jsonl", "roadmap_api_create",
        )
        assert audit is not None, "roadmap_api_create audit missing"
        assert audit["slug"] == str(phase_id), (
            f"AZ-001 regression: slug must be the phase_id "
            f"({phase_id!r}), not {audit['slug']!r}."
        )
        assert audit["slug"] != admin.username
        assert audit["user"] == "session:admin"
        assert audit["user_id"] == admin.id

    def test_marketing_regenerate_uses_fleet_slug(self, tmp_logs):
        """marketing_regenerate is a fleet action — no per-row
        target. Pre-fix the user.username was in the slug-position
        and a per-snapshot-result string was in the key_hint
        position (overriding the actor). Post-fix: slug='-' (matches
        the emergency_stop precedent in admin.py) and key_hint is
        the resolved actor."""
        client, admin = _admin_audit_client()
        r = client.post("/api/admin/marketing/regenerate")
        assert r.status_code in (200, 207, 500), r.text
        audit = _find_action(
            tmp_logs / "audit.jsonl", "marketing_regenerate",
        )
        assert audit is not None, "marketing_regenerate audit missing"
        assert audit["slug"] == "-", (
            f"AZ-001 regression: marketing_regenerate slug must be "
            f"'-' (fleet action, no target), not {audit['slug']!r}."
        )
        assert audit["slug"] != admin.username
        assert audit["user"] == "session:admin"
        assert audit["user_id"] == admin.id
