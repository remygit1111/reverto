# Plugin Split — Phase 1 Audit

> **Status.** Phase 1 deliverable (analysis only, no code changes). Companion documents: [plugin_split_design.md](plugin_split_design.md), [plugin_split_migration.md](plugin_split_migration.md).
>
> **Scope.** Catalogue every file in the codebase as framework-only (A), live-plugin-only (B), or shared-but-coupled (C). Inform the design of a `TradingEngine` base class, a `LiveProvider` interface, and a migration plan.

---

## 0. Executive summary

The codebase is **far less coupled than the multiple-large-file footprint suggests**. The audit found:

- **8 runtime coupling points** between framework and live trading (5 in `web/`, 3 elsewhere). Each is a single `if cfg.mode == Mode.LIVE` branch or an inline string compare.
- **LiveEngine adds 3 methods and overrides 3** on PaperEngine. The remaining ~30 PaperEngine methods are inherited as-is, of which ~13 are pure trading logic and ~20 are infrastructure (state I/O, notifications, market-data caches).
- **5 test files** out of ~120 are plugin-only. The rest are framework or framework-with-paper-fixtures. No test currently exercises both engines in the same process, so no test needs splitting.
- **`exchanges/bitget.py` and `exchanges/kraken.py`** are 100% live-only (order placement, leverage, idempotency). They move to the plugin as-is. `exchanges/public_exchange.py` and `exchanges/base_exchange.py` stay in the framework with read-only API contracts.
- **Two real coupling leaks** in the framework that need callback injection rather than ABC inheritance: `core/circuit_breaker.py` and `exchanges/public_exchange.py` each import `notifications/telegram.py` to fan out a "circuit breaker permanently open" message. Neither is live-specific in principle (paper bots also use circuit breakers), but the Telegram fan-out is a plugin-style optional dependency.

The split is **mostly mechanical** at the file level. The non-trivial work is the `PaperEngine → TradingEngine + PaperEngine` extract-base-class refactor and the introduction of an `OrderProvider`-style seam where the engine currently calls `self._place_market_order` directly.

---

## 1.1 Full coupling inventory

### Convention

- **A — Framework only.** No live trading awareness. Stays in the open-source repo verbatim.
- **B — Live plugin only.** Entire file moves to the closed-source plugin.
- **C — Shared, currently coupled.** Framework code with an inline live-aware branch, an import from `live/`, or a runtime dependency that has to be inverted via an interface or callback.

### 1.1.1 Top-level runners

| File | LoC | Cat | Coupling | Notes |
|---|---:|---|---|---|
| `main_paper.py` | 255 | A | None (validates `mode != PAPER` and exits) | Stays as-is. Hard mode-check is a defensive guard, not coupling. |
| `main_live.py` | 377 | B | Entire file is the live runner | Moves to plugin. Contains `_authenticated_exchange()`, contract-type compat check, the live banner, the operator confirmation prompt, and the `LiveEngine` construction. |
| `main_backtest.py` | 177 | A | None | Backtest is part of the OSS framework. |
| `main_scheduler.py` | 324 | A | None (snapshots portfolio per account; mode-agnostic) | Stays. |
| `main_web.py` | small | A | None | Stays. |

### 1.1.2 Engines

| File | LoC | Cat | Coupling | Notes |
|---|---:|---|---|---|
| `paper/paper_engine.py` | 1913 | C | No live imports, but is the inheritance parent of `LiveEngine` | Refactor: extract `TradingEngine` base into `core/trading_engine.py`; leave a thinner `PaperEngine(TradingEngine)` that owns the simulated-balance accounting. |
| `paper/paper_state.py` | 277 | A | None | Dataclasses for deals/orders/state. Framework. |
| `paper/state_io.py` | 410 | A | None | JSON serialisation, atomic write. Framework. |
| `paper/close_handler.py` | 442 | A | None | Deal-close orchestration. Framework — both engines use it. |
| `paper/errors.py` | 185 | A | None | Exception classification. Framework. |
| `live/live_engine.py` | 324 | B | Inherits PaperEngine; imports `notifications/telegram.py`, `core/clock_monitor.py`, `live/order_reconciliation.py` | Becomes the plugin's `RealOrderProvider` plus the `LiveEngine(TradingEngine)` subclass. The clock-skew gate and reconciler tick stay in the plugin. |
| `live/order_reconciliation.py` | 259 | B | None outward; only inward from `live_engine.py` | Pure plugin module. |
| `backtest/backtest_engine.py` | 418 | A | None | Browser-side simulation. Framework. |
| `backtest/backtest_report.py` | 184 | A | None | Stats reporting. Framework. |

