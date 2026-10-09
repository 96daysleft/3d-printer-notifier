import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from printer_notifier.config import NotifyConfig
from printer_notifier.printers import create_printer_client, parse_printer_config

FIXTURES = Path(__file__).parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


def fixture_json(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def make_client():
    def _make(raw: Dict[str, Any], **notify: Any):
        config = parse_printer_config(raw)
        return create_printer_client(config, NotifyConfig(**notify))
    return _make
