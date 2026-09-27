from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_desk.data import EvidenceError

FPT_SESSION = {
    "symbol": "FPT",
    "tradingDate": "18/09/2026",
    "close": "65182.47",
    "closeRaw": "71700",
    "open": "67727.95",
    "openRaw": "74500",
    "high": "68000.68",
    "highRaw": "74800",
    "low": "65182.47",
    "lowRaw": "71700",
    "ceilingPrice": "79500",
    "floorPrice": "69100",
    "refPrice": "74300",
    "totalMatchVol": "15500700",
    "totalMatchVal": "1129617650000",
    "avgPrice": "66250.663",
    "totalBuyTrade": "10000",
    "totalSellTrade": "9000",
    "foreignBuyVolTotal": "100000",
    "foreignSellVolTotal": "200000",
    "foreignCurrentRoom": "50000000",
    "netBuySellVol": "-100000",
}


def test_mid_is_the_raw_traded_price_never_the_adjusted_one():
    from crypto_desk.vn_data import session_mid

    assert session_mid(FPT_SESSION) == Decimal("71700")


def test_the_adjusted_close_would_sit_below_the_floor_and_must_never_be_compared_to_it():
    """Bất biến 1: FPT close 65182.47 < floor 69100. Trộn hai loại là kết luận điều bất khả."""
    from crypto_desk.vn_data import session_mid

    mid = session_mid(FPT_SESSION)
    floor = Decimal(FPT_SESSION["floorPrice"])
    ceiling = Decimal(FPT_SESSION["ceilingPrice"])

    assert floor <= mid <= ceiling
    assert Decimal(FPT_SESSION["close"]) < floor


def test_cross_source_check_passes_when_both_ssi_services_agree():
    from crypto_desk.vn_data import assert_sources_agree

    assert_sources_agree(Decimal("71700"), Decimal("71700"))


def test_cross_source_check_catches_comparing_against_the_adjusted_price():
    """Nhầm close (65182.47) thay cho closeRaw (71700) lệch ~9%, guard phải kêu."""
    from crypto_desk.vn_data import assert_sources_agree

    with pytest.raises(EvidenceError, match="lệch"):
        assert_sources_agree(Decimal("71700"), Decimal("65182.47"))


def test_cross_source_check_tolerates_half_a_percent():
    from crypto_desk.vn_data import assert_sources_agree

    assert_sources_agree(Decimal("1000"), Decimal("1004"))
    with pytest.raises(EvidenceError):
        assert_sources_agree(Decimal("1000"), Decimal("1006"))


def test_friday_session_is_fresh_when_the_cutoff_falls_on_saturday():
    """Bất biến 3: quy tắc theo đồng hồ sẽ báo stale mọi cuối tuần."""
    from datetime import datetime, timezone

    from crypto_desk.vn_data import assert_latest_session_closed

    assert_latest_session_closed(
        latest_session_date="18/09/2026",          # thứ Sáu
        cutoff=datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc),
    )


def test_a_session_more_recent_than_the_cutoff_is_rejected():
    from datetime import datetime, timezone

    from crypto_desk.vn_data import assert_latest_session_closed

    with pytest.raises(EvidenceError):
        assert_latest_session_closed(
            latest_session_date="21/09/2026",
            cutoff=datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc),
        )


def test_vn_evidence_builder_builds_snapshot_with_four_items():
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from crypto_desk.data import Fetched
    from crypto_desk.vn_data import VNEvidenceBuilder

    now = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    mock_client = MagicMock()
    mock_client.now.return_value = now
    mock_client.stock_info.return_value = Fetched(
        "ssi", "stock_info", now, now, [FPT_SESSION]
    )
    mock_client.charts_history.return_value = Fetched(
        "ssi", "charts", now, now, {"c": ["65182.47", "66000.0"]}
    )
    mock_client.board_snapshot.return_value = Fetched(
        "ssi", "board", now, now, [{"stockSymbol": "FPT", "matchedPrice": "71700"}]
    )
    mock_client.company_profile.return_value = Fetched(
        "ssi", "profile", now, now, {"industryName": "Công nghệ Thông tin"}
    )
    mock_client.news.return_value = Fetched(
        "rss", "news", now, now, [{"title": "Tin mới", "url": "https://example.com", "published_at": now.isoformat()}]
    )

    builder = VNEvidenceBuilder(mock_client)
    snapshot = builder.build("FPT", now)

    assert snapshot.symbol == "FPT"
    assert snapshot.mid == Decimal("71700")
    assert snapshot.industry == "Công nghệ Thông tin"
    assert {item.kind for item in snapshot.items} == {"spot", "news", "flow", "reference"}
    assert len(snapshot.evidence_ids) == 4


