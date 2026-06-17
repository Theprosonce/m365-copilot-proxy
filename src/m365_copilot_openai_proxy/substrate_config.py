from __future__ import annotations

import json
import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_substrate_config() -> dict[str, Any]:
    """Capture-derived substrate protocol payload (variants, optionsSets, frame, …).

    Defaults ship as package data in `substrate.json`. Point `M365_SUBSTRATE_CONFIG` at a file
    to swap in a fresh capture without editing the installed package.
    """
    override = os.environ.get("M365_SUBSTRATE_CONFIG")
    if override:
        text = Path(override).read_text(encoding="utf-8")
    else:
        text = (
            resources.files(__package__)
            .joinpath("substrate.json")
            .read_text(encoding="utf-8")
        )
    return json.loads(text)
