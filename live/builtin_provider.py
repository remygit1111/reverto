"""BuiltinLiveProvider — temporary in-tree LiveProvider scaffold.

⚠️ THIS FILE IS TEMPORARY SCAFFOLDING ⚠️

Phase 3.7 of the plugin-split migration plan deletes this entire
file when the real `reverto-live` pip package becomes available.
See docs/plugin_split_migration.md §3.1 (Task 2.4) and Phase 3.7
for the migration plan.

## Purpose

This class implements the LiveProvider Protocol (core/live_provider.py)
by wrapping the still-in-tree `live/` package. It exists during the
2-week interim window between:
- Phase 2 (TradingEngine extract + plugin seam introduced)
- Phase 3 (live/ physically moves to reverto-live repo)

During this window the framework needs a working LiveProvider so
Tasks 2.5-2.7 can refactor web/ routes against a real provider.

## Why own / snapshot logic from web/ ?

- ``start_bot_dry_run`` is the FULL implementation as of Phase 2
  Task 2.5 (validation + registry gate + subprocess spawn). The
  ownership inverted: ``web/app.py:start_bot_dry_run`` is now a
  thin wrapper that delegates *to* this provider. The framework
  internals it needs (``_BOT_SLUG_RE``, ``_bot_subprocess_env``,
  ``BASE_DIR``, ``PYTHON_BIN``, ``registry``) are imported inside
  the method so there is no circular import at module load
  (core.plugin_loader imports this module). The implementation is
  byte-equivalent to the pre-2.5 web/app.py version.
- ``list_live_slugs`` is a verbatim snapshot of the *current*
  ``web/routes/portfolio.py:_live_bot_slugs`` (the real logic reads
  a nested ``bot:`` block, case-insensitively — not a flat
  top-level ``mode`` key). It is copied so Tasks 2.5-2.7 can rewire
  web/ to call this provider without a behaviour change; circular
  imports prevent importing it from web/ once that rewire happens.

The snapshot is frozen at Task 2.4 time and is NOT maintained as
web/ changes — Phase 3.7 deletes this file entirely.

## What this is NOT

- NOT a production live plugin (that's the future `reverto-live`)
- NOT a long-term abstraction (it's deleted in 2-6 weeks)
- NOT a model for plugin implementations (the real plugin will
  live in a separate repo and have different concerns)
"""

from __future__ import annotations

import logging

import yaml

from config.models import BotConfig, Mode
from core import paths
from core.live_provider import SUPPORTED_INTERFACE_VERSION


logger = logging.getLogger(__name__)