### 1.1.3 `core/`

The `core/` package is overwhelmingly framework. Two files need callback injection:

| File | LoC | Cat | Coupling |
|---|---:|---|---|
| `core/database.py` | 1017 | A | None — generic SQLite + thread-safe pool. |
| `core/deal_store.py` | 754 | A | None — deal/order ledger ops, mode-agnostic. |
| `core/credentials.py` | 598 | A | None — Fernet encrypt/decrypt; used by both engines. |
| `core/user_store.py` | 478 | A | None — auth + user table. |
| `core/exchange_account_store.py` | 440 | A | None — per-user account metadata. |
| `core/roadmap_store.py` | 425 | A | None — admin tile. |
| `core/paths.py` | 399 | A | None — path layout. |
| `core/telegram_config_store.py` | 318 | A | None — per-user notify prefs (used by both modes). |
| `core/circuit_breaker.py` | 283 | **C** | Imports `notifications/telegram.py` + `telegram_config_store` inside `_make_permanent_open_callback`. The callback is already injectable via constructor (`on_permanent_open=`); the leak is the *default factory* that wires the Telegram fan-out without an opt-in. |
| `core/changelog_store.py` | 272 | A | None. |
| `core/audit_findings_store.py` | 257 | A | None. |
| `core/price_feed.py` | 234 | A | None — CoinGecko/Bitget USD pricing. |
| `core/portfolio_store.py` | 230 | A | None — snapshot append. |
| `core/liquidation_guard.py` | 217 | A | None — pure math + threshold checks. |
| `core/markets.py` | 215 | A | None — markets registry. |
| `core/logging_setup.py` | 206 | A | None. |
| `core/drawdown_guard.py` | 163 | A | None — used by both engines. |
| `core/totp.py` | 175 | A | None. |
| Other small `core/*.py` files | <150 each | A | None |

**Coupling detail — `core/circuit_breaker.py` lines 64–142.** `_make_permanent_open_callback()` is constructed at module import and imports `notifications.telegram.TelegramNotifier` plus `core.telegram_config_store`. Both `PublicExchange` and `LiveEngine` use this circuit breaker, so the framework cannot just drop the callback. Fix: the callback factory moves to the plugin and is *registered* with the breaker at framework boot if the plugin is installed (`core.plugin_loader.load_live_provider().on_breaker_permanent_open`); otherwise the breaker logs locally and stops.

### 1.1.4 `config/`

| File | LoC | Cat | Coupling |
|---|---:|---|---|
| `config/config_loader.py` | 42 | A | None — YAML → `BotConfig`. |
| `config/models.py` | 227 | **C** | The `Mode` enum has `LIVE`. `BotConfig.exchange_account_id` is required for live, optional in spirit for paper/backtest (a paper bot does not need real exchange credentials). |

See §1.4 for full Mode-enum analysis and §2.7 of the design doc for the recommended split (`BaseBotConfig` + `LiveBotConfig`).

### 1.1.5 `exchanges/`

| File | LoC | Cat | Coupling |
|---|---:|---|---|
| `exchanges/base_exchange.py` | 195 | A | None — ABC with read-only methods (`get_ticker`, `get_ohlcv`) and trading methods (`place_market_order` etc.) that subclasses implement. |
| `exchanges/public_exchange.py` | 312 | **C** | Same Telegram-callback leak as `core/circuit_breaker.py` (see lines 64–142). Read-only methods are framework-clean; trading methods raise `NotImplementedError` (safe default). |
| `exchanges/bitget.py` | 355 | B | 100% live-trading — order placement, idempotent retries with client order IDs, leverage configuration. Moves to plugin. |
| `exchanges/kraken.py` | 214 | B | 100% live-trading. Moves to plugin. |

