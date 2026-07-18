"""Outbound command runner with a durable exactly-once journal."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from .commands import (
    EXECUTION_KINDS,
    STATE_CHANGING_KINDS,
    UnsafeResultError,
    command_hash,
    ensure_safe_result,
)
from .config import Settings
from .dashboard import publish_dashboard_if_configured
from .dispatcher import (
    CommandDispatcher,
    DispatchError,
    execution_mode,
)
from .store import Store

COMMAND_API_URL_ENV = "CRYPTO_DESK_COMMAND_API_URL"
RUNNER_TOKEN_ENV = "CRYPTO_DESK_RUNNER_TOKEN"
RUNNER_ENABLED_ENV = "CRYPTO_DESK_COMMAND_RUNNER_ENABLED"

HEARTBEAT_SECONDS = 5.0
IDLE_POLL_SECONDS = 2.0
LEASE_RENEW_SECONDS = 10.0


class RunnerProtocolError(RuntimeError):
    """Safe protocol failure returned by the command API."""


class CommandRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str,
        runner_token: str,
        sites_bypass_token: str,
        store: Store | None = None,
        dispatcher: CommandDispatcher | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        session_id: str | None = None,
        enabled: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda message: None,
    ):
        self.settings = settings
        self.store = (
            store
            if store is not None
            else Store(settings.database)
        )
        self.dispatcher = (
            dispatcher
            if dispatcher is not None
            else CommandDispatcher(settings)
        )
        headers = {
            "Authorization": f"Bearer {runner_token}",
            "OAI-Sites-Authorization": (
                f"Bearer {sites_bypass_token}"
            ),
        }
        self.client = client or httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=10,
            follow_redirects=False,
            transport=transport,
        )
        if client is not None:
            self.client.headers.update(headers)
        self.session_id = session_id or self._load_session_id()
        self.enabled = enabled
        self._sleep = sleep
        self._log = log
        self._active_command_id: str | None = None
        self._active_lock = threading.Lock()
        self._last_lease_renew = 0.0
        self._stop = threading.Event()

    def _load_session_id(self) -> str:
        path = self.settings.database.parent / "runner_session"
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        session_id = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")
        return session_id

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        response = self.client.post(path, json=payload)
        if response.status_code >= 500:
            raise RunnerProtocolError(
                "runner API failed "
                f"(status {response.status_code})"
            )
        return response

    def register(self) -> None:
        response = self._post(
            "/runner/session",
            {
                "session_id": self.session_id,
                "execution_mode": execution_mode(),
            },
        )
        if response.status_code == 409:
            raise RunnerProtocolError(
                "SESSION_CONFLICT: another runner session is active"
            )
        if response.status_code != 200:
            raise RunnerProtocolError(
                "session register rejected "
                f"(status {response.status_code})"
            )

    def heartbeat(self) -> None:
        response = self._post(
            "/runner/heartbeat",
            {
                "session_id": self.session_id,
                "execution_mode": execution_mode(),
            },
        )
        if response.status_code == 409:
            raise RunnerProtocolError(
                "SESSION_MISMATCH: singleton session changed"
            )
        if response.status_code != 200:
            raise RunnerProtocolError(
                "heartbeat rejected "
                f"(status {response.status_code})"
            )

    def claim(self) -> dict[str, Any] | None:
        response = self._post(
            "/runner/claim",
            {"session_id": self.session_id},
        )
        if response.status_code != 200:
            raise RunnerProtocolError(
                f"claim rejected (status {response.status_code})"
            )
        return response.json().get("command")

    def renew_lease(self, command_id: str) -> bool:
        response = self._post(
            "/runner/lease",
            {
                "session_id": self.session_id,
                "command_id": command_id,
            },
        )
        return response.status_code == 200

    def report(
        self,
        command_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool:
        response = self._post(
            "/runner/result",
            {
                "session_id": self.session_id,
                "command_id": command_id,
                "status": status,
                "result": result,
                "error_code": error_code,
            },
        )
        if response.status_code != 200:
            raise RunnerProtocolError(
                "result report rejected "
                f"(status {response.status_code})"
            )
        return True

    def recover(self) -> None:
        for entry in self.store.journal_running():
            if entry["kind"] in EXECUTION_KINDS:
                self.store.journal_finish(
                    entry["command_id"],
                    state="NEEDS_REVIEW",
                    error_code="EXECUTION_UNCERTAIN",
                )
            else:
                self.store.journal_finish(
                    entry["command_id"],
                    state="FAILED",
                    error_code="RUNNER_RESTART",
                )
        for entry in self.store.journal_unreported():
            self._report_entry(entry)

    def _report_entry(self, entry: dict[str, Any]) -> None:
        try:
            self.report(
                entry["command_id"],
                status=entry["state"],
                result=entry["result"],
                error_code=entry["error_code"],
            )
        except (httpx.HTTPError, RunnerProtocolError) as exc:
            self._log(
                "report failed "
                f"({type(exc).__name__}); will retry"
            )
            return
        self.store.journal_mark_reported(entry["command_id"])

    def process_one(self) -> bool:
        recovering = bool(
            self.store.journal_running()
            or self.store.journal_unreported()
        )
        self.recover()
        if recovering or self.store.journal_unreported():
            return False

        claimed = self.claim()
        if claimed is None:
            return False
        command_id = str(claimed["id"])
        kind = str(claimed["kind"])
        args = dict(claimed.get("args") or {})
        digest = command_hash(command_id, kind, args)
        entry = self.store.journal_entry(command_id)
        if entry is not None:
            if entry["command_hash"] != digest:
                try:
                    self.report(
                        command_id,
                        status="FAILED",
                        error_code="COMMAND_INTEGRITY",
                    )
                except (
                    httpx.HTTPError,
                    RunnerProtocolError,
                ) as exc:
                    self._log(
                        "integrity report failed "
                        f"({type(exc).__name__})"
                    )
                return True
            if entry["state"] != "RUNNING":
                self._report_entry(entry)
            return True

        self.store.journal_start(command_id, digest, kind)
        with self._active_lock:
            self._active_command_id = command_id
        try:
            model = self.dispatcher.dispatch(
                kind,
                args,
                operator_email=str(
                    claimed.get("operator_email", "")
                ),
                environment=str(
                    claimed.get("environment", "testnet")
                ),
            )
            self.store.journal_finish(
                command_id,
                state="SUCCEEDED",
                result=ensure_safe_result(model),
            )
        except DispatchError as exc:
            self.store.journal_finish(
                command_id,
                state="FAILED",
                error_code=exc.code,
            )
        except UnsafeResultError:
            self.store.journal_finish(
                command_id,
                state="FAILED",
                error_code="RESULT_UNSAFE",
            )
        except Exception as exc:  # noqa: BLE001
            if kind in EXECUTION_KINDS:
                self.store.journal_finish(
                    command_id,
                    state="NEEDS_REVIEW",
                    error_code="EXECUTION_UNCERTAIN",
                )
            else:
                self.store.journal_finish(
                    command_id,
                    state="FAILED",
                    error_code="DISPATCH_FAILED",
                )
            self._log(
                f"dispatch {kind} failed ({type(exc).__name__})"
            )
        finally:
            with self._active_lock:
                self._active_command_id = None

        entry = self.store.journal_entry(command_id)
        if entry is None:
            raise RunnerProtocolError(
                "journal entry disappeared after dispatch"
            )
        self._report_entry(entry)
        if (
            entry["state"] == "SUCCEEDED"
            and kind in STATE_CHANGING_KINDS
        ):
            warning = publish_dashboard_if_configured(
                self.settings
            )
            if warning:
                self._log(warning)
        return True

    def run_forever(self) -> None:
        self.register()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            while not self._stop.is_set():
                if not self.enabled:
                    self.recover()
                    self._sleep(IDLE_POLL_SECONDS)
                    continue
                worked = False
                try:
                    worked = self.process_one()
                except (
                    httpx.HTTPError,
                    RunnerProtocolError,
                ) as exc:
                    self._log(
                        f"poll failed ({type(exc).__name__})"
                    )
                if not worked:
                    self._sleep(IDLE_POLL_SECONDS)
        finally:
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                self.heartbeat()
                with self._active_lock:
                    active = self._active_command_id
                elapsed = (
                    time.monotonic() - self._last_lease_renew
                )
                if (
                    active
                    and elapsed >= LEASE_RENEW_SECONDS
                    and self.renew_lease(active)
                ):
                    self._last_lease_renew = time.monotonic()
            except (httpx.HTTPError, RunnerProtocolError):
                continue
