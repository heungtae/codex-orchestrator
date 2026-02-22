from __future__ import annotations

from dataclasses import dataclass

TELEGRAM_MAX_MESSAGE_LEN = 4096


@dataclass(frozen=True)
class TelegramInboundMessage:
    chat_id: str
    user_id: str
    text: str


def parse_update(update: dict) -> TelegramInboundMessage | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    chat = message.get("chat", {})
    sender = message.get("from", {})

    if not isinstance(text, str):
        return None

    chat_id = chat.get("id")
    user_id = sender.get("id")
    if chat_id is None or user_id is None:
        return None

    return TelegramInboundMessage(chat_id=str(chat_id), user_id=str(user_id), text=text)


def split_telegram_text(text: str, max_chars: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at <= 0:
            split_at = max_chars
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks
