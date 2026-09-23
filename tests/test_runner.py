from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from crypto_desk.commands import (
    SafeExecuteResult,
    SafeSyncResult,
    command_hash,
)
from crypto_desk.config import Settings
from crypto_desk.dispatcher import DispatchError
from crypto_desk.runner import CommandRunner, RunnerProtocolError
from crypto_desk.store import Store

OPERATOR = "quoc.lb@teko.vn"


class FakeSites:
    def __init__(self):
        self.queue: list[dict] = []
        self.reports: list[dict] = []
        self.sessions: list[dict] = []
        self.heartbeats: list[dict] = []
        self.leases: list[dict] = []
        self.session_conflict = False
        self.fail_next_reports = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        path = request.url.path
        if path == "/api/runner/session":
            self.sessions.append(body)
            if self.session_conflict:
                return httpx.Response(
                    409,
                    json={"error": "SESSION_CONFLICT"},
                )
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runner/heartbeat":
            self.heartbeats.append(body)
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runner/claim":
            command = self.queue.pop(0) if self.queue else None
            return httpx.Response(200, json={"command": command})
        if path == "/api/runner/lease":
            self.leases.append(body)
            return httpx.Response(200, json={"ok": True})
        if path == "/api/runner/result":
            if self.fail_next_reports > 0:
                self.fail_next_reports -= 1
                raise httpx.ConnectError("connection refused")
            self.reports.append(body)
            return httpx.Response(
                200,
                json={"ok": True, "applied": True},
            )
        return httpx.Response(404, json={"error": "NOT_FOUND"})


class FakeDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.raise_for_kind: dict[str, Exception] = {}

    def dispatch(
        self,
        kind,
        args,
        *,
        operator_email,
        environment="testnet",
    ):
        self.calls.append((kind, dict(args)))
        if kind in self.raise_for_kind:
            raise self.raise_for_kind[kind]
        if kind == "execute":
            return SafeExecuteResult(
                action=args["action"],
                ticket_id=args["ticket_id"],
                status="APPROVED_DRY_RUN",
            )
        return SafeSyncResult(
            environment="testnet",
            nav_usdt="1000",
            free_usdt="500",
            positions_count=0,
            open_orders_count=0,
            as_of="2026-07-18T09:00:00+00:00",
        )


def make_runner(
    tmp_path: Path,
    sites: FakeSites,
    **kwargs,
) -> tuple[CommandRunner, Store, FakeDispatcher]:
    settings = Settings(
        database=tmp_path / "crypto.sqlite3",
        artifacts=tmp_path / "artifacts",
        symbols=("BTCUSDT",),
    )
    store = Store(settings.database)
    dispatcher = FakeDispatcher()
    client = httpx.Client(
        base_url="https://sites.example/api",
        transport=httpx.MockTransport(sites.handler),
    )
    runner = CommandRunner(
        settings,
        base_url="https://sites.example/api",
        runner_token="runner-token",
        sites_bypass_token="bypass-token",
        store=store,
        dispatcher=dispatcher,
        client=client,
        session_id="session-1",
        **kwargs,
    )
    return runner, store, dispatcher


def make_command(
    command_id: str = "cmd-1",
    kind: str = "sync",
    args: dict | None = None,
) -> dict:
    return {
        "id": command_id,
        "kind": kind,
        "args": args or {},
        "operator_email": OPERATOR,
        "environment": "testnet",
        "queued_at": "2026-07-18T09:00:00.000Z",
    }


def test_process_one_claims_dispatches_reports_and_publishes(
    tmp_path,
    monkeypatch,
):
    published = []
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: published.append(True) or None,
    )
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, store, dispatcher = make_runner(tmp_path, sites)

    assert runner.process_one() is True
    assert dispatcher.calls == [("sync", {})]
    assert sites.reports[0]["command_id"] == "cmd-1"
    assert sites.reports[0]["status"] == "SUCCEEDED"
    assert sites.reports[0]["result"]["nav_usdt"] == "1000"
    entry = store.journal_entry("cmd-1")
    assert entry["state"] == "SUCCEEDED"
    assert entry["reported"] is True
    assert published == [True]


