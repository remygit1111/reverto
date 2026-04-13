# main_paper.py
# Entry point for a single Reverto paper trading bot instance.
#
# Usage:
#   python3 main_paper.py --config config/bots/btc_paper.yaml

import argparse
import logging
import os
import sys
import atexit
import signal
from logging.handlers import RotatingFileHandler

# ── Arguments ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Reverto paper trading bot")
parser.add_argument(
    "--config",
    default="config/bots/btc_paper.yaml",
    help="Path to bot YAML config file"
)
args = parser.parse_args()

config_path = args.config
bot_slug    = os.path.splitext(os.path.basename(config_path))[0]

os.makedirs("logs", exist_ok=True)
os.makedirs("logs/pids", exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

file_handler = RotatingFileHandler(
    f"logs/{bot_slug}.log",
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

for noisy in ["httpx", "httpcore", "ccxt", "ccxt.base.exchange",
              "urllib3", "urllib3.connectionpool", "asyncio"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── PID file ──────────────────────────────────────────────────────────────────
pid_file   = f"logs/pids/{bot_slug}.pid"
state_file = f"logs/{bot_slug}.state.json"

with open(pid_file, "w") as f:
    f.write(str(os.getpid()))

def cleanup():
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass

atexit.register(cleanup)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

# ── Start engine ──────────────────────────────────────────────────────────────
from config.config_loader import load_bot_config
from exchanges.public_exchange import PublicExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

logger.info(f"Starting Reverto — config: {config_path}")

config   = load_bot_config(config_path)
exchange = PublicExchange(config.exchange.value)
notifier = TelegramNotifier(notify_on=config.telegram.notify_on)

engine = PaperEngine(
    config=config,
    exchange=exchange,
    notifier=notifier,
    initial_balance_btc=0.1,
    poll_interval=10,
    state_file=state_file,
)

engine.start()
