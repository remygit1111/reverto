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
    conn.execute(
        """
        CREATE TABLE deals (
            id TEXT PRIMARY KEY,
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
            "INSERT INTO deals (id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-1', 'mybot', 'closed', 1000, 2.5)"
        )
        conn.execute(
            "INSERT INTO deals (id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-2', 'mybot', 'open', NULL, NULL)"
        )
        conn.commit()
        conn.close()

        df = load_deal_history(str(temp_db), "mybot")
        assert len(df) == 1
        assert df.iloc[0]["id"] == "D-1"

    def test_filters_by_bot_slug(self, temp_db):
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO deals (id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-1', 'bot_a', 'closed', 1000, 2.5)"
        )
        conn.execute(
            "INSERT INTO deals (id, bot_slug, status, closed_at, pnl_pct) "
            "VALUES ('D-2', 'bot_b', 'closed', 2000, 1.5)"
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
            "INSERT INTO deals (id, bot_slug, status, closed_at, opened_at, pnl_pct) "
            "VALUES ('D-2', 'mybot', 'closed', 2000, 2000, 1.5)"
        )
        conn.execute(
            "INSERT INTO deals (id, bot_slug, status, closed_at, opened_at, pnl_pct) "
            "VALUES ('D-1', 'mybot', 'closed', 1500, 1000, 2.5)"
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

class TestOptimizeParameters:

    def test_missing_config_returns_skipped(self):
        """A bot slug whose YAML does not exist must not crash the
        pipeline — we fail soft with a clear reason."""
        result = optimize_parameters("definitely_nonexistent_bot", pd.DataFrame())
        # Either optuna is missing (skipped=optuna_missing) or config is
        # missing (skipped=config_missing). Both are valid fail-soft paths.
        assert result.get("skipped") is True
        assert result.get("reason") in {"optuna_missing", "config_missing"}


# ── run_pipeline ────────────────────────────────────────────────────────────

class TestRunPipeline:

    def test_no_deals_returns_early(self, temp_db):
        """Running the pipeline against an empty DB must produce a
        results dict with deals_loaded=0 and skipped=True."""
        result = run_pipeline("empty_bot", str(temp_db))
        assert result.get("deals_loaded") == 0
        assert result.get("skipped") is True
        assert result.get("reason") == "no_deals"

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
        run_pipeline("empty_bot", str(temp_db))
        assert results_file.exists()
