# tests/conftest.py
import sys, os, pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

# ── Helpers — beschikbaar in alle testbestanden via conftest ──────────────────

def make_order(price, size=0.001, order_type="base", order_number=1):
    return PaperOrder(order_number=order_number, price=price, size=size,
                      timestamp=datetime.now(UTC), order_type=order_type)

def make_deal(entry_price=80000.0, size=0.001, side="long", leverage=1):
    return PaperDeal(id="TEST-0001", bot_name="test-bot", symbol="BTC/USD",
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
