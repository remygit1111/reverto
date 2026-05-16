# tests/conftest.py
import sys, os, pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock

# Audit r1-058: the boot-time config validator requires
# REVERTO_SECRET_KEY. Production sets it via .env; the test harness
# seeds a placeholder so lifespan startup succeeds in TestClient.
# setdefault so a test that explicitly sets its own value still wins.
os.environ.setdefault("REVERTO_SECRET_KEY", "testkey-for-pytest-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Audit r1-073: auto-set CSRF cookie + header on every TestClient
# constructed during the suite. Tests that manually mint a
# session cookie via ``_create_session_cookie`` don't go through
# /auth/login and therefore don't get the CSRF cookie the real
# login flow mints. Injecting it at TestClient construction means
# every mutating request carries a matching (cookie, header) pair
# without each test file needing to know about CSRF. Tests that
# explicitly exercise the CSRF-failure path (see
# tests/test_csrf.py) remove the cookie/header per-call.
_TEST_CSRF_TOKEN = "pytest-csrf-token-r1073"
try:
    from fastapi.testclient import TestClient as _TC
    _orig_tc_init = _TC.__init__

    def _csrf_patched_init(self, *args, **kwargs):
        _orig_tc_init(self, *args, **kwargs)
        try:
            self.cookies.set("reverto_csrf", _TEST_CSRF_TOKEN)
            self.headers.update({"X-CSRF-Token": _TEST_CSRF_TOKEN})
        except Exception:
            # Defensive: if a future TestClient change removes the
            # ``.cookies`` / ``.headers`` attributes, don't brick
            # the entire suite — just skip the CSRF seed and let
            # specific tests that need it set it manually.
            pass

    _TC.__init__ = _csrf_patched_init
except ImportError:
    # fastapi not importable (e.g. running a narrow subset of
    # core-only tests with a stripped virtualenv). Move on.
    pass

from paper.paper_state import PaperState, PaperDeal, PaperOrder
from core import database as _database


@pytest.fixture(autouse=True)
def _isolate_reverto_db(tmp_path_factory):
    """Route the SQLite ledger at a tmp DB for every test so the real
    logs/reverto.db is never touched by the suite. Each test gets its
    own fresh DB file so state never leaks between tests.

    Tests that need direct ledger access (tests/test_database.py) override
    this by calling core.database.set_db_path themselves — they still run
    under this fixture, but their own set_db_path call wins."""
    db_dir = tmp_path_factory.mktemp("reverto_ledger")
    _database.set_db_path(db_dir / "ledger.db")
    _database.init_db()
    yield
    _database.close_db()


@pytest.fixture(autouse=True)
def _isolate_marketing_export(tmp_path_factory, monkeypatch):
    """Redirect ``core.marketing_export`` writes to a per-test tmp
    directory so the publish/unpublish/edit hooks added in PR 2 do
    not try to write to ``/var/www/reverto-marketing/data/`` (which
    does not exist on Reverto-Dev or in CI). Without this, every
    existing publish-endpoint test would log a noisy PermissionError
    even though the wrapper swallows the failure.
    """
    snapshot_dir = tmp_path_factory.mktemp("reverto_marketing")
    monkeypatch.setenv("REVERTO_MARKETING_DATA_DIR", str(snapshot_dir))
    yield snapshot_dir

# ── Helpers — beschikbaar in alle testbestanden via conftest ──────────────────

def make_order(price, size=0.001, order_type="base", order_number=1):
    return PaperOrder(order_number=order_number, price=price, size=size,
                      timestamp=datetime.now(UTC), order_type=order_type)

def make_deal_id(suffix: int = 1) -> str:
    """Return a well-formed deal id in the post-collision-fix format
    (YYYYMMDDHHMM-RRRR) for test fixtures. The timestamp prefix is
    fixed so assertions stay deterministic — we don't rely on the
    real generator, which uses wall-clock UTC. ``suffix`` goes into
    the random-slot half so tests can mint distinct IDs."""
    return f"202604191342-{suffix:04d}"


def make_deal(entry_price=80000.0, size=0.001, side="long", leverage=1):
    return PaperDeal(id=make_deal_id(1), bot_name="test-bot", symbol="BTC/USD",
                     side=side, leverage=leverage, orders=[make_order(entry_price, size)])

def make_notifier():
    n = MagicMock()
    for m in ["notify_startup","notify_shutdown","notify_entry","notify_dca",
              "notify_take_profit","notify_stop_loss","notify_error"]:
        setattr(n, m, MagicMock())
    return n

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def state():
    return PaperState(initial_balance_btc=0.1)

@pytest.fixture
def deal():
    return make_deal()

@pytest.fixture
def notifier():
    return make_notifier()


# ── LiveProvider mocking ─────────────────────────────────────────


@pytest.fixture
def mock_live_provider(monkeypatch):
    """Mocked LiveProvider for tests that need to control live-mode
    behavior without spinning up BuiltinLiveProvider.

    The mock satisfies the LiveProvider Protocol structurally:
    - ``interface_version`` attribute set to SUPPORTED_INTERFACE_VERSION
    - All four Protocol methods (3 async + 1 sync) as MagicMock/
      AsyncMock with sensible defaults

    Tests can override specific method behaviors::

        def test_something(mock_live_provider):
            mock_live_provider.is_live_config = AsyncMock(return_value=True)
            # ... rest of test ...

    The fixture patches ``core.plugin_loader.load_live_provider`` to
    return this mock, and resets the loader cache before and after
    the test for isolation.

    Use this fixture in tests that exercise code paths calling
    ``load_live_provider()`` where you want to control the provider's
    return values. For tests that need to verify the "no provider"
    (None) case, use explicit ``monkeypatch.setattr(
    "core.plugin_loader.load_live_provider", lambda: None)`` instead.

    Added in Phase 2 Task 2.9 per docs/plugin_split_migration.md §3.1.
    """
    from unittest.mock import AsyncMock, MagicMock
    from core.live_provider import SUPPORTED_INTERFACE_VERSION
    from core import plugin_loader

    mock = MagicMock()
    mock.interface_version = SUPPORTED_INTERFACE_VERSION

    # Three async methods with sensible defaults
    mock.start_bot_dry_run = AsyncMock(
        return_value={"ok": True, "message": "mocked"}
    )
    mock.is_live_config = AsyncMock(return_value=False)
    mock.list_live_slugs = AsyncMock(return_value=set())

    # One sync callback
    mock.on_breaker_permanent_open = MagicMock()

    # Patch the loader and reset cache
    plugin_loader.reset_cache()
    monkeypatch.setattr(
        "core.plugin_loader.load_live_provider",
        lambda: mock,
    )

    yield mock

    # Cleanup: reset cache so subsequent tests get fresh state
    plugin_loader.reset_cache()
