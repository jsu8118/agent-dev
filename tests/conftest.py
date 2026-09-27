"""Test configuration: every test runs against the offline mock API, quietly."""

import os

os.environ["LABKIT_MODE"] = "mock"
os.environ["LABKIT_QUIET"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

import pytest  # noqa: E402

from labkit import LEDGER, mock_api  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_mock():
    mock_api().reset()
    LEDGER.reset()
    yield
    mock_api().reset()
