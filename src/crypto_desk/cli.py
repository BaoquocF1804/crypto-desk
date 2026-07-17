from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import typer
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from .broker import BinanceSpotBroker
from .committee import CryptoCommittee, OpenAIStructuredClient
from .config import Settings, load_settings
from .data import EvidenceBuilder, PublicDataClient
from .domain import to_jsonable
from .execution import ExecutionService, confirmation_code
from .service import AnalysisRun, CryptoDeskService
from .store import Store


app = typer.Typer(
    name="desk",
    help="Crypto Desk — nghiên cứu và giao dịch Binance Spot có duyệt.",
    no_args_is_help=True,
)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option("--config", help="Đường dẫn YAML cấu hình."),
    ] = Path("config.yaml"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Xuất một JSON value ổn định."),
    ] = False,
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["json"] = json_output


@app.command()
def doctor(
    ctx: typer.Context,
    online: Annotated[
        bool,
        typer.Option("--online", help="Bao gồm smoke checks trực tuyến."),
    ] = False,
) -> None:
    settings = _load(ctx)
    store = Store(settings.database)
    payload = doctor_report(settings, store, online=online)
    _emit(ctx, payload)


@app.command()
def sync(ctx: typer.Context) -> None:
    settings = _load(ctx)
    service = _service(settings, broker=True)
    _emit(ctx, service.sync())


@app.command()
def screen(ctx: typer.Context) -> None:
    settings = _load(ctx)
    service = _service(settings, committee=False)
    _emit(ctx, service.screen())


@app.command()
def analyze(ctx: typer.Context, symbol: str) -> None:
    settings = _load(ctx)
    normalized = symbol.upper()
    if normalized not in settings.symbols:
        _fail("Symbol is outside the configured allowlist")
    service = _service(settings)
    _emit(ctx, service.analyze(normalized))


@app.command()
def daily(
    ctx: typer.Context,
    due: Annotated[bool, typer.Option("--due")] = False,
    catch_up: Annotated[bool, typer.Option("--catch-up")] = False,
) -> None:
    settings = _load(ctx)
    _emit(
        ctx,
        _service(settings).daily(due=due, catch_up=catch_up),
    )


@app.command()
def health(
    ctx: typer.Context,
    due: Annotated[bool, typer.Option("--due")] = False,
) -> None:
    settings = _load(ctx)
    service = _service(settings, broker=True, execution=True)
    _emit(ctx, service.health(due=due))


@app.command()
def tickets(ctx: typer.Context) -> None:
    settings = _load(ctx)
    _emit(ctx, Store(settings.database).list_tickets())


@app.command()
def approve(
    ctx: typer.Context,
    ticket_id: str,
    actor: Annotated[str, typer.Option("--actor")] = "local-operator",
    channel: Annotated[str, typer.Option("--channel")] = "local",
    code: Annotated[str | None, typer.Option("--code")] = None,
) -> None:
    settings = _load(ctx)
    service = _execution_service(settings)
    try:
        result = service.approve(
            ticket_id,
            actor=actor,
            channel=channel,
            code=code,
        )
    except ValueError as exc:
        _fail(str(exc))
    _emit(ctx, result)


@app.command()
def reject(
    ctx: typer.Context,
    ticket_id: str,
    actor: Annotated[str, typer.Option("--actor")] = "local-operator",
    channel: Annotated[str, typer.Option("--channel")] = "local",
) -> None:
    settings = _load(ctx)
    service = _execution_service(settings)
    try:
        result = service.reject(
            ticket_id,
            actor=actor,
            channel=channel,
        )
    except ValueError as exc:
        _fail(str(exc))
    _emit(ctx, result)


@app.command()
def orders(ctx: typer.Context) -> None:
    settings = _load(ctx)
    _emit(ctx, Store(settings.database).list_submissions())


@app.command("live-code")
def live_code(ctx: typer.Context, ticket_id: str) -> None:
    settings = _load(ctx)
    store = Store(settings.database)
    ticket = store.ticket(ticket_id)
    if ticket.environment != "mainnet":
        _fail("Live confirmation code is only valid for Mainnet tickets")
    secret = os.getenv("LIVE_CONFIRMATION_SECRET")
    if not secret:
        _fail("LIVE_CONFIRMATION_SECRET is missing")
    typer.echo(confirmation_code(secret, ticket_id, _utcnow()))


@app.command()
def reflections(
    ctx: typer.Context,
    symbol: str | None = None,
) -> None:
    settings = _load(ctx)
    normalized = symbol.upper() if symbol else None
    _emit(
        ctx,
        Store(settings.database).list_reflections(normalized),
    )


