# Plugin Split: Phase 1 Design

> **Status.** Phase 1 deliverable (design only, no code changes). Companion documents: [plugin_split_audit.md](plugin_split_audit.md), [plugin_split_migration.md](plugin_split_migration.md).
>
> **Premise.** The audit (see companion) found 8 runtime coupling points across ~30 kLoC of non-test Python. The split is mostly mechanical at the file level; the non-trivial work is the engine-base extraction and the introduction of a `LiveProvider` boundary.

---

## 2.1 Architecture overview

```
┌───────────────────────────────────────────────────────────────────────────┐
│                      REVERTO FRAMEWORK (open source)                       │
│                                                                            │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  ┌────────────┐  │
│   │  main_paper  │   │ main_backtest│   │   main_web   │  │ main_sched │  │
│   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘  └─────┬──────┘  │
│          │                   │                  │                │         │
│          ▼                   ▼                  ▼                ▼         │
│   ┌──────────────────────────────────────────────────────────────────┐    │
│   │             core/trading_engine.py  (TradingEngine ABC)           │    │
│   │  Shared loop, indicator gating, DCA/TP/SL, sentinels, drawdown    │    │
│   └────────────────┬────────────────────────────────┬─────────────────┘    │
│                    │                                │                       │
│                    ▼                                ▼                       │
│         ┌──────────────────┐              ┌─────────────────────┐          │
│         │   PaperEngine    │              │   OrderProvider      │  ◄─┐    │
│         │  (TradingEngine) │  ──uses─►   │   (Protocol)         │   │    │
│         └──────────────────┘              └─────────────────────┘   │    │
│                                                     ▲                │    │
│   web/app.py + web/routes/*  ── call ──► plugin_loader.live_provider │    │
│   (paper-only / portal infra)                                        │    │
│                                                                       │   │
└───────────────────────────────────────────────────────────────────────┼───┘
                                                                        │
                                                  load via importlib    │
┌───────────────────────────────────────────────────────────────────────┼───┐
│                  REVERTO-LIVE PLUGIN (closed source)                  │   │
│                                                                       │   │
│   ┌──────────────┐                ┌──────────────────────────────┐   │   │
│   │  main_live   │ ────uses──►    │  LiveEngine(TradingEngine)   │   │   │
│   └──────────────┘                │  + RealOrderProvider          │ ──┘   │
│                                   └──────────────────────────────┘        │
│                                                                            │
│   exchanges/bitget.py    exchanges/kraken.py    live/order_reconciliation │
│   live/license.py        live/provider.py  ◄── entrypoint                 │
│                                                                            │
└───────────────────────────────────────────────────────────────────────────┘
```

**Communication seams:**

1. **Subprocess seam.** Portal calls `plugin_loader.live_provider.start_bot_dry_run(user_id, slug)`, which spawns the plugin's `main_live.py` as an independent child process. No live code runs inside the portal process. *This is already how the codebase works.*
2. **In-process seam.** When the live runner constructs its engine, it injects a `RealOrderProvider` into the shared `TradingEngine` base. *This is the new seam introduced by the refactor.*
3. **Optional callback seam.** Framework `core/circuit_breaker.py` exposes an `on_permanent_open` hook. If the plugin is installed, the plugin registers its Telegram-fanout callback at framework boot; otherwise the breaker logs locally and stops the bot. *This decouples the existing leak.*

**Data flow.** YAML config → `BotConfig` (or `LiveBotConfig` if mode == live) → engine constructor → tick loop → `OrderProvider.place_market_order()` → exchange. The framework never imports the plugin; the plugin always imports the framework.

---

## 2.2 Class hierarchy redesign

### Today

```
PaperEngine                                ← parent (in framework)
    └── LiveEngine(PaperEngine)            ← child (in live/)
```

### Proposed

```
TradingEngine (abc.ABC, in framework)      ← shared trading logic
    ├── PaperEngine(TradingEngine)         ← framework; uses SimulatedOrderProvider
    └── LiveEngine(TradingEngine)          ← plugin;    uses RealOrderProvider

OrderProvider (typing.Protocol, in framework)
    ├── SimulatedOrderProvider             ← framework; in-memory fills + simulated balance
    └── RealOrderProvider                  ← plugin;    ccxt order placement + reconciliation
```

### Class responsibilities

#### `TradingEngine` (framework, `core/trading_engine.py`)

**Owns:**

