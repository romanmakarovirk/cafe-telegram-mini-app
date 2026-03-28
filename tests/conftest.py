"""Shared test configuration: env vars and sys.path setup."""
import os
import sys
from pathlib import Path

# Add project root to sys.path (parent of tests/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set minimal env vars before importing app modules (no real API keys needed)
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("DATABASE_URL", "sqlite:///test_system.db")
os.environ.setdefault("FRESH_ENABLED", "false")
os.environ.setdefault("SBP_TEST_MODE", "true")
os.environ.setdefault("ATOL_TEST_MODE", "true")
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("KITCHEN_API_KEY", "test-kitchen-key-12345")
