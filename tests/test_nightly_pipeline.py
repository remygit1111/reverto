"""Tests for ml/nightly_pipeline.py.

The pipeline's public functions each have a fail-soft contract —
insufficient data, missing configs and missing optional dependencies
should return a ``{skipped: True, reason: ...}`` dict instead of
raising. These tests pin that contract and cover the SQL path with
an in-memory sqlite fixture so no real logs/reverto.db is touched.
"""

import sqlite3
import sys

import pandas as pd
import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from ml.nightly_pipeline import (
    _get_bot_symbol,
    _get_bot_timeframe,
    load_deal_history,
    optimize_parameters,
    run_pipeline,
    train_entry_filter,
)


@pytest.fixture
def temp_db(tmp_path):
    """File-backed SQLite with the minimum deals schema the pipeline reads.

    The real DB has more columns (peak_price, created_at, ...) but
    load_deal_history uses ``SELECT *``, so only the column set it
    projects matters for the tests — the SELECT will return whatever
    rows we inserted without needing the full ledger schema.
    """
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    # Users table + admin seed so the FK in deals/orders/backtest_runs
    # resolves; otherwise the NOT NULL / REFERENCES pair fails every
    # insert with IntegrityError.
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'admin')")
    conn.execute(
        """
        CREATE TABLE deals (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            bot_slug TEXT,
            bot_name TEXT,
            side TEXT,
            status TEXT,
            close_reason TEXT,
            opened_at REAL,
            closed_at REAL,
            initial_price REAL,
            avg_entry REAL,
            close_price REAL,
            total_size REAL,
            leverage REAL,
            pnl_btc REAL,
            pnl_pct REAL,
            entry_trigger TEXT,
            exit_trigger TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db_file


# ── load_deal_history ───────────────────────────────────────────────────────

class TestLoadDealHistory:

    def test_empty_db_returns_empty(self, temp_db):
        df = load_deal_history(str(temp_db), "any_bot")
        assert len(df) == 0

    def test_loads_closed_deals_only(self, temp_db):
        """Only closed_at IS NOT NULL rows are selected — open deals
        have no realised pnl so they cannot be trained on."""
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-1', 1, 'mybot', 'closed', 1000, 2.5)"
        )
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-2', 1, 'mybot', 'open', NULL, NULL)"
        )
        conn.commit()
        conn.close()

        df = load_deal_history(str(temp_db), "mybot")
        assert len(df) == 1
        assert df.iloc[0]["id"] == "D-1"

    def test_filters_by_bot_slug(self, temp_db):
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-1', 1, 'bot_a', 'closed', 1000, 2.5)"
        )
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-2', 1, 'bot_b', 'closed', 2000, 1.5)"
        )
        conn.commit()
        conn.close()

        df_a = load_deal_history(str(temp_db), "bot_a")
        assert len(df_a) == 1
        assert df_a.iloc[0]["id"] == "D-1"

    def test_orders_by_opened_at(self, temp_db):
        """Chronological ordering is required for TimeSeriesSplit later."""
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, status, closed_at, opened_at, pnl_pct) "
            "VALUES ('D-2', 1, 'mybot', 'closed', 2000, 2000, 1.5)"
        )
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, status, closed_at, opened_at, pnl_pct) "
            "VALUES ('D-1', 1, 'mybot', 'closed', 1500, 1000, 2.5)"
        )
        conn.commit()
        conn.close()

        df = load_deal_history(str(temp_db), "mybot")
        assert list(df["id"]) == ["D-1", "D-2"]


# ── train_entry_filter ──────────────────────────────────────────────────────

class TestTrainEntryFilter:

    def test_insufficient_data_skipped(self):
        """< 20 feature records → {skipped: True, reason: insufficient_data}."""
        deals_df = pd.DataFrame({"pnl_pct": [1.0] * 10})
        result = train_entry_filter(deals_df, [{} for _ in range(10)])
        assert result.get("skipped") is True
        assert "insufficient_data" in result.get("reason", "")

    def test_empty_features_handled(self):
        """Empty feature store is still the insufficient_data path — we
        must not raise on an empty iterable."""
        deals_df = pd.DataFrame({"pnl_pct": []})
        result = train_entry_filter(deals_df, [])
        assert result.get("skipped") is True


# ── optimize_parameters ─────────────────────────────────────────────────────

class TestUserScopedConfigPath:
    """Audit v26-18: every YAML lookup must route through
    ``core.paths.bot_yaml_path`` (``config/bots/<user_id>/<slug>.yaml``)
    — the Phase-1 flat path is gone and any reader still hitting it
    silently returned defaults on real installs."""

    def test_get_bot_symbol_reads_user_scoped_yaml(
        self, tmp_path, monkeypatch,
    ):
        """Write a YAML at the Phase-2 path and confirm the helper
        returns the configured pair, not the BTC/USD fallback."""
        import core.paths as paths_mod

        monkeypatch.setattr(paths_mod, "BASE_DIR", tmp_path)
        user_dir = tmp_path / "config" / "bots" / "7"
        user_dir.mkdir(parents=True)
        (user_dir / "alpha.yaml").write_text(
            "bot:\n  name: alpha\n  pair: ETH/USDT\n  timeframe: 4h\n",
        )

        assert _get_bot_symbol(7, "alpha") == "ETH/USDT"
        assert _get_bot_timeframe(7, "alpha") == "4h"

    def test_get_bot_symbol_ignores_phase1_flat_path(
        self, tmp_path, monkeypatch,
    ):
        """A stale Phase-1 layout (YAML directly under config/bots/)
        must NOT be picked up — otherwise the fix would only move the
        bug rather than closing it."""
        import core.paths as paths_mod

        monkeypatch.setattr(paths_mod, "BASE_DIR", tmp_path)
        flat_dir = tmp_path / "config" / "bots"
        flat_dir.mkdir(parents=True)
        # Phase-1 flat file — should be ignored.
        (flat_dir / "alpha.yaml").write_text(
            "bot:\n  name: alpha\n  pair: STALE/COIN\n  timeframe: 12h\n",
        )

        # User-scoped lookup for user_id=7 finds nothing → falls back
        # to the BTC/USD default. If the old flat-path code path ever
        # came back, this would return STALE/COIN instead.
        assert _get_bot_symbol(7, "alpha") == "BTC/USD"
        assert _get_bot_timeframe(7, "alpha") == "1h"

    def test_optimize_parameters_finds_user_scoped_config(
        self, tmp_path, monkeypatch,
    ):
        """``optimize_parameters`` used to return config_missing on
        every real install because it looked at the flat path. With a
        YAML at the user-scoped path + optuna available, the skipped
        reason must NOT be config_missing."""
        import core.paths as paths_mod

        monkeypatch.setattr(paths_mod, "BASE_DIR", tmp_path)
        user_dir = tmp_path / "config" / "bots" / "1"
        user_dir.mkdir(parents=True)
        (user_dir / "beta.yaml").write_text(
            "bot:\n  name: beta\n  pair: BTC/USD\n  timeframe: 1h\n",
        )

        result = optimize_parameters(1, "beta", pd.DataFrame())
        # Optuna may or may not be installed in the test env — what
        # matters is that we did NOT abort on config_missing.
        assert result.get("reason") != "config_missing"


class TestOptimizeParameters:

    def test_missing_config_returns_skipped(self):
        """A bot slug whose YAML does not exist must not crash the
        pipeline — we fail soft with a clear reason."""
        result = optimize_parameters(
            1, "definitely_nonexistent_bot", pd.DataFrame(),
        )
        # Either optuna is missing (skipped=optuna_missing) or config is
        # missing (skipped=config_missing). Both are valid fail-soft paths.
        assert result.get("skipped") is True
        assert result.get("reason") in {"optuna_missing", "config_missing"}


# ── run_pipeline ────────────────────────────────────────────────────────────

class TestRunPipeline:

    def test_no_deals_returns_early(self, temp_db):
        """Running the pipeline against an empty DB must produce a
        results dict with deals_loaded=0 and skipped=True."""
        result = run_pipeline(1, "empty_bot", str(temp_db))
        assert result.get("deals_loaded") == 0
        assert result.get("skipped") is True
        assert result.get("reason") == "no_deals"
        # Audit v26-18: the user_id used to run the pipeline is
        # echoed back in the results dict so the JSON summary names
        # the tenant the run belonged to.
        assert result.get("user_id") == 1

    def test_results_persisted(self, tmp_path, temp_db, monkeypatch):
        """The summary should land in ml/results_<bot>.json so operators
        can diff runs. Redirect the path helper at a tmp dir so we don't
        pollute the real ml/ directory."""
        from ml import nightly_pipeline

        results_file = tmp_path / "results_empty_bot.json"

        def fake_persist(bot_slug, results):
            results_file.write_text("persisted")
            return results_file

        monkeypatch.setattr(nightly_pipeline, "_persist_results", fake_persist)
        run_pipeline(1, "empty_bot", str(temp_db))
        assert results_file.exists()


# ── Feature-store integration ───────────────────────────────────────────────


def _seed_closed_deal(
    db_file, deal_id: str, bot_slug: str, opened_at: float, pnl_pct: float,
    user_id: int = 1,
):
    """Insert a minimal closed deal row for pipeline integration tests."""
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        INSERT INTO deals (
            id, user_id, bot_slug, bot_name, side, status, close_reason,
            opened_at, closed_at, initial_price, avg_entry, close_price,
            total_size, leverage, pnl_btc, pnl_pct,
            entry_trigger, exit_trigger
        ) VALUES (?, ?, ?, ?, 'long', 'closed', 'take_profit',
                  ?, ?, 80000.0, 80000.0, 81000.0, 0.001, 1, 0.000001, ?,
                  NULL, NULL)
        """,
        (
            deal_id, user_id, bot_slug, bot_slug,
            opened_at, opened_at + 3600,
            pnl_pct,
        ),
    )
    conn.commit()
    conn.close()


