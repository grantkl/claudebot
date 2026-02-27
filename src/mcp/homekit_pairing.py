"""HomeKit pairing data management."""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PAIRING_FILE = Path.home() / ".homekit_pairings.json"


def get_pairing_file() -> Path:
    return Path(os.environ.get("HOMEKIT_PAIRING_FILE") or str(DEFAULT_PAIRING_FILE))


def load_pairings() -> dict[str, Any]:
    path = get_pairing_file()
    if not path.exists():
        return {}
    with open(path) as f:
        result: dict[str, Any] = json.load(f)
        return result


def save_pairings(data: dict[str, Any]) -> None:
    path = get_pairing_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def list_aliases() -> list[str]:
    return list(load_pairings().keys())
