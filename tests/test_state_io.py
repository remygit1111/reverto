"""Tests for paper/state_io.py — the persistence primitives extracted
from paper_engine in the v22 refactor.

These verify the file-I/O contract in isolation. Business-logic tests
(full engine restore round-trips, drawdown persistence, etc.) stay in
test_paper_engine.py and test_paper_state_persistence.py.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from paper.state_io import StateIO, deal_to_dict, dict_to_deal  # noqa: E402


# ── load() ──────────────────────────────────────────────────────────────────

class TestStateIOLoad:

    def test_load_returns_none_for_missing_file(self, tmp_path):
        io = StateIO(tmp_path / "missing.json", "test")
        assert io.load() is None

    def test_load_returns_none_for_corrupt_json(self, tmp_path, caplog):
        state_file = tmp_path / "corrupt.json"
        state_file.write_text("not valid json {{{")
        io = StateIO(state_file, "test")
        with caplog.at_level("WARNING"):
            assert io.load() is None
        # The warning must surface the corruption; silent swallow hid
        # real problems in the pre-refactor engine.
        assert any("could not be parsed" in r.message for r in caplog.records)

    def test_load_returns_dict_for_valid_json(self, tmp_path):
        state_file = tmp_path / "valid.json"
        state_file.write_text('{"balance_btc": 1.5, "open_deals": []}')
        io = StateIO(state_file, "test")
        assert io.load() == {"balance_btc": 1.5, "open_deals": []}

    def test_load_none_when_state_file_is_none(self):
        """Used by paper bots that run with no persistence target."""
        assert StateIO(None, "test").load() is None


# ── write() ─────────────────────────────────────────────────────────────────

class TestStateIOWrite:

    def test_write_creates_file(self, tmp_path):
        state_file = tmp_path / "new.json"
        StateIO(state_file, "test").write({"balance_btc": 2.0})
        assert state_file.exists()

    def test_write_is_atomic_no_tmp_leftover(self, tmp_path):
        """Successful write must remove the .tmp sibling."""
        state_file = tmp_path / "atomic.json"
        StateIO(state_file, "test").write({"balance_btc": 3.0})
        assert not state_file.with_suffix(".tmp").exists()
        assert state_file.exists()

    def test_write_roundtrip_via_load(self, tmp_path):
        state_file = tmp_path / "rt.json"
        io = StateIO(state_file, "test")
        payload = {"balance_btc": 0.123, "open_deals": [], "extra": True}
        io.write(payload)
        assert io.load() == payload

    def test_write_does_not_raise_on_bad_target(self, tmp_path, caplog):
        """Disk-full / permission error must be swallowed+logged so the
        hot tick loop keeps ticking."""
        # Point at a non-existent parent dir — mkdir isn't called here,
        # so write() should log at DEBUG and return.
        state_file = tmp_path / "nonexistent" / "nope.json"
        with caplog.at_level("DEBUG"):
            StateIO(state_file, "test").write({"x": 1})
        assert not state_file.exists()

    def test_write_noop_when_state_file_none(self):
        """Bot without persistence target — write must not raise."""
        StateIO(None, "test").write({"x": 1})


# ── cleanup_orphan_tmps() ───────────────────────────────────────────────────

class TestOrphanCleanup:

    def test_cleanup_removes_state_tmp(self, tmp_path):
        state_file = tmp_path / "state.json"
        tmp = tmp_path / "state.json.tmp"
        tmp.write_text("orphan from SIGKILL")

        StateIO(state_file, "test").cleanup_orphan_tmps()
        assert not tmp.exists()

    def test_cleanup_triggered_by_load(self, tmp_path):
        """load() must sweep before attempting to read — if it doesn't,
        an orphan tmp from a crashed rotation could accumulate."""
        state_file = tmp_path / "state.json"
        tmp = tmp_path / "state.json.tmp"
        tmp.write_text("orphan")
        StateIO(state_file, "test").load()
        assert not tmp.exists()

    def test_cleanup_preserves_foreign_tmp(self, tmp_path):
        """Other modules write their own .tmp files (credentials, etc.)
        in shared dirs. The sweep MUST only touch siblings matching the
        state_file name prefix."""
        state_file = tmp_path / "state.json"
        foreign = tmp_path / "credentials.json.tmp"
        foreign.write_text("not ours")

        StateIO(state_file, "test").cleanup_orphan_tmps()
        assert foreign.exists()

    def test_cleanup_noop_when_state_file_none(self):
        StateIO(None, "test").cleanup_orphan_tmps()

    def test_cleanup_tolerates_unreadable_dir(self, tmp_path, caplog):
        """If the parent dir was deleted, glob raises OSError. Must not
        propagate — the sweep is best-effort."""
        state_file = tmp_path / "gone" / "state.json"
        StateIO(state_file, "test").cleanup_orphan_tmps()


# ── mark_stopped() ──────────────────────────────────────────────────────────

class TestMarkStopped:

    def test_flips_running_flag(self, tmp_path):
        """Preserves the pre-refactor _clear_state semantic: rewrite
        running/current_price, leave other state intact."""
        import json
        state_file = tmp_path / "st.json"
        state_file.write_text(json.dumps({
            "bot_name": "x",
            "running": True,
            "current_price": 50_000.0,
            "balance_btc": 0.5,
        }))

        StateIO(state_file, "test").mark_stopped()
        data = json.loads(state_file.read_text())
        assert data["running"] is False
        assert data["current_price"] == 0.0
        # Other fields untouched.
        assert data["balance_btc"] == 0.5
        assert data["bot_name"] == "x"

    def test_creates_file_when_absent(self, tmp_path):
        """If the state file doesn't exist yet, mark_stopped writes a
        minimal {running: False} dict rather than raising."""
        import json
        state_file = tmp_path / "fresh.json"
        StateIO(state_file, "test").mark_stopped()
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["running"] is False

    def test_noop_when_state_file_none(self):
        StateIO(None, "test").mark_stopped()


# ── deal_to_dict / dict_to_deal ─────────────────────────────────────────────

class TestDealSerialisation:
    """Smoke tests for the round-trip; deep coverage lives in
    test_paper_engine.py (which still imports these via the
    backwards-compat aliases in paper_engine.py)."""

    def test_roundtrip_basic(self):
        from datetime import UTC, datetime

        from paper.paper_state import PaperDeal, PaperOrder

        orders = [PaperOrder(
            order_number=1, price=80_000.0, size=0.001,
            timestamp=datetime(2026, 4, 18, tzinfo=UTC),
            order_type="base",
        )]
        deal = PaperDeal(
            id="P-1", bot_name="t", symbol="BTC/USD",
            side="long", leverage=1, orders=orders,
        )
        restored = dict_to_deal(deal_to_dict(deal, current_price=80_500.0))
        assert restored.id == "P-1"
        assert restored.side == "long"
        assert restored.orders[0].price == 80_000.0

    def test_closed_deal_uses_stored_pnl(self):
        """For closed deals, deal_to_dict must use the realised PnL — not
        re-derive from current_price. Regression test for the v20 fix."""
        from datetime import UTC, datetime

        from paper.paper_state import PaperDeal, PaperOrder
        orders = [PaperOrder(
            order_number=1, price=80_000.0, size=0.001,
            timestamp=datetime(2026, 4, 18, tzinfo=UTC),
            order_type="base",
        )]
        deal = PaperDeal(
            id="P-2", bot_name="t", symbol="BTC/USD",
            side="long", leverage=1, orders=orders,
        )
        deal.is_open = False
        deal.pnl_btc = 0.005
        deal.pnl_pct = 5.0

        out = deal_to_dict(deal, current_price=99_999.0)
        assert out["pnl_btc"] == 0.005
        assert out["pnl_pct"] == 5.0


# ── paper_engine backwards-compat aliases ───────────────────────────────────

class TestBackwardsCompatAliases:

    def test_paper_engine_still_exports_deal_helpers(self):
        """tests/test_paper_engine.py + test_paper_state_persistence.py
        import the underscored aliases from paper.paper_engine. This
        test pins that the re-export is still in place."""
        from paper.paper_engine import _deal_to_dict, _dict_to_deal
        # Same callable as the canonical state_io version.
        assert _deal_to_dict is deal_to_dict
        assert _dict_to_deal is dict_to_deal


# ── Concurrency invariants ──────────────────────────────────────────────────

class TestStateIOConcurrency:
    """Production design gives each PaperEngine its own state_file (per
    bot slug), so concurrent writes to the SAME StateIO are never
    supposed to happen. These tests pin the invariant anyway — the
    atomic-rename pattern must survive a hypothetical multi-thread
    writer without raising or corrupting the file."""

    def test_concurrent_writes_land_valid_json(self, tmp_path):
        """10 threads racing writes of distinct values — every write
        either wins atomically or gets overwritten by a later write,
        but the final file is ALWAYS a parseable JSON dict from one of
        the writers. No half-written state, no exceptions."""
        import json
        import threading

        state_file = tmp_path / "concurrent.json"
        io = StateIO(state_file, "test")

        errors: list[str] = []

        def writer(value):
            try:
                io.write({"value": value, "written_by": f"t{value}"})
            except Exception as e:  # noqa: BLE001 — test is the catch
                errors.append(f"{value}: {e}")

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"unexpected errors: {errors}"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        # The winning writer's value wins — but it MUST be one of the
        # 10 writers, not some in-between garbage.
        assert 0 <= data["value"] < 10
        assert data["written_by"] == f"t{data['value']}"

    def test_concurrent_reads_dont_block_each_other(self, tmp_path):
        """20 threads all reading the same state file must each see
        the full payload. load() is side-effect-free on the read path
        (cleanup_orphan_tmps is idempotent) so there's no mutex needed."""
        import json
        import threading

        state_file = tmp_path / "read.json"
        state_file.write_text(json.dumps({"value": 42}))
        io = StateIO(state_file, "test")

        results: list = []
        results_lock = threading.Lock()

        def reader():
            data = io.load()
            with results_lock:
                results.append(data)

        threads = [threading.Thread(target=reader) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 20
        assert all(r == {"value": 42} for r in results)

    def test_reader_during_writer_sees_valid_state(self, tmp_path):
        """A reader interleaved with a writer must see either the old
        state or the new state — never a partial file. POSIX rename
        is atomic so the .tmp → final swap is visible instantly."""
        import json
        import threading
        import time as _time

        state_file = tmp_path / "raced.json"
        io = StateIO(state_file, "test")
        io.write({"value": "initial"})

        writer_done = threading.Event()

        def writer():
            for i in range(50):
                io.write({"value": f"v{i}"})
            writer_done.set()

        reads: list[str] = []

        def reader():
            while not writer_done.is_set():
                data = io.load()
                if data is not None:
                    reads.append(data["value"])
                _time.sleep(0.001)

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start(); t_r.start()
        t_w.join(); t_r.join()

        # Every observed value must match one that was actually written.
        legal = {"initial"} | {f"v{i}" for i in range(50)}
        assert all(v in legal for v in reads), (
            f"reader saw illegal value(s): "
            f"{[v for v in reads if v not in legal]}"
        )