- Tick loop (`start`, `stop`, `_tick`).
- All `INFRA` methods from the PaperEngine audit (state I/O, notify queue, candle cache, schedule transition, sentinels, logging helpers).
- All `TRADING` methods from the PaperEngine audit (`_check_entry`, `_open_deal`, `_monitor_open_deals`, `_check_tp`, `_check_sl`, `_check_dca`, `_calc_fee`, `_update_deal_wick_trackers`, `_current_equity_btc`, `_update_drawdown_guard`, `_check_manual_trigger`, `_manual_trigger_liq_safe`, `_update_liq_guard`).
- Composes (does not inherit) an `OrderProvider`.

**Abstract methods (must override):**

```python
@abstractmethod
def _get_current_price(self) -> float: ...

@abstractmethod
def _place_market_order(self, side: str, size: float,
                        price: Optional[float] = None) -> dict: ...

@abstractmethod
def kind(self) -> Literal["paper", "live"]: ...
```

**Concrete methods that subclasses *may* override:**

- `_tick()`: LiveEngine overrides to insert clock-skew pre-check + reconciler tick. Marked `_tick_hook_before/_after()` or kept overridable.
- `_deduct_balance()`: paper consults `PaperState.balance_btc`; live consults the real exchange balance. Default in base is a no-op (treat as gateless); subclasses replace.

#### `PaperEngine(TradingEngine)` (framework, `paper/paper_engine.py`)

After refactor, shrinks from 1913 → ~250 LoC. Owns:

- `_deduct_balance()` (the only PAPER-bucket method from the audit).
- `_place_market_order()`: synthetic fill, balance update in `PaperState`.
- `_get_current_price()`: read from polled `Ticker.mark_price` / `Ticker.last`.
- `kind()` returns `"paper"`.

#### `LiveEngine(TradingEngine)` (plugin, `reverto_live/live_engine.py`)

Owns:

- Clock-skew monitor (`ClockMonitor`) and the `_tick()` override that gates on it.
- `OrderReconciler` and the per-N-tick reconciliation pass.
- `_place_market_order()`: dry-run synthetic in Phase 1; ccxt call in Phase 3.
- `_get_current_price()`: real-time ticker via `BaseExchange.get_ticker()`.
- `kind()` returns `"live"`.

### Trade-offs considered

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| ABC vs Protocol for `TradingEngine` | **ABC** | Protocol (PEP 544) | ABC gives `super().__init__()` ergonomics that match the existing engine. Protocols are better for the cross-process plugin interface (`LiveProvider`) where duck-typing is enough; ABCs are better for the in-process engine hierarchy where shared infrastructure matters. |
| Composition vs inheritance for ordering | **Composition** (`OrderProvider`) | Keep inheritance, just rename | Composition isolates the seam: framework tests can inject `SimulatedOrderProvider`; plugin tests inject `RealOrderProvider`. No mock subclass gymnastics. |
| Where `_deduct_balance` lives | **Subclass** | Base with a strategy callback | Only PaperEngine uses simulated balance; LiveEngine has a real exchange balance that the operator topped up out-of-band. A strategy callback would be over-engineered. |
| Single `Engine` class with feature flags | **Rejected** | One class, `mode` field | Mode-flag classes are how the current `PaperEngine`-as-base situation evolved. Inheritance + composition keeps each class doing one thing. |

---

## 2.3 `LiveProvider` interface specification

The plugin's entry-point. Framework calls this; plugin implements it.

**Location in framework:** `core/live_provider.py`.
**Location of implementation in plugin:** `reverto_live/__init__.py` exports `provider` attribute.

```python
# core/live_provider.py (in framework)

from typing import Protocol, Optional, Literal
from config.models import BotConfig


class LiveProvider(Protocol):
    """Interface that the closed-source live plugin implements.

    Framework code calls this; plugin provides the implementation.
    The plugin is loaded lazily via core.plugin_loader.load_live_provider().
    Returns None if the plugin is not installed; callers degrade gracefully.

    Interface version: 1. If a plugin returns a different
    ``interface_version``, the framework refuses to use it and logs a
    descriptive error. Bump this when adding a non-optional method.
    """

    interface_version: int  # = 1

    # ── Bot lifecycle ──────────────────────────────────────────────

    async def start_bot_dry_run(
        self, user_id: int, slug: str,
    ) -> dict:
        """Spawn main_live.py --dry-run as a subprocess for the given
        slug. Returns {"ok": bool, "message"|"error": str}. Same shape
        as framework's start_bot for caller symmetry."""

    async def is_live_config(self, config: BotConfig) -> bool:
        """True iff the config will boot under LiveEngine. Lets
        web/app.py:restart_bot dispatch without importing Mode."""

    # ── Portal data hooks ─────────────────────────────────────────

    async def list_live_slugs(self, user_id: int) -> set[str]:
        """Return slugs of live-mode bots for the user. Replaces the
        inline YAML scan in web/routes/portfolio.py:_live_bot_slugs."""

    # ── Optional callbacks ────────────────────────────────────────

    def on_breaker_permanent_open(
        self, breaker_name: str, reason: str,
    ) -> None:
        """Called by core/circuit_breaker when a breaker latches open
        permanently. Plugin typically fans this out to Telegram for
        every connected user. Framework's default is to log + stop."""
```

