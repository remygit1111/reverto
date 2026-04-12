# config/config_loader.py
# Loads and validates a YAML bot configuration file

import yaml
from pathlib import Path
from config.models import BotConfig


def load_bot_config(path: str) -> BotConfig:
    """
    Loads a YAML configuration file and returns a validated BotConfig object.
    Raises a clear error if the YAML file is invalid or missing required fields.
    """
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    try:
        config = BotConfig(**raw.get("bot", {}))
        print(f"✅ Config loaded: {config.name} ({config.mode} on {config.exchange})")
        return config
    except Exception as e:
        raise ValueError(f"❌ Invalid bot configuration in {path}:\n{e}")