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
              decision TEXT NOT NULL DEFAULT 'APPROVE',
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
            CREATE TABLE IF NOT EXISTS command_journal (
              command_id TEXT PRIMARY KEY,
              command_hash TEXT NOT NULL,
              kind TEXT NOT NULL,
              state TEXT NOT NULL,
              reported INTEGER NOT NULL DEFAULT 0,
              result TEXT,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        if self.db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
            self.db.execute("INSERT INTO schema_meta(version) VALUES (1)")
        version = int(self.db.execute("SELECT version FROM schema_meta").fetchone()[0])
        if version < 2:
            approval_columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(approvals)").fetchall()
            }
            if "decision" not in approval_columns:
                self.db.execute(
                    """
                    ALTER TABLE approvals
                    ADD COLUMN decision TEXT NOT NULL DEFAULT 'APPROVE'
                    """
                )
            self.db.execute("UPDATE schema_meta SET version=2")
            version = 2
        if version < 3:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS command_journal (
                  command_id TEXT PRIMARY KEY,
                  command_hash TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  state TEXT NOT NULL,
                  reported INTEGER NOT NULL DEFAULT 0,
                  result TEXT,
                  error_code TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            self.db.execute("UPDATE schema_meta SET version=3")
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

    def latest_valid_run(self, symbol: str) -> dict[str, Any] | None:
        rows = self.db.execute(
            """
            SELECT * FROM research_runs
            WHERE symbol=? ORDER BY cutoff DESC LIMIT 100
            """,
            (symbol,),
        ).fetchall()
        for row in rows:
            decision = json.loads(row["decision"])
            if decision.get("evidence_ids"):
                return {
                    "id": row["id"],
                    "symbol": row["symbol"],
                    "cutoff": row["cutoff"],
                    "decision": decision,
                    "report_dir": row["report_dir"],
                }
        return None

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
        self.record_approval(
            ticket_id,
            actor=actor,
            channel=channel,
            status="APPROVED",
        )

    def record_approval(
        self,
        ticket_id: str,
        *,
        actor: str,
        channel: str,
        status: str,
        decision: str | None = None,
    ) -> None:
        if not self.db.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone():
            raise ValueError(f"Unknown ticket: {ticket_id}")
        try:
            self.db.execute(
                """
                INSERT INTO approvals(
                  ticket_id,actor,channel,decision,approved_at
                ) VALUES (?,?,?,?,?)
                """,
                (
                    ticket_id,
                    actor,
                    channel,
                    decision or ("REJECT" if status == "REJECTED" else "APPROVE"),
                    iso(),
                ),
            )
            self.db.execute(
                "UPDATE tickets SET status=? WHERE id=?",
                (status, ticket_id),
            )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Ticket {ticket_id} already has an approval decision") from exc

    def set_ticket_status(self, ticket_id: str, status: str) -> None:
        cursor = self.db.execute(
            "UPDATE tickets SET status=? WHERE id=?",
            (status, ticket_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown ticket: {ticket_id}")
        self.db.commit()

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

    def submission(self, ticket_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM submissions WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "ticket_id": row["ticket_id"],
            "environment": row["environment"],
            "client_order_id": row["client_order_id"],
            "status": row["status"],
            "updated_at": row["updated_at"],
            "payload": json.loads(row["payload"]),
        }

    def submission_by_client_order_id(
        self,
        environment: Environment,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT ticket_id FROM submissions
            WHERE environment=? AND client_order_id=?
            """,
            (environment, client_order_id),
        ).fetchone()
        return self.submission(row["ticket_id"]) if row else None

    def list_tickets(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT id,environment,symbol,status,created_at,expires_at
            FROM tickets ORDER BY created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_submissions(
        self,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self.db.execute(
                f"""
                SELECT ticket_id FROM submissions
                WHERE status IN ({placeholders})
                ORDER BY updated_at
                """,
                statuses,
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT ticket_id FROM submissions ORDER BY updated_at DESC"
            ).fetchall()
        return [
            submission
            for row in rows
            if (submission := self.submission(row["ticket_id"])) is not None
        ]

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
        self.db.execute(
            "UPDATE tickets SET status=? WHERE id=?",
            (status, ticket_id),
        )
        self.db.commit()

    def recent_order_events(self, limit: int, environment: Environment) -> list[dict[str, str]]:
        rows = self.db.execute(
            """
            SELECT oe.ticket_id,oe.status,oe.event_time
            FROM order_events oe
            JOIN submissions s ON s.ticket_id = oe.ticket_id
            WHERE s.environment = ?
            ORDER BY oe.event_time DESC, oe.id DESC LIMIT ?
            """,
            (environment, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def completed_mainnet_chains(self) -> int:
        placeholders = ",".join("?" for _ in TERMINAL_CHAIN_STATES)
        row = self.db.execute(
            f"""
            SELECT COUNT(*) FROM submissions
            WHERE environment='mainnet'
              AND status IN ({placeholders})
              AND COALESCE(json_extract(payload, '$._reconciled'), 1) = 1
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

    def latest_scheduled_run(self, kind: str) -> dict[str, str] | None:
        row = self.db.execute(
            """
            SELECT kind,bucket,completed_at FROM scheduled_runs
            WHERE kind=? ORDER BY completed_at DESC LIMIT 1
            """,
            (kind,),
        ).fetchone()
        return dict(row) if row else None

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

    def list_reflections(
        self,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        if symbol:
            rows = self.db.execute(
                """
                SELECT * FROM reflections
                WHERE symbol=? ORDER BY created_at DESC
                """,
                (symbol,),
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM reflections ORDER BY created_at DESC").fetchall()
        return [
            {
                "run_id": row["run_id"],
                "symbol": row["symbol"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def journal_start(
        self,
        command_id: str,
        command_hash: str,
        kind: str,
    ) -> None:
        now = iso()
        try:
            self.db.execute(
                "INSERT INTO command_journal("
                "command_id, command_hash, kind, state, reported,"
                " created_at, updated_at"
                ") VALUES (?, ?, ?, 'RUNNING', 0, ?, ?)",
                (command_id, command_hash, kind, now, now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"Command {command_id} is already journaled"
            ) from None
        self.db.commit()

    def journal_entry(
        self,
        command_id: str,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT command_id, command_hash, kind, state,"
            " reported, result, error_code, created_at, updated_at"
            " FROM command_journal WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        return self._journal_row(row) if row else None

    def journal_finish(
        self,
        command_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        cursor = self.db.execute(
            "UPDATE command_journal SET state = ?, result = ?,"
            " error_code = ?, updated_at = ? WHERE command_id = ?",
            (
                state,
                _json(result) if result is not None else None,
                error_code,
                iso(),
                command_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"Command {command_id} has no journal entry"
            )
        self.db.commit()

    def journal_mark_reported(self, command_id: str) -> None:
        cursor = self.db.execute(
            "UPDATE command_journal SET reported = 1,"
            " updated_at = ? WHERE command_id = ?",
            (iso(), command_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"Command {command_id} has no journal entry"
            )
        self.db.commit()

    def journal_running(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT command_id, command_hash, kind, state,"
            " reported, result, error_code, created_at, updated_at"
            " FROM command_journal WHERE state = 'RUNNING'"
            " ORDER BY created_at",
        ).fetchall()
        return [self._journal_row(row) for row in rows]

    def journal_unreported(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT command_id, command_hash, kind, state,"
            " reported, result, error_code, created_at, updated_at"
            " FROM command_journal WHERE state != 'RUNNING'"
            " AND reported = 0 ORDER BY created_at",
        ).fetchall()
        return [self._journal_row(row) for row in rows]

    def _journal_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "command_id": row["command_id"],
            "command_hash": row["command_hash"],
            "kind": row["kind"],
            "state": row["state"],
            "reported": bool(row["reported"]),
            "result": (
                json.loads(row["result"])
                if row["result"]
                else None
            ),
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
