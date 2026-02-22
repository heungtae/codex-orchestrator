from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Sequence


class CodexMcpStatusError(RuntimeError):
    pass


@dataclass
class CodexMcpServer:
    status_command: Sequence[str] | None = None
    status_timeout_sec: float = 2.0
    process_detect_timeout_sec: float = 2.0
    process_match_query: str = "codex mcp-server"
    auto_detect_process: bool = True
    running: bool = False
    ready: bool = False
    pid: int | None = None
    started_at_monotonic: float | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def mark_running(self, pid: int | None = None, ready: bool = False) -> None:
        self.running = True
        self.ready = ready
        self.pid = pid
        self.started_at_monotonic = time.monotonic()
        self.last_error = None

    def mark_ready(self) -> None:
        self.ready = True

    def mark_stopped(self) -> None:
        self.running = False
        self.ready = False
        self.pid = None
        self.started_at_monotonic = None

    def record_error(self, message: str) -> None:
        self.last_error = message

    def _uptime_sec(self) -> int | None:
        if self.started_at_monotonic is None:
            return None
        return max(0, int(time.monotonic() - self.started_at_monotonic))

    @staticmethod
    def _parse_kv_status(output: str) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        pairs = [part.strip() for part in output.split(",") if part.strip()]
        for part in pairs:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
        return parsed

    @staticmethod
    def _coerce_status(payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        if "running" in payload:
            value = payload["running"]
            if isinstance(value, str):
                result["running"] = value.lower() == "true"
            else:
                result["running"] = bool(value)

        if "ready" in payload:
            value = payload["ready"]
            if isinstance(value, str):
                result["ready"] = value.lower() == "true"
            else:
                result["ready"] = bool(value)

        if "pid" in payload and payload["pid"] not in (None, ""):
            try:
                result["pid"] = int(payload["pid"])
            except (TypeError, ValueError):
                pass

        if "uptime_sec" in payload and payload["uptime_sec"] not in (None, ""):
            try:
                result["uptime_sec"] = int(payload["uptime_sec"])
            except (TypeError, ValueError):
                pass

        if payload.get("last_error"):
            result["last_error"] = str(payload["last_error"])

        return result

    def _query_external_status(self) -> dict[str, Any]:
        if not self.status_command:
            return {}

        try:
            completed = subprocess.run(
                list(self.status_command),
                capture_output=True,
                text=True,
                timeout=self.status_timeout_sec,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise CodexMcpStatusError(f"failed to run mcp status command: {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise CodexMcpStatusError(
                f"mcp status command failed (exit={completed.returncode}): {stderr}"
            )

        output = completed.stdout.strip()
        if not output:
            return {}

        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                return self._coerce_status(parsed)
        except json.JSONDecodeError:
            pass

        return self._coerce_status(self._parse_kv_status(output))

    def _query_process_status(self) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,etimes=,args="],
                capture_output=True,
                text=True,
                timeout=self.process_detect_timeout_sec,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise CodexMcpStatusError(f"failed to inspect process list: {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise CodexMcpStatusError(
                f"process inspection failed (exit={completed.returncode}): {stderr}"
            )

        rows = completed.stdout.splitlines()
        match_query = self.process_match_query.lower()
        own_pid = os.getpid()

        for row in rows:
            line = row.strip()
            if not line:
                continue

            parts = line.split(None, 2)
            if len(parts) < 3:
                continue

            pid_raw, etimes_raw, command = parts
            try:
                pid = int(pid_raw)
            except ValueError:
                continue

            if pid == own_pid:
                continue

            lowered_command = command.lower()
            if match_query not in lowered_command:
                continue

            # Ignore detector subprocesses such as:
            # bash -c ps ... codex mcp-server ...
            if "ps -eo" in lowered_command:
                continue
            if "telegram_polling_runner.py" in lowered_command:
                continue

            uptime_sec: int | None = None
            try:
                uptime_sec = int(etimes_raw)
            except ValueError:
                uptime_sec = None

            return {
                "running": True,
                "ready": True,
                "pid": pid,
                "uptime_sec": uptime_sec,
            }

        return {}

    def get_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "running": self.running,
            "ready": self.ready,
            "pid": self.pid,
            "uptime_sec": self._uptime_sec(),
            "last_error": self.last_error,
        }

        if self.status_command:
            external = self._query_external_status()
        elif self.auto_detect_process:
            external = self._query_process_status()
        else:
            external = {}
        status.update(external)
        return status