def test_vn_evidence_builder_reflection_closes_counts_sessions():
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from crypto_desk.data import Fetched
    from crypto_desk.vn_data import VNEvidenceBuilder

    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    mock_client = MagicMock()
    # 20 timestamps and closes
    t_list = [int((start + timezone.utc.utcoffset(None) or start).timestamp()) + i * 86400 for i in range(20)]
    c_list = [100 + i for i in range(20)]
    mock_client.charts_history.return_value = Fetched(
        "ssi", "charts", start, start, {"t": t_list, "c": c_list}
    )

    builder = VNEvidenceBuilder(mock_client)
    closes = builder.reflection_closes("FPT", start, periods=20)
    assert len(closes) == 20
    assert closes[0] == Decimal("100")

    # When fewer than 20 sessions exist
    mock_client.charts_history.return_value = Fetched(
        "ssi", "charts", start, start, {"t": t_list[:15], "c": c_list[:15]}
    )
    with pytest.raises(EvidenceError, match="Cần 20 phiên"):
        builder.reflection_closes("FPT", start, periods=20)





def _session(date: str, close: str, close_raw: str, high: str, low: str) -> dict:
    return {"tradingDate": date, "close": close, "closeRaw": close_raw, "high": high, "low": low}


def _window_client(rows: list[dict]):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from crypto_desk.data import Fetched

    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    client = MagicMock()
    client.stock_info.return_value = Fetched("ssi", "stock-info", at, at, rows)
    return client


def test_vn_reflection_window_returns_the_decision_session_and_adjusted_bars_after_it():
    from crypto_desk.vn_data import VNEvidenceBuilder

    decision = _session("01/09/2026", "90", "100", "91", "89")
    after = [
        _session(f"{day:02d}/09/2026", str(100 + day), str(110 + day), str(101 + day), str(99 + day))
        for day in range(2, 22)
    ]
    # SSI trả phiên mới nhất trước.
    client = _window_client(list(reversed(after)) + [decision])

    base, bars = VNEvidenceBuilder(client).reflection_window("FPT", "01/09/2026", periods=20)

    assert base == (Decimal("90"), Decimal("100"))
    assert len(bars) == 20
    # high/low/close điều chỉnh, không phải closeRaw.
    assert bars[0] == (Decimal("103"), Decimal("101"), Decimal("102"))
    # 50 ngày lịch đủ 20 phiên kể cả khi có Tết, và không vượt 40 dòng SSI cho phép.
    client.stock_info.assert_called_once_with("FPT", "01/09/2026", "21/10/2026", page_size=40)


def test_vn_reflection_window_waits_until_enough_sessions_have_closed():
    from crypto_desk.vn_data import VNEvidenceBuilder

    rows = [_session(f"{day:02d}/09/2026", "100", "100", "101", "99") for day in range(15, 0, -1)]

    with pytest.raises(EvidenceError, match="Cần 20 phiên"):
        VNEvidenceBuilder(_window_client(rows)).reflection_window("FPT", "01/09/2026")


def test_vn_reflection_window_requires_the_decision_session_itself():
    from crypto_desk.vn_data import VNEvidenceBuilder

    rows = [_session(f"{day:02d}/09/2026", "100", "100", "101", "99") for day in range(25, 1, -1)]

    with pytest.raises(EvidenceError, match="phiên quyết định"):
        VNEvidenceBuilder(_window_client(rows)).reflection_window("FPT", "01/09/2026")
