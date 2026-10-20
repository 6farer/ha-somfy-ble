"""Pytest configuration for the Somfy Bluetooth integration tests."""

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def somfy_pin() -> str:
    """Return a non-secret protocol-shaped test PIN without embedding a credential."""
    return "0" + "12345"
