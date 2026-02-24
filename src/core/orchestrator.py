from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.command_router import CommandRouter
from core.models import BotMode, BotSession, RouteResult
from core.profiles import ExecutionProfile, ProfileRegistry
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
    plan_workflow: Workflow
    codex_mcp: CodexMcpServer
    working_directory: str | None = None
    profile_registry: ProfileRegistry = field(default_factory=ProfileRegistry.build_default)
    _running_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict, init=False, repr=False)
    _running_tasks_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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
                "Check MCP client installation and conf.toml [codex].mcp_command/[codex].mcp_args settings.\n"
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

    async def preview_workflow_mode(
        self,
        chat_id: str | int,
        user_id: str | int,
        text: str | None,
    ) -> BotMode | None:
        route = self.router.route(text)
        if route.kind == "bot_command":
            return None

        async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
            session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
            if self._ensure_session_profile(session):
                await self.session_manager.save(session)
            return session.mode

    async def _handle_bot_command(
        self,
        *,
        chat_id: str | int,
        user_id: str | int,
        route: RouteResult,
    ) -> tuple[str, str]:
        if route.command == "start":
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                current_mode = session.mode
            return (
                self._help_text(
                    mode=current_mode,
                    working_directory=self._resolve_working_directory(self.working_directory)
                ),
                "single",
            )

        if route.command == "mode":
            mode = route.args[0] if route.args else ""
            if mode not in ("single", "plan"):
                return "[Error]: usage=/mode single|plan", "plan"

            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                session.mode = mode
                session.last_error = None
                await self.session_manager.save(session)
            return f"[Mode]: {mode}", mode

        if route.command == "new":
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.reset(chat_id=chat_id, user_id=user_id)
                self._apply_profile_to_session(session, self.profile_registry.default_profile())
                await self.session_manager.save(session)
            return f"[Session]: reset, mode={session.mode}", session.mode

        if route.command == "status":
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                if self._ensure_session_profile(session):
                    await self.session_manager.save(session)
            mcp_status = self._safe_mcp_status()
            return self._format_status(session=session, mcp_status=mcp_status), session.mode

        if route.command == "profile":
            profile_arg = route.args[0].strip() if route.args else ""
            if not profile_arg:
                async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                    session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                    if self._ensure_session_profile(session):
                        await self.session_manager.save(session)
                return "[Error]: usage=/profile list|<name>", session.mode

            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                changed = self._ensure_session_profile(session)

                if profile_arg.lower() == "list":
                    if changed:
                        await self.session_manager.save(session)
                    return self._format_profile_list(session), session.mode

                profile = self.profile_registry.get(profile_arg)
                if profile is None:
                    if changed:
                        await self.session_manager.save(session)
                    return f"[Error]: profile not found: {profile_arg}", session.mode

                self._apply_profile_to_session(session, profile)
                session.last_error = None
                await self.session_manager.save(session)
                model_text = session.profile_model or "-"
                working_directory_text = session.profile_working_directory or "-"
                return (
                    (
                        f"[Profile]: {session.profile_name}\n"
                        f"model={model_text}\n"
                        f"working_directory={working_directory_text}"
                    ),
                    session.mode,
                )

        if route.command == "cancel":
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                session_id = session.session_id
                if self._ensure_session_profile(session):
                    await self.session_manager.save(session)
                mode = session.mode
                run_lock = session.run_lock

            running_task = await self._get_running_task(session_id)
            if running_task is None or running_task.done():
                if run_lock:
                    async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                        stale_session = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                        if stale_session.run_lock:
                            stale_session.run_lock = False
                            await self.session_manager.save(stale_session)
                            mode = stale_session.mode
                return "[Cancel]: no running task", mode

            running_task.cancel()
            return "[Cancel]: requested", mode

        return "[Error]: unsupported command", "single"

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
            profile_changed = self._ensure_session_profile(session)

            if session.run_lock:
                if profile_changed:
                    await self.session_manager.save(session)
                return (
                    "A task is already running for this session. Please try again shortly.",
                    session.mode,
                    session.last_review_round,
                    session.last_review_result,
                )

            session.run_lock = True
            await self.session_manager.save(session)
            if session.mode == "single":
                workflow = self.single_workflow
            else:
                workflow = self.plan_workflow
            mode = session.mode
            session_id = session.session_id

        current_task = asyncio.current_task()
        if current_task is not None:
            await self._set_running_task(session_id=session_id, task=current_task)

        try:
            result = await workflow.run(input_text=route.text, session=session)
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                latest = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                latest.history = result.get("next_history", latest.history)
                latest.last_run_status = "ok"
                latest.last_run_latency_ms = int((time.monotonic() - started) * 1000)
                latest.last_error = None
                if "review_round" in result:
                    latest.last_review_round = int(result["review_round"])
                if "review_result" in result:
                    latest.last_review_result = result["review_result"]
                latest.run_lock = False
                await self.session_manager.save(latest)
            return (
                result.get("output_text", ""),
                mode,
                latest.last_review_round,
                latest.last_review_result,
            )
        except asyncio.CancelledError:
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                latest = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                latest.last_run_status = "error"
                latest.last_error = "cancelled"
                latest.run_lock = False
                await self.session_manager.save(latest)
            return ("[Cancel]: done", mode, latest.last_review_round, latest.last_review_result)
        except Exception as exc:
            async with self.session_manager.lock(chat_id=chat_id, user_id=user_id):
                latest = await self.session_manager.load(chat_id=chat_id, user_id=user_id)
                latest.last_run_status = "error"
                latest.last_error = str(exc)
                latest.run_lock = False
                await self.session_manager.save(latest)
            raise
        finally:
            if current_task is not None:
                await self._clear_running_task(session_id=session_id, task=current_task)

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

    async def _set_running_task(self, *, session_id: str, task: asyncio.Task[Any]) -> None:
        async with self._running_tasks_lock:
            self._running_tasks[session_id] = task

    async def _clear_running_task(self, *, session_id: str, task: asyncio.Task[Any]) -> None:
        async with self._running_tasks_lock:
            current = self._running_tasks.get(session_id)
            if current is task:
                self._running_tasks.pop(session_id, None)

    async def _get_running_task(self, session_id: str) -> asyncio.Task[Any] | None:
        async with self._running_tasks_lock:
            task = self._running_tasks.get(session_id)
            if task is not None and task.done():
                self._running_tasks.pop(session_id, None)
                return None
            return task

    def _safe_mcp_status(self) -> dict[str, Any] | None:
        try:
            return self.codex_mcp.get_status()
        except CodexMcpStatusError as exc:
            self.codex_mcp.record_error(str(exc))
            return None

    @staticmethod
    def _help_text(*, mode: str, working_directory: str) -> str:
        return "\n".join(
            [
                "[Commands]:",
                "/start",
                "/mode single|plan",
                "/new",
                "/status",
                "/profile list|<name>",
                "/cancel",
                "plain text → Codex",
                "",
                "[Current]:",
                f"mode={mode}",
                f"working_directory={working_directory}",
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
            "[Status]:",
            f"mode={session.mode}",
            (
                f"profile={session.profile_name}, "
                f"model={session.profile_model or '-'}, "
                f"working_directory={session.profile_working_directory or '-'}"
            ),
            (
                f"last_run={session.last_run_status} "
                f"({session.last_run_latency_ms}ms)"
                if session.last_run_latency_ms is not None
                else f"last_run={session.last_run_status}"
            ),
        ]

        if session.mode == "plan":
            result = session.last_review_result or "-"
            lines.append(f"plan_review=rounds={session.last_review_round}/3, result={result}")
        elif session.mode == "single":
            lines.append("single_run=direct")

        if not mcp_status:
            lines.append("codex_mcp=unknown")
        else:
            running = mcp_status.get("running")
            ready = mcp_status.get("ready")
            pid = mcp_status.get("pid")
            uptime = mcp_status.get("uptime_sec")
            uptime_display = "-" if uptime is None else f"{uptime}s"
            lines.append(
                f"codex_mcp=running={_to_display_bool(running)}, "
                f"ready={_to_display_bool(ready)}, "
                f"pid={pid or '-'}, "
                f"uptime={uptime_display}"
            )

        lines.append(f"last_error={session.last_error or '-'}")

        return "\n".join(lines)

    def _ensure_session_profile(self, session: BotSession) -> bool:
        selected = self.profile_registry.get(session.profile_name)
        if selected is None:
            selected = self.profile_registry.default_profile()
            self._apply_profile_to_session(session, selected)
            return True

        normalized_name = selected.name
        normalized_model = selected.model
        normalized_working_directory = selected.working_directory
        normalized_agent_models: dict[str, str] = {}
        normalized_agent_prompts: dict[str, str] = {}
        for agent_name, agent_profile in selected.agent_overrides.items():
            normalized_agent = str(agent_name).strip().lower()
            if not normalized_agent:
                continue
            if agent_profile.model:
                normalized_agent_models[normalized_agent] = agent_profile.model
            if agent_profile.system_prompt:
                normalized_agent_prompts[normalized_agent] = agent_profile.system_prompt
        if (
            session.profile_name != normalized_name
            or session.profile_model != normalized_model
            or session.profile_working_directory != normalized_working_directory
            or session.profile_agent_models != normalized_agent_models
            or session.profile_agent_system_prompts != normalized_agent_prompts
        ):
            self._apply_profile_to_session(session, selected)
            return True
        return False

    @staticmethod
    def _apply_profile_to_session(session: BotSession, profile: ExecutionProfile) -> None:
        session.profile_name = profile.name
        session.profile_model = profile.model
        session.profile_working_directory = profile.working_directory
        agent_models: dict[str, str] = {}
        agent_prompts: dict[str, str] = {}
        for agent_name, agent_profile in profile.agent_overrides.items():
            normalized_agent = str(agent_name).strip().lower()
            if not normalized_agent:
                continue
            if agent_profile.model:
                agent_models[normalized_agent] = agent_profile.model
            if agent_profile.system_prompt:
                agent_prompts[normalized_agent] = agent_profile.system_prompt
        session.profile_agent_models = agent_models
        session.profile_agent_system_prompts = agent_prompts

    def _format_profile_list(self, session: BotSession) -> str:
        default_profile = self.profile_registry.default_profile()
        lines = ["[Profiles]:"]
        for name in sorted(self.profile_registry.profiles.keys(), key=str.lower):
            profile = self.profile_registry.profiles[name]
            active_prefix = "*" if profile.name == session.profile_name else "-"
            default_suffix = " (default)" if profile.name == default_profile.name else ""
            model_text = profile.model or "-"
            working_directory_text = profile.working_directory or "-"
            lines.append(
                (
                    f"{active_prefix} {profile.name}{default_suffix}: "
                    f"model={model_text}, working_directory={working_directory_text}"
                )
            )
        return "\n".join(lines)
