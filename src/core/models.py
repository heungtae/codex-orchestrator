from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

BotMode = Literal["single", "multi"]
InputKind = Literal["bot_command", "codex_slash", "text"]
BotCommandName = Literal["start", "mode", "new", "status"]
ReviewResult = Literal["approved", "needs_changes", "max_rounds_reached"]
RunStatus = Literal["idle", "ok", "error"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RouteResult:
    kind: InputKind
    text: str
    command: BotCommandName | None = None
    args: tuple[str, ...] = ()


@dataclass
class BotSession:
    session_id: str
    chat_id: str
    user_id: str
    mode: BotMode = "single"
    history: list[dict[str, Any]] = field(default_factory=list)
    run_lock: bool = False
    last_error: str | None = None
    last_run_status: RunStatus = "idle"
    last_run_latency_ms: int | None = None
    last_review_round: int = 0
    last_review_result: ReviewResult | None = None
    updated_at: str = field(default_factory=utc_now_iso)

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "mode": self.mode,
            "history": self.history,
            "run_lock": self.run_lock,
            "last_error": self.last_error,
            "last_run_status": self.last_run_status,
            "last_run_latency_ms": self.last_run_latency_ms,
            "last_review_round": self.last_review_round,
            "last_review_result": self.last_review_result,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BotSession":
        mode = payload.get("mode", "single")
        if mode not in ("single", "multi"):
            mode = "single"

        last_run_status = payload.get("last_run_status", "idle")
        if last_run_status not in ("idle", "ok", "error"):
            last_run_status = "idle"

        last_review_result = payload.get("last_review_result")
        if last_review_result not in ("approved", "needs_changes", "max_rounds_reached", None):
            last_review_result = None

        history = payload.get("history", [])
        if not isinstance(history, list):
            history = []

        return cls(
            session_id=str(payload["session_id"]),
            chat_id=str(payload["chat_id"]),
            user_id=str(payload["user_id"]),
            mode=mode,
            history=history,
            run_lock=bool(payload.get("run_lock", False)),
            last_error=payload.get("last_error"),
            last_run_status=last_run_status,
            last_run_latency_ms=payload.get("last_run_latency_ms"),
            last_review_round=int(payload.get("last_review_round", 0) or 0),
            last_review_result=last_review_result,
            updated_at=str(payload.get("updated_at", utc_now_iso())),
        )


class WorkflowResult(TypedDict, total=False):
    output_text: str
    next_history: list[dict[str, Any]]
    review_round: int
    review_result: ReviewResult
    metadata: dict[str, Any]


class TraceRecord(TypedDict, total=False):
    timestamp: str
    run_id: str
    session_id: str
    mode: BotMode
    review_round: int
    review_result: ReviewResult
    input_kind: InputKind
    input_text: str
    output_text: str
    status: Literal["ok", "error"]
    latency_ms: int
    error_message: str
