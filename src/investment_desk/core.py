from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml


Action = Literal["HOLD", "ACCUMULATE", "REDUCE", "EXIT", "NO_TRADE"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(slots=True)
class EvidenceItem:
    provider: str
    source: str
    fetched_at: str
    as_of: str
    delayed: bool
    stale: bool
    payload: dict[str, Any]
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raw = json_dumps(
                {"provider": self.provider, "as_of": self.as_of, "payload": self.payload}
            )
            self.id = hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(slots=True)
class ResearchDecision:
    ticker: str
    action: Action
    conviction: float
    bull_case: str
    bear_case: str
    catalysts: list[str]
    invalidation: str
    entry: float | None
    stop: float | None
    target: float | None
    evidence_ids: list[str]
    raw_signal: str = ""
    reason: str = ""


@dataclass(slots=True)
class TradeTicket:
    id: str
    ticker: str
    con_id: int
    currency: str
    intent: Literal["OPEN", "ADD", "REDUCE", "CLOSE"]
    side: Literal["BUY", "SELL"]
    quantity: int
    limit_price: float
    stop_price: float
    target_price: float
    risk_snapshot: dict[str, Any]
    created_at: str
    expires_at: str
    status: str = "PENDING"

    @classmethod
    def create(cls, **values: Any) -> "TradeTicket":
        created = utcnow()
        return cls(
            id=str(uuid.uuid4()),
            created_at=iso(created),
            expires_at=iso(created + timedelta(minutes=values.pop("ttl_minutes"))),
            **values,
        )


@dataclass(slots=True)
class RiskSettings:
    per_trade: float = 0.01
    max_symbol: float = 0.15
    max_sector: float = 0.30
    max_gross: float = 1.0
    max_quote_deviation: float = 0.005
    ticket_ttl_minutes: int = 30


@dataclass(slots=True)
class BrokerSettings:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 71
    account: str = ""


@dataclass(slots=True)
class ModelSettings:
    quick: str = "gpt-5.4-mini"
    deep: str = "gpt-5.5"
    debate_rounds: int = 2


@dataclass(slots=True)
class Settings:
    base_currency: str = "USD"
    database: Path = Path("data/desk.sqlite3")
    artifacts: Path = Path("artifacts")
    risk: RiskSettings = field(default_factory=RiskSettings)
    broker: BrokerSettings = field(default_factory=BrokerSettings)
    models: ModelSettings = field(default_factory=ModelSettings)
    telegram_allowlist: list[str] = field(default_factory=list)
    markets: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_settings(path: Path) -> Settings:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent

    def local_path(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else base / candidate

    settings = Settings(
        base_currency=str(raw.get("base_currency", "USD")),
        database=local_path(raw.get("database", "data/desk.sqlite3")),
        artifacts=local_path(raw.get("artifacts", "artifacts")),
        risk=RiskSettings(**raw.get("risk", {})),
        broker=BrokerSettings(**raw.get("broker", {})),
        models=ModelSettings(**raw.get("models", {})),
        telegram_allowlist=[str(v) for v in raw.get("telegram_allowlist", [])],
        markets=raw.get("markets", {}),
    )
    for name, value in asdict(settings.risk).items():
        if not 0 < value <= 1 and name != "ticket_ttl_minutes":
            raise ValueError(f"risk.{name} must be between 0 and 1")
    if settings.risk.ticket_ttl_minutes <= 0:
        raise ValueError("risk.ticket_ttl_minutes must be positive")
    return settings


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
              id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, ticker TEXT NOT NULL, created_at TEXT NOT NULL,
              decision TEXT NOT NULL, report_dir TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
              id TEXT PRIMARY KEY, ticker TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT NOT NULL,
              approved_by TEXT, approved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
              ticket_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL,
              payload TEXT NOT NULL, FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            );
            CREATE TABLE IF NOT EXISTS health_runs (
              market TEXT NOT NULL, session TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(market, session)
            );
            CREATE TABLE IF NOT EXISTS reflections (
              id INTEGER PRIMARY KEY, run_id TEXT UNIQUE, ticker TEXT NOT NULL,
              created_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(reflections)")}
        if "run_id" not in columns:
            self.db.execute("ALTER TABLE reflections ADD COLUMN run_id TEXT")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS reflections_run_id ON reflections(run_id)"
            )

    def save_portfolio(self, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO portfolio_snapshots(created_at,payload) VALUES (?,?)",
            (iso(), json_dumps(payload)),
        )
        self.db.commit()

    def latest_portfolio(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_run(self, run_id: str, decision: ResearchDecision, report_dir: Path) -> None:
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            (run_id, decision.ticker, iso(), json_dumps(asdict(decision)), str(report_dir)),
        )
        self.db.commit()

    def latest_run(self, ticker: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT decision,created_at FROM runs WHERE ticker=? ORDER BY created_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return None
        result = json.loads(row["decision"])
        result["created_at"] = row["created_at"]
        return result

    def runs(self) -> list[dict[str, Any]]:
        result = []
        for row in self.db.execute("SELECT * FROM runs ORDER BY created_at"):
            result.append(
                {
                    "id": row["id"],
                    "ticker": row["ticker"],
                    "created_at": row["created_at"],
                    "decision": json.loads(row["decision"]),
                    "report_dir": row["report_dir"],
                }
            )
        return result

    def save_ticket(self, ticket: TradeTicket) -> None:
        self.db.execute(
            "INSERT INTO tickets(id,ticker,status,created_at,expires_at,payload) VALUES (?,?,?,?,?,?)",
            (
                ticket.id,
                ticket.ticker,
                ticket.status,
                ticket.created_at,
                ticket.expires_at,
                json_dumps(asdict(ticket)),
            ),
        )
        self.db.commit()

    def ticket(self, ticket_id: str) -> TradeTicket:
        row = self.db.execute(
            "SELECT payload,status FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown ticket: {ticket_id}")
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        return TradeTicket(**payload)

    def tickets(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT payload,status,approved_by,approved_at FROM tickets ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload"])
            item.update(
                status=row["status"], approved_by=row["approved_by"], approved_at=row["approved_at"]
            )
            result.append(item)
        return result

    def latest_ticket(self, ticker: str) -> TradeTicket | None:
        row = self.db.execute(
            "SELECT payload,status FROM tickets WHERE ticker=? ORDER BY created_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        return TradeTicket(**payload)

    def update_ticket(self, ticket_id: str, status: str, actor: str | None = None) -> None:
        if actor:
            self.db.execute(
                "UPDATE tickets SET status=?,approved_by=?,approved_at=? WHERE id=?",
                (status, actor, iso(), ticket_id),
            )
        else:
            self.db.execute("UPDATE tickets SET status=? WHERE id=?", (status, ticket_id))
        self.db.commit()

    def save_order(self, ticket_id: str, status: str, payload: dict[str, Any]) -> None:
        try:
            self.db.execute(
                "INSERT INTO orders VALUES (?,?,?,?)",
                (ticket_id, status, iso(), json_dumps(payload)),
            )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Ticket {ticket_id} already has an order") from exc

    def orders(self) -> list[dict[str, Any]]:
        return [
            dict(row) | {"payload": json.loads(row["payload"])}
            for row in self.db.execute("SELECT * FROM orders ORDER BY updated_at DESC")
        ]

    def update_order(self, ticket_id: str, status: str, payload: dict[str, Any]) -> None:
        row = self.db.execute(
            "SELECT payload FROM orders WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if not row:
            return
        merged = json.loads(row["payload"])
        merged.update(payload)
        self.db.execute(
            "UPDATE orders SET status=?,updated_at=?,payload=? WHERE ticket_id=?",
            (status, iso(), json_dumps(merged), ticket_id),
        )
        self.db.commit()

    def health_done(self, market: str, session: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM health_runs WHERE market=? AND session=?", (market, session)
            ).fetchone()
            is not None
        )

    def mark_health(self, market: str, session: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO health_runs VALUES (?,?,?)", (market, session, iso())
        )
        self.db.commit()

    def save_reflection(self, run_id: str, ticker: str, payload: dict[str, Any]) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO reflections(run_id,ticker,created_at,payload) VALUES (?,?,?,?)",
            (run_id, ticker, iso(), json_dumps(payload)),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def reflections(self) -> list[dict[str, Any]]:
        return [
            dict(row) | {"payload": json.loads(row["payload"])}
            for row in self.db.execute("SELECT * FROM reflections ORDER BY created_at DESC")
        ]


def calculate_buy_quantity(
    *,
    nav: float,
    buying_power: float,
    entry: float,
    stop: float,
    symbol_exposure: float,
    sector_exposure: float,
    gross_exposure: float,
    risk: RiskSettings,
) -> tuple[int, dict[str, float]]:
    if min(nav, buying_power, entry, stop) <= 0 or stop >= entry:
        raise ValueError("BUY requires positive values and stop below entry")
    risk_room = risk.per_trade * nav / (entry - stop)
    symbol_room = max(0.0, risk.max_symbol * nav - symbol_exposure) / entry
    sector_room = max(0.0, risk.max_sector * nav - sector_exposure) / entry
    gross_room = max(0.0, risk.max_gross * nav - gross_exposure) / entry
    cash_room = buying_power / entry
    limits = {
        "risk": risk_room,
        "symbol": symbol_room,
        "sector": sector_room,
        "gross": gross_room,
        "cash": cash_room,
    }
    quantity = math.floor(min(limits.values()))
    if quantity < 1:
        raise ValueError("Risk limits leave no whole-share quantity")
    return quantity, limits


def validate_ticket_price(ticket: TradeTicket, latest_price: float, max_deviation: float) -> None:
    if latest_price <= 0:
        raise ValueError("Latest price is unavailable")
    deviation = abs(latest_price - ticket.limit_price) / ticket.limit_price
    if deviation > max_deviation:
        raise ValueError(f"Quote moved {deviation:.2%}; maximum is {max_deviation:.2%}")


def validate_approval(
    ticket: TradeTicket,
    user: str,
    allowlist: list[str],
    now: datetime | None = None,
) -> None:
    if ticket.status != "PENDING":
        raise ValueError(f"Ticket is {ticket.status}, not PENDING")
    if user != "local" and user not in allowlist:
        raise ValueError("Telegram user is not allowlisted")
    if datetime.fromisoformat(ticket.expires_at) <= (now or utcnow()):
        raise ValueError("Ticket has expired")


def validate_paper_account(account: str) -> None:
    if not account.startswith("DU"):
        raise ValueError("Refusing non-paper IBKR account; account must start with DU")