### Design constraints

- **Stable interface, evolving implementation.** Add new methods as optional (subclasses of `LiveProvider` can implement them); never change existing signatures without bumping `interface_version`.
- **Minimal surface.** Only what framework genuinely needs. List today: lifecycle (1 method), config introspection (1 method), portal data (1 method), optional callback (1 method). **4 methods total.**
- **Async-first.** Lifecycle methods are async because they spawn subprocesses and the portal is FastAPI.
- **No live concepts leak into the interface.** No `ClockMonitor`, no `OrderReconciler`, no `ccxt`. The plugin hides those.

### Why Protocol, not ABC, for `LiveProvider`

- The plugin lives in a separate package; the framework cannot `from reverto_live import BaseLiveProvider` (circular). Protocols let the plugin satisfy the interface structurally without importing anything from framework.
- Protocols permit easy mocking: tests inject `Mock(spec=LiveProvider)`.
- `interface_version` field is a runtime check, not a type check; Protocol matches that flexibility.

---

## 2.4 `TradingEngine` base class: detailed design

### Constructor

```python
class TradingEngine(abc.ABC):
    def __init__(
        self,
        *,
        config: BotConfig,
        exchange: BaseExchange,
        notifier: TelegramNotifier,
        initial_balance_btc: float = 0.1,
        poll_interval: int = 10,
        state_file: Optional[str] = None,
        manual_trigger_file: Optional[str] = None,
        slug: Optional[str] = None,
        user_id: int = 1,
        exchange_type: str = "",
    ) -> None:
        # Wires:
        #   - PaperState (used by both engines for deal tracking)
        #   - IndicatorEngine, ScheduleGuard, LiquidationGuard, DrawdownGuard
        #   - StateIO, sentinel file, notify queue + worker thread
        ...
```

Identical to today's `PaperEngine.__init__`. The constructor stays in the base because all setup is shared. The only thing the subclasses do extra is:

- `PaperEngine`: nothing; inherits constructor verbatim.
- `LiveEngine`: calls `super().__init__()`, then wires `ClockMonitor` + `OrderReconciler`.

### Method inventory (post-refactor)

All from the PaperEngine audit table (§1.2 of the audit), bucketed as `TRADING` or `INFRA`, move into `TradingEngine`. The single `PAPER` method (`_deduct_balance`) stays in PaperEngine.

Abstract methods on `TradingEngine`:

```python
@abstractmethod
def _get_current_price(self) -> float: ...

@abstractmethod
def _place_market_order(
    self, side: str, size: float, price: Optional[float] = None,
) -> dict: ...

@abstractmethod
def kind(self) -> Literal["paper", "live"]: ...

# Hook with safe default
def _deduct_balance(self, btc_amount: float) -> bool:
    """Override to enforce a balance cap. Base default: no cap."""
    return True
```

### `_tick()` extensibility

LiveEngine adds two pre-tick gates (clock skew) and a per-N-tick reconciliation pass. Two options:

