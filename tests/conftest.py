from __future__ import annotations

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
        help="ignored; opencode tests are disabled",
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if Path(collection_path).name == "test_opencode.py":
        return True
    return None