Recommendation: keep `base_exchange.py` and `public_exchange.py` in framework. The framework's read-only data path uses `PublicExchange`; the plugin's `BitgetExchange` / `KrakenExchange` inherit from `BaseExchange` and override the trading methods.

### 1.1.6 `strategies/`, `notifications/`, `ml/`

| Path | Cat | Coupling |
|---|---|---|
| `strategies/indicator_engine.py` (596 LoC) | A | Entry/TP signal eval. Framework. |
| `strategies/indicators/*` | A | Indicator implementations. Framework. |
| `notifications/telegram.py` (504 LoC) | **C** | The `TelegramNotifier` is used by both engines (paper + live) for notifications. It is framework-grade infrastructure. The coupling lies in the *circuit-breaker callback factory* (see above), not in the notifier itself. **Stays in framework.** |
| `ml/*.py` | A | Candle loader, feature pipeline, nightly retrain. Framework (unimplemented in production but no live-mode coupling). |

### 1.1.7 `web/routes/` and `web/app.py`

Per the agent audit (see §1.4 below for line-level Mode-enum hits):

| File | LoC | Cat | Coupling |
|---|---:|---|---|
| `web/app.py` | 3759 | **C** | 2 coupling points: `start_bot_dry_run` (line 1960 — refuses non-LIVE bots) and `restart_bot` (line 2050 — dispatches LIVE bots to dry-run). |
| `web/routes/bots.py` | 864 | A | 2 advisory warnings only (lines 417, 443). No behavioural coupling. |
| `web/routes/deals.py` | 552 | A | Comments reference "LiveEngine is Phase 1 dry-run only" but no runtime branches. Phase 3 will need to call `live_provider.cancel_open_orders()` at line ~462; today the close path is mode-agnostic. |
| `web/routes/portfolio.py` | 513 | **C** | `_live_bot_slugs()` at line 115 scans every YAML to filter `mode == "live"`. Used by `/api/portfolio/per-bot` (line 347) to restrict the per-bot breakdown to live bots only. |
| `web/routes/chart.py` | 521 | A | None. |
| `web/routes/admin_bots.py` | 404 | **C** | `admin_start_bot_dry_run` route at line 158 calls `start_bot_dry_run`. Moves to plugin (or stays as a thin proxy that 404s if plugin absent). |
| `web/routes/auth.py` | 1091 | A | None — auth is mode-agnostic. |
| All other route files | — | A | None. |

The 8 runtime coupling points across the entire codebase (excluding `main_live.py` / `live/`) are:

1. `main_paper.py:139` — refuses non-PAPER (defensive guard, not coupling).
2. `main_live.py:181` — refuses non-LIVE (defensive guard, lives in plugin anyway).
3. `web/app.py:1960` — `start_bot_dry_run` rejects non-LIVE bots.
4. `web/app.py:2050` — `restart_bot` dispatches LIVE → dry-run, others → paper.
5. `web/routes/bots.py:417` — advisory warning on base-order size for live.
6. `web/routes/bots.py:443` — advisory warning on missing drawdown guard for live.
7. `web/routes/portfolio.py:141` — `_live_bot_slugs()` filters YAML scan to `mode == "live"`.
8. `paper/paper_engine.py:563/745/794` — `self.config.mode.value` is *logged* (not branched on).

### 1.1.8 `scripts/`

All 13 scripts are **A** (operator-facing infrastructure):

| Script | Purpose |
|---|---|
| `backup.sh`, `restore.sh`, `rollback.sh` | Snapshot/restore/rollback. |
| `migrate_to_user_fs.py`, `reset_db.py`, `wipe_deals.py` | Schema migration & destructive resets. |
| `recover_fernet.py`, `rotate_credentials.py` | Key recovery & rotation. |
| `setup_admin.py`, `totp_admin_reset.py` | Admin account setup. |
| `seed_audit_findings.py` | Audit tracker import. |
| `parity_compare.py` (775 LoC) | Paper-vs-live-dry deal diffing. Reads ledger only; no live execution. |
| `notify.sh` | Manual `beep` notification. |

