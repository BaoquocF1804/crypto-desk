from crypto_desk.notifications import analysis_summary, daily_summary, health_summary


def test_analysis_summary_contains_price_reasoning_and_risk_plan():
    text = analysis_summary(
        {
            "run_id": "run-1",
            "current_price": "63930.99500000",
            "ticket_id": None,
            "decision": {
                "symbol": "BTCUSDT",
                "action": "HOLD",
                "conviction": "7",
                "bull_case": "ETF tiếp tục mua ròng.",
                "bear_case": "Cấu trúc Daily còn giảm.",
                "catalysts": ["Đóng nến ngày trên kháng cự."],
                "entry": None,
                "stop": "62290",
                "target": "70000",
                "invalidation": "Đóng nến ngày dưới 62,290 USDT.",
            },
        }
    )

    assert "Giá hiện tại: 63,930.995 USDT" in text
    assert "LUẬN ĐIỂM TĂNG" in text
    assert "Cấu trúc Daily còn giảm." in text
    assert "Stop: 62,290" in text
    assert "Ticket: Không tạo" in text
    assert "run-1" in text


def test_health_summary_omits_raw_positions():
    text = health_summary(
        {
            "bucket": "2026-07-17T19:00Z",
            "alerts": [],
            "reconciled": [],
            "snapshot": {
                "nav_usdt": "123.45",
                "free_usdt": "100",
                "positions": [{"asset": "MEGA", "value_usdt": "382.46"}],
                "open_orders": [],
            },
        }
    )

    assert "NAV: 123.45 USDT" in text
    assert "Vị thế: 1" in text
    assert "MEGA" not in text


def test_daily_summary_lists_screen_results_without_raw_json():
    text = daily_summary(
        {
            "bucket": "2026-07-17",
            "screen": [{"symbol": "ETHUSDT", "score": "0.25", "passes": True, "reasons": []}],
            "run_ids": ["run-1"],
        }
    )

    assert "✅ ETHUSDT: 0.25" in text
    assert "Analysis runs: 1" in text
    assert "run-1" not in text
