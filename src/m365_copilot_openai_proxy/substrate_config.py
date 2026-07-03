from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from .config import Settings


@lru_cache(maxsize=1)
def load_substrate_config() -> dict[str, Any]:
    """Capture-derived substrate protocol payload (variants, optionsSets, frame, …).

    Defaults ship as package data in `substrate.json`. Set `substrate_config_path` in config.ini
    to swap in a fresh capture without editing the installed package.
    """
    configured = Settings().substrate_config_path
    if configured:
        text = Path(configured).read_text(encoding="utf-8")
    else:
        packaged = resources.files(__package__).joinpath("substrate.json")
        dev_config = Path(__file__).parents[2] / "config" / "substrate.json"
        source = dev_config if dev_config.exists() else packaged
        text = source.read_text(encoding="utf-8")
    return json.loads(text)
