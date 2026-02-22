from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


class CodexExecutionError(RuntimeError):
    pass


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
class CodexMcpExecutor:
    """Runs Codex by calling the `codex` MCP tool directly."""

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

    _startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _session: Any | None = field(default=None, init=False, repr=False)
    _session_cm: Any | None = field(default=None, init=False, repr=False)
    _stdio_cm: Any | None = field(default=None, init=False, repr=False)
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

        if self._session is None:
            raise CodexExecutionError("codex mcp session is not initialized")

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
            result = await self._session.call_tool("codex", payload)
        except asyncio.CancelledError:
            # Ensure transport/session context managers are closed in the same task
            # where they were entered to avoid AnyIO cancel-scope task affinity errors.
            await self._cleanup_after_cancel()
            self._set_status(stopped=True, error="request cancelled")
            raise
        except Exception as exc:
            await self._reset_after_transport_error(str(exc))
            raise CodexExecutionError(f"failed to call mcp tool 'codex': {exc}") from exc

        output_text, is_error = self._extract_call_result(result)
        if is_error:
            raise CodexExecutionError(output_text or "codex mcp tool returned error")

        if not output_text:
            raise CodexExecutionError("codex mcp tool returned empty output")

        return output_text

    async def warmup(self) -> None:
        """Eagerly start MCP stdio transport and initialize the session."""
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
            self._session = None
            self._session_cm = None
            self._stdio_cm = None
            self._started = False
        finally:
            if current_task is not None:
                for _ in range(uncancel_count):
                    current_task.cancel()

    async def _reset_after_transport_error(self, error_message: str) -> None:
        # If the transport/session has broken, force a fresh connection on next run.
        try:
            await self.close()
        except Exception:
            pass
        finally:
            self._session = None
            self._session_cm = None
            self._stdio_cm = None
            self._started = False
            self._set_status(stopped=True, error=error_message)

    async def close(self) -> None:
        async with self._startup_lock:
            if not self._started:
                return

            session_close_error: Exception | None = None
            stdio_close_error: Exception | None = None

            if self._session_cm is not None:
                try:
                    await asyncio.wait_for(
                        self._session_cm.__aexit__(None, None, None),
                        timeout=self.close_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    session_close_error = RuntimeError(
                        "codex mcp session close timed out "
                        f"after {self.close_timeout_seconds:.1f}s"
                    )
                except Exception as exc:
                    session_close_error = exc
            self._session = None
            self._session_cm = None

            if self._stdio_cm is not None:
                try:
                    await asyncio.wait_for(
                        self._stdio_cm.__aexit__(None, None, None),
                        timeout=self.close_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    stdio_close_error = RuntimeError(
                        "codex mcp stdio close timed out "
                        f"after {self.close_timeout_seconds:.1f}s"
                    )
                except Exception as exc:
                    stdio_close_error = exc
            self._stdio_cm = None
            self._started = False
            self._set_status(stopped=True)

            if stdio_close_error is not None:
                raise stdio_close_error
            if session_close_error is not None:
                raise session_close_error

    async def _ensure_started(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            try:
                from mcp import ClientSession
                from mcp.client.stdio import StdioServerParameters, stdio_client
            except Exception as exc:  # pragma: no cover - import error path
                raise CodexExecutionError(
                    "MCP client SDK is required. Install package `mcp`."
                ) from exc

            params = StdioServerParameters(
                command=self.mcp_command,
                args=list(self.mcp_args),
            )

            try:
                stdio_cm = stdio_client(params)
                read_stream, write_stream = await stdio_cm.__aenter__()

                session_cm = ClientSession(read_stream, write_stream)
                session = await session_cm.__aenter__()
                await session.initialize()

                tools = await session.list_tools()
                tool_names = {getattr(tool, "name", "") for tool in getattr(tools, "tools", [])}
                if "codex" not in tool_names:
                    raise CodexExecutionError("mcp server does not expose required tool: codex")
            except Exception as exc:
                self._set_status(error=f"failed to start codex mcp server: {exc}")
                raise CodexExecutionError(f"failed to start codex mcp server: {exc}") from exc

            self._stdio_cm = stdio_cm
            self._session_cm = session_cm
            self._session = session
            self._started = True
            self._set_status(running=True, ready=True)

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
            # Tracker failure must not block request execution.
            pass


# Backward-compatible alias for older imports.
AgentsSdkCodexExecutor = CodexMcpExecutor


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