- **Option A: Template method.** Base `_tick()` calls `self._pre_tick_hook()` and `self._post_tick_hook()`. PaperEngine implements both as no-ops; LiveEngine implements clock-skew + reconciliation.
- **Option B: Full override.** LiveEngine overrides `_tick()` entirely (today's behaviour).

**Recommend Option A.** Explicit hook points make the base contract clearer and avoid copy-pasting the 175-line `_tick()` body into the plugin. The hooks can be inlined later if they grow in number.

---

## 2.5 Web routes refactor strategy

Per the audit (§1.1.7), the only web files with runtime coupling are:

| File | Coupling | Post-refactor strategy |
|---|---|---|
| `web/app.py:1960` (`start_bot_dry_run`) | Rejects non-LIVE bots inline | Delegate the entire function to `live_provider.start_bot_dry_run()`. If plugin absent, return `{"ok": false, "error": "Live plugin not installed"}`. |
| `web/app.py:2050` (`restart_bot`) | Dispatches LIVE → dry-run | Replace `cfg.mode == Mode.LIVE` with `await live_provider.is_live_config(cfg)`. If plugin absent → False → always falls through to `start_bot(user_id, slug)`. |
| `web/routes/bots.py:417/443` (validator) | Advisory warnings only | Keep. A framework-only deploy will never see `mode: live` configs because no live bot can be created without the plugin. The warning is dead code in that scenario; harmless. |
| `web/routes/portfolio.py:115/141` (`_live_bot_slugs`) | Inline YAML scan | Replace with `await live_provider.list_live_slugs(user_id)`. If plugin absent → empty set → `/api/portfolio/per-bot` returns `{"bots": []}`. |
| `web/routes/admin_bots.py:158` (`admin_start_bot_dry_run`) | Same as `start_bot_dry_run` | Same delegation. |

### UI degradation when plugin is absent

- **Dry-run button** in the bot detail page: hidden if `await live_provider.is_live_config(cfg)` returns False (i.e. always, when plugin absent).
- **`/api/portfolio/per-bot`** returns `{"bots": []}`. The frontend already renders an empty state, so no JS change is required.
- **Bot creation wizard** does not currently offer "live" as a mode option to non-admin users in the standard build; will be hidden entirely when plugin is absent.
- **Admin emergency-stop** still works for paper bots (it only kills child PIDs); no plugin dependency.

### No new live-only route file

The audit considered adding a `web/routes/live.py` for plugin-specific endpoints. Recommendation: **don't**. The only "live-specific" endpoints today are the two dry-run lifecycle hooks, which are conceptually generic lifecycle operations. Hiding them behind the `live_provider` interface keeps the route file count stable.

If Phase 3+ introduces genuinely live-only routes (e.g. `/api/live/reconciliation-status`), they live in the plugin's own FastAPI router and are mounted at framework boot if the plugin is present:

```python
# main_web.py
from core import plugin_loader

app = FastAPI(...)
# ... mount framework routers ...

live_provider = plugin_loader.load_live_provider()
if live_provider is not None and hasattr(live_provider, "router"):
    app.include_router(live_provider.router, prefix="/api/live")
```

---

## 2.6 Plugin loading mechanism

### Loader

```python
# core/plugin_loader.py (in framework)

import importlib
import logging
from typing import Optional
from core.live_provider import LiveProvider

logger = logging.getLogger(__name__)

_FRAMEWORK_INTERFACE_VERSION = 1
_cached_provider: Optional[LiveProvider] = None
_load_attempted = False


def load_live_provider() -> Optional[LiveProvider]:
    """Lazily load the live-trading plugin. Returns None if absent or
    incompatible. Cached after first call; restart the framework to
    pick up plugin install/upgrade.
    """
    global _cached_provider, _load_attempted
    if _load_attempted:
        return _cached_provider
    _load_attempted = True

    try:
        module = importlib.import_module("reverto_live")
    except ImportError:
        logger.info("reverto-live plugin not installed; live trading disabled")
        return None

    provider = getattr(module, "provider", None)
    if provider is None:
        logger.error("reverto_live loaded but exports no `provider`")
        return None

    plugin_version = getattr(provider, "interface_version", None)
    if plugin_version != _FRAMEWORK_INTERFACE_VERSION:
        logger.error(
            "reverto-live plugin interface version %s incompatible with "
            "framework version %s; live trading disabled",
            plugin_version, _FRAMEWORK_INTERFACE_VERSION,
        )
        return None

    _cached_provider = provider
    logger.info("reverto-live plugin loaded (interface v%d)", plugin_version)
    return provider
```

### Version-mismatch handling

- The framework hardcodes `_FRAMEWORK_INTERFACE_VERSION`. Bump it whenever a method is added to `LiveProvider` that the framework will call unconditionally.
- The plugin exposes `provider.interface_version`. Operators upgrading framework but not plugin (or vice versa) get a clear `incompatible` log line and live trading is disabled; they don't get cryptic `AttributeError` at runtime.
- Optional methods (e.g. `on_breaker_permanent_open`) do not require a version bump; framework checks `hasattr(provider, "on_breaker_permanent_open")` before calling.

### Crash isolation

- Plugin import errors → loader returns None, framework continues.
- Plugin runtime exceptions during a `live_provider.X()` call → bubble up to the caller (`web/app.py:start_bot_dry_run`). The caller already wraps in `try/except` and returns `{"ok": false, "error": ...}` to the operator. **Framework does not crash on plugin errors.**
- Live engine subprocess crashes → already handled by the existing PID-file lifecycle and the registry's stale-PID detection. No plugin-specific work needed.

### Testing with/without plugin

- **Framework CI:** `pip install -e .` only. Plugin absent. `load_live_provider()` returns None. All routes degrade. Test count: 115 (the framework subset from the audit).
- **Plugin CI:** `pip install -e .` for framework + `pip install -e ../reverto-live` for plugin. `load_live_provider()` returns the real provider. Test count: 5 plugin-only tests run in the plugin repo; framework tests stay in framework repo.
- **Integration CI:** A third job in the plugin repo runs the full framework test suite + plugin tests together. Catches surface drift early.

---

## 2.7 Data model considerations

### `Mode` enum

The enum stays; it is a useful YAML serialisation surface, and `Mode.BACKTEST` keeps the trinary semantics. Three options were considered:

| Option | Pros | Cons |
|---|---|---|
| **Keep `Mode` as-is** (recommended) | Zero migration cost. YAML is unchanged. | Framework code still references `Mode.LIVE`. |
| Replace with dynamic check (`live_provider.is_installed()`) | Removes enum from the framework. | YAML still needs *some* discriminator; renaming changes operator UX. |
| Split into `FrameworkMode` (paper/backtest) + plugin's own enum | Cleanest typing. | Operators see two enums for the same YAML field. |

**Recommendation:** keep the enum. Framework code that today reads `cfg.mode == Mode.LIVE` switches to `await live_provider.is_live_config(cfg)`, but the enum itself stays for log lines, YAML validation, and the in-process `kind()` check.

### `BotConfig` split

The audit (§1.1.4) classifies each field:

| Field | Required for paper? | Required for live? | Recommendation |
|---|---|---|---|
| `name`, `pair`, `contract_type`, `direction`, `timeframe` | yes | yes | Stay in `BotConfig`. |
| `dca`, `entry`, `take_profit`, `stop_loss`, `ml`, `schedule`, `drawdown_guard`, `use_wick_simulation` | yes | yes | Stay in `BotConfig`. |
| `leverage` | optional (default disabled) | typically required | Stay in `BotConfig`. Paper bots ignore it. |
| `mode` | yes | yes | Stay in `BotConfig`. |
| `exchange_account_id` | yes (paper uses the public client linked to the account; the user_id link is still needed for per-user state path) | yes | Stay in `BotConfig`. |

**Recommendation:** do NOT split into `BaseBotConfig` + `LiveBotConfig`. The fields are all valid for both modes; the only field that's *load-bearing* differently is `mode` itself. A subclass split would force operators to think about which config schema applies, which is a regression in UX. If genuinely live-only fields show up later (e.g. `live_only: LiveOnlyConfig` with reconciliation tuning, kill-switch hotkeys, OFAC opt-outs), add them as `Optional[LiveOnlyConfig] = None` so paper YAMLs validate without it.

---

## 2.8 Distribution and packaging

### Naming

- **Framework PyPI package:** `reverto` (no namespace prefix). Currently the repo isn't pip-installable; the refactor makes it so.
- **Plugin PyPI package:** `reverto-live`. Hyphen for distribution name (PyPI convention); module import name `reverto_live` (PEP 8). Matches the existing `reverto-internal-docs` sibling.

### Versioning strategy

- **Framework:** SemVer, independent. `0.x` until first commercial release; `1.0` when API stabilises.
- **Plugin:** SemVer, aligned with `interface_version` rather than framework version. Plugin's `install_requires` pins `reverto>=X.Y,<X+1` to gate against incompatible framework upgrades.
- **`interface_version` is the contract.** Framework and plugin can release at independent cadences; the only hard coupling is the integer.

### Dependencies

- Framework declares its dependencies only (ccxt, fastapi, pydantic, sqlite3, etc.).
- Plugin declares: `reverto>=X.Y,<X+1`, plus any plugin-only deps (e.g. a license-validation library).
- ccxt stays a framework dep (the framework already uses it for read-only market data via `PublicExchange`).

### Distribution channel

| Stage | Channel |
|---|---|
| Phase 2-3 (refactor in progress) | Plugin lives in a separate private GitHub repo; framework installs it via git URL in dev env. |
| Phase 4-5 (first commercial users) | Plugin distributed as a tarball gated on license server. Operator runs `pip install ./reverto_live-1.0.0.tar.gz` after purchase. |
| Phase 6+ (mature) | Private PyPI index (e.g. `--index-url https://pypi.reverto.bot/simple/` with token auth). |

PyPI public listing is **not recommended**. The plugin is closed-source and a public listing invites scrapers, mirror sites, and license bypass attempts.

### Installation, upgrade, licence delivery

```bash
# Operator install (Phase 5+):
pip install reverto                                    # framework, from public PyPI
pip install ./reverto_live-1.0.0-cp311-none-any.whl    # plugin, from purchased download

# Upgrade:
pip install --upgrade reverto reverto-live

# License token (option A: env var, recommended):
export REVERTO_LIVE_LICENSE_KEY=...        # in .env, read by plugin at import
make start
```

License-token delivery via env var is the same surface as Telegram bot tokens and Fernet keys; the operator already keeps `.env` secret. CLI args were considered and rejected (would leak into `ps` output).

---

## 2.9 Testing strategy

### Framework CI (no plugin)

- Runs `pytest tests/` with plugin uninstalled.
- Expected pass count: 115 tests (per audit §1.1.9), 2 skipped.
- Verifies: framework boots without plugin, all routes degrade gracefully, `load_live_provider()` returns None, no `ImportError`.
- A dedicated `tests/test_framework_standalone.py` asserts that `live_provider is None` and that key portal routes work (e.g. `GET /api/portfolio/latest` returns 200 even when per-bot endpoint would 404).

### Plugin CI (plugin installed against framework)

- Runs `pytest tests/` for plugin tests only (the 5 plugin-test files moved from framework `tests/`).
- Plugin tests live in `reverto-live/tests/` and import `from reverto_live import ...`.
- Verifies: provider satisfies the `LiveProvider` Protocol structurally, dry-run order log behaves, reconciler timeouts trigger, clock-skew gate fires.

### Integration CI (both installed)

- A job in the plugin repo installs both framework and plugin, then runs the **full framework test suite** + **plugin test suite** together.
- Verifies: provider integration doesn't break framework tests, mode-aware routes behave correctly with a live config, restart dispatch hits the dry-run path.
- This is where current `tests/test_web_routes.py` cases that touch `restart_bot` get verified end-to-end.

### Mock provider for framework tests of live-aware routes

A few framework tests touch the `restart_bot` dispatch (currently inline `Mode.LIVE`). Post-refactor they will need a mocked `LiveProvider` to verify both branches without the plugin installed. Recommendation:

```python
# tests/conftest.py (framework)
@pytest.fixture
def mock_live_provider(monkeypatch):
    """Inject a fake LiveProvider so framework tests can exercise
    the "live config detected" branch without installing the plugin."""
    fake = Mock(spec=LiveProvider)
    fake.interface_version = 1
    fake.is_live_config = AsyncMock(return_value=False)  # default
    fake.list_live_slugs = AsyncMock(return_value=set())
    monkeypatch.setattr(plugin_loader, "_cached_provider", fake)
    monkeypatch.setattr(plugin_loader, "_load_attempted", True)
    return fake
```

This is the **only** new conftest fixture required. The existing 115 framework tests need no changes.

### What does NOT need to change

- `tests/test_paper_engine.py`: instantiates `PaperEngine`, which is still in the framework. Imports may need to drop the `Mode.LIVE` reference (currently absent) but otherwise verbatim.
- `tests/test_state_recovery.py`: paper-only flow. Stays.
- `tests/test_drawdown_guard.py`: guard is framework. Stays.
- `tests/test_web_routes.py`: paper bots only. Stays. (A new test for the live-dispatch branch needs the mock provider, but the existing tests pass unchanged.)

---

## 2.10 Open design questions deferred to the operator

These are **not blocking** Phase 1 acceptance but **must** be resolved before Phase 2 begins:

1. **Should `LiveEngine` move to the plugin in the same PR as the `TradingEngine` extraction, or in a follow-up?** Same-PR is cleaner (one atomic refactor); follow-up is safer (extract base first, validate paper still works, then move live).
2. **Where does `live/license.py` live during Phase 2?** Plugin doesn't exist yet as a separate repo. Suggest a `live/` subtree that is mechanically extractable later.
3. **Does the plugin need its own `TelegramNotifier` or share the framework's?** Shared is simpler; the only plugin-specific concern is license/quota notifications which can be added as a `notify_kind="license"` value in the framework notifier.

(See migration plan §3.3 for the full operator-input list.)
