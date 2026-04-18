"""Nightly ML pipeline for Reverto.

Designed to run once per night (cron 05 23 * * *) and produce:
    1. An updated entry-filter classifier trained on the most recent
       deal history.
    2. A parameter-optimisation report suggesting DCA / TP tweaks.
    3. A summary JSON the operator can diff against the previous run.

Heavy ML dependencies (xgboost, optuna) are imported lazily inside
the functions that need them so the module stays importable — and
the Reverto test suite stays runnable — on a bare paper-install
without the ML extras. When a dependency is missing the affected
step is logged and skipped rather than aborting the whole pipeline.

Usage
-----
    python ml/nightly_pipeline.py --bot indi_group_test
    python ml/nightly_pipeline.py --bot btc_bot --db /path/to/reverto.db
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Step 1: load deal history ────────────────────────────────────────────────

def load_deal_history(
    db_path: str, bot_slug: str, user_id: int = 1,
) -> pd.DataFrame:
    """Read the closed-deals ledger for a single bot.

    Only closed deals are returned — open deals don't yet have a
    realised pnl_btc / pnl_pct to train on. Sorted by opened_at so
    later time-series cross-validation in training can use a
    chronological split without re-sorting.

    ``user_id`` is required by the multi-tenant schema (v3+).
    Defaults to 1 (admin) so the Phase-1 cron wiring doesn't have
    to know about sessions yet; Phase 2 will read it from the bot's
    owning user folder.
    """
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(
            """
            SELECT *
              FROM deals
             WHERE user_id = ? AND bot_slug = ? AND closed_at IS NOT NULL
             ORDER BY opened_at
            """,
            conn,
            params=[user_id, bot_slug],
        )
    finally:
        conn.close()

    logger.info("Loaded %d deals for %s", len(df), bot_slug)
    return df


# ── Step 2: train entry filter ──────────────────────────────────────────────

def train_entry_filter(
    deals_df: pd.DataFrame,
    feature_store: list[dict],
) -> dict:
    """Train the XGBoost entry-filter classifier on aligned features.

    The caller is expected to have built ``feature_store`` by running
    ``ml.features.compute_features`` on the candles-at-entry for each
    deal in ``deals_df`` — the order of the two collections must
    match so TimeSeriesSplit can carve a chronologically honest
    train/val split.

    Returns a result dict that is safe to JSON-dump. Fails soft:
    insufficient data or missing xgboost → the function returns
    a status dict instead of raising.
    """
    if len(feature_store) < 20:
        logger.warning(
            "Too few deals (%d) to train entry filter — need >= 20",
            len(feature_store),
        )
        return {"skipped": True, "reason": "insufficient_data"}

    try:
        import xgboost as xgb
    except ImportError:
        logger.warning("xgboost not installed — skipping entry filter training")
        return {"skipped": True, "reason": "xgboost_missing"}

    try:
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit
    except ImportError as e:
        logger.warning("scikit-learn / joblib missing: %s", e)
        return {"skipped": True, "reason": "sklearn_missing"}

    features_df = pd.DataFrame(feature_store)
    X = features_df.drop(["won", "pnl_pct"], axis=1, errors="ignore")
    y = (features_df.get("pnl_pct", pd.Series([0] * len(features_df))) > 0).astype(int)

    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric="logloss",
        verbosity=0,
    )

    # Chronological cross-validation — the dataset is sorted by opened_at
    # so TimeSeriesSplit guarantees training folds always precede their
    # validation fold. min() clamps n_splits when the dataset is tiny.
    n_splits = max(2, min(5, len(X) // 5))
    tscv = TimeSeriesSplit(n_splits=n_splits)
    scores: list[float] = []
    for train_idx, val_idx in tscv.split(X):
        model.fit(X.iloc[train_idx], y.iloc[train_idx])
        preds = model.predict_proba(X.iloc[val_idx])[:, 1]
        if len(set(y.iloc[val_idx])) > 1:
            scores.append(float(roc_auc_score(y.iloc[val_idx], preds)))

    # Train the final model on ALL data — CV scores above are only
    # used as a quality estimate, the deployed model sees everything.
    model.fit(X, y)

    model_dir = Path(__file__).parent / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_file = model_dir / "entry_filter.pkl"
    # Atomic write — a crash mid-dump would otherwise leave a
    # half-written .pkl that the next EntryFilter load would silently
    # fail-open on, masking the training failure.
    from ml.model_io import atomic_dump_model
    atomic_dump_model(model, model_file)

    result = {
        "n_deals": int(len(deals_df)),
        "n_features": int(X.shape[1]),
        "win_rate": float(y.mean()),
        "roc_auc_cv_mean": float(np.mean(scores)) if scores else None,
        "roc_auc_cv_std": float(np.std(scores)) if scores else None,
        "model_path": str(model_file),
        "model_saved": True,
    }
    logger.info("Entry filter trained: %s", result)
    return result


# ── Step 3: parameter optimisation ──────────────────────────────────────────

def optimize_parameters(bot_slug: str, deals_df: pd.DataFrame) -> dict:
    """Bayesian parameter search placeholder.

    Stubbed against the real backtest engine until the operator wires
    in the integration — for now the objective returns 0 so the
    pipeline can exercise the Optuna import path without polluting
    real results. When the real backtest hook lands, replace the
    ``return 0.0`` with a call to ``BacktestEngine(...).run()`` and
    return the Sortino ratio.
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        logger.warning("optuna not installed — skipping parameter optimization")
        return {"skipped": True, "reason": "optuna_missing"}

    config_path = Path(__file__).parent.parent / "config" / "bots" / f"{bot_slug}.yaml"
    if not config_path.exists():
        logger.warning("Bot config not found: %s", config_path)
        return {"skipped": True, "reason": "config_missing"}

    logger.info("Starting parameter optimisation for %s", bot_slug)

    def objective(trial: "optuna.Trial") -> float:
        trial.suggest_float("tp_pct", 0.5, 6.0)
        trial.suggest_float("dca_spacing", 0.3, 4.0)
        trial.suggest_float("dca_multiplier", 1.0, 2.5)
        trial.suggest_int("max_dca_orders", 2, 10)
        # TODO: replace placeholder with BacktestEngine.run() using
        # `params` to override the YAML config, returning the Sortino
        # ratio from the result. The harness is deliberately silent
        # for now so the CI/cron entry-point doesn't crash on fresh
        # installs that lack a wired backtest runner.
        return 0.0

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=50, show_progress_bar=False)

    return {
        "best_params": dict(study.best_params),
        "best_value": float(study.best_value),
        "n_trials": len(study.trials),
    }


