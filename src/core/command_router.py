from __future__ import annotations

from core.models import RouteResult


class CommandRouter:
    """Parses Telegram text into bot commands or Codex-forwardable input."""

    def route(self, text: str | None) -> RouteResult:
        raw = (text or "").strip()
        if not raw:
            return RouteResult(kind="text", text="")

        if raw.startswith("/"):
            parts = raw.split()
            command = parts[0].lower()

            if command == "/start":
                return RouteResult(kind="bot_command", text=raw, command="start")

            if command == "/new":
                return RouteResult(kind="bot_command", text=raw, command="new")

            if command == "/status":
                return RouteResult(kind="bot_command", text=raw, command="status")

            if command == "/mode":
                mode_arg = parts[1].lower() if len(parts) > 1 else ""
                return RouteResult(
                    kind="bot_command",
                    text=raw,
                    command="mode",
                    args=(mode_arg,),
                )

            return RouteResult(kind="codex_slash", text=raw)

        return RouteResult(kind="text", text=raw)