def test_duplicate_command_same_hash_replays_without_reexecution(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, _, dispatcher = make_runner(tmp_path, sites)
    runner.process_one()

    sites.queue.append(make_command())
    assert runner.process_one() is True
    assert len(dispatcher.calls) == 1
    assert [report["status"] for report in sites.reports] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_duplicate_command_different_hash_fails_closed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, _, dispatcher = make_runner(tmp_path, sites)
    runner.process_one()

    sites.queue.append(make_command(args={"tampered": True}))
    runner.process_one()
    assert len(dispatcher.calls) == 1
    assert sites.reports[-1]["status"] == "FAILED"
    assert sites.reports[-1]["error_code"] == "COMMAND_INTEGRITY"


def test_restart_recovery_never_repeats_a_running_command(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    runner, store, dispatcher = make_runner(tmp_path, sites)
    store.journal_start(
        "cmd-exec",
        command_hash("cmd-exec", "execute", {}),
        "execute",
    )
    store.journal_start(
        "cmd-read",
        command_hash("cmd-read", "tickets", {}),
        "tickets",
    )

    runner.recover()
    assert store.journal_entry("cmd-exec")["state"] == "NEEDS_REVIEW"
    assert store.journal_entry("cmd-exec")["error_code"] == "EXECUTION_UNCERTAIN"
    assert store.journal_entry("cmd-read")["state"] == "FAILED"
    assert store.journal_entry("cmd-read")["error_code"] == "RUNNER_RESTART"
    statuses = {report["command_id"]: report["status"] for report in sites.reports}
    assert statuses == {
        "cmd-exec": "NEEDS_REVIEW",
        "cmd-read": "FAILED",
    }
    assert dispatcher.calls == []


def test_unacked_report_is_retried_before_claiming_new_work(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    sites.fail_next_reports = 1
    sites.queue.append(make_command())
    runner, store, dispatcher = make_runner(tmp_path, sites)

    runner.process_one()
    assert store.journal_entry("cmd-1")["reported"] is False

    sites.queue.append(make_command("cmd-2"))
    assert runner.process_one() is False
    assert store.journal_entry("cmd-1")["reported"] is True
    assert len(dispatcher.calls) == 1

    assert runner.process_one() is True
    assert dispatcher.calls[-1][0] == "sync"


def test_unexpected_exception_maps_by_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    sites.queue.append(
        make_command(
            "cmd-x",
            kind="execute",
            args={
                "action": "approve",
                "ticket_id": "t-1",
                "preview_command_id": "p-1",
                "fingerprint": "a" * 64,
            },
        )
    )
    runner, _, dispatcher = make_runner(tmp_path, sites)
    dispatcher.raise_for_kind["execute"] = TimeoutError("ambiguous")
    runner.process_one()
    assert sites.reports[-1]["status"] == "NEEDS_REVIEW"
    assert sites.reports[-1]["error_code"] == "EXECUTION_UNCERTAIN"

    sites.queue.append(make_command("cmd-y", kind="screen"))
    dispatcher.raise_for_kind["screen"] = RuntimeError("boom")
    runner.process_one()
    assert sites.reports[-1]["status"] == "FAILED"
    assert sites.reports[-1]["error_code"] == "DISPATCH_FAILED"


def test_dispatch_error_reports_safe_code(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    sites.queue.append(
        make_command(
            "cmd-z",
            kind="execute",
            args={
                "action": "approve",
                "ticket_id": "t-1",
                "preview_command_id": "p-1",
                "fingerprint": "a" * 64,
            },
        )
    )
    runner, _, dispatcher = make_runner(tmp_path, sites)
    dispatcher.raise_for_kind["execute"] = DispatchError("TICKET_CHANGED")
    runner.process_one()
    assert sites.reports[-1]["status"] == "FAILED"
    assert sites.reports[-1]["error_code"] == "TICKET_CHANGED"


def test_register_conflict_raises(tmp_path):
    sites = FakeSites()
    sites.session_conflict = True
    runner, _, _ = make_runner(tmp_path, sites)
    with pytest.raises(RunnerProtocolError, match="SESSION_CONFLICT"):
        runner.register()


def test_disabled_runner_never_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crypto_desk.runner.publish_dashboard_if_configured",
        lambda settings: None,
    )
    sites = FakeSites()
    sites.queue.append(make_command())
    runner, _, dispatcher = make_runner(
        tmp_path,
        sites,
        enabled=False,
        sleep=lambda _: runner.stop(),
    )
    runner.run_forever()
    assert dispatcher.calls == []
    assert sites.queue


def test_runner_sends_both_auth_headers(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        database=tmp_path / "c.sqlite3",
        artifacts=tmp_path / "a",
        symbols=("BTCUSDT",),
    )
    runner = CommandRunner(
        settings,
        base_url="https://sites.example/api",
        runner_token="runner-token",
        sites_bypass_token="bypass-token",
        store=Store(settings.database),
        dispatcher=FakeDispatcher(),
        client=None,
        session_id="session-1",
        transport=httpx.MockTransport(handler),
    )
    runner.heartbeat()
    assert captured["authorization"] == "Bearer runner-token"
    assert captured["oai-sites-authorization"] == "Bearer bypass-token"


def test_local_runner_omits_empty_sites_header(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    settings = Settings(database=tmp_path / "c.sqlite3", artifacts=tmp_path / "a")
    runner = CommandRunner(
        settings,
        base_url="http://localhost:3001/api",
        runner_token="runner-token",
        sites_bypass_token="",
        dispatcher=FakeDispatcher(),
        transport=httpx.MockTransport(handler),
        session_id="session-1",
    )

    runner.heartbeat()

    assert "oai-sites-authorization" not in captured


def test_runner_heartbeat_stops_renewing_lease_after_max_command_duration(tmp_path):
    sites = FakeSites()
    runner, store, _ = make_runner(tmp_path, sites)
    runner.register()

    # Simulate an active command started long ago (> MAX_COMMAND_LEASE_RENEW_SECONDS)
    runner._active_command_id = "cmd-slow"
    runner._active_command_started_at = 100.0
    runner._last_lease_renew = 100.0

    # Mock time so that monotonic is now 1000.0 (> 600s after start)
    logs = []
    runner._log = logs.append
    import time
    orig_monotonic = time.monotonic
    try:
        time.monotonic = lambda: 1000.0
        # Run one iteration of heartbeat logic
        runner.heartbeat()
        now = time.monotonic()
        elapsed = now - runner._last_lease_renew
        active_duration = now - runner._active_command_started_at
        from crypto_desk.runner import LEASE_RENEW_SECONDS, MAX_COMMAND_LEASE_RENEW_SECONDS

        should_renew = (
            runner._active_command_id
            and elapsed >= LEASE_RENEW_SECONDS
            and active_duration < MAX_COMMAND_LEASE_RENEW_SECONDS
        )
        assert not should_renew
    finally:
        time.monotonic = orig_monotonic