# ── Feature-store builder ───────────────────────────────────────────────────


def _get_bot_symbol(bot_slug: str) -> str:
    """Read the pair from a bot's YAML config. Falls back to BTC/USD
    when the file is missing or malformed — the ML pipeline should
    still try to produce SOMETHING rather than abort on a config typo."""
    import yaml

    config_path = (
        Path(__file__).parent.parent / "config" / "bots" / f"{bot_slug}.yaml"
    )
    if not config_path.exists():
        logger.warning("Bot config not found for %s — defaulting to BTC/USD", bot_slug)
        return "BTC/USD"
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Bot config parse failed for %s (%s) — defaulting", bot_slug, e)
        return "BTC/USD"
    pair = (cfg.get("bot") or {}).get("pair") or "BTC/USD"
    return str(pair)


def _get_bot_timeframe(bot_slug: str) -> str:
    """Same lookup pattern as _get_bot_symbol, for the candle
    resolution. Defaults to 1h when unset — the bot's own default."""
    import yaml

    config_path = (
        Path(__file__).parent.parent / "config" / "bots" / f"{bot_slug}.yaml"
    )
    if not config_path.exists():
        return "1h"
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return "1h"
    return str((cfg.get("bot") or {}).get("timeframe") or "1h")


def _build_feature_store(
    deals_df: pd.DataFrame, bot_slug: str,
) -> list[dict]:
    """Iterate deals and build a list of feature dicts aligned with
    ``deals_df`` order. Per-deal failures are logged + skipped so one
    bad candle fetch doesn't torpedo the whole run.

    The returned list is SHORTER than deals_df when some deals can't
    be featurised (not enough candle history, fetch failure, etc.).
    train_entry_filter pairs labels with features by enumerating
    ``feature_store`` directly — it no longer needs to line up with
    deals_df's index, so the shrink is safe.
    """
    from ml.candle_loader import load_candles_for_deal
    from ml.features import compute_features

    symbol = _get_bot_symbol(bot_slug)
    timeframe = _get_bot_timeframe(bot_slug)
    logger.info("Feature build: symbol=%s timeframe=%s", symbol, timeframe)

    feature_store: list[dict] = []
    for _, deal in deals_df.iterrows():
        deal_dict = deal.to_dict()
        try:
            candles = load_candles_for_deal(
                deal_dict,
                symbol=symbol,
                timeframe=timeframe,
                lookback_periods=100,
            )
        except Exception as e:  # noqa: BLE001 — defensive catch-all
            logger.debug(
                "Candle load failed for deal %s: %s", deal_dict.get("id"), e,
            )
            continue
        if not candles:
            continue
        try:
            features = compute_features(candles, deal_dict)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "Feature compute failed for deal %s: %s", deal_dict.get("id"), e,
            )
            continue
        if not features:
            continue
        features["deal_id"] = deal_dict.get("id")
        features["pnl_pct"] = float(deal_dict.get("pnl_pct") or 0.0)
        features["won"] = 1 if (deal_dict.get("pnl_pct") or 0) > 0 else 0
        feature_store.append(features)
    return feature_store


