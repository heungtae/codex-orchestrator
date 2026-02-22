import asyncio
import unittest

from workflows.single_agent_workflow import (
    LlmDeveloperAgent,
    LlmReviewerAgent,
    SingleAgentWorkflow,
)
from core.models import BotSession
from integrations.codex_executor import CodexExecutionError, EchoCodexExecutor
from workflows.types import ReviewDecision


class FakeDeveloper:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    async def develop(
        self,
        *,
        user_input: str,
        session: BotSession,
        round_index: int,
        review_feedback: str | None,
    ) -> str:
        self.calls.append((round_index, review_feedback))
        if review_feedback:
            return f"draft-{round_index}-fixed"
        return f"draft-{round_index}"


class FakeReviewer:
    def __init__(self, decisions: list[ReviewDecision]) -> None:
        self._decisions = decisions
        self.calls = 0

    async def review(
        self,
        *,
        user_input: str,
        candidate_output: str,
        artifacts: list[str],
        session: BotSession,
        round_index: int,
    ) -> ReviewDecision:
        decision = self._decisions[self.calls]
        self.calls += 1
        return decision


class FailIfCalledReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(
        self,
        *,
        user_input: str,
        candidate_output: str,
        artifacts: list[str],
        session: BotSession,
        round_index: int,
    ) -> ReviewDecision:
        self.calls += 1
        raise AssertionError("review must be skipped when no implementation artifacts exist")


class SingleWorkflowTests(unittest.TestCase):
    @staticmethod
    def _session() -> BotSession:
        return BotSession(session_id="tg:1:1", chat_id="1", user_id="1")

    def test_single_workflow_loops_until_approved(self) -> None:
        workflow = SingleAgentWorkflow(
            developer=FakeDeveloper(),
            reviewer=FakeReviewer(
                decisions=[
                    ReviewDecision(result="needs_changes", feedback="add tests"),
                    ReviewDecision(result="approved", feedback="ok"),
                ]
            ),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 2)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("rounds=2/3", result["output_text"])

    def test_single_workflow_stops_at_max_rounds(self) -> None:
        workflow = SingleAgentWorkflow(
            developer=FakeDeveloper(),
            reviewer=FakeReviewer(
                decisions=[
                    ReviewDecision(result="needs_changes", feedback="fix A"),
                    ReviewDecision(result="needs_changes", feedback="fix B"),
                    ReviewDecision(result="needs_changes", feedback="fix C"),
                ]
            ),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 3)
        self.assertEqual(result["review_result"], "max_rounds_reached")
        self.assertIn("max_rounds_reached", result["output_text"])

    def test_single_workflow_fails_fast_on_prompt_echo(self) -> None:
        echo_executor = EchoCodexExecutor()
        workflow = SingleAgentWorkflow(
            developer=LlmDeveloperAgent(executor=echo_executor),
            reviewer=LlmReviewerAgent(executor=echo_executor),
            max_review_rounds=3,
        )

        with self.assertRaises(CodexExecutionError):
            asyncio.run(workflow.run("show me the full file list", self._session()))

    def test_single_workflow_skips_review_when_no_artifacts_detected(self) -> None:
        reviewer = FailIfCalledReviewer()
        workflow = SingleAgentWorkflow(
            developer=FakeDeveloper(),
            reviewer=reviewer,
            max_review_rounds=3,
            review_only_with_artifacts=True,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 1)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("rounds=1/3", result["output_text"])
        self.assertEqual(reviewer.calls, 0)

    def test_history_is_sanitized_before_single_flow(self) -> None:
        workflow = SingleAgentWorkflow(
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.history = [
            {"role": "reviewer", "content": "should be removed"},
            {"role": "assistant", "content": "You are Developer Agent. Return only the improved developer response."},
            {"role": "assistant", "content": "valid assistant output"},
            {"role": "user", "content": "valid user input"},
        ]

        asyncio.run(workflow.run("request", session))

        self.assertEqual(
            session.history,
            [
                {"role": "assistant", "content": "valid assistant output"},
                {"role": "user", "content": "valid user input"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