class TestFeatureStoreIntegration:
    """Pins the candle_loader wiring: run_pipeline must call the loader
    for each deal, compute features, and surface the count. The
    exchange is monkeypatched so no network I/O happens."""

    @pytest.fixture
    def _mock_fetch(self, monkeypatch, tmp_path):
        """Stub ccxt + redirect the candle cache. Returns a canned 120-bar
        OHLCV series that is strong enough for compute_features to pass
        its MIN_CANDLES=78 gate."""
        import ml.candle_loader as loader_mod

        monkeypatch.setattr(
            loader_mod, "CACHE_DIR", tmp_path / "candle_cache",
        )

        def _fake(symbol, timeframe, since_ms, limit=200):
            # 120 hourly bars ending at a reference time. The absolute
            # timestamps don't need to line up with the deal's
            # opened_at because the loader filters by opened_at <=
            # deal — we backdate the series so every bar is eligible.
            base_ts = int(since_ms / 1000)
            return [
                {
                    "timestamp": base_ts + i * 3600,
                    "open":   80_000.0 + i * 5,
                    "high":   80_050.0 + i * 5,
                    "low":    79_950.0 + i * 5,
                    "close":  80_020.0 + i * 5,
                    "volume": 1.0,
                }
                for i in range(120)
            ]
        monkeypatch.setattr(loader_mod, "_fetch_candles_ccxt", _fake)
        yield

    @pytest.fixture
    def _bot_config(self, tmp_path, monkeypatch):
        """Stub the YAML-reading helpers to return fixed values.

        The real helpers now resolve through ``core.paths.bot_yaml_path``
        (audit v26-18), but for the feature-store integration tests we
        only care that the pipeline wiring calls them — the actual
        config content is already fixed by this stub. Signatures match
        the post-fix (user_id, bot_slug) arity.
        """
        from ml import nightly_pipeline
        monkeypatch.setattr(
            nightly_pipeline, "_get_bot_symbol",
            lambda _user_id, _slug: "BTC/USD",
        )
        monkeypatch.setattr(
            nightly_pipeline, "_get_bot_timeframe",
            lambda _user_id, _slug: "1h",
        )
        yield

    def test_pipeline_populates_feature_store(
        self, temp_db, _mock_fetch, _bot_config, monkeypatch,
    ):
        """End-to-end: seed 5 closed deals, run pipeline, assert feature
        engineering was kicked off and the result dict carries the
        populated count."""
        from ml import nightly_pipeline

        # Bypass the real JSON-persist so the test doesn't write into
        # the repo's ml/ directory.
        monkeypatch.setattr(
            nightly_pipeline, "_persist_results",
            lambda slug, results: None,
        )

        base_ts = 1_750_000_000.0
        for i in range(5):
            _seed_closed_deal(
                temp_db, f"D-{i}", "pt_feat_bot",
                opened_at=base_ts + i * 86400,
                pnl_pct=(0.5 if i % 2 == 0 else -0.3),
            )

        result = run_pipeline(1, "pt_feat_bot", str(temp_db))
        assert result["deals_loaded"] == 5
        # features_computed key MUST be present — it's the contract
        # that proves the candle loader was invoked.
        assert "features_computed" in result
        assert result["features_computed"] >= 1
        # Entry filter should still skip (< 20 deals), but the reason
        # must be insufficient_data — NOT "pending_candle_loader".
        ef = result["entry_filter"]
        assert ef.get("skipped") is True
        assert ef.get("reason") == "insufficient_data"

    def test_pipeline_survives_loader_exception(
        self, temp_db, _bot_config, monkeypatch,
    ):
        """A ccxt error on one deal must NOT crash the whole run —
        the pipeline logs + skips the deal and presses on."""
        from ml import nightly_pipeline
        import ml.candle_loader as loader_mod

        monkeypatch.setattr(
            nightly_pipeline, "_persist_results",
            lambda slug, results: None,
        )
        # Every fetch blows up — feature_store ends up empty but
        # run_pipeline still returns a shape-complete results dict.
        monkeypatch.setattr(
            loader_mod, "_fetch_candles_ccxt",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        _seed_closed_deal(
            temp_db, "D-1", "pt_feat_bot", 1_750_000_000.0, 0.5,
        )
        result = run_pipeline(1, "pt_feat_bot", str(temp_db))
        assert result["features_computed"] == 0
        assert "entry_filter" in result