### 1.1.9 `tests/`

The agent audit classified all ~120 test files. Headline counts:

- **115 Category A** (framework, run without plugin installed)
- **5 Category B** (plugin-only):
  - `test_live_engine.py` — engine scaffolding and dry-run order path
  - `test_order_reconciliation.py` + `test_order_reconciliation_concurrent.py`
  - `test_clock_monitor_wired.py` — the wiring test (the standalone `test_clock_monitor.py` stays in framework)
  - `test_main_live.py` — runner-level smoke tests
- **0 Category C** — no test currently exercises both engines in the same process.

`tests/conftest.py` is framework-clean (isolated tmp DB, isolated marketing-export dir, no `from live.*` imports anywhere). No fixture refactoring required.

---

## 1.2 PaperEngine method classification

Per the agent audit of `paper/paper_engine.py` (1913 LoC, 34 methods).

### Buckets

- **TRADING** (13 methods) — shared trading semantics; extract to `TradingEngine` base.
- **PAPER** (1 method) — `_deduct_balance()` is the only truly paper-specific method; it simulates an insufficient-funds rejection by checking against an in-memory balance that lives in `PaperState`. Live mode replaces this with a hard pre-flight check against the exchange's reported balance.
- **INFRA** (20 methods) — state I/O, notifications, market-data caches, schedule transitions, sentinel handling, logging.

### Detailed table

| Method | Line | Bucket | LiveEngine overrides? | Reason |
|---|---:|---|---|---|
| `_resolve_notify_queue_max` | 103 | INFRA | No | Env-var resolution. |
| `_collect_active_indicator_types` | 174 | INFRA | No | Config introspection. |
| `__init__` | 208 | INFRA | Extends (calls `super().__init__`) | Engine wiring. |
| `_deduct_balance` | 362 | **PAPER** | No | Simulated balance gate. |
| `_db_save_deal` | 386 | INFRA | No | Ledger persistence. |
| `_db_create_deal_with_retry` | 404 | INFRA | No | Collision retry. |
| `_db_save_order` | 446 | INFRA | No | Order ledger. |
| `_db_close_deal` | 457 | INFRA | No | Close audit. |
| `_notify` | 473 | INFRA | No | Async queue enqueue. |
| `_notify_worker` | 510 | INFRA | No | Notification consumer thread. |
| `_write_state` | 552 | INFRA | No | `state.json` writer. |
| `_load_state` | 613 | INFRA | No | State restore + migration. |
| `_clear_state` | 719 | INFRA | No | Mark `state.json` stopped. |
| `start` | 734 | INFRA | No | Main loop entry. |
| `stop` | 756 | INFRA | No | Graceful shutdown. |
| `_tick` | 787 | INFRA | **Yes** (live adds clock-skew gate + reconciler tick) | Main loop body. |
| `_check_schedule_transition` | 962 | INFRA | No | Schedule state changes. |
| `_fetch_closes_if_needed` | 990 | INFRA | No | Candle cache. |
| `_refresh_wick_candle` | 1041 | INFRA | No | Wick refresh. |
| `_wick_high_low` | 1074 | INFRA | No | Market-data transform. |
| `_current_equity_btc` | 1094 | **TRADING** | No | Unrealised PnL. |
| `_update_drawdown_guard` | 1112 | **TRADING** | No | Drawdown evaluation. |
| `_check_manual_trigger` | 1149 | **TRADING** | No | Manual entry gate. |
| `_manual_trigger_liq_safe` | 1172 | **TRADING** | No | Liq-distance check. |
| `_make_close_handler` | 1206 | INFRA | No | Factory. |
| `_check_deal_sentinels` | 1223 | INFRA | No | Portal sentinel I/O. |
| `_format_indicator_log` | 1303 | INFRA | No | Logging helper. |
| `_check_entry` | 1357 | **TRADING** | No | Entry-signal dispatch. |
| `_calc_fee` | 1380 | **TRADING** | No | Taker fee math. |
| `_open_deal` | 1384 | **TRADING** | No | Deal init + base-order fill. |
| `_update_deal_wick_trackers` | 1458 | **TRADING** | No | Per-deal high/low. |
| `_monitor_open_deals` | 1488 | **TRADING** | No | TP/SL/DCA dispatch. |
| `_check_tp` | 1525 | **TRADING** | No | TP trigger. |
| `_check_sl` | 1684 | **TRADING** | No | SL trigger. |
| `_check_dca` | 1817 | **TRADING** | No | DCA threshold + fill. |
| `_update_liq_guard` | 1901 | **TRADING** | No | Position feed to liq guard. |

