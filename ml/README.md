# Reverto ML pipeline

Optional machine-learning layer on top of Reverto's paper/backtest
engines. Everything here is **additive** — the paper engine runs fine
without any of this, and the filter fails open when a model is
missing, so a fresh clone can ignore the pipeline entirely until
enough deal history has accumulated.

## Layout

```
ml/
├── __init__.py
├── candle_loader.py     # load_candles_for_deal() — historical OHLCV + cache
├── features.py          # compute_features() — indicator/context features
├── market_regime.py     # KMeans regime classifier
├── entry_filter.py      # EntryFilter — XGBoost gate, fail-open
├── nightly_pipeline.py  # Cron-driven training + param search
├── models/              # Persisted classifiers (gitignored)
├── candle_cache/        # Per-day CSV cache for ccxt fetches (gitignored)
└── README.md

notebooks/
└── reverto_analysis.ipynb  # Exploratory deal / regime / feature analysis
```

### Candle loader

`ml/candle_loader.py` resolves the OHLCV window that preceded a
deal's entry via `exchanges.public_exchange.PublicExchange` (no
credentials needed — public market data only). Results are cached
as CSV in `ml/candle_cache/` at per-day granularity so repeated
nightly runs hit the local cache instead of the Bitget API.

- First run per day per (symbol, timeframe) = one ccxt fetch + cache write.
- Subsequent runs on the same day = cache hit, zero API calls.
- CSV (not parquet) keeps `requirements-ml.txt` free of the pyarrow
  dependency and lets operators `less` a cache file when debugging.

Force-refresh the cache (e.g. after a ccxt upgrade or suspected
stale data):

```python
from ml.candle_loader import clear_cache
clear_cache()
```

## Installation

The ML extras are optional — add them to your venv only when you
want to run training or the analysis notebook:

```bash
.venv/bin/pip install -r requirements-ml.txt
```

Or individually:

```bash
.venv/bin/pip install \
    optuna xgboost lightgbm scikit-learn joblib \
    jupyter matplotlib seaborn plotly \
    numba llvmlite scipy
```

`numpy` and `pandas` are already part of the base requirements, so
feature / regime code stays importable even without the extras above
(the ML entry-points each fail-open when their optional dep is
missing — see the "Fail-open safety net" table below).

## Usage

### Analysis notebook

```bash
cd ~/reverto
jupyter notebook notebooks/reverto_analysis.ipynb
```

The notebook reads `logs/reverto.db` directly — no portal needed.

### Nightly pipeline (manual run)

```bash
.venv/bin/python ml/nightly_pipeline.py --bot indi_group_test
```

Outputs:
- `ml/models/entry_filter.pkl` — retrained XGBoost classifier
- `ml/models/regime_model.pkl` / `regime_scaler.pkl` — regime artifacts
- `ml/results_<bot_slug>.json` — summary of the run

### Scheduled (cron)

```cron
# Reverto ML — train nightly at 23:05 local time
5 23 * * * cd ~/reverto && .venv/bin/python ml/nightly_pipeline.py \
    --bot indi_group_test >> logs/ml.log 2>&1
```

## Pipeline phases

The ML surface expands as deal history accumulates:

1. **Now** — `features.compute_features()` + analysis notebook.
   Zero runtime cost, no model files needed.
2. **After ~4 weeks of data** — enable `entry_filter.EntryFilter`
   inside the paper engine as an optional gate. Fail-open contract
   means a broken model never blocks entries.
3. **After ~8 weeks** — wire the Optuna search in
   `nightly_pipeline.optimize_parameters` to a real backtest runner
   and start acting on its suggestions (currently the objective is
   stubbed to `return 0.0`).
4. **After ~3 months** — consider a regime-conditional model family
   (one classifier per regime returned by `market_regime`).

## Fail-open safety net

Every public entry point returns a safe default when its dependency
or model is missing:

| Call                                 | No model / missing dep | Result           |
|--------------------------------------|------------------------|------------------|
| `EntryFilter().predict(features)`    | no `entry_filter.pkl`  | `(1.0, True)`    |
| `detect_current_regime(candles)`     | no regime model        | `"unknown"`      |
| `train_entry_filter(...)`            | xgboost missing        | `{skipped: True}`|
| `optimize_parameters(...)`           | optuna missing         | `{skipped: True}`|

This lets an operator ship `ml/` to production without forcing the
ML extras into the runtime image — the engine keeps its baseline
behaviour and ML acts only when the artifacts are present.
