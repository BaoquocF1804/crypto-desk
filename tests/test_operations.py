from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script_name", "command"),
    [
        ("health.sh", "health"),
        ("daily.sh", "daily"),
    ],
)
def test_due_wrapper_is_quiet_when_bucket_is_complete(
    tmp_path: Path,
    script_name: str,
    command: str,
) -> None:
    script = tmp_path / script_name
    shutil.copy(ROOT / "scripts" / script_name, script)
    script.chmod(0o755)
    desk = tmp_path / "desk"
    desk.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' '{\"status\": \"ALREADY_DONE\"}'\n",
        encoding="utf-8",
    )
    desk.chmod(0o755)

    result = subprocess.run([script], check=True, capture_output=True, text=True)

    assert result.stdout == ""
    assert result.stderr == ""


def test_hermes_skill_maps_telegram_identity_and_confirmation_code_separately() -> None:
    skill = (ROOT / "hermes" / "crypto-desk" / "SKILL.md").read_text(encoding="utf-8")

    assert "/crypto-desk approve TICKET_ID [CODE]" in skill
    assert "--actor TELEGRAM_USER_ID --channel telegram" in skill
    assert "--code CODE" in skill
    assert "--user" not in skill
    assert "Never run `desk live-code`" in skill
    assert "Never change `BINANCE_ENV`" in skill


def test_hermes_skill_limits_symbols_and_public_commands() -> None:
    skill = (ROOT / "hermes" / "crypto-desk" / "SKILL.md").read_text(encoding="utf-8")

    assert "BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT" in skill
    for command in (
        "doctor",
        "sync",
        "screen",
        "analyze SYMBOL",
        "tickets",
        "approve TICKET_ID [CODE]",
        "reject TICKET_ID",
        "orders",
    ):
        assert f"/crypto-desk {command}" in skill
    assert "/crypto-desk live-code" not in skill
    assert "/crypto-desk daily" not in skill
    assert "/crypto-desk health" not in skill


def test_openbb_service_uses_crypto_categories() -> None:
    service = (ROOT / "services" / "openbb-mcp.service").read_text(encoding="utf-8")

    assert "--allowed-categories crypto,news,economy" in service


def test_distribution_and_docs_no_longer_reference_equity_stack() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert 'packages = ["src/crypto_desk"]' in pyproject
    legacy_terms = (
        "IB" + "KR",
        "Trading" + "Agents",
        "y" + "finance",
        "exchange_" + "calendars",
        "PAPER_" + "EXECUTION",
    )
    for legacy in legacy_terms:
        assert legacy not in readme
        assert legacy not in pyproject


def test_skill_ui_metadata_is_valid() -> None:
    metadata_path = ROOT / "hermes" / "crypto-desk" / "agents" / "openai.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))

    interface = metadata["interface"]
    assert interface["display_name"] == "Crypto Desk"
    assert 25 <= len(interface["short_description"]) <= 64
    assert "$crypto-desk" in interface["default_prompt"]
