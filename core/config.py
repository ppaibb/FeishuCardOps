import logging
from pathlib import Path
from typing import Any, Dict

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

logger = logging.getLogger("feishu_gitlab_card_http")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("Missing config.yaml.")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
