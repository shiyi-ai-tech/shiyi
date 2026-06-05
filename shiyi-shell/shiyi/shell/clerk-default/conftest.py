"""clerk-default test fixtures"""
import sys
import pytest
from pathlib import Path

# Ensure clerk-default is importable
sys.path.insert(0, str(Path(__file__).parent))

from worker import ClerkWorker, ClerkConfig


@pytest.fixture
def clerk_config():
    """Provide a ClerkConfig for testing."""
    return ClerkConfig(
        clerk_id="clerk-default",
        clerk_dir=str(Path(__file__).parent),
    )


@pytest.fixture
def clerk(clerk_config):
    """Provide a ClerkWorker instance for testing."""
    return ClerkWorker(clerk_config)
