from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-opencode",
        action="store_true",
        default=False,
        help="run opencode tests",
    )


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _run_opencode_enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--run-opencode")) or _truthy(
        os.getenv("M365_RUN_OPENCODE_TESTS")
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if Path(collection_path).name == "test_opencode.py" and not _run_opencode_enabled(
        config
    ):
        return True
    return None
