# main_web.py
# Starts the Reverto web portal on port 8080.
# Writes its own log to logs/portal.log so it appears in the dashboard.
#
# Usage:
#   python3 main_web.py
#   ./start.sh   (runs in background)
#
# Open: http://localhost:8080

import logging
import os
import atexit
from logging.handlers import RotatingFileHandler

os.makedirs("logs", exist_ok=True)
os.makedirs("logs/pids", exist_ok=True)

# Write portal PID
with open("logs/pids/portal.pid", "w") as f:
    f.write(str(os.getpid()))

atexit.register(lambda: os.path.exists("logs/pids/portal.pid")
                and os.remove("logs/pids/portal.pid"))

# ── Logging — console + logs/portal.log ──────────────────────────────────────
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

file_handler = RotatingFileHandler(
    "logs/portal.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
file_handler.setLevel(logging.INFO)  # INFO+ in portal log
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

logging.basicConfig(
    level=logging.INFO,
    handlers=[console_handler, file_handler],
    force=True
)

for noisy in ["uvicorn", "uvicorn.access", "uvicorn.error",
              "fastapi", "httpx", "httpcore"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Reverto dashboard starting on http://localhost:8080")
    if not os.environ.get("REVERTO_API_KEY"):
        logger.warning(
            "REVERTO_API_KEY is not set — an ephemeral key will be generated "
            "and printed below. For persistent auth, set it before starting:"
        )
        logger.warning(
            "    export REVERTO_API_KEY=$(python3 -c "
            "'import secrets; print(secrets.token_hex(32))')"
        )
    from web.app import run_portal
    run_portal(host="0.0.0.0", port=8080)
