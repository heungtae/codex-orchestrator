from __future__ import annotations

from dataclasses import dataclass

from integrations.codex_executor import CodexExecutor
from workflows.multi_agent_workflow import MultiAgentWorkflow
from workflows.single_agent_workflow import (
    LlmDeveloperAgent,
    LlmPlannerAgent,
    LlmReviewerAgent,
    SingleAgentWorkflow,
)


@dataclass
class AgentFactory:
    executor: CodexExecutor
    max_review_rounds: int = 3

    def create_single_workflow(self) -> SingleAgentWorkflow:
        planner = LlmPlannerAgent(executor=self.executor)
        developer = LlmDeveloperAgent(executor=self.executor)
        reviewer = LlmReviewerAgent(executor=self.executor)
        return SingleAgentWorkflow(
            planner=planner,
            developer=developer,
            reviewer=reviewer,
            max_review_rounds=self.max_review_rounds,
        )

    def create_multi_workflow(self) -> MultiAgentWorkflow:
        return MultiAgentWorkflow(executor=self.executor)
