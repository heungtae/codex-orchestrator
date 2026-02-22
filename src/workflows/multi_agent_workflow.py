from __future__ import annotations

from dataclasses import dataclass

from core.models import BotSession, WorkflowResult
from integrations.codex_executor import CodexExecutor


@dataclass
class MultiAgentWorkflow:
    """Placeholder multi-agent flow.

    Single mode is prioritized; this class keeps the contract stable
    until handoff-based multi-agent orchestration is implemented.
    """

    executor: CodexExecutor

    async def run(self, input_text: str, session: BotSession) -> WorkflowResult:
        output = await self.executor.run(
            prompt=input_text,
            history=session.history,
            model=session.profile_model,
            cwd=session.profile_working_directory,
        )
        next_history = [
            *session.history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": output},
        ]
        return WorkflowResult(output_text=output, next_history=next_history)