class BuiltinLiveProvider:
    """In-tree LiveProvider implementation.

    Temporary scaffolding — see module docstring.

    Implements the LiveProvider Protocol by wrapping the still-in-tree
    `live/` package. All four Protocol methods are implemented;
    `on_breaker_permanent_open` is a no-op until Task 2.8 refactors
    the circuit-breaker callback wiring.
    """

    interface_version: int = SUPPORTED_INTERFACE_VERSION

    # ── Bot lifecycle ─────────────────────────────────────────────

    async def start_bot_dry_run(
        self, user_id: int, slug: str,
    ) -> dict:
        """Spawn a LIVE-mode bot in dry-run via main_live.py.

        Full implementation: validation + registry gate + subprocess
        spawn. Phase 2 Task 2.5 moved this implementation out of
        web/app.py:start_bot_dry_run to here (that function is now a
        thin delegation wrapper).

        Framework internals (_BOT_SLUG_RE, _bot_subprocess_env,
        BASE_DIR, PYTHON_BIN, registry) are imported lazily inside
        this method to avoid a circular import at module load —
        core.plugin_loader imports this module, and web/app.py is
        fully loaded by the time this method is ever called. This
        tight coupling is intentional and temporary: Phase 3.7
        deletes this file; the real reverto-live plugin owns its own
        copy of this logic with its own architectural choices.

        Byte-equivalent to the pre-2.5 web/app.py implementation:
        same validation order, error messages, env handling,
        subprocess args, PID-wait loop, and try/finally end_start.

        Args:
            user_id: tenant owning the bot
            slug: bot slug (filesystem-safe identifier)

        Returns:
            {"ok": True, "message": str} on success
            {"ok": False, "error": str} on failure
        """
        import asyncio
        import subprocess
        import time

        from web.app import (
            _BOT_SLUG_RE,
            _bot_subprocess_env,
            BASE_DIR,
            PYTHON_BIN,
            registry,
        )
        from config.config_loader import load_bot_config

        # Defense-in-depth: the route handler validates slug via
        # _BOT_SLUG_RE and main_live.py's own regex re-validates. Belt-
        # and-braces here so any non-route caller (tests, scripts) still
        # gets a safe early-exit instead of reaching subprocess.Popen.
        if not _BOT_SLUG_RE.match(slug):
            return {"ok": False, "error": f"Invalid bot slug: {slug!r}"}
        bot = await registry.get(user_id, slug)
        if not bot:
            return {"ok": False, "error": f"Unknown bot: {slug}"}
        if bot.running:
            return {"ok": False, "error": f"{slug} already running (PID {bot.pid})"}

        # Only live-mode bots may be launched dry-run. A paper bot would
        # fail the hard mode check inside main_live.py, but bouncing it at
        # the portal is friendlier than letting the subprocess exit 1 and
        # surface as a silent no-op.
        try:
            cfg = load_bot_config(bot.config_file)
        except Exception as e:
            return {"ok": False, "error": f"Could not load config: {e}"}
        if cfg.mode != Mode.LIVE:
            return {
                "ok": False,
                "error": (
                    f"{slug} is mode={cfg.mode.value}; dry-run is only for live-mode bots"
                ),
            }

        if not await registry.begin_start(user_id, slug):
            return {"ok": False, "error": "Bot is already starting"}

        try:
            paths.user_pid_dir(user_id)

            # Same allowlist-only env as start_bot (r1-023). DRY_RUN is
            # set explicitly below because this spawn path deliberately
            # asks main_live.py to skip its input() confirmation.
            env = _bot_subprocess_env(user_id)
            env["PYTHONPATH"] = str(BASE_DIR)
            # main_live.py prompts the operator on non-dry-run launches and
            # also respects DRY_RUN=1 as a bypass — set it explicitly so a
            # non-TTY portal subprocess never hangs on input().
            env["DRY_RUN"] = "1"

            # ``start_new_session=True`` ≡ ``preexec_fn=os.setsid`` — see the
            # commentary on ``start_bot`` for the full rationale. Identical
            # PGID-isolation argument applies to live-mode dry-run subprocs.
            with open(bot.log_file, "a") as log_out:
                proc = subprocess.Popen(
                    [PYTHON_BIN, str(BASE_DIR / "main_live.py"),
                     "--bot", slug, "--user-id", str(user_id), "--dry-run"],
                    cwd=str(BASE_DIR),
                    stdout=log_out,
                    stderr=log_out,
                    env=env,
                    start_new_session=True,  # ≡ preexec_fn=os.setsid
                )
            logger.info(f"Bot {slug} started in DRY-RUN (PID {proc.pid})")

            deadline = time.time() + 3.0
            while time.time() < deadline:
                if bot.pid_file.exists():
                    break
                await asyncio.sleep(0.1)

            return {
                "ok": True,
                "message": f"{slug} started in DRY-RUN (PID {proc.pid})",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            await registry.end_start(user_id, slug)

    async def is_live_config(self, config: BotConfig) -> bool:
        """Determine whether a config will boot under LiveEngine.

        For the in-tree scaffold this is a simple mode check.
        The real plugin may apply additional criteria.
        """
        return config.mode == Mode.LIVE

    # ── Portal data hooks ─────────────────────────────────────────

    async def list_live_slugs(self, user_id: int) -> set[str]:
        """Return slugs of live-mode bots for the user.

        Verbatim snapshot of web/routes/portfolio.py:_live_bot_slugs
        as of Task 2.4 of the plugin-split migration. The real helper
        reads a nested ``bot:`` block (case-insensitive ``mode``),
        which this copy mirrors exactly so a future Task 2.7 rewire
        is behaviour-preserving.
        """
        # ── COPY of web/routes/portfolio.py:_live_bot_slugs logic ──
        # (frozen snapshot; not maintained as web/ changes — deleted
        # in Phase 3.7).
        user_dir = paths.user_bots_dir(user_id)
        if not user_dir.exists():
            return set()
        live: set[str] = set()
        for yaml_path in user_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            block = data.get("bot") if isinstance(data, dict) else None
            if not isinstance(block, dict):
                continue
            mode = block.get("mode")
            if isinstance(mode, str) and mode.lower() == "live":
                live.add(yaml_path.stem)
        return live

    # ── Optional callbacks ────────────────────────────────────────

    def on_breaker_permanent_open(
        self, breaker_name: str, reason: str,
    ) -> None:
        """No-op for BuiltinLiveProvider.

        Task 2.8 will rewire the circuit-breaker callback through
        the LiveProvider Protocol. Until then, the existing
        in-process telegram fan-out in core/circuit_breaker.py
        remains the active path. This method exists to satisfy
        the Protocol; it does not yet receive calls.
        """
        logger.debug(
            "BuiltinLiveProvider.on_breaker_permanent_open called: "
            "breaker=%s reason=%s (no-op until Task 2.8)",
            breaker_name,
            reason,
        )


# Module-level provider singleton — used by core/plugin_loader's
# fallback when the reverto_live package is not installed.
provider = BuiltinLiveProvider()
