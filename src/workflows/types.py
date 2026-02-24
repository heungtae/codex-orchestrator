from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from core.models import BotSession, WorkflowResult


@dataclass
class SelectorDecision:
    mode: Literal["single", "plan"]
    reason: str
    callback: Callable[[str, str], None] | None = None


@dataclass
class ReviewDecision:
    result: Literal["approved", "needs_changes"]
    feedback: str


class SelectorAgent(Protocol):
    async def select_mode(
        self,
        *,
        user_input: str,
        session: BotSession,
    ) -> SelectorDecision:
        ...


class PlannerAgent(Protocol):
    async def plan(
        self,
        *,
        user_input: str,
        session: BotSession,
    ) -> str:
        ...


class DeveloperAgent(Protocol):
    async def develop(
        self,
        *,
        user_input: str,
        session: BotSession,
        round_index: int,
        review_feedback: str | None,
    ) -> str:
        ...


class ReviewerAgent(Protocol):
    async def review(
        self,
        *,
        user_input: str,
        candidate_output: str,
        artifacts: list[str],
        session: BotSession,
        round_index: int,
    ) -> ReviewDecision:
        ...


class Workflow(Protocol):
    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        ...


HistoryItem = dict[str, Any]
