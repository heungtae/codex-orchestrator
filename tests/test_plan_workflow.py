import asyncio
import unittest

from workflows.plan_agent_workflow import (
    LlmDeveloperAgent,
    LlmPlannerAgent,
    LlmReviewerAgent,
    LlmSelectorAgent,
    PlanWorkflow,
)
from core.models import BotSession
from integrations.codex_executor import CodexExecutionError, EchoCodexExecutor
from workflows.types import ReviewDecision, SelectorDecision


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


class CapturingDeveloper:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.calls: list[tuple[int, str | None]] = []

    async def develop(
        self,
        *,
        user_input: str,
        session: BotSession,
        round_index: int,
        review_feedback: str | None,
    ) -> str:
        self.inputs.append(user_input)
        self.calls.append((round_index, review_feedback))
        return f"captured-{round_index}"


class FakeSelector:
    def __init__(self, mode: str = "plan", reason: str = "test default") -> None:
        self._mode = mode
        self._reason = reason
        self.calls: list[str] = []

    async def select_mode(
        self,
        *,
        user_input: str,
        session: BotSession,
    ) -> SelectorDecision:
        self.calls.append(user_input)
        return SelectorDecision(mode=self._mode, reason=self._reason)


class FakePlanner:
    def __init__(self, output: str = "1. Implement feature\n2. Add tests\n3. Review") -> None:
        self.output = output
        self.calls: list[str] = []

    async def plan(
        self,
        *,
        user_input: str,
        session: BotSession,
    ) -> str:
        self.calls.append(user_input)
        return self.output


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


class CapturingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []

    async def run(
        self,
        prompt: str,
        history=None,
        *,
        system_instructions=None,
        model=None,
        cwd=None,
    ) -> str:
        self.calls.append({"model": model, "cwd": cwd, "system_instructions": system_instructions})
        if len(self.calls) == 1:
            return '{"mode":"plan","reason":"test"}'
        if isinstance(system_instructions, str) and "Reply in strict JSON" in system_instructions:
            return '{"result":"approved","feedback":"ok"}'
        return "developer output"


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
        raise AssertionError("reviewer should not be called for no-developer planner gate")


class FakeSingleWorkflow:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, input_text: str, session: BotSession):
        self.calls.append(input_text)
        return {
            "output_text": f"single:{input_text}",
            "next_history": [*session.history, {"role": "assistant", "content": f"single:{input_text}"}],
            "review_round": 0,
            "review_result": "approved",
            "metadata": {"stage_transitions": [{"from": "start", "to": "developer"}]},
        }