### Implication for design

After extract-base-class, `PaperEngine` shrinks to ~80 lines — only `_deduct_balance()` and the `_place_market_order()`/`_get_current_price()` simulated implementations. Everything else lives in `TradingEngine`.

`LiveEngine`'s overrides become provider injection rather than inheritance: see §2.2 of the design doc for the proposed shape.

---

## 1.3 Import graph for `live/`

### Outgoing (what `live/` imports)

```
live/live_engine.py
    from config.models import BotConfig              # framework
    from core.clock_monitor import ClockMonitor       # framework
    from exchanges.base_exchange import BaseExchange  # framework
    from live.order_reconciliation import OrderReconciler  # plugin
    from notifications.telegram import TelegramNotifier   # framework
    from paper.paper_engine import PaperEngine        # framework (becomes TradingEngine)

live/order_reconciliation.py
    (stdlib only)
```

After the refactor, `live/live_engine.py`'s imports become:

```
from reverto.config.models import BaseBotConfig     # framework (renamed pkg)
from reverto.core.clock_monitor import ClockMonitor
from reverto.exchanges.base_exchange import BaseExchange
from reverto.notifications.telegram import TelegramNotifier
from reverto.core.trading_engine import TradingEngine   # extracted base
from reverto_live.order_reconciliation import OrderReconciler  # plugin-internal
```

### Incoming (what imports `live/`)

```
main_live.py:
    from live.live_engine import LiveEngine

tests/test_live_engine.py
tests/test_clock_monitor_wired.py
tests/test_order_reconciliation.py
tests/test_order_reconciliation_concurrent.py
tests/test_web_routes.py    # only an import-presence assertion; no runtime use
```

**Critically**: zero `web/routes/*.py` files import `live/` directly. The web layer interacts with live bots through `web/app.py:start_bot_dry_run`, which spawns `main_live.py` as a subprocess. This means the plugin interface boundary is at the **subprocess spawn**, not at a Python-level API call — a much cleaner seam.

This also means the **portal can run without the live plugin installed**. The dry-run endpoint becomes a 404 (or a friendly "live plugin not installed" message); paper bots are unaffected.

---

## 1.4 `Mode` enum usage hotspots

Every reference to `Mode.LIVE`, `Mode.PAPER`, `Mode.BACKTEST`, or the bare strings `"live"` / `"paper"` / `"backtest"` in non-test code:

