# main_paper.py
import logging
import os
from logging.handlers import RotatingFileHandler
from config.config_loader import load_bot_config
from exchanges.public_exchange import PublicExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

# Console handler — INFO only, clean overview
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

# File handler — DEBUG, full detail, auto-rotating
# Max 5MB per file, keeps last 3 files → max 15MB total
file_handler = RotatingFileHandler(
    "logs/reverto.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB
    backupCount=3,
    encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

# Root logger op DEBUG zodat file handler alles ontvangt
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[console_handler, file_handler],
    force=True
)

# Suppress noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("ccxt").setLevel(logging.WARNING)
logging.getLogger("ccxt.base.exchange").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Starting Reverto paper trading...")

    config = load_bot_config("config/bots/btc_paper.yaml")
    exchange = PublicExchange(config.exchange.value)

    notifier = TelegramNotifier(
        notify_on=config.telegram.notify_on
    )

    engine = PaperEngine(
        config=config,
        exchange=exchange,
        notifier=notifier,
        initial_balance_btc=0.1,
        poll_interval=10
    )

    engine.start()