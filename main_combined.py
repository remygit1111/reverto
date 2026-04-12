# main_combined.py
# Starts both the Reverto paper engine AND the web portal in one process.
# The portal runs in a background thread; the bot runs in the main thread.
#
# Usage:
#   python3 main_combined.py
#
# Portal: http://localhost:8080

import logging
import os
import threading
from logging.handlers import RotatingFileHandler

os.makedirs("logs", exist_ok=True)

# ── Logging setup ─────────────────────────────────────────────────────────────
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

file_handler = RotatingFileHandler(
    "logs/reverto.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[console_handler, file_handler],
    force=True
)

# Suppress noisy third-party loggers
for noisy in ["httpx", "httpcore", "ccxt", "ccxt.base.exchange",
              "urllib3", "urllib3.connectionpool", "asyncio",
              "uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── Start portal in background thread ────────────────────────────────────────
def start_portal():
    import uvicorn
    from web.app import app
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")

portal_thread = threading.Thread(target=start_portal, name="WebPortal", daemon=True)
portal_thread.start()
logger.info("Web portal started on http://localhost:8080")

# Small delay to let uvicorn bind before the bot starts
import time
time.sleep(1)

# ── Start bot in main thread ──────────────────────────────────────────────────
from config.config_loader import load_bot_config
from exchanges.public_exchange import PublicExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

logger.info("Starting Reverto paper trading...")

config   = load_bot_config("config/bots/btc_paper.yaml")
exchange = PublicExchange(config.exchange.value)
notifier = TelegramNotifier(notify_on=config.telegram.notify_on)

engine = PaperEngine(
    config=config,
    exchange=exchange,
    notifier=notifier,
    initial_balance_btc=0.1,
    poll_interval=10
)

engine.start()