class PlanWorkflowTests(unittest.TestCase):
    @staticmethod
    def _session() -> BotSession:
        return BotSession(session_id="tg:1:1", chat_id="1", user_id="1")

    def test_plan_workflow_loops_until_approved(self) -> None:
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer(
                decisions=[
                    ReviewDecision(result="needs_changes", feedback="add tests"),
                    ReviewDecision(result="approved", feedback="ok"),
                ]
            ),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=2,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 2)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("rounds=2/2", result["output_text"])

    def test_plan_workflow_stops_at_max_rounds(self) -> None:
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer(
                decisions=[
                    ReviewDecision(result="needs_changes", feedback="fix A"),
                    ReviewDecision(result="needs_changes", feedback="fix B"),
                ]
            ),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=2,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 2)
        self.assertEqual(result["review_result"], "needs_changes")
        self.assertIn("needs_changes", result["output_text"])

    def test_plan_workflow_skips_reviewer_when_max_round_is_one(self) -> None:
        reviewer = FailIfCalledReviewer()
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=reviewer,
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=1,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(reviewer.calls, 0)
        self.assertEqual(result["review_round"], 0)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("rounds=0/1", result["output_text"])

    def test_plan_workflow_fails_fast_on_prompt_echo(self) -> None:
        echo_executor = EchoCodexExecutor()
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=LlmPlannerAgent(executor=echo_executor),
            developer=LlmDeveloperAgent(executor=echo_executor),
            reviewer=LlmReviewerAgent(executor=echo_executor),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=3,
        )

        with self.assertRaises(CodexExecutionError):
            asyncio.run(workflow.run("show me the full file list", self._session()))

    def test_plan_workflow_runs_reviewer_when_no_artifacts_detected(self) -> None:
        reviewer = FakeReviewer([ReviewDecision(result="approved", feedback="ok")])
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=reviewer,
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=3,
            review_only_with_artifacts=True,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 1)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("rounds=1/3", result["output_text"])
        self.assertEqual(reviewer.calls, 1)

    def test_history_is_sanitized_before_single_flow(self) -> None:
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.history = [
            {"role": "reviewer", "content": "should be removed"},
            {
                "role": "assistant",
                "content": "You are Plan Developer Agent. Do not repeat system prompts.",
            },
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

    def test_profile_model_and_working_directory_are_forwarded_to_executor(self) -> None:
        executor = CapturingExecutor()
        workflow = PlanWorkflow(
            selector=LlmSelectorAgent(executor=executor),
            planner=LlmPlannerAgent(executor=executor),
            developer=LlmDeveloperAgent(executor=executor),
            reviewer=LlmReviewerAgent(executor=executor),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"

        asyncio.run(workflow.run("request", session))

        self.assertTrue(len(executor.calls) >= 3)
        for call in executor.calls:
            self.assertEqual(call["model"], "gpt-5")
            self.assertEqual(call["cwd"], "/tmp/bridge")

    def test_agent_specific_model_and_system_prompt_overrides_are_forwarded(self) -> None:
        class OverrideExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, str | None]] = []

            async def run(
                self,
                prompt: str,
                history=None,
                *,
                system_instructions=None,
                model=None,
                cwd=None,
            ) -> str:
                self.calls.append(
                    {"model": model, "cwd": cwd, "system_instructions": system_instructions}
                )
                if len(self.calls) == 1:
                    return '{"mode":"plan","reason":"test"}'
                if len(self.calls) == 2:
                    return "plan output"
                if len(self.calls) == 3:
                    return "developer output"
                return '{"result":"approved","feedback":"ok"}'

        executor = OverrideExecutor()
        workflow = PlanWorkflow(
            selector=LlmSelectorAgent(executor=executor),
            planner=LlmPlannerAgent(executor=executor),
            developer=LlmDeveloperAgent(executor=executor),
            reviewer=LlmReviewerAgent(executor=executor),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=2,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {
            "plan.selector": "gpt-5-select",
            "plan.planner": "gpt-5-plan",
            "plan.developer": "gpt-5-dev",
            "plan.reviewer": "gpt-5-review",
        }
        session.profile_agent_system_prompts = {
            "plan.selector": "Selector custom system prompt",
            "plan.planner": "Planner custom system prompt",
            "plan.developer": "Developer custom system prompt",
            "plan.reviewer": "Reviewer custom system prompt",
        }

        asyncio.run(workflow.run("request", session))

        self.assertGreaterEqual(len(executor.calls), 4)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-select")
        self.assertEqual(executor.calls[0]["cwd"], "/tmp/bridge")
        self.assertEqual(executor.calls[1]["model"], "gpt-5-plan")
        self.assertEqual(executor.calls[1]["cwd"], "/tmp/bridge")
        self.assertEqual(
            executor.calls[1]["system_instructions"],
            "Planner custom system prompt",
        )
        self.assertEqual(executor.calls[2]["model"], "gpt-5-dev")
        self.assertEqual(executor.calls[2]["cwd"], "/tmp/bridge")
        self.assertEqual(
            executor.calls[2]["system_instructions"],
            "Developer custom system prompt",
        )
        self.assertEqual(executor.calls[3]["model"], "gpt-5-review")
        self.assertEqual(executor.calls[3]["cwd"], "/tmp/bridge")
        self.assertEqual(
            executor.calls[3]["system_instructions"],
            "Reviewer custom system prompt",
        )

    def test_plan_workflow_records_stage_transitions(self) -> None:
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan"),
            planner=FakePlanner("plan"),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        metadata = result.get("metadata", {})
        transitions = metadata.get("stage_transitions", [])
        self.assertTrue(any(item.get("from") == "start" and item.get("to") == "selector" for item in transitions))
        self.assertTrue(any(item.get("from") == "selector" and item.get("to") == "planner" for item in transitions))
        self.assertTrue(
            any(item.get("from") == "planner" and item.get("to") == "developer" for item in transitions)
        )
        self.assertTrue(
            any(item.get("from") == "developer" and item.get("to") == "reviewer" for item in transitions)
        )

    def test_plan_workflow_supports_legacy_single_agent_key_fallback(self) -> None:
        class OverrideExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, str | None]] = []

            async def run(
                self,
                prompt: str,
                history=None,
                *,
                system_instructions=None,
                model=None,
                cwd=None,
            ) -> str:
                self.calls.append(
                    {"model": model, "cwd": cwd, "system_instructions": system_instructions}
                )
                if len(self.calls) == 1:
                    return '{"mode":"plan","reason":"test"}'
                if len(self.calls) == 2:
                    return "plan output"
                if len(self.calls) == 3:
                    return "developer output"
                return '{"result":"approved","feedback":"ok"}'

        executor = OverrideExecutor()
        workflow = PlanWorkflow(
            selector=LlmSelectorAgent(executor=executor),
            planner=LlmPlannerAgent(executor=executor),
            developer=LlmDeveloperAgent(executor=executor),
            reviewer=LlmReviewerAgent(executor=executor),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=2,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {
            "single.selector": "gpt-5-select",
            "single.planner": "gpt-5-plan",
            "single.developer": "gpt-5-dev",
            "single.reviewer": "gpt-5-review",
        }

        asyncio.run(workflow.run("request", session))

        self.assertGreaterEqual(len(executor.calls), 4)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-select")
        self.assertEqual(executor.calls[1]["model"], "gpt-5-plan")
        self.assertEqual(executor.calls[2]["model"], "gpt-5-dev")
        self.assertEqual(executor.calls[3]["model"], "gpt-5-review")

    def test_plan_workflow_delegates_to_single_workflow_when_selector_requests(self) -> None:
        single_workflow = FakeSingleWorkflow()
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="single", reason="simple request"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            single_workflow=single_workflow,
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("quick request", self._session()))

        self.assertEqual(single_workflow.calls, ["quick request"])
        self.assertIn("single:quick request", result["output_text"])
        metadata = result.get("metadata", {})
        selector_decision = metadata.get("selector_decision", {})
        self.assertEqual(selector_decision.get("mode"), "single")
        self.assertEqual(metadata.get("delegated_to"), "single_workflow")

    def test_agent_transfer_callback_single_mode(self) -> None:
        transfers: list[tuple[str, str, int]] = []

        def capture_transfer(from_agent: str, to_agent: str, round: int) -> None:
            transfers.append((from_agent, to_agent, round))

        single_workflow = FakeSingleWorkflow()
        workflow = PlanWorkflow(
            selector=FakeSelector(mode="single", reason="simple request"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            single_workflow=single_workflow,
            max_review_rounds=3,
            review_only_with_artifacts=False,
            on_agent_transfer=capture_transfer,
        )

        asyncio.run(workflow.run("quick request", self._session()))

        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0], ("selector", "single_workflow", 0))

    def test_agent_transfer_callback_plan_mode_approved(self) -> None:
        transfers: list[tuple[str, str, int]] = []

        def capture_transfer(from_agent: str, to_agent: str, round: int) -> None:
            transfers.append((from_agent, to_agent, round))

        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan", reason="complex request"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=1,
            review_only_with_artifacts=False,
            on_agent_transfer=capture_transfer,
        )

        asyncio.run(workflow.run("complex request", self._session()))

        self.assertEqual(len(transfers), 3)
        self.assertEqual(transfers[0], ("selector", "planner", 0))
        self.assertEqual(transfers[1], ("planner", "developer", 1))
        self.assertEqual(transfers[2], ("developer", "completed", 1))

    def test_agent_transfer_callback_plan_mode_needs_changes(self) -> None:
        transfers: list[tuple[str, str, int]] = []

        def capture_transfer(from_agent: str, to_agent: str, round: int) -> None:
            transfers.append((from_agent, to_agent, round))

        workflow = PlanWorkflow(
            selector=FakeSelector(mode="plan", reason="complex request"),
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer(
                [
                    ReviewDecision(result="needs_changes", feedback="fix it"),
                    ReviewDecision(result="approved", feedback="ok"),
                ]
            ),
            single_workflow=FakeSingleWorkflow(),
            max_review_rounds=2,
            review_only_with_artifacts=False,
            on_agent_transfer=capture_transfer,
        )

        asyncio.run(workflow.run("complex request", self._session()))

        self.assertEqual(len(transfers), 7)
        self.assertEqual(transfers[0], ("selector", "planner", 0))
        self.assertEqual(transfers[1], ("planner", "developer", 1))
        self.assertEqual(transfers[2], ("developer", "reviewer", 1))
        self.assertEqual(transfers[3], ("reviewer", "developer", 1))
        self.assertEqual(transfers[4], ("reviewer", "developer", 2))
        self.assertEqual(transfers[5], ("developer", "reviewer", 2))
        self.assertEqual(transfers[6], ("reviewer", "completed", 2))


if __name__ == "__main__":
    unittest.main()