# ── Pipeline runner ─────────────────────────────────────────────────────────

def run_pipeline(bot_slug: str, db_path: str) -> dict:
    """Run the three-step nightly pipeline and return a summary dict.

    Every step reports its own success/skip reason; a step failing
    (e.g. optuna missing) never aborts later steps. The final summary
    is persisted to ``ml/results_{bot_slug}.json`` so an operator
    diffing last-night vs tonight can spot parameter drift at a glance.
    """
    logger.info("=== Nightly ML Pipeline: %s ===", bot_slug)
    logger.info("Started at %s", datetime.now().isoformat())

    results: dict = {"started_at": datetime.now().isoformat(), "bot_slug": bot_slug}

    deals_df = load_deal_history(db_path, bot_slug)
    results["deals_loaded"] = int(len(deals_df))

    if len(deals_df) == 0:
        logger.warning("No deals found — pipeline skipped")
        results["skipped"] = True
        results["reason"] = "no_deals"
        _persist_results(bot_slug, results)
        return results

    # Candle loading + per-deal feature-store build. Each deal gets
    # re-fetched historical OHLCV via ml.candle_loader (CSV-cached so
    # repeated runs don't hammer Bitget). Individual failures skip the
    # deal rather than aborting the batch — a couple of missing
    # candles shouldn't kill tonight's training.
    feature_store = _build_feature_store(deals_df, bot_slug)
    results["features_computed"] = len(feature_store)
    results["features_skipped"] = int(len(deals_df)) - len(feature_store)
    logger.info(
        "Feature store: %d / %d deals produced features",
        len(feature_store), len(deals_df),
    )

    results["entry_filter"] = train_entry_filter(deals_df, feature_store)
    results["optimization"] = optimize_parameters(bot_slug, deals_df)

    results["finished_at"] = datetime.now().isoformat()
    _persist_results(bot_slug, results)
    return results


def _persist_results(bot_slug: str, results: dict) -> Path:
    """Write the pipeline summary to ml/results_{bot_slug}.json."""
    out_path = Path(__file__).parent / f"results_{bot_slug}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("Pipeline complete. Results: %s", out_path)
    return out_path


if __name__ == "__main__":
    # Only insert the project root on actual CLI invocation — import-time
    # sys.path mutation was a side effect that complicated test setup and
    # shadowed real import errors. The cron entry-point still needs it
    # because it runs `python ml/nightly_pipeline.py` from an arbitrary
    # cwd and needs to find the sibling `core`, `ml`, `config` packages.
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Reverto Nightly ML Pipeline")
    parser.add_argument("--bot", required=True, help="Bot slug (e.g. indi_group_test)")
    parser.add_argument(
        "--db",
        default="logs/reverto.db",
        help="Path to SQLite database (default: logs/reverto.db)",
    )
    args = parser.parse_args()

    # Canonicalize --db and refuse anything outside the project root.
    # The CLI is nominally trusted (operators run it directly) but
    # an accidental `--db /etc/passwd` or a symlink trick is still
    # worth rejecting with a clear error rather than letting sqlite3
    # open it.
    BASE_DIR = Path(__file__).parent.parent.resolve()
    db_path = Path(args.db).resolve()

    try:
        db_path.relative_to(BASE_DIR)
    except ValueError:
        print(f"Error: --db must be within {BASE_DIR}", file=sys.stderr)
        sys.exit(1)

    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    run_pipeline(args.bot, str(db_path))
