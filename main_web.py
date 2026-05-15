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
import sys
from logging.handlers import RotatingFileHandler

# --version short-circuit must run before any module-level side effects
# below (PID-file write, log dir creation) so it is safe to query even
# while a portal is running.
if "--version" in sys.argv:
    from core._version import __version__

    print(f"Reverto v{__version__}")
    sys.exit(0)

from core.logging_setup import RequestIdFilter

os.makedirs("logs", exist_ok=True)
os.makedirs("logs/pids", exist_ok=True)

# Write portal PID
with open("logs/pids/portal.pid", "w") as f:
    f.write(str(os.getpid()))

atexit.register(lambda: os.path.exists("logs/pids/portal.pid")
                and os.remove("logs/pids/portal.pid"))

# ── Logging — console + logs/portal.log ──────────────────────────────────────
# ``%(request_id)s`` is populated by web.app._RequestIdFilter when
# the filter is installed on this handler at app-import time. Records
# logged outside any request (boot, atexit) fall through the filter's
# default "-" so the column never blows up with KeyError.
_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    _LOG_FORMAT,
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
    _LOG_FORMAT,
    datefmt="%Y-%m-%d %H:%M:%S"
))

# Attach the request-id filter to every handler BEFORE any log line
# fires. Each handler runs its filters before formatting, so this
# guarantees ``record.request_id`` exists for the ``%(request_id)s``
# column. Records logged at boot (before RequestIdMiddleware runs)
# pick up the contextvar's default "-".
console_handler.addFilter(RequestIdFilter())
file_handler.addFilter(RequestIdFilter())

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
    # First-run hint: surface the env vars an operator needs to make
    # the portal production-safe. Each branch logs only when its var
    # is missing so the noise disappears once the operator sets them.
    missing = []
    if not os.environ.get("REVERTO_API_KEY"):
        missing.append("REVERTO_API_KEY")
    if not os.environ.get("REVERTO_SECRET_KEY"):
        missing.append("REVERTO_SECRET_KEY")
    if missing:
        logger.warning(
            "%s not set — ephemeral value(s) will be generated. "
            "For production add to ~/.bashrc:",
            " / ".join(missing),
        )
        if "REVERTO_API_KEY" in missing:
            logger.warning(
                "    export REVERTO_API_KEY=$(python3 -c "
                "'import secrets; print(secrets.token_hex(32))')"
            )
        if "REVERTO_SECRET_KEY" in missing:
            logger.warning(
                "    export REVERTO_SECRET_KEY=$(python3 -c "
                "'import secrets; print(secrets.token_hex(32))')"
            )
    if "REVERTO_INSECURE_COOKIES" not in os.environ:
        # Not a warning — just a one-line hint. Production behind TLS
        # leaves this unset; localhost dev sets it to 1.
        logger.info(
            "REVERTO_INSECURE_COOKIES not set — cookies require HTTPS. "
            "For local http://localhost development add to ~/.bashrc: "
            "export REVERTO_INSECURE_COOKIES=1"
        )
    from web.app import run_portal
    run_portal(host="0.0.0.0", port=8080)
