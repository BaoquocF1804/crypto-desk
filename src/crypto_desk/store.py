from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import (
    Environment,
    PortfolioSnapshot,
    ResearchDecision,
    TradeTicket,
    iso,
    to_jsonable,
)


TERMINAL_CHAIN_STATES = frozenset({"FILLED", "EXPIRED", "CANCELED"})


def _json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
              version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
              id INTEGER PRIMARY KEY,
              environment TEXT NOT NULL,
              as_of TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_runs (
              id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              cutoff TEXT NOT NULL,
              decision TEXT NOT NULL,
              report_dir TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
              id TEXT PRIMARY KEY,
              environment TEXT NOT NULL,
              symbol TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
              ticket_id TEXT PRIMARY KEY,
              actor TEXT NOT NULL,
              channel TEXT NOT NULL,
              approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
              ticket_id TEXT PRIMARY KEY,
              environment TEXT NOT NULL,
              client_order_id TEXT NOT NULL,
              status TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              payload TEXT NOT NULL,
              UNIQUE(environment, client_order_id)
            );
            CREATE TABLE IF NOT EXISTS order_events (
              id INTEGER PRIMARY KEY,
              ticket_id TEXT NOT NULL,
              status TEXT NOT NULL,
              event_time TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_runs (
              kind TEXT NOT NULL,
              bucket TEXT NOT NULL,
              completed_at TEXT NOT NULL,
              PRIMARY KEY(kind, bucket)
            );
            CREATE TABLE IF NOT EXISTS reflections (
              run_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              created_at TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            """
        )
        if self.db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
            self.db.execute("INSERT INTO schema_meta(version) VALUES (1)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def schema_version(self) -> int:
        return int(self.db.execute("SELECT version FROM schema_meta").fetchone()[0])

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self.db.execute(
            """
            INSERT INTO portfolio_snapshots(environment,as_of,payload)
            VALUES (?,?,?)
            """,
            (snapshot.environment, snapshot.as_of, _json(asdict(snapshot))),
        )
        self.db.commit()

    def latest_snapshot(self, environment: Environment) -> PortfolioSnapshot | None:
        row = self.db.execute(
            """
            SELECT payload FROM portfolio_snapshots
            WHERE environment=? ORDER BY id DESC LIMIT 1
            """,
            (environment,),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        return PortfolioSnapshot(
            environment=payload["environment"],
            nav_usdt=Decimal(payload["nav_usdt"]),
            free_usdt=Decimal(payload["free_usdt"]),
            positions=tuple(payload["positions"]),
            open_orders=tuple(payload["open_orders"]),
            as_of=payload["as_of"],
        )

    def save_run(
        self,
        run_id: str,
        cutoff: str,
        decision: ResearchDecision,
        report_dir: Path,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO research_runs(id,symbol,cutoff,decision,report_dir)
            VALUES (?,?,?,?,?)
            """,
            (run_id, decision.symbol, cutoff, _json(asdict(decision)), str(report_dir)),
        )
        self.db.commit()

    def latest_run(self, symbol: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT * FROM research_runs
            WHERE symbol=? ORDER BY cutoff DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "symbol": row["symbol"],
            "cutoff": row["cutoff"],
            "decision": json.loads(row["decision"]),
            "report_dir": row["report_dir"],
        }

    def save_ticket(self, ticket: TradeTicket) -> None:
        self.db.execute(
            """
            INSERT INTO tickets(
              id,environment,symbol,status,created_at,expires_at,payload
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                ticket.id,
                ticket.environment,
                ticket.symbol,
                ticket.status,
                ticket.created_at,
                ticket.expires_at,
                _json(asdict(ticket)),
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
        for name in (
            "quantity",
            "limit_price",
            "stop_price",
            "target_price",
            "notional_usdt",
        ):
            payload[name] = Decimal(payload[name])
        return TradeTicket(**payload)

    def approve_ticket(self, ticket_id: str, *, actor: str, channel: str) -> None:
        if not self.db.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone():
            raise ValueError(f"Unknown ticket: {ticket_id}")
        try:
            self.db.execute(
                """
                INSERT INTO approvals(ticket_id,actor,channel,approved_at)
                VALUES (?,?,?,?)
                """,
                (ticket_id, actor, channel, iso()),
            )
            self.db.execute("UPDATE tickets SET status='APPROVED' WHERE id=?", (ticket_id,))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Ticket {ticket_id} is already approved") from exc

    def approval(self, ticket_id: str) -> dict[str, str]:
        row = self.db.execute("SELECT * FROM approvals WHERE ticket_id=?", (ticket_id,)).fetchone()
        if not row:
            raise ValueError(f"Ticket {ticket_id} has no approval")
        return dict(row)

    def save_submission(
        self,
        ticket_id: str,
        environment: Environment,
        client_order_id: str,
        payload: dict[str, Any],
    ) -> None:
        status = str(payload.get("status", "SUBMITTED"))
        try:
            self.db.execute(
                """
                INSERT INTO submissions(
                  ticket_id,environment,client_order_id,status,updated_at,payload
                ) VALUES (?,?,?,?,?,?)
                """,
                (ticket_id, environment, client_order_id, status, iso(), _json(payload)),
            )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Ticket {ticket_id} or client order {client_order_id} is already submitted"
            ) from exc

    def record_order_event(self, ticket_id: str, status: str, payload: dict[str, Any]) -> None:
        if not self.db.execute(
            "SELECT 1 FROM submissions WHERE ticket_id=?", (ticket_id,)
        ).fetchone():
            raise ValueError(f"Ticket {ticket_id} has no submission")
        self.db.execute(
            """
            INSERT INTO order_events(ticket_id,status,event_time,payload)
            VALUES (?,?,?,?)
            """,
            (ticket_id, status, iso(), _json(payload)),
        )
        self.db.execute(
            """
            UPDATE submissions SET status=?,updated_at=?,payload=?
            WHERE ticket_id=?
            """,
            (status, iso(), _json(payload), ticket_id),
        )
        self.db.commit()

    def completed_mainnet_chains(self) -> int:
        placeholders = ",".join("?" for _ in TERMINAL_CHAIN_STATES)
        row = self.db.execute(
            f"""
            SELECT COUNT(*) FROM submissions
            WHERE environment='mainnet' AND status IN ({placeholders})
            """,
            tuple(sorted(TERMINAL_CHAIN_STATES)),
        ).fetchone()
        return int(row[0])

    def scheduled_done(self, kind: str, bucket: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM scheduled_runs WHERE kind=? AND bucket=?",
                (kind, bucket),
            ).fetchone()
            is not None
        )

    def mark_scheduled(self, kind: str, bucket: str) -> bool:
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO scheduled_runs(kind,bucket,completed_at)
            VALUES (?,?,?)
            """,
            (kind, bucket, iso()),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def save_reflection(self, run_id: str, symbol: str, payload: dict[str, Any]) -> bool:
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO reflections(run_id,symbol,created_at,payload)
            VALUES (?,?,?,?)
            """,
            (run_id, symbol, iso(), _json(payload)),
        )
        self.db.commit()
        return cursor.rowcount == 1
