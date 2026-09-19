from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import signal
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import typer
from dotenv import load_dotenv
from google import genai
from openai import OpenAI
from pydantic import BaseModel

from .broker import BinanceSpotBroker
from .committee import (
    CryptoCommittee,
    GeminiStructuredClient,
    OpenAIStructuredClient,
    ProviderError,
    StructuredClient,
)
from .config import MAINNET_GRADUATION_CHAINS, Settings, load_settings
from .dashboard import (
    DashboardPublishError,
    SITES_BYPASS_TOKEN_ENV,
    build_dashboard_snapshot,
    is_loopback_url,
    publish_dashboard_if_configured,
    publish_dashboard_from_env,
)
from .data import EvidenceBuilder, PublicDataClient
from .domain import to_jsonable
from .execution import ExecutionService, confirmation_code, telegram_approval_proof
from .runner import (
    COMMAND_API_URL_ENV,
    RUNNER_ENABLED_ENV,
    RUNNER_TOKEN_ENV,
    CommandRunner,
    RunnerProtocolError,
)
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
    result = service.sync()
    _publish_dashboard_if_configured(settings)
    _emit(ctx, result)


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
    result = service.analyze(normalized)
    _publish_dashboard_if_configured(settings)
    _emit(ctx, result)


@app.command()
def daily(
    ctx: typer.Context,
    due: Annotated[bool, typer.Option("--due")] = False,
    catch_up: Annotated[bool, typer.Option("--catch-up")] = False,
) -> None:
    settings = _load(ctx)
    result = _service(settings).daily(due=due, catch_up=catch_up)
    if result.get("status") == "COMPLETED":
        _publish_dashboard_if_configured(settings)
    _emit(ctx, result)


@app.command()
def health(
    ctx: typer.Context,
    due: Annotated[bool, typer.Option("--due")] = False,
) -> None:
    settings = _load(ctx)
    service = _service(settings, broker=True, execution=True)
    result = service.health(due=due)
    if result.get("status") == "COMPLETED":
        _publish_dashboard_if_configured(settings)
    _emit(ctx, result)


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
    telegram_proof: Annotated[
        str | None,
        typer.Option("--telegram-proof", hidden=True),
    ] = None,
) -> None:
    settings = _load(ctx)
    if (
        telegram_proof is None
        and channel == "telegram"
        and code is not None
        and (ingress_secret := os.getenv("HERMES_TELEGRAM_INGRESS_SECRET"))
    ):
        telegram_proof = telegram_approval_proof(
            ingress_secret,
            ticket_id,
            actor,
            code,
        )
    service = _execution_service(settings)
    try:
        result = service.approve(
            ticket_id,
            actor=actor,
            channel=channel,
            code=code,
            telegram_proof=telegram_proof,
        )
    except ValueError as exc:
        _fail(str(exc))
    _publish_dashboard_if_configured(settings)
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
    _publish_dashboard_if_configured(settings)
    _emit(ctx, result)


@app.command()
def orders(
    ctx: typer.Context,
    reconcile: Annotated[
        bool,
        typer.Option("--reconcile", help="Chỉ tra cứu lại order chain chưa terminal."),
    ] = False,
) -> None:
    settings = _load(ctx)
    store = Store(settings.database)
    submissions = store.list_submissions()
    store.close()
    if not reconcile:
        _emit(ctx, submissions)
        return

    terminal = {"FILLED", "EXPIRED", "CANCELED", "REJECTED", "FAILED_SAFE"}
    service = _execution_service(settings)
    results = []
    errors = []
    for submission in submissions:
        if submission["status"] in terminal:
            continue
        try:
            results.append(service.reconcile(submission["ticket_id"]))
        except Exception as exc:
            errors.append(
                {
                    "ticket_id": submission["ticket_id"],
                    "error": type(exc).__name__,
                }
            )
    refreshed = Store(settings.database)
    current = refreshed.list_submissions()
    refreshed.close()
    if results:
        _publish_dashboard_if_configured(settings)
    _emit(
        ctx,
        {
            "orders": current,
            "reconciled": results,
            "errors": errors,
        },
    )


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


@app.command("publish-dashboard")
def publish_dashboard_command(ctx: typer.Context) -> None:
    settings = _load(ctx)
    store = None
    try:
        store = Store(settings.database)
        snapshot = build_dashboard_snapshot(settings, store)
        publish_dashboard_from_env(snapshot, strict=True)
    except (DashboardPublishError, OSError, sqlite3.Error) as exc:
        _fail(str(exc))
    finally:
        if store is not None:
            store.close()
    _emit(ctx, {"status": "PUBLISHED", "generated_at": snapshot.generated_at})


