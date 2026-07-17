from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import UTC, date, datetime, time
from typing import Any

from .core import EvidenceItem, ResearchDecision, Settings, iso, utcnow, validate_paper_account


def _as_utc(value: Any) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if not isinstance(value, datetime):
        raise ValueError(f"Unsupported market timestamp: {value!r}")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MarketData:
    def yfinance(self, ticker: str) -> EvidenceItem:
        import yfinance as yf

        instrument = yf.Ticker(ticker)
        history = instrument.history(period="3mo", auto_adjust=False)
        if history.empty:
            raise ValueError(f"No yfinance data for {ticker}")
        row = history.iloc[-1]
        closes = [float(v) for v in history["Close"].dropna().tolist()]
        try:
            metadata = instrument.fast_info
            currency = metadata.get("currency") or ""
        except Exception:
            currency = ""
        timestamp = _as_utc(history.index[-1])
        return EvidenceItem(
            provider="yfinance",
            source=f"https://finance.yahoo.com/quote/{ticker}",
            fetched_at=iso(),
            as_of=iso(timestamp),
            delayed=True,
            stale=(utcnow() - timestamp).days > 4,
            payload={
                "ticker": ticker,
                "close": float(row["Close"]),
                "currency": currency,
                "average_volume": float(history["Volume"].tail(20).mean()),
                "closes": closes,
            },
        )

    def openbb(self, ticker: str) -> EvidenceItem:
        from openbb import obb

        frame = obb.equity.price.historical(symbol=ticker, provider="yfinance").to_df()
        if frame.empty:
            raise ValueError(f"No OpenBB data for {ticker}")
        row = frame.iloc[-1]
        timestamp = _as_utc(frame.index[-1])
        return EvidenceItem(
            provider="openbb:yfinance",
            source="openbb://equity/price/historical",
            fetched_at=iso(),
            as_of=iso(timestamp),
            delayed=True,
            stale=(utcnow() - timestamp).days > 4,
            payload={
                "ticker": ticker,
                "close": float(row["close"]),
                "currency": "",
                "closes": [float(v) for v in frame["close"].dropna().tail(65).tolist()],
            },
        )

    def evidence(self, ticker: str) -> tuple[list[EvidenceItem], str | None]:
        items: list[EvidenceItem] = []
        errors: list[str] = []
        for name in ("openbb", "yfinance"):
            try:
                items.append(getattr(self, name)(ticker))
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if len(items) != 2:
            return items, "; ".join(errors) or "Two independent evidence sources are required"
        prices = [float(item.payload["close"]) for item in items]
        deviation = abs(prices[0] - prices[1]) / prices[1]
        if deviation > 0.005:
            return items, f"Data sources differ by {deviation:.2%}"
        if any(item.stale for item in items):
            return items, "Core market data is stale"
        return items, None

    def sector(self, ticker: str) -> str:
        try:
            import yfinance as yf

            return str(yf.Ticker(ticker).info.get("sector") or "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    def fx_rate(self, currency: str, base: str) -> float:
        if currency == base:
            return 1.0
        import yfinance as yf

        direct = yf.Ticker(f"{currency}{base}=X").history(period="5d")
        if not direct.empty and float(direct["Close"].iloc[-1]) > 0:
            return float(direct["Close"].iloc[-1])
        inverse = yf.Ticker(f"{base}{currency}=X").history(period="5d")
        if not inverse.empty and float(inverse["Close"].iloc[-1]) > 0:
            return 1 / float(inverse["Close"].iloc[-1])
        raise ValueError(f"Missing FX rate {currency}/{base}")

    def performance(self, ticker: str, start: date) -> float:
        import yfinance as yf

        history = yf.Ticker(ticker).history(start=start.isoformat(), auto_adjust=True)
        closes = history["Close"].dropna()
        if len(closes) < 2 or float(closes.iloc[0]) <= 0:
            raise ValueError(f"Insufficient performance data for {ticker}")
        return float(closes.iloc[-1] / closes.iloc[0] - 1)


def screen_item(evidence: EvidenceItem) -> dict[str, Any]:
    closes = evidence.payload.get("closes", [])
    if len(closes) < 21:
        raise ValueError("At least 21 closes are required")
    price = closes[-1]
    mom20 = price / closes[-21] - 1
    mom60 = price / closes[-61] - 1 if len(closes) >= 61 else 0.0
    volume = float(evidence.payload.get("average_volume", 0))
    return {
        "ticker": evidence.payload["ticker"],
        "price": price,
        "momentum_20d": mom20,
        "momentum_60d": mom60,
        "average_volume": volume,
        "passes": volume >= 100_000 and mom20 > 0,
        "score": round(mom20 * 0.7 + mom60 * 0.3, 6),
    }


def _number(text: str, label: str) -> float | None:
    match = re.search(rf"\*\*{re.escape(label)}\*\*:\s*\$?([0-9][0-9,]*(?:\.\d+)?)", text, re.I)
    return float(match.group(1).replace(",", "")) if match else None


def _conviction(text: str) -> float:
    match = re.search(
        r"(?:conviction|confidence)\D{0,15}(10|[0-9](?:\.\d+)?)\s*(?:/\s*10)?", text, re.I
    )
    return min(10.0, float(match.group(1))) if match else 5.0


def run_research(
    ticker: str, settings: Settings, evidence: list[EvidenceItem], data_error: str | None
) -> tuple[ResearchDecision, dict[str, Any]]:
    if data_error:
        return ResearchDecision(
            ticker=ticker,
            action="NO_TRADE",
            conviction=0,
            bull_case="",
            bear_case="",
            catalysts=[],
            invalidation=data_error,
            entry=None,
            stop=None,
            target=None,
            evidence_ids=[e.id for e in evidence],
            reason=data_error,
        ), {}
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required for analysis")

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    config.update(
        llm_provider="openai",
        quick_think_llm=settings.models.quick,
        deep_think_llm=settings.models.deep,
        max_debate_rounds=settings.models.debate_rounds,
        max_risk_discuss_rounds=1,
        checkpoint_enabled=True,
        output_language="Vietnamese",
    )
    graph = TradingAgentsGraph(debug=False, config=config)
    state, signal = graph.propagate(ticker, datetime.now().date().isoformat())
    mapping = {
        "buy": "ACCUMULATE",
        "overweight": "ACCUMULATE",
        "hold": "HOLD",
        "underweight": "REDUCE",
        "sell": "EXIT",
    }
    action = mapping.get(str(signal).strip().lower(), "NO_TRADE")
    final = str(state.get("final_trade_decision", ""))
    trader = str(state.get("trader_investment_plan", ""))
    entry = _number(trader, "Entry Price")
    stop = _number(trader, "Stop Loss")
    target = entry + 2 * (entry - stop) if entry and stop and stop < entry else None
    debate = state.get("investment_debate_state", {})
    decision = ResearchDecision(
        ticker=ticker,
        action=action,
        conviction=_conviction(final),
        bull_case=str(debate.get("bull_history", "")),
        bear_case=str(debate.get("bear_history", "")),
        catalysts=[],
        invalidation=f"Stop below {stop}" if stop else "No validated stop",
        entry=entry,
        stop=stop,
        target=target,
        evidence_ids=[e.id for e in evidence],
        raw_signal=str(signal),
        reason=final,
    )
    if action == "ACCUMULATE" and not (entry and stop and target and stop < entry):
        decision.action = "NO_TRADE"
        decision.reason = (
            "TradingAgents did not return a valid entry/stop; order creation is blocked"
        )
    return decision, state


class IBKRBroker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ib = None

    def connect(self):
        from ib_async import IB

        self.ib = IB()
        self.ib.connect(
            self.settings.broker.host,
            self.settings.broker.port,
            clientId=self.settings.broker.client_id,
            readonly=False,
            timeout=5,
        )
        accounts = self.ib.managedAccounts()
        account = self.settings.broker.account or (accounts[0] if accounts else "")
        try:
            validate_paper_account(account)
        except ValueError:
            self.ib.disconnect()
            raise
        self.settings.broker.account = account
        return self

    def close(self) -> None:
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()

    def snapshot(self) -> dict[str, Any]:
        if not self.ib:
            raise RuntimeError("Broker is not connected")
        account = self.settings.broker.account
        values = {v.tag: v.value for v in self.ib.accountSummary(account)}
        positions = []
        for pos in self.ib.positions(account):
            if pos.position <= 0 or pos.contract.secType not in {"STK", "ETF"}:
                continue
            ticker = pos.contract.localSymbol or pos.contract.symbol
            positions.append(
                {
                    "ticker": ticker,
                    "quantity": float(pos.position),
                    "avg_cost": float(pos.avgCost),
                    "con_id": int(pos.contract.conId),
                    "currency": pos.contract.currency,
                    "exchange": pos.contract.primaryExchange or pos.contract.exchange,
                    "market_value": float(pos.position * pos.avgCost),
                }
            )
        nav = float(values.get("NetLiquidation", 0))
        cash = float(values.get("AvailableFunds", 0))
        return {
            "account": account,
            "nav": nav,
            "cash": cash,
            "buying_power": min(cash, float(values.get("BuyingPower", cash))),
            "positions": positions,
            "open_orders": [str(order) for order in self.ib.openOrders()],
            "created_at": iso(),
        }

    def resolve(
        self, ticker: str, exchange: str | None = None, currency: str = "USD"
    ) -> dict[str, Any]:
        from ib_async import Stock

        contract = Stock(ticker, exchange or "SMART", currency)
        qualified = self.ib.qualifyContracts(contract) if self.ib else []
        if len(qualified) != 1:
            raise ValueError(f"IBKR contract is ambiguous or missing for {ticker}")
        item = qualified[0]
        return {"con_id": int(item.conId), "currency": item.currency, "exchange": item.exchange}

    def place_ticket(self, ticket) -> dict[str, Any]:
        from ib_async import Contract, LimitOrder

        if not self.ib:
            raise RuntimeError("Broker is not connected")
        validate_paper_account(self.settings.broker.account)
        contract = Contract(
            conId=ticket.con_id, exchange="SMART", currency=ticket.currency, secType="STK"
        )
        orders = (
            self.ib.bracketOrder(
                ticket.side,
                ticket.quantity,
                ticket.limit_price,
                ticket.target_price,
                ticket.stop_price,
            )
            if ticket.side == "BUY"
            else [LimitOrder("SELL", ticket.quantity, ticket.limit_price)]
        )
        trades = []
        for order in orders:
            order.account = self.settings.broker.account
            order.orderRef = ticket.id
            order.tif = "DAY"
            order.outsideRth = False
            trades.append(self.ib.placeOrder(contract, order))
        self.ib.sleep(1)
        return {
            "ticket_id": ticket.id,
            "order_ids": [trade.order.orderId for trade in trades],
            "statuses": [trade.orderStatus.status for trade in trades],
        }

    def order_statuses(self) -> dict[str, dict[str, Any]]:
        if not self.ib:
            raise RuntimeError("Broker is not connected")
        result: dict[str, dict[str, Any]] = {}
        for trade in self.ib.trades():
            ref = str(trade.order.orderRef or "")
            if not ref:
                continue
            result[ref] = {
                "status": trade.orderStatus.status,
                "filled": float(trade.orderStatus.filled),
                "remaining": float(trade.orderStatus.remaining),
                "average_fill_price": float(trade.orderStatus.avgFillPrice),
                "order_id": int(trade.order.orderId),
            }
        return result


def serialize_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]