def doctor_report(
    settings: Settings,
    store: Store,
    *,
    online: bool,
) -> dict[str, Any]:
    environment = settings.binance.environment
    prefix = environment.upper()
    packages = {}
    for package in (
        "crypto-desk",
        "binance-sdk-spot",
        "openai",
        "httpx",
        "pydantic",
    ):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = "missing"
    report = {
        "python": platform.python_version(),
        "packages": packages,
        "openai": {
            "key_present": bool(os.getenv("OPENAI_API_KEY")),
            "online_smoke_requested": online,
        },
        "binance": {
            "environment": environment,
            "base_url": (
                "https://testnet.binance.vision"
                if environment == "testnet"
                else "https://api.binance.com"
            ),
            "credentials_present": bool(
                os.getenv(f"BINANCE_{prefix}_API_KEY") and os.getenv(f"BINANCE_{prefix}_API_SECRET")
            ),
            "testnet_execution_enabled": (os.getenv("TESTNET_EXECUTION_ENABLED") == "1"),
            "live_execution_enabled": (os.getenv("LIVE_EXECUTION_ENABLED") == "1"),
            "confirmation_secret_present": bool(os.getenv("LIVE_CONFIRMATION_SECRET")),
            "completed_mainnet_chains": (store.completed_mainnet_chains()),
            "initial_cap_active": store.completed_mainnet_chains() < 20,
        },
        "telegram": {
            "token_present": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "allowlist_count": len(settings.telegram_allowlist),
        },
        "schedule": to_jsonable(settings.schedule),
        "hermes": {
            "installed": shutil.which("hermes") is not None,
        },
    }
    if online:
        report["online"] = _online_doctor(settings)
    return report


class _DoctorSmoke(BaseModel):
    ok: Literal[True]


def _online_doctor(settings: Settings) -> dict[str, Any]:
    results: dict[str, Any] = {}
    with httpx.Client(timeout=10) as client:
        try:
            response = client.get("https://api.binance.com/api/v3/time")
            response.raise_for_status()
            server_time = int(response.json()["serverTime"])
            local_time = int(datetime.now(UTC).timestamp() * 1000)
            results["binance_spot"] = {
                "ok": True,
                "clock_drift_ms": abs(local_time - server_time),
            }
        except Exception as exc:
            results["binance_spot"] = {
                "ok": False,
                "error": type(exc).__name__,
            }
        try:
            response = client.get("https://fapi.binance.com/fapi/v1/time")
            response.raise_for_status()
            results["binance_derivatives_public"] = {"ok": True}
        except Exception as exc:
            results["binance_derivatives_public"] = {
                "ok": False,
                "error": type(exc).__name__,
            }
        try:
            response = client.get("https://api.coingecko.com/api/v3/ping")
            response.raise_for_status()
            results["coingecko"] = {"ok": True}
        except Exception as exc:
            results["coingecko"] = {
                "ok": False,
                "error": type(exc).__name__,
            }
        rss_results = []
        for feed in settings.news_feeds:
            try:
                response = client.get(feed)
                response.raise_for_status()
                rss_results.append({"url": feed, "ok": True})
            except Exception as exc:
                rss_results.append(
                    {
                        "url": feed,
                        "ok": False,
                        "error": type(exc).__name__,
                    }
                )
        results["rss"] = rss_results

    if os.getenv("OPENAI_API_KEY"):
        try:
            response = OpenAI().responses.parse(
                model=settings.models.quick,
                input=[
                    {
                        "role": "system",
                        "content": "Return ok=true.",
                    },
                    {"role": "user", "content": "Health check."},
                ],
                text_format=_DoctorSmoke,
            )
            results["openai_structured_output"] = {
                "ok": bool(response.output_parsed and response.output_parsed.ok)
            }
        except Exception as exc:
            results["openai_structured_output"] = {
                "ok": False,
                "error": type(exc).__name__,
            }
    else:
        results["openai_structured_output"] = {
            "ok": False,
            "error": "missing_key",
        }
    return results


def _service(
    settings: Settings,
    *,
    broker: bool = False,
    committee: bool = True,
    execution: bool = False,
) -> CryptoDeskService:
    store = Store(settings.database)
    evidence_builder = EvidenceBuilder(
        PublicDataClient(settings.news_feeds),
        settings.coingecko_ids,
    )
    selected_broker = BinanceSpotBroker(settings.binance.environment) if broker else None
    selected_committee = None
    if committee:
        selected_committee = CryptoCommittee(
            OpenAIStructuredClient(OpenAI()),
            quick_model=settings.models.quick,
            deep_model=settings.models.deep,
            debate_rounds=settings.models.debate_rounds,
        )
    selected_execution = None
    if execution:
        assert selected_broker is not None
        selected_execution = ExecutionService(
            store,
            selected_broker,
            settings,
        )
    return CryptoDeskService(
        settings,
        store,
        broker=selected_broker,
        evidence_builder=evidence_builder,
        committee=selected_committee,
        execution=selected_execution,
    )


def _execution_service(settings: Settings) -> ExecutionService:
    store = Store(settings.database)
    broker = BinanceSpotBroker(settings.binance.environment)
    return ExecutionService(store, broker, settings)


def _load(ctx: typer.Context) -> Settings:
    load_dotenv()
    try:
        return load_settings(Path(ctx.obj["config"]))
    except (OSError, ValueError) as exc:
        _fail(str(exc))


def _emit(ctx: typer.Context, payload: Any) -> None:
    normalized = to_jsonable(payload)
    if ctx.obj["json"]:
        typer.echo(
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    if isinstance(payload, AnalysisRun):
        typer.echo(
            f"# {payload.decision.symbol}: {payload.decision.action}\n\n"
            f"Báo cáo: {payload.report_dir}"
        )
    else:
        typer.echo(
            json.dumps(
                normalized,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=2)


def _utcnow():
    from .domain import utcnow

    return utcnow()
