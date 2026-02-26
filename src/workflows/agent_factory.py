from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from integrations.codex_executor import CodexExecutor
from workflows.plan_agent_workflow import (
    LlmDeveloperAgent,
    LlmPlannerAgent,
    LlmReviewerAgent,
    LlmSelectorAgent,
    PlanWorkflow,
)
from workflows.single_agent_workflow import LlmSingleDeveloperAgent, SingleAgentWorkflow
from workflows.types import Workflow


@dataclass
class AgentFactory:
    executor: CodexExecutor
    max_review_rounds: int = 1

    def create_single_workflow(self) -> SingleAgentWorkflow:
        developer = LlmSingleDeveloperAgent(executor=self.executor)
        return SingleAgentWorkflow(developer=developer)

    def create_selector_agent(self) -> LlmSelectorAgent:
        return LlmSelectorAgent(executor=self.executor)

    def create_plan_workflow(
        self,
        *,
        single_workflow: Workflow,
        on_mode_selected: Callable[[str, str], None] | None = None,
    ) -> PlanWorkflow:
        selector = LlmSelectorAgent(executor=self.executor)
        planner = LlmPlannerAgent(executor=self.executor)
        developer = LlmDeveloperAgent(executor=self.executor)
        reviewer = LlmReviewerAgent(executor=self.executor)
        return PlanWorkflow(
            selector=selector,
            planner=planner,
            developer=developer,
            reviewer=reviewer,
            single_workflow=single_workflow,
            max_review_rounds=self.max_review_rounds,
            on_mode_selected=on_mode_selected,
        )