| File | Line | Code | Check polarity | Post-refactor behaviour |
|---|---:|---|---|---|
| `config/models.py` | 16 | Enum definition | n/a | Keep enum; possibly add `LiveOnly` marker via subclass split (see §2.7). |
| `config/config_loader.py` | 28 | Log line | n/a | No change. |
| `main_paper.py` | 19, 139, 143, 244 | Hard mode gate + log lines | "is paper" | No change. |
| `main_live.py` | 22, 181, 185, 366 | Hard mode gate + log lines | "is live" | Moves to plugin. |
| `paper/paper_engine.py` | 14, 563, 745, 794 | `self.config.mode.value` in log lines | n/a | No change; or substitute `engine.kind()` after refactor. |
| `live/live_engine.py` | 39 | Imports `BotConfig` | n/a | Moves to plugin. |
| `web/app.py` | 26 | Imports `Mode` | n/a | Keep import (used by `start_bot_dry_run`). |
| `web/app.py` | 1960 | `if cfg.mode != Mode.LIVE` | "is live" | After refactor: `live_provider.start_bot_dry_run(user_id, slug)` returns the error itself; framework loses the inline check. |
| `web/app.py` | 2050 | `if cfg.mode == Mode.LIVE` | "is live" | After refactor: `live_provider.is_live_config(cfg)` (returns False if plugin absent → fall through to `start_bot`). |
| `web/routes/bots.py` | 362, 417, 443 | Advisory warnings only | "is live" | Keep as-is. The validator is UI feedback; an operator running framework-only will never see `mode: live` because no live bot can be created without the plugin. |
| `web/routes/portfolio.py` | 141 | `mode.lower() == "live"` | "is live" | After refactor: `_live_bot_slugs()` becomes a `live_provider.list_live_slugs(user_id)` call; if plugin absent, returns empty set. |
| `scripts/parity_compare.py` | 689 | Column header `"live"` in diff output | n/a | No change. |
| `core/liquidation_guard.py`, `core/schedule_guard.py`, `strategies/indicator_engine.py` | various | `from config.models import BotConfig` | n/a | Import path only. |

### Polarity summary

Of the runtime checks:

- **"Is live?"** (6): `web/app.py:1960`, `web/app.py:2050`, `web/routes/bots.py:417`, `web/routes/bots.py:443`, `web/routes/portfolio.py:141`, `main_live.py:181`.
- **"Is paper?"** (1): `main_paper.py:139`.

All "is live?" checks become `live_provider.X()` calls after the refactor. The "is paper?" check stays as a defensive guard.

---

## 1.5 Surprises / concerns surfaced during the audit

1. **The split is structurally tractable.** A 30,000-LoC application has 8 runtime coupling points. That is unusually low and suggests the existing `LiveEngine extends PaperEngine` design was deliberate even if the inheritance direction is upside-down.

2. **Inheritance direction is the only real refactor.** PaperEngine is the parent; LiveEngine is the child. This is awkward because *paper is the simulation of live*, so the conceptual hierarchy is reversed. Inverting to `TradingEngine` as a true base and both `PaperEngine` and `LiveEngine` as siblings will look obvious in hindsight but requires touching ~30 method signatures and rewriting `tests/test_paper_engine.py` fixtures.

3. **The portal-↔-live boundary is already a subprocess.** `start_bot_dry_run` spawns `main_live.py` as a child process and communicates via the PID file plus the bot's state.json file. **No live code runs inside the portal process.** This means the portal can ship plugin-less and the only operator-facing degradation is "Live dry-run" button → 404. The web routes do not need to import `live/` at all.

4. **`notifications/telegram.py` belongs to the framework, not the plugin.** It is the per-user notify service used by paper bots, the portal, and admin alerts. The temptation to call this "live infrastructure" because real-money bots need it is wrong — paper bots send the same restart/error/deal-close notifications.

5. **The circuit-breaker callback leak is the most subtle issue.** `core/circuit_breaker.py` constructs a default callback that imports `notifications/telegram.py` to fan out "exchange unavailable" alerts. The breaker itself is framework-grade (paper bots use it via `PublicExchange`); the *Telegram fan-out* is plugin-style optional behaviour. This is the only place where the framework will need a plugin-registered hook (rather than a plugin-loaded interface) — see design doc §2.6.

---

## 1.6 What is NOT in scope of this audit

- The frontend (`web/static/app.js`, 14k LoC). The plugin split does not touch the JS layer; the existing mode-aware DOM elements (live bot indicator, dry-run button, per-bot portfolio panel) already degrade gracefully when no live bots exist for the user. The 404 from `/api/portfolio/per-bot` is the only API surface that needs a friendly empty-state handler — already present.

- The plugin's commercial concerns (licensing, payment, distribution, obfuscation). These are Phase 4-6 work and are listed only at the design-doc and migration-plan level.

- The decision to actually do the split. This audit is design input; the operator decides go/no-go after reviewing the three documents.
