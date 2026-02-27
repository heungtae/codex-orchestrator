from __future__ import annotations

import asyncio
import builtins
import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class CodexExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentTextNotification:
    message_id: str
    phase: str
    text: str
    agent_name: str | None = None


_ACTIVE_CODEX_AGENT_NAME: ContextVar[str | None] = ContextVar(
    "active_codex_agent_name",
    default=None,
)


def _stdout_print(*values: object, **kwargs: Any) -> None:
    file = kwargs.get("file")
    if file is None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if values:
            normalized_values: list[object] = list(values)
            first_value = str(normalized_values[0])
            normalized_values[0] = "\n".join(
                f"[{timestamp}] {line}" for line in first_value.splitlines() or [""]
            )
            values = tuple(normalized_values)
        else:
            values = (f"[{timestamp}]",)

    builtins.print(*values, **kwargs)


@contextmanager
def codex_agent_name_scope(agent_name: str | None):
    cleaned = str(agent_name).strip() if isinstance(agent_name, str) else ""
    token = _ACTIVE_CODEX_AGENT_NAME.set(cleaned or None)
    try:
        yield
    finally:
        _ACTIVE_CODEX_AGENT_NAME.reset(token)


def get_active_codex_agent_name() -> str | None:
    return _ACTIVE_CODEX_AGENT_NAME.get()


class CodexExecutor(Protocol):
    async def run(
        self,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
        *,
        system_instructions: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
    ) -> str:
        ...


