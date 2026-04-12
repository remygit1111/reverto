# main_paper.py
import logging
import time
from config.config_loader import load_bot_config
from exchanges.public_exchange import PublicExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

# Logging setup — local time
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True
)

# Suppress httpx — hides Telegram token and noise from logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Starting Reverto paper trading...")

    config = load_bot_config("config/bots/btc_paper.yaml")
    exchange = PublicExchange(config.exchange.value)
    notifier = TelegramNotifier()

    # Warm up Telegram connection so first message doesn't skew timestamps
    notifier._warm_up()

    engine = PaperEngine(
        config=config,
        exchange=exchange,
        notifier=notifier,
        initial_balance_btc=0.1,
        poll_interval=10
    )

    engine.start()