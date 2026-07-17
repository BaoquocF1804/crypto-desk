from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import exchange_calendars as xcals
import typer
from dotenv import load_dotenv

from .core import (
    Store,
    TradeTicket,
    calculate_buy_quantity,
    json_dumps,
    load_settings,
    validate_approval,
    validate_paper_account,
    validate_ticket_price,
)
from .integrations import IBKRBroker, MarketData, run_research, screen_item, serialize_evidence

app = typer.Typer(no_args_is_help=True, help="AI Investment Desk (IBKR Paper only)")


class Context:
    def __init__(self, config: Path, json_output: bool):
        self.config = config
        load_dotenv(config.parent / ".env", override=False)
        self.json_output = json_output
        self.market = MarketData()
        self._settings = None
        self._store = None

    @property
    def settings(self):
        if self._settings is None:
            if not self.config.is_file():
                raise typer.BadParameter(f"Configuration file does not exist: {self.config}")
            self._settings = load_settings(self.config)
        return self._settings

    @property
    def store(self):
        if self._store is None:
            self._store = Store(self.settings.database)
        return self._store


def emit(ctx: Context, value: Any) -> None:
    if ctx.json_output:
        typer.echo(json_dumps(value))
        return
    if isinstance(value, str):
        typer.echo(value)
    else:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.callback()
def main(
    ctx: typer.Context,
    config: Path = typer.Option(Path("config.yaml"), readable=True),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    ctx.obj = Context(config, json_output)


@app.command()
def doctor(ctx: typer.Context) -> None:
    c: Context = ctx.obj
    checks: dict[str, Any] = {
        "python": {"ok": sys.version_info >= (3, 12), "value": sys.version.split()[0]},
        "openai_key": {
            "ok": bool(os.getenv("OPENAI_API_KEY")),
            "value": "configured" if os.getenv("OPENAI_API_KEY") else "missing",
        },
        "paper_execution": {"ok": True, "value": os.getenv("PAPER_EXECUTION_ENABLED", "0") == "1"},
    }
    packages = {
        "openbb": "openbb",
        "openbb_mcp_server": "openbb-mcp-server",
        "tradingagents": "tradingagents",
        "ib_async": "ib-async",
    }
    for module, package in packages.items():
        try:
            checks[module] = {"ok": True, "value": version(package)}
        except PackageNotFoundError as exc:
            checks[module] = {"ok": False, "value": str(exc)}
    hermes = shutil.which("hermes")
    if hermes:
        try:
            output = subprocess.run(
                [hermes, "--version"], capture_output=True, text=True, timeout=5, check=False
            )
            version_text = (output.stdout or output.stderr).strip()
            checks["hermes"] = {"ok": "0.18.2" in version_text, "value": version_text}
        except Exception as exc:
            checks["hermes"] = {"ok": False, "value": str(exc)}
    else:
        checks["hermes"] = {"ok": False, "value": "not installed"}
    checks["telegram"] = {
        "ok": bool(os.getenv("TELEGRAM_BOT_TOKEN") and c.settings.telegram_allowlist),
        "value": "configured" if os.getenv("TELEGRAM_BOT_TOKEN") else "missing token",
    }
    try:
        with socket.create_connection(("127.0.0.1", 8001), timeout=1):
            checks["openbb_mcp"] = {"ok": True, "value": "127.0.0.1:8001"}
    except OSError as exc:
        checks["openbb_mcp"] = {"ok": False, "value": str(exc)}
    try:
        broker = IBKRBroker(c.settings).connect()
        checks["ibkr"] = {"ok": True, "value": c.settings.broker.account}
        broker.close()
    except Exception as exc:
        checks["ibkr"] = {"ok": False, "value": str(exc)}
    emit(c, checks)
    if any(not item["ok"] for key, item in checks.items() if key != "paper_execution"):
        raise typer.Exit(1)


@app.command("sync")
def sync_portfolio(ctx: typer.Context) -> None:
    c: Context = ctx.obj
    broker = IBKRBroker(c.settings).connect()
    try:
        snapshot = broker.snapshot()
        for position in snapshot["positions"]:
            position["sector"] = c.market.sector(position["ticker"])
            position["fx_rate"] = c.market.fx_rate(position["currency"], c.settings.base_currency)
            position["market_value"] *= position["fx_rate"]
        c.store.save_portfolio(snapshot)
        emit(c, snapshot)
    finally:
        broker.close()


@app.command()
def screen(ctx: typer.Context, watchlist: str | None = None) -> None:
    c: Context = ctx.obj
    tickers: set[str] = set()
    portfolio = c.store.latest_portfolio() or {}
    tickers.update(p["ticker"] for p in portfolio.get("positions", []))
    for market, item in c.settings.markets.items():
        if watchlist is None or market == watchlist:
            tickers.update(item.get("watchlist", []))
    results = []
    for ticker in sorted(tickers):
        try:
            evidence, error = c.market.evidence(ticker)
            if error:
                raise ValueError(error)
            yahoo = next(item for item in evidence if item.provider == "yfinance")
            result = screen_item(yahoo)
            result["evidence_ids"] = [item.id for item in evidence]
            results.append(result)
        except Exception as exc:
            results.append({"ticker": ticker, "passes": False, "error": str(exc)})
    results.sort(key=lambda item: item.get("score", float("-inf")), reverse=True)
    emit(c, results)


def _report(decision, state: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Báo cáo {decision.ticker}",
            "",
            f"- Quyết định: **{decision.action}**",
            f"- Conviction: **{decision.conviction}/10**",
            f"- Entry / Stop / Target: `{decision.entry}` / `{decision.stop}` / `{decision.target}`",
            "",
            "## Bull case",
            decision.bull_case or "Không có.",
            "",
            "## Bear case",
            decision.bear_case or "Không có.",
            "",
            "## Portfolio manager",
            decision.reason or "Không có.",
            "",
            "## Analyst reports",
            *[
                f"### {key}\n{state.get(key, '')}"
                for key in (
                    "market_report",
                    "sentiment_report",
                    "news_report",
                    "fundamentals_report",
                )
            ],
        ]
    )