@dataclass
class OpenAIAgentsExecutor:
    """Runs Codex via OpenAI Agents SDK MCPServerStdio."""

    mcp_command: str = "npx"
    mcp_args: tuple[str, ...] = ("-y", "codex", "mcp-server")
    mcp_server_name: str = "Codex CLI"
    client_session_timeout_seconds: int = 360000
    default_model: str | None = None
    include_history: bool = True
    history_window: int = 12
    history_char_limit: int = 6000
    status_tracker: Any | None = None
    approval_policy: str = "never"
    sandbox: str = "danger-full-access"
    cwd: str | None = None
    close_timeout_seconds: float = 2.0
    on_agent_message: Callable[[AgentTextNotification], None] | None = None
    verbose_stdout: bool = False

    _startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _server: Any | None = field(default=None, init=False, repr=False)
    _server_cm: Any | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False)

    async def run(
        self,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
        *,
        system_instructions: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
    ) -> str:
        await self._ensure_started()

        if self._server is None:
            raise CodexExecutionError("codex mcp server is not initialized")

        final_prompt = self._compose_prompt(prompt=prompt, history=history)
        payload: dict[str, Any] = {
            "prompt": final_prompt,
            "approval-policy": self.approval_policy,
            "sandbox": self.sandbox,
            "cwd": cwd or self.cwd or os.getcwd(),
        }

        selected_model = model or self.default_model
        if selected_model:
            payload["model"] = selected_model

        if system_instructions:
            payload["developer-instructions"] = system_instructions

        try:
            result = await self._server.call_tool("codex", payload)
        except asyncio.CancelledError:
            await self._cleanup_after_cancel()
            self._set_status(stopped=True, error="request cancelled")
            raise
        except Exception as exc:
            await self._reset_after_transport_error(str(exc))
            raise CodexExecutionError(f"failed to call mcp tool 'codex': {exc}") from exc

        self._print_mcp_response_messages(result)
        output_text, is_error = self._extract_call_result(result)
        if is_error:
            raise CodexExecutionError(output_text or "codex mcp tool returned error")

        if not output_text:
            raise CodexExecutionError("codex mcp tool returned empty output")

        return output_text

    async def warmup(self) -> None:
        await self._ensure_started()

    async def _cleanup_after_cancel(self) -> None:
        current_task = asyncio.current_task()
        uncancel_count = 0
        if current_task is not None and hasattr(current_task, "uncancel"):
            while current_task.cancelling():
                current_task.uncancel()
                uncancel_count += 1

        try:
            await self.close()
        except Exception:
            self._server = None
            self._server_cm = None
            self._started = False
        finally:
            if current_task is not None:
                for _ in range(uncancel_count):
                    current_task.cancel()

    async def _reset_after_transport_error(self, error_message: str) -> None:
        try:
            await self.close()
        except Exception:
            pass
        finally:
            self._server = None
            self._server_cm = None
            self._started = False
            self._set_status(stopped=True, error=error_message)

    async def close(self) -> None:
        async with self._startup_lock:
            if not self._started:
                return

            server_close_error: Exception | None = None
            if self._server_cm is not None:
                try:
                    await asyncio.wait_for(
                        self._server_cm.__aexit__(None, None, None),
                        timeout=self.close_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    server_close_error = RuntimeError(
                        "codex mcp server close timed out "
                        f"after {self.close_timeout_seconds:.1f}s"
                    )
                except Exception as exc:
                    server_close_error = exc

            self._server = None
            self._server_cm = None
            self._started = False
            self._set_status(stopped=True)

            if server_close_error is not None:
                raise server_close_error

    async def _ensure_started(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            server = None
            try:
                server = self._create_mcp_server()
                await server.__aenter__()
                tools = await server.list_tools()
                tool_names = {getattr(tool, "name", "") for tool in tools}
                if "codex" not in tool_names:
                    raise CodexExecutionError("mcp server does not expose required tool: codex")
            except Exception as exc:
                if server is not None:
                    try:
                        await server.__aexit__(None, None, None)
                    except Exception:
                        pass
                self._set_status(error=f"failed to start codex mcp server: {exc}")
                raise CodexExecutionError(f"failed to start codex mcp server: {exc}") from exc

            self._server = server
            self._server_cm = server
            self._started = True
            self._set_status(running=True, ready=True)

    def _create_mcp_server(self) -> Any:
        try:
            from agents.mcp import MCPServerStdio
        except Exception as exc:  # pragma: no cover - import error path
            raise CodexExecutionError(
                "OpenAI Agents SDK is required. Install package `openai-agents`."
            ) from exc

        return MCPServerStdio(
            name=self.mcp_server_name,
            params={
                "command": self.mcp_command,
                "args": list(self.mcp_args),
            },
            client_session_timeout_seconds=self.client_session_timeout_seconds,
            message_handler=self._handle_mcp_message,
        )

    async def _handle_mcp_message(self, message: Any) -> None:
        if self.verbose_stdout:
            self._log_codex_event_message(message)
        notification = self._extract_notification_from_session_message(message)
        if notification is not None:
            self._emit_agent_notification(notification)

    def _compose_prompt(self, *, prompt: str, history: list[dict[str, Any]] | None) -> str:
        if not self.include_history or not history:
            return prompt

        rendered: list[str] = []
        for item in history[-self.history_window :]:
            role = str(item.get("role", "assistant"))
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            rendered.append(f"{role}: {content}")

        if not rendered:
            return prompt

        history_text = "\n".join(rendered)
        if len(history_text) > self.history_char_limit:
            history_text = history_text[-self.history_char_limit :]

        return (
            "Conversation history (most recent):\n"
            f"{history_text}\n\n"
            "Current request:\n"
            f"{prompt}"
        )

    @staticmethod
    def _extract_call_result(result: Any) -> tuple[str, bool]:
        payload: dict[str, Any]
        if hasattr(result, "model_dump"):
            payload = result.model_dump()
        else:
            payload = dict(result) if isinstance(result, dict) else {}

        is_error = bool(payload.get("isError") or payload.get("is_error"))

        structured = payload.get("structuredContent") or payload.get("structured_content")
        if isinstance(structured, dict):
            content = structured.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip(), is_error

        content_items = payload.get("content")
        if isinstance(content_items, list):
            texts: list[str] = []
            for item in content_items:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
            if texts:
                return "\n".join(texts), is_error

        if payload:
            return json.dumps(payload, ensure_ascii=False), is_error

        return "", is_error

    @staticmethod
    def _print_mcp_response_messages(result: Any) -> None:
        payload: dict[str, Any]
        if hasattr(result, "model_dump"):
            payload = result.model_dump()
        else:
            payload = dict(result) if isinstance(result, dict) else {}

        content_items = payload.get("content")
        if isinstance(content_items, list) and content_items:
            for item in content_items:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        _stdout_print(f"[codex mcp-response] {text.strip()}", flush=True)
                        continue
                _stdout_print(
                    f"[codex mcp-response] {json.dumps(item, ensure_ascii=False)}",
                    flush=True,
                )
            return

        structured = payload.get("structuredContent") or payload.get("structured_content")
        if isinstance(structured, dict):
            structured_text = structured.get("content")
            if isinstance(structured_text, str) and structured_text.strip():
                _stdout_print(f"[codex mcp-response] {structured_text.strip()}", flush=True)
                return

        if payload:
            _stdout_print(
                f"[codex mcp-response] {json.dumps(payload, ensure_ascii=False)}",
                flush=True,
            )

    def _emit_agent_notification(self, notification: AgentTextNotification) -> None:
        agent_name = notification.agent_name or "-"
        _stdout_print(
            "[codex-notification] "
            f"id={notification.message_id} "
            f"phase={notification.phase} "
            f"agent={agent_name} "
            f"text={notification.text}",
            flush=True,
        )

        callback = self.on_agent_message
        if callback is None:
            return

        try:
            callback(notification)
        except Exception:
            pass

    @staticmethod
    def _extract_notification_from_session_message(message: Any) -> AgentTextNotification | None:
        notification: Any = message
        if hasattr(message, "root"):
            notification = message.root
        if hasattr(notification, "model_dump"):
            notification = notification.model_dump()
        elif hasattr(notification, "__dict__"):
            notification = dict(vars(notification))

        if not isinstance(notification, dict):
            return None

        if notification.get("method") != "codex/event":
            return None

        params = notification.get("params")
        if not isinstance(params, dict):
            return None
        return OpenAIAgentsExecutor._extract_notification_from_event_params(params)

    def _log_codex_event_message(self, message: Any) -> None:
        notification: Any = message
        if hasattr(message, "root"):
            notification = message.root
        if hasattr(notification, "model_dump"):
            notification = notification.model_dump()
        elif hasattr(notification, "__dict__"):
            notification = dict(vars(notification))
        if not isinstance(notification, dict):
            return
        if notification.get("method") != "codex/event":
            return
        params = notification.get("params")
        _stdout_print(
            f"[codex-event] method=codex/event params={json.dumps(params, ensure_ascii=False)}",
            flush=True,
        )

    @staticmethod
    def _extract_notification_from_event_params(params: dict[str, Any]) -> AgentTextNotification | None:
        msg = params.get("msg")
        if not isinstance(msg, dict):
            return None

        msg_type = msg.get("type")
        item: dict[str, Any] | None = None
        phase: str | None = None
        text: str | None = None

        if msg_type == "item_completed":
            item = msg.get("item")
            if not isinstance(item, dict) or item.get("type") != "AgentMessage":
                return None

            phase_value = item.get("phase")
            if not isinstance(phase_value, str) or not phase_value:
                return None
            phase = phase_value

            content = item.get("content")
            if not isinstance(content, list):
                return None

            text_parts: list[str] = []
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                if content_item.get("type") != "Text":
                    continue
                content_text = content_item.get("text")
                if isinstance(content_text, str) and content_text.strip():
                    text_parts.append(content_text.strip())
            if not text_parts:
                return None
            text = "\n".join(text_parts)
        elif msg_type == "agent_message":
            message = msg.get("message")
            if not isinstance(message, str) or not message.strip():
                return None
            phase_value = msg.get("phase")
            phase = phase_value if isinstance(phase_value, str) and phase_value else "commentary"
            text = message.strip()
            item = {}
        elif msg_type == "agent_message_delta":
            delta = msg.get("delta")
            if not isinstance(delta, str) or not delta:
                return Nonagents.mcp.MCPServerStdioe
            phase_value = msg.get("phase")
            phase = phase_value if isinstance(phase_value, str) and phase_value else "commentary"
            text = delta
            item = {}
        else:
            return None

        agent_name = OpenAIAgentsExecutor._extract_agent_name(params=params, msg=msg, item=item)
        message_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(message_id, str) or not message_id:
            message_id = params.get("id")
        return AgentTextNotification(
            message_id=message_id if isinstance(message_id, str) else "",
            phase=phase,
            text=text,
            agent_name=agent_name,
        )

    @staticmethod
    def _extract_agent_name(
        *,
        params: dict[str, Any],
        msg: dict[str, Any],
        item: dict[str, Any],
    ) -> str | None:
        scoped_agent_name = get_active_codex_agent_name()
        if isinstance(scoped_agent_name, str):
            normalized_scoped = scoped_agent_name.strip()
            if normalized_scoped:
                return normalized_scoped

        keys = ("agent_name", "agent", "agentName", "role_name", "role")
        containers: list[dict[str, Any]] = [item, msg, params]
        for node in (item.get("metadata"), msg.get("metadata"), params.get("metadata")):
            if isinstance(node, dict):
                containers.append(node)

        for container in containers:
            for key in keys:
                value = container.get(key)
                if isinstance(value, str):
                    normalized = value.strip()
                    if normalized:
                        return normalized

        return None

    def _set_status(
        self,
        *,
        running: bool | None = None,
        ready: bool | None = None,
        error: str | None = None,
        stopped: bool = False,
    ) -> None:
        tracker = self.status_tracker
        if tracker is None:
            return

        try:
            if stopped and hasattr(tracker, "mark_stopped"):
                tracker.mark_stopped()

            if running is not None and hasattr(tracker, "mark_running"):
                tracker.mark_running(pid=None, ready=bool(ready))
            elif ready and hasattr(tracker, "mark_ready"):
                tracker.mark_ready()

            if error and hasattr(tracker, "record_error"):
                tracker.record_error(error)
        except Exception:
            pass


# Backward-compatible names for existing imports.
CodexMcpExecutor = OpenAIAgentsExecutor
AgentsSdkCodexExecutor = OpenAIAgentsExecutor


class EchoCodexExecutor:
    """Debug-only executor that mirrors input."""

    async def run(
        self,
        prompt: str,
        history: list[dict[str, Any]] | None = None,
        *,
        system_instructions: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
    ) -> str:
        return prompt
