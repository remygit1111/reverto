#!/usr/bin/env python3
"""CLI entry-point for rotating the Fernet master key.

Usage:
    .venv/bin/python scripts/rotate_credentials.py

Run from the repo root. The script mutates logs/.credentials.key and
logs/credentials.json; stop every running bot before rotating so no
engine reads a half-rotated store. A copy of the old key lands in
logs/.credentials.key.bak — keep it for the 7-day rollback window
before you discard.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.credentials import rotate_fernet_key  # noqa: E402


def main() -> int:
    try:
        result = rotate_fernet_key()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Rotation refused: {e}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