def analyze_symbol(c: Context, ticker: str, exchange: str | None = None) -> dict[str, Any]:
    evidence, data_error = c.market.evidence(ticker)
    decision, state = run_research(ticker, c.settings, evidence, data_error)
    run_id = str(uuid.uuid4())
    report_dir = c.settings.artifacts / datetime.now().date().isoformat() / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "decision.json").write_text(json_dumps(asdict(decision)), encoding="utf-8")
    (report_dir / "evidence.json").write_text(
        json_dumps(serialize_evidence(evidence)), encoding="utf-8"
    )
    (report_dir / "report.md").write_text(_report(decision, state), encoding="utf-8")
    c.store.save_run(run_id, decision, report_dir)
    result: dict[str, Any] = {
        "run_id": run_id,
        "decision": asdict(decision),
        "report_dir": str(report_dir),
    }

    if decision.action not in {"ACCUMULATE", "REDUCE", "EXIT"}:
        return result
    portfolio = c.store.latest_portfolio()
    if not portfolio:
        result["ticket_error"] = "Run desk sync before creating tickets"
        return result
    position = next((p for p in portfolio["positions"] if p["ticker"] == ticker), None)
    side = "BUY" if decision.action == "ACCUMULATE" else "SELL"
    quote_currency = next(
        (item.payload.get("currency") for item in evidence if item.payload.get("currency")),
        c.settings.base_currency,
    )
    try:
        broker = IBKRBroker(c.settings).connect()
        try:
            contract = broker.resolve(ticker, exchange, quote_currency)
        finally:
            broker.close()
    except Exception as exc:
        result["ticket_error"] = f"IBKR contract unavailable: {exc}"
        return result
    try:
        fx_rate = c.market.fx_rate(contract["currency"], c.settings.base_currency)
    except ValueError as exc:
        result["ticket_error"] = str(exc)
        return result
    if side == "BUY":
        if not all((decision.entry, decision.stop, decision.target)):
            result["ticket_error"] = "Entry, stop and target are required"
            return result
        symbol_exposure = float(position["market_value"]) if position else 0.0
        sector = c.market.sector(ticker)
        if sector == "UNKNOWN" or any(p.get("sector") == "UNKNOWN" for p in portfolio["positions"]):
            result["ticket_error"] = "Sector exposure cannot be verified"
            return result
        sector_exposure = sum(
            float(p["market_value"]) for p in portfolio["positions"] if p.get("sector") == sector
        )
        gross_exposure = sum(float(p["market_value"]) for p in portfolio["positions"])
        try:
            quantity, limits = calculate_buy_quantity(
                nav=float(portfolio["nav"]),
                buying_power=float(portfolio["buying_power"]),
                entry=float(decision.entry) * fx_rate,
                stop=float(decision.stop) * fx_rate,
                symbol_exposure=symbol_exposure,
                sector_exposure=sector_exposure,
                gross_exposure=gross_exposure,
                risk=c.settings.risk,
            )
        except ValueError as exc:
            result["ticket_error"] = str(exc)
            return result
        intent = "ADD" if position else "OPEN"
    else:
        if not position:
            result["ticket_error"] = "Long-only desk cannot sell an unowned position"
            return result
        quantity = int(
            position["quantity"] if decision.action == "EXIT" else max(1, position["quantity"] // 2)
        )
        limits = {"owned_quantity": float(position["quantity"])}
        intent = "CLOSE" if decision.action == "EXIT" else "REDUCE"

    ticket = TradeTicket.create(
        ticker=ticker,
        con_id=contract["con_id"],
        currency=contract["currency"],
        intent=intent,
        side=side,
        quantity=quantity,
        limit_price=float(decision.entry or evidence[0].payload["close"]),
        stop_price=float(decision.stop or evidence[0].payload["close"] * 1.02),
        target_price=float(decision.target or evidence[0].payload["close"] * 0.98),
        risk_snapshot={
            "limits": limits,
            "nav": portfolio["nav"],
            "sector": c.market.sector(ticker),
            "fx_rate": fx_rate,
        },
        ttl_minutes=c.settings.risk.ticket_ttl_minutes,
    )
    c.store.save_ticket(ticket)
    result["ticket"] = asdict(ticket)
    return result


@app.command()
def analyze(ctx: typer.Context, ticker: str, exchange: str | None = None) -> None:
    emit(ctx.obj, analyze_symbol(ctx.obj, ticker.upper(), exchange))


@app.command()
def tickets(ctx: typer.Context) -> None:
    emit(ctx.obj, ctx.obj.store.tickets())


@app.command()
def approve(ctx: typer.Context, ticket_id: str, user: str = "local") -> None:
    c: Context = ctx.obj
    ticket = c.store.ticket(ticket_id)
    try:
        validate_approval(ticket, user, c.settings.telegram_allowlist)
    except ValueError as exc:
        if "expired" in str(exc).lower():
            c.store.update_ticket(ticket.id, "EXPIRED")
        raise typer.BadParameter(str(exc)) from exc
    latest = c.market.yfinance(ticket.ticker).payload["close"]
    validate_ticket_price(ticket, float(latest), c.settings.risk.max_quote_deviation)
    if os.getenv("PAPER_EXECUTION_ENABLED", "0") != "1":
        c.store.update_ticket(ticket.id, "APPROVED", user)
        emit(
            c,
            {
                "ticket_id": ticket.id,
                "status": "APPROVED",
                "submitted": False,
                "reason": "PAPER_EXECUTION_ENABLED is off",
            },
        )
        return
    broker = IBKRBroker(c.settings).connect()
    try:
        snapshot = broker.snapshot()
        validate_paper_account(snapshot["account"])
        if snapshot["account"] != c.settings.broker.account:
            raise ValueError("Paper account changed during approval")
        if ticket.side == "BUY":
            sector = ticket.risk_snapshot["sector"]
            for position in snapshot["positions"]:
                position["sector"] = c.market.sector(position["ticker"])
                position["fx_rate"] = c.market.fx_rate(
                    position["currency"], c.settings.base_currency
                )
                position["market_value"] *= position["fx_rate"]
            symbol_exposure = sum(
                float(p["market_value"])
                for p in snapshot["positions"]
                if p["ticker"] == ticket.ticker
            )
            sector_exposure = sum(
                float(p["market_value"]) for p in snapshot["positions"] if p["sector"] == sector
            )
            gross_exposure = sum(float(p["market_value"]) for p in snapshot["positions"])
            allowed, _ = calculate_buy_quantity(
                nav=float(snapshot["nav"]),
                buying_power=float(snapshot["buying_power"]),
                entry=ticket.limit_price * ticket.risk_snapshot["fx_rate"],
                stop=ticket.stop_price * ticket.risk_snapshot["fx_rate"],
                symbol_exposure=symbol_exposure,
                sector_exposure=sector_exposure,
                gross_exposure=gross_exposure,
                risk=c.settings.risk,
            )
            if ticket.quantity > allowed:
                raise ValueError(
                    f"Risk changed: ticket quantity {ticket.quantity} exceeds {allowed}"
                )
        c.store.update_ticket(ticket.id, "APPROVED", user)
        order = broker.place_ticket(ticket)
        c.store.save_order(ticket.id, "SUBMITTED", order)
        c.store.update_ticket(ticket.id, "SUBMITTED")
        emit(c, order)
    finally:
        broker.close()


@app.command()
def reject(ctx: typer.Context, ticket_id: str, user: str = "local") -> None:
    c: Context = ctx.obj
    ticket = c.store.ticket(ticket_id)
    try:
        validate_approval(ticket, user, c.settings.telegram_allowlist)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    c.store.update_ticket(ticket.id, "REJECTED", user)
    emit(c, {"ticket_id": ticket.id, "status": "REJECTED"})


@app.command()
def orders(ctx: typer.Context, reconcile: bool = False) -> None:
    c: Context = ctx.obj
    if reconcile:
        broker = IBKRBroker(c.settings).connect()
        try:
            for ticket_id, status in broker.order_statuses().items():
                c.store.update_order(ticket_id, status["status"], status)
        finally:
            broker.close()
    emit(c, c.store.orders())


def _due_sessions(c: Context, catch_up: bool) -> list[tuple[str, str]]:
    now = datetime.now(UTC)
    due: list[tuple[str, str]] = []
    for market, spec in c.settings.markets.items():
        cal = xcals.get_calendar(spec.get("calendar", market))
        session = cal.date_to_session(now.date(), direction="previous")
        close = cal.session_close(session).to_pydatetime().astimezone(UTC)
        if close > now:
            session = cal.previous_session(session)
            close = cal.session_close(session).to_pydatetime().astimezone(UTC)
        minutes = (now - close).total_seconds() / 60
        is_due = minutes >= 15 if catch_up else 15 <= minutes <= 45
        session_name = str(session.date())
        if is_due and not c.store.health_done(market, session_name):
            due.append((market, session_name))
    return due


def _market_for_ticker(c: Context, ticker: str) -> tuple[str, dict[str, Any]]:
    for market, spec in c.settings.markets.items():
        if ticker in spec.get("watchlist", []):
            return market, spec
    if not c.settings.markets:
        raise ValueError("At least one market calendar is required")
    return next(iter(c.settings.markets.items()))


def _benchmark(ticker: str) -> str:
    mapping = {
        ".NS": "^NSEI",
        ".BO": "^BSESN",
        ".T": "^N225",
        ".HK": "^HSI",
        ".L": "^FTSE",
        ".TO": "^GSPTSE",
        ".AX": "^AXJO",
        ".SS": "000001.SS",
        ".SZ": "399001.SZ",
    }
    return next((value for suffix, value in mapping.items() if ticker.endswith(suffix)), "SPY")


def reflect_due(c: Context) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    created: list[dict[str, Any]] = []
    for run in c.store.runs():
        _, spec = _market_for_ticker(c, run["ticker"])
        cal = xcals.get_calendar(spec.get("calendar"))
        start = datetime.fromisoformat(run["created_at"]).date()
        sessions = cal.sessions_in_range(start, now.date())
        if len(sessions) < 20:
            continue
        benchmark = _benchmark(run["ticker"])
        try:
            raw_return = c.market.performance(run["ticker"], start)
            benchmark_return = c.market.performance(benchmark, start)
        except Exception:
            continue
        payload = {
            "run_id": run["id"],
            "ticker": run["ticker"],
            "horizon_sessions": len(sessions),
            "raw_return": raw_return,
            "benchmark": benchmark,
            "benchmark_return": benchmark_return,
            "alpha": raw_return - benchmark_return,
            "source": "yfinance adjusted close; evaluation only",
        }
        if c.store.save_reflection(run["id"], run["ticker"], payload):
            created.append(payload)
    return created


@app.command()
def reflections(ctx: typer.Context, evaluate: bool = False) -> None:
    c: Context = ctx.obj
    created = reflect_due(c) if evaluate else []
    emit(c, {"created": created, "reflections": c.store.reflections()})


@app.command()
def health(
    ctx: typer.Context,
    due: bool = typer.Option(False, "--due"),
    catch_up: bool = typer.Option(False, "--catch-up"),
) -> None:
    c: Context = ctx.obj
    if due and catch_up:
        raise typer.BadParameter("Choose --due or --catch-up")
    sessions = (
        _due_sessions(c, catch_up)
        if due or catch_up
        else [("MANUAL", datetime.now().date().isoformat())]
    )
    if (due or catch_up) and not sessions:
        return
    portfolio = c.store.latest_portfolio()
    if not portfolio:
        raise typer.BadParameter("Run desk sync first")
    reports = []
    nav = float(portfolio["nav"])
    gross = sum(float(p["market_value"]) for p in portfolio["positions"])
    for market, session in sessions:
        alerts = []
        spec = c.settings.markets.get(market, {})
        exchanges = set(spec.get("ibkr_exchanges", []))
        watchlist = set(spec.get("watchlist", []))
        positions = [
            p
            for p in portfolio["positions"]
            if market == "MANUAL" or p.get("exchange") in exchanges or p["ticker"] in watchlist
        ]
        sectors: dict[str, float] = {}
        for position in positions:
            sectors[position.get("sector", "UNKNOWN")] = sectors.get(
                position.get("sector", "UNKNOWN"), 0
            ) + float(position["market_value"])
            exposure = float(position["market_value"]) / nav if nav else 1
            latest = c.store.latest_run(position["ticker"])
            protection = c.store.latest_ticket(position["ticker"])
            if exposure > c.settings.risk.max_symbol:
                alerts.append(
                    {"ticker": position["ticker"], "reason": f"symbol exposure {exposure:.2%}"}
                )
            if not latest or datetime.fromisoformat(latest["created_at"]) < datetime.now(
                UTC
            ) - timedelta(days=7):
                alerts.append({"ticker": position["ticker"], "reason": "missing or stale thesis"})
            if not protection or protection.status not in {"SUBMITTED", "FILLED"}:
                alerts.append(
                    {"ticker": position["ticker"], "reason": "no tracked protective stop"}
                )
            elif c.market.yfinance(position["ticker"]).payload["close"] <= protection.stop_price:
                alerts.append({"ticker": position["ticker"], "reason": "stop breached"})
        for sector, value in sectors.items():
            if nav and value / nav > c.settings.risk.max_sector:
                alerts.append({"sector": sector, "reason": f"sector exposure {value / nav:.2%}"})
        if nav and gross / nav > c.settings.risk.max_gross:
            alerts.append({"portfolio": True, "reason": f"gross exposure {gross / nav:.2%}"})
        analyses = []
        if os.getenv("OPENAI_API_KEY"):
            for ticker in sorted({alert.get("ticker") for alert in alerts if alert.get("ticker")}):
                try:
                    analyses.append(analyze_symbol(c, ticker))
                except Exception as exc:
                    analyses.append({"ticker": ticker, "error": str(exc)})
        reports.append(
            {
                "market": market,
                "session": session,
                "alerts": alerts,
                "analyses": analyses,
                "status": "WATCH" if alerts else "HOLD",
            }
        )
        c.store.mark_health(market, session)
    emit(c, {"health": reports, "new_reflections": reflect_due(c)})


if __name__ == "__main__":
    app()
