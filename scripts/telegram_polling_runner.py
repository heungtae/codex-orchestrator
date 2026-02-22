#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from bot.telegram_adapter import parse_update, split_telegram_text
from integrations.codex_executor import (
    CodexMcpExecutor,
    EchoCodexExecutor,
)
from main import build_orchestrator


@dataclass
class TelegramBotApi:
    token: str

    def __post_init__(self) -> None:
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def _post(self, method: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"telegram request failed: {exc}") from exc

        try:
            payload_json = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"telegram response is not valid json: {raw}") from exc

        if not payload_json.get("ok"):
            description = payload_json.get("description", "unknown error")
            raise RuntimeError(f"telegram api error: {description}")

        return payload_json.get("result")

    def delete_webhook(self, drop_pending_updates: bool = False) -> None:
        self._post(
            "deleteWebhook",
            {
                "drop_pending_updates": drop_pending_updates,
            },
        )

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, *, chat_id: str, text: str) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )


class _CodexEventValidationFilter(logging.Filter):
    """Suppress known MCP notification schema noise from `codex/event`."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "Failed to validate notification" in message and "codex/event" in message:
            return False
        return True


def _configure_logging() -> None:
    logging.getLogger().addFilter(_CodexEventValidationFilter())


def _parse_allowed_chat_ids(raw: str) -> set[str] | None:
    value = raw.strip()
    if not value:
        return None
    return {piece.strip() for piece in value.split(",") if piece.strip()}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def _safe_send(api: TelegramBotApi, chat_id: str, text: str) -> None:
    for chunk in split_telegram_text(text):
        try:
            api.send_message(chat_id=chat_id, text=chunk)
        except Exception as exc:
            print(f"[warn] failed to send telegram message: {exc}")


def _extract_executor(orchestrator: Any) -> Any | None:
    single_workflow = getattr(orchestrator, "single_workflow", None)
    developer = getattr(single_workflow, "developer", None)
    executor = getattr(developer, "_executor", None)
    if executor is not None:
        return executor

    multi_workflow = getattr(orchestrator, "multi_workflow", None)
    return getattr(multi_workflow, "executor", None)


def _format_mcp_status(status: dict[str, Any]) -> str:
    running = status.get("running")
    ready = status.get("ready")
    pid = status.get("pid")
    uptime = status.get("uptime_sec")
    uptime_text = "-" if uptime is None else f"{uptime}s"
    return f"running={running}, ready={ready}, pid={pid}, uptime={uptime_text}"


async def _warmup_codex_mcp(orchestrator: Any) -> bool:
    executor = _extract_executor(orchestrator)
    if isinstance(executor, EchoCodexExecutor):
        print("[warn] CODEX_ALLOW_ECHO_EXECUTOR=true (debug mode). mcp warmup is skipped.")
        return False

    if not isinstance(executor, CodexMcpExecutor):
        print(f"[warn] unknown executor type: {type(executor).__name__}; skip mcp warmup")
        return False

    try:
        await executor.warmup()
        status = orchestrator.codex_mcp.get_status()
        print(f"[info] codex mcp-server connected: {_format_mcp_status(status)}")
        return True
    except Exception as exc:
        print(f"[error] codex mcp-server connection failed: {exc}")
        return False


async def _close_codex_mcp(orchestrator: Any) -> None:
    executor = _extract_executor(orchestrator)
    if not isinstance(executor, CodexMcpExecutor):
        return

    try:
        await executor.close()
    except Exception as exc:
        print(f"[warn] failed to close codex mcp session: {exc}")


async def _run_polling() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    poll_timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    loop_sleep_sec = float(os.getenv("TELEGRAM_LOOP_SLEEP_SEC", "1"))
    clear_webhook = _env_bool("TELEGRAM_DELETE_WEBHOOK_ON_START", True)
    drop_pending = _env_bool("TELEGRAM_DROP_PENDING_UPDATES", False)
    allowed_chat_ids = _parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))

    api = TelegramBotApi(token=token)
    if clear_webhook:
        try:
            await asyncio.to_thread(api.delete_webhook, drop_pending_updates=drop_pending)
        except Exception as exc:
            print(f"[warn] failed to delete webhook on startup: {exc}")

    orchestrator = build_orchestrator()
    try:
        require_mcp_warmup = _env_bool("TELEGRAM_REQUIRE_MCP_WARMUP", True)
        warmup_ok = await _warmup_codex_mcp(orchestrator)
        if require_mcp_warmup and not warmup_ok:
            raise SystemExit(
                "codex mcp-server warmup failed. "
                "Check CODEX_MCP_COMMAND/CODEX_MCP_ARGS and runtime auth settings, "
                "or disable strict check with TELEGRAM_REQUIRE_MCP_WARMUP=false"
            )
        next_offset: int | None = None

        print("[info] telegram polling runner started")
        while True:
            try:
                updates = await asyncio.to_thread(
                    api.get_updates,
                    offset=next_offset,
                    timeout=poll_timeout,
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        next_offset = update_id + 1

                    inbound = parse_update(update)
                    if not inbound:
                        continue

                    if allowed_chat_ids is not None and inbound.chat_id not in allowed_chat_ids:
                        await asyncio.to_thread(_safe_send, api, inbound.chat_id, "not allowed chat")
                        continue

                    try:
                        output = await orchestrator.handle_message(
                            inbound.chat_id,
                            inbound.user_id,
                            inbound.text,
                        )
                    except Exception as exc:
                        output = f"internal error: {exc}"

                    await asyncio.to_thread(_safe_send, api, inbound.chat_id, output)
            except Exception as exc:
                print(f"[warn] polling loop error: {exc}")

            if loop_sleep_sec > 0:
                await asyncio.sleep(loop_sleep_sec)
    finally:
        await _close_codex_mcp(orchestrator)


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(_run_polling())
    except KeyboardInterrupt:
        print("\n[info] stopped by user")


if __name__ == "__main__":
    main()
