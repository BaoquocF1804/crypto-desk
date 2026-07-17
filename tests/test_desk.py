from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from investment_desk.core import (
    RiskSettings,
    Store,
    TradeTicket,
    calculate_buy_quantity,
    validate_approval,
    validate_paper_account,
    validate_ticket_price,
)
from investment_desk.integrations import _as_utc


def ticket(**overrides):
    values = dict(
        ticker="NVDA",
        con_id=1,
        currency="USD",
        intent="OPEN",
        side="BUY",
        quantity=10,
        limit_price=100,
        stop_price=90,
        target_price=120,
        risk_snapshot={},
        ttl_minutes=30,
    )
    values.update(overrides)
    return TradeTicket.create(**values)


def test_risk_quantity_uses_tightest_limit():
    quantity, limits = calculate_buy_quantity(
        nav=100_000,
        buying_power=50_000,
        entry=100,
        stop=90,
        symbol_exposure=5_000,
        sector_exposure=20_000,
        gross_exposure=70_000,
        risk=RiskSettings(),
    )
    assert quantity == 100
    assert limits["risk"] == 100


@pytest.mark.parametrize("entry,stop", [(0, 90), (100, 100), (100, 110)])
def test_risk_rejects_invalid_stop(entry, stop):
    with pytest.raises(ValueError):
        calculate_buy_quantity(
            nav=100_000,
            buying_power=10_000,
            entry=entry,
            stop=stop,
            symbol_exposure=0,
            sector_exposure=0,
            gross_exposure=0,
            risk=RiskSettings(),
        )


def test_quote_deviation_boundary():
    validate_ticket_price(ticket(), 100.5, 0.005)
    with pytest.raises(ValueError):
        validate_ticket_price(ticket(), 100.51, 0.005)


def test_store_enforces_one_order_per_ticket(tmp_path: Path):
    store = Store(tmp_path / "desk.db")
    item = ticket()
    store.save_ticket(item)
    store.save_order(item.id, "SUBMITTED", {"order_ids": [1, 2, 3]})
    with pytest.raises(ValueError, match="already has an order"):
        store.save_order(item.id, "SUBMITTED", {"order_ids": [4, 5, 6]})


def test_ticket_expiry_is_utc():
    item = ticket()
    expiry = datetime.fromisoformat(item.expires_at)
    assert expiry.tzinfo == UTC
    assert expiry > datetime.now(UTC) + timedelta(minutes=29)


def test_approval_rejects_unknown_user_and_expired_ticket():
    item = ticket()
    with pytest.raises(ValueError, match="allowlisted"):
        validate_approval(item, "intruder", ["owner"])
    with pytest.raises(ValueError, match="expired"):
        validate_approval(item, "owner", ["owner"], datetime.now(UTC) + timedelta(hours=1))


def test_live_account_is_blocked_in_code():
    validate_paper_account("DU123456")
    with pytest.raises(ValueError, match="non-paper"):
        validate_paper_account("U123456")


def test_market_date_is_normalized_to_utc():
    assert _as_utc(date(2026, 7, 17)) == datetime(2026, 7, 17, tzinfo=UTC)


def test_reflection_and_order_updates_are_idempotent(tmp_path: Path):
    store = Store(tmp_path / "desk.db")
    item = ticket()
    store.save_ticket(item)
    store.save_order(item.id, "SUBMITTED", {"order_ids": [1]})
    store.update_order(item.id, "FILLED", {"filled": 10})
    assert store.orders()[0]["status"] == "FILLED"
    assert store.save_reflection("run-1", "NVDA", {"alpha": 0.1})
    assert not store.save_reflection("run-1", "NVDA", {"alpha": 0.2})
