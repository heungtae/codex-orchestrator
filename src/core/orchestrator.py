from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.command_router import CommandRouter
from core.models import BotSession, RouteResult
from core.session_manager import SessionManager
from core.trace_logger import TraceLogger
from integrations.codex_executor import CodexExecutionError
from integrations.codex_mcp import CodexMcpServer, CodexMcpStatusError
from workflows.types import Workflow


@dataclass
class BotOrchestrator:
    router: CommandRouter
    session_manager: SessionManager
    trace_logger: TraceLogger
    single_workflow: Workflow
    multi_workflow: Workflow
    codex_mcp: CodexMcpServer
    working_directory: str | None = None

    async def handle_message(self, chat_id: str | int, user_id: str | int, text: str | None) -> str:
        run_id = str(uuid.uuid4())
        started = time.monotonic()
        route = self.router.route(text)

        trace_mode = "single"
        review_round = None
        review_result = None
        output_text = ""
        error_message = None

        try:
            if route.kind == "bot_command":
                output_text, trace_mode = await self._handle_bot_command(
                    chat_id=chat_id,
                    user_id=user_id,
                    route=route,
                )
            elif route.kind == "codex_slash" and not route.text:
                output_text = "usage: /codex /..."
            else:
                (
                    output_text,
                    trace_mode,
                    review_round,
                    review_result,
                ) = await self._handle_workflow_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    route=route,
                    started=started,
                )
        except CodexExecutionError as exc:
            detail = str(exc).strip() or "unknown error"
            if len(detail) > 280:
                detail = detail[:280] + "..."
            output_text = (
                "Codex runtime configuration error. "
                "Check MCP client installation and CODEX_MCP_COMMAND/CODEX_MCP_ARGS settings.\n"
                f"detail: {detail}"
            )
            error_message = str(exc)
            await self._mark_error(chat_id=chat_id, user_id=user_id, error_message=error_message)
        except Exception as exc:
            output_text = "An error occurred while processing the request. Please try again later."
            error_message = str(exc)
            await self._mark_error(chat_id=chat_id, user_id=user_id, error_message=error_message)

        latency_ms = int((time.monotonic() - started) * 1000)
        self._safe_trace(
            {
                "run_id": run_id,
                "session_id": SessionManager.session_id(chat_id=chat_id, user_id=user_id),
                "mode": trace_mode,
                "review_round": review_round,
                "review_result": review_result,
                "input_kind": route.kind,
                "input_text": route.text,
                "output_text": output_text,
                "status": "error" if error_message else "ok",
                "latency_ms": latency_ms,
                "error_message": error_message,
            }
        )

        return output_text

    async def _handle_bot_command(
        self,
        *,
        chat_id: str | int,
        user_id: str | int,
        route: RouteResult,
    ) -> tuple[str, str]:
        if route.command == "start":
            return (
                self._help_text(
                    working_directory=self._resolve_working_directory(self.working_directory)
                ),
                "single",
            )

        if route.command == "mode":
            mode = route.args[0] if route.args else ""
            if mode not in ("single", "multi"):
                return "usage: /mode single|multi", "single"

            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                session.mode = mode
                session.last_error = None
                await self.session_manager.save(session)
            return f"mode set to {mode}", mode

        if route.command == "new":
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.reset(chat_id=chat_id, user_id=user_id)
            return "session reset. mode=single", session.mode

        if route.command == "status":
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
            mcp_status = self._safe_mcp_status()
            return self._format_status(session=session, mcp_status=mcp_status), session.mode

        return "unsupported command", "single"

    async def _handle_workflow_message(
        self,
        *,
        chat_id: str | int,
        user_id: str | int,
        route: RouteResult,
        started: float,
    ) -> tuple[str, str, int | None, str | None]:
        async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
            session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)

            if session.run_lock:
                return (
                    "A task is already running for this session. Please try again shortly.",
                    session.mode,
                    session.last_review_round,
                    session.last_review_result,
                )

            session.run_lock = True
            await self.session_manager.save(session)

            try:
                workflow = (
                    self.single_workflow if session.mode == "single" else self.multi_workflow
                )
                result = await workflow.run(input_text=route.text, session=session)
                session.history = result.get("next_history", session.history)
                session.last_run_status = "ok"
                session.last_run_latency_ms = int((time.monotonic() - started) * 1000)
                session.last_error = None
                if "review_round" in result:
                    session.last_review_round = int(result["review_round"])
                if "review_result" in result:
                    session.last_review_result = result["review_result"]

                return (
                    result.get("output_text", ""),
                    session.mode,
                    session.last_review_round,
                    session.last_review_result,
                )
            except Exception as exc:
                session.last_run_status = "error"
                session.last_error = str(exc)
                raise
            finally:
                session.run_lock = False
                await self.session_manager.save(session)

    async def _mark_error(
        self,
        *,
        chat_id: str | int,
        user_id: str | int,
        error_message: str,
    ) -> None:
        async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
            session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
            session.last_run_status = "error"
            session.last_error = error_message
            session.run_lock = False
            await self.session_manager.save(session)

    def _safe_trace(self, payload: dict[str, Any]) -> None:
        try:
            self.trace_logger.append(payload)
        except Exception:
            pass

    def _safe_mcp_status(self) -> dict[str, Any] | None:
        try:
            return self.codex_mcp.get_status()
        except CodexMcpStatusError as exc:
            self.codex_mcp.record_error(str(exc))
            return None

    @staticmethod
    def _help_text(*, working_directory: str) -> str:
        return "\n".join(
            [
                "available commands:",
                "/start",
                "/mode single|multi",
                "/new",
                "/status",
                "/codex /...",
                "plain text is forwarded to Codex workflow",
                f"session_working_directory: {working_directory}",
            ]
        )

    @staticmethod
    def _resolve_working_directory(raw_path: str | None) -> str:
        if isinstance(raw_path, str):
            candidate = raw_path.strip()
            if candidate:
                return str(Path(candidate).resolve())
        return str(Path.cwd().resolve())

    @staticmethod
    def _format_status(session: BotSession, mcp_status: dict[str, Any] | None) -> str:
        def _to_display_bool(value: Any) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        lines = [
            f"mode: {session.mode}",
            (
                f"last_run: {session.last_run_status} "
                f"({session.last_run_latency_ms}ms)"
                if session.last_run_latency_ms is not None
                else f"last_run: {session.last_run_status}"
            ),
        ]

        if session.mode == "single":
            result = session.last_review_result or "-"
            lines.append(f"single_review: rounds={session.last_review_round}/3, result={result}")

        if not mcp_status:
            lines.append("codex_mcp: unknown")
        else:
            running = mcp_status.get("running")
            ready = mcp_status.get("ready")
            pid = mcp_status.get("pid")
            uptime = mcp_status.get("uptime_sec")
            uptime_display = "-" if uptime is None else f"{uptime}s"
            lines.append(
                "codex_mcp: "
                f"running={_to_display_bool(running)}, "
                f"ready={_to_display_bool(ready)}, pid={pid}, uptime={uptime_display}"
            )

        lines.append(f"last_error: {session.last_error or '-'}")
        return "\n".join(lines)
