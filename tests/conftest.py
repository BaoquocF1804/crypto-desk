from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_live_dashboard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure test suite runs never leak temporary test snapshots into a live local dev server.

    Must use setenv with empty string (not delenv) because cli._load() calls
    load_dotenv(override=False), which would otherwise re-populate from .env.
    """
    monkeypatch.setenv("CRYPTO_DESK_DASHBOARD_INGEST_URL", "")
    monkeypatch.setenv("CRYPTO_DESK_COMMAND_API_URL", "")
