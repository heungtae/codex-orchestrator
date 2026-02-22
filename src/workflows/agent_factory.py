from __future__ import annotations

from dataclasses import dataclass

from integrations.codex_executor import CodexExecutor
from workflows.multi_agent_workflow import MultiAgentWorkflow
from workflows.plan_workflow import (
    LlmDeveloperAgent,
    LlmPlannerAgent,
    LlmReviewerAgent,
    PlanWorkflow,
)
from workflows.single_agent_workflow import LlmSingleDeveloperAgent, SingleAgentWorkflow
from workflows.types import Workflow


@dataclass
class AgentFactory:
    executor: CodexExecutor
    max_review_rounds: int = 3

    def create_single_workflow(self) -> SingleAgentWorkflow:
        developer = LlmSingleDeveloperAgent(executor=self.executor)
        return SingleAgentWorkflow(developer=developer)

    def create_plan_workflow(
        self,
        *,
        single_fallback_workflow: Workflow | None = None,
    ) -> PlanWorkflow:
        planner = LlmPlannerAgent(executor=self.executor)
        developer = LlmDeveloperAgent(executor=self.executor)
        reviewer = LlmReviewerAgent(executor=self.executor)
        return PlanWorkflow(
            planner=planner,
            developer=developer,
            reviewer=reviewer,
            single_fallback_workflow=single_fallback_workflow,
            max_review_rounds=self.max_review_rounds,
        )

    def create_multi_workflow(self) -> MultiAgentWorkflow:
        return MultiAgentWorkflow(executor=self.executor)