@app.command()
def runner(ctx: typer.Context) -> None:
    """Chạy command runner outbound cho dashboard tương tác."""
    settings = _load(ctx)
    base_url = os.getenv(COMMAND_API_URL_ENV)
    runner_token = os.getenv(RUNNER_TOKEN_ENV)
    bypass_token = os.getenv(SITES_BYPASS_TOKEN_ENV)
    missing = [
        name
        for name, value in ((COMMAND_API_URL_ENV, base_url), (RUNNER_TOKEN_ENV, runner_token))
        if not value
    ]
    if base_url and not is_loopback_url(base_url) and not bypass_token:
        missing.append(SITES_BYPASS_TOKEN_ENV)
    if missing:
        _fail(f"Thiếu biến môi trường bắt buộc: {', '.join(missing)}")

    worker = CommandRunner(
        settings,
        base_url=base_url,
        runner_token=runner_token,
        sites_bypass_token=bypass_token or "",
        enabled=os.getenv(RUNNER_ENABLED_ENV, "1") == "1",
        log=lambda message: typer.echo(message, err=True),
    )
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(
        signal.SIGTERM,
        lambda signum, frame: worker.stop(),
    )
    signal.signal(
        signal.SIGINT,
        lambda signum, frame: worker.stop(),
    )
    try:
        worker.run_forever()
    except RunnerProtocolError as exc:
        _fail(str(exc))
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


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
        "google-genai",
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
            "active": settings.models.provider == "openai",
            "online_smoke_requested": online,
        },
        "gemini": {
            "key_present": bool(
                os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                or os.getenv("GOOGLE_CLOUD_PROJECT")
            ),
            "active": settings.models.provider in {"gemini", "vertexai"},
            "online_smoke_requested": online,
        },
        "llm": {
            "provider": settings.models.provider,
            "quick_model": settings.models.quick,
            "deep_model": settings.models.deep,
            "quick_thinking": settings.models.quick_thinking,
            "deep_thinking": settings.models.deep_thinking,
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
            "initial_cap_active": store.completed_mainnet_chains() < MAINNET_GRADUATION_CHAINS,
        },
        "telegram": {
            "token_present": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "trusted_ingress_present": bool(os.getenv("HERMES_TELEGRAM_INGRESS_SECRET")),
            "allowlist_count": len(settings.telegram_allowlist),
        },
        "news": {
            "feeds_configured": len(settings.news_feeds),
            "analyze_possible": bool(settings.news_feeds),
        },
        "schedule": to_jsonable(settings.schedule),
        "hermes": {
            "installed": _hermes_installed(),
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

    provider_result = f"{settings.models.provider}_structured_output"
    try:
        response = _structured_client(settings).generate(
            stage="doctor",
            model=settings.models.quick,
            thinking=settings.models.quick_thinking,
            response_model=_DoctorSmoke,
            system_prompt="Return JSON with ok=true.",
            payload={"check": "health"},
        )
        results[provider_result] = {"ok": bool(response.ok)}
    except ProviderError as exc:
        results[provider_result] = {"ok": False, "error": exc.category}
    except Exception as exc:
        results[provider_result] = {"ok": False, "error": type(exc).__name__}
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
    selected_broker = _broker(settings) if broker else None
    selected_committee = None
    if committee:
        selected_committee = CryptoCommittee(
            _structured_client(settings),
            provider=settings.models.provider,
            quick_model=settings.models.quick,
            deep_model=settings.models.deep,
            quick_thinking=settings.models.quick_thinking,
            deep_thinking=settings.models.deep_thinking,
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


def _structured_client(settings: Settings) -> StructuredClient:
    if settings.models.provider in {"gemini", "vertexai"}:
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        if credentials_path or project or settings.models.provider == "vertexai":
            if credentials_path and not os.path.isabs(credentials_path):
                resolved = Path(credentials_path).resolve()
                if resolved.exists():
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(resolved)
            kwargs: dict[str, Any] = {"vertexai": True}
            if project:
                kwargs["project"] = project
            if location:
                kwargs["location"] = location
            return GeminiStructuredClient(
                genai.Client(**kwargs),
                min_interval_seconds=float(os.getenv("GEMINI_MIN_REQUEST_INTERVAL_SECONDS", "6")),
            )

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ProviderError("missing_key")
        api_version = (
            "v1beta"
            if any(
                model.endswith("-preview")
                for model in (settings.models.quick, settings.models.deep)
            )
            else "v1"
        )
        return GeminiStructuredClient(
            genai.Client(
                api_key=api_key,
                http_options={"api_version": api_version},
            ),
            min_interval_seconds=float(os.getenv("GEMINI_MIN_REQUEST_INTERVAL_SECONDS", "6")),
        )
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ProviderError("missing_key")
    return OpenAIStructuredClient(OpenAI(api_key=api_key))


def _execution_service(settings: Settings) -> ExecutionService:
    store = Store(settings.database)
    broker = _broker(settings)
    return ExecutionService(store, broker, settings)


def _broker(settings: Settings) -> BinanceSpotBroker:
    selected = os.getenv("BINANCE_ENV")
    if selected not in {"testnet", "mainnet"}:
        raise ValueError("BINANCE_ENV must be testnet or mainnet")
    if selected != settings.binance.environment:
        raise ValueError("BINANCE_ENV does not match config binance.environment")
    return BinanceSpotBroker(selected)


def _load(ctx: typer.Context) -> Settings:
    load_dotenv()
    try:
        return load_settings(Path(ctx.obj["config"]))
    except (OSError, ValueError) as exc:
        _fail(str(exc))


def _publish_dashboard_if_configured(settings: Settings) -> None:
    warning = publish_dashboard_if_configured(
        settings,
        store_factory=Store,
    )
    if warning:
        typer.echo(warning, err=True)


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
        msg = f"# {payload.decision.symbol}: {payload.decision.action}\n"
        if payload.decision.futures_bias:
            msg += f"- Futures Bias: {payload.decision.futures_bias}\n"
        if payload.decision.futures_setups:
            msg += "- Futures Setups (Non-executing):\n"
            for setup in payload.decision.futures_setups:
                msg += f"  • [{setup.direction}] Entry: {setup.entry} | SL: {setup.stop} | TP: {setup.target} (R:R {setup.risk_reward_ratio})\n"
        msg += f"\nBáo cáo: {payload.report_dir}"
        typer.echo(msg)
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


def _hermes_installed(*, home: Path | None = None) -> bool:
    if shutil.which("hermes") is not None:
        return True
    candidate = (home or Path.home()) / ".local" / "bin" / "hermes"
    return candidate.is_file() and os.access(candidate, os.X_OK)
