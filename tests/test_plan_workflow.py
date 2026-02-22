import asyncio
import unittest

from workflows.plan_workflow import (
    LlmDeveloperAgent,
    LlmPlannerAgent,
    LlmReviewerAgent,
    PlanWorkflow,
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


class FakePlanner:
    def __init__(self, output: str = "1. inspect\n2. implement\n3. validate") -> None:
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
        raise AssertionError("review must be skipped when no implementation artifacts exist")


class FailIfCalledDeveloper:
    async def develop(
        self,
        *,
        user_input: str,
        session: BotSession,
        round_index: int,
        review_feedback: str | None,
    ) -> str:
        raise AssertionError("developer must be skipped by planner gate")


class FakeSingleFallbackWorkflow:
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
            planner=FakePlanner(),
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

    def test_plan_workflow_stops_at_max_rounds(self) -> None:
        workflow = PlanWorkflow(
            planner=FakePlanner(),
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

    def test_plan_workflow_fails_fast_on_prompt_echo(self) -> None:
        echo_executor = EchoCodexExecutor()
        workflow = PlanWorkflow(
            planner=LlmPlannerAgent(executor=echo_executor),
            developer=LlmDeveloperAgent(executor=echo_executor),
            reviewer=LlmReviewerAgent(executor=echo_executor),
            max_review_rounds=3,
        )

        with self.assertRaises(CodexExecutionError):
            asyncio.run(workflow.run("show me the full file list", self._session()))

    def test_plan_workflow_skips_review_when_no_artifacts_detected(self) -> None:
        reviewer = FailIfCalledReviewer()
        workflow = PlanWorkflow(
            planner=FakePlanner(),
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
        workflow = PlanWorkflow(
            planner=FakePlanner(),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
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
            planner=LlmPlannerAgent(executor=executor),
            developer=LlmDeveloperAgent(executor=executor),
            reviewer=LlmReviewerAgent(executor=executor),
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
                    return "plan output"
                if len(self.calls) == 2:
                    return "developer output"
                return '{"result":"approved","feedback":"ok"}'

        executor = OverrideExecutor()
        workflow = PlanWorkflow(
            planner=LlmPlannerAgent(executor=executor),
            developer=LlmDeveloperAgent(executor=executor),
            reviewer=LlmReviewerAgent(executor=executor),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {
            "plan.planner": "gpt-5-plan",
            "plan.developer": "gpt-5-dev",
            "plan.reviewer": "gpt-5-review",
        }
        session.profile_agent_system_prompts = {
            "plan.planner": "Planner custom system prompt",
            "plan.developer": "Developer custom system prompt",
            "plan.reviewer": "Reviewer custom system prompt",
        }

        asyncio.run(workflow.run("request", session))

        self.assertGreaterEqual(len(executor.calls), 3)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-plan")
        self.assertEqual(executor.calls[0]["cwd"], "/tmp/bridge")
        self.assertEqual(
            executor.calls[0]["system_instructions"],
            "Planner custom system prompt",
        )
        self.assertEqual(executor.calls[1]["model"], "gpt-5-dev")
        self.assertEqual(executor.calls[1]["cwd"], "/tmp/bridge")
        self.assertEqual(
            executor.calls[1]["system_instructions"],
            "Developer custom system prompt",
        )
        self.assertEqual(executor.calls[2]["model"], "gpt-5-review")
        self.assertEqual(executor.calls[2]["cwd"], "/tmp/bridge")
        self.assertEqual(
            executor.calls[2]["system_instructions"],
            "Reviewer custom system prompt",
        )

    def test_plan_workflow_records_stage_transitions(self) -> None:
        workflow = PlanWorkflow(
            planner=FakePlanner("plan"),
            developer=FakeDeveloper(),
            reviewer=FakeReviewer([ReviewDecision(result="approved", feedback="ok")]),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        metadata = result.get("metadata", {})
        transitions = metadata.get("stage_transitions", [])
        self.assertTrue(any(item.get("from") == "start" and item.get("to") == "planner" for item in transitions))
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
                    return "plan output"
                if len(self.calls) == 2:
                    return "developer output"
                return '{"result":"approved","feedback":"ok"}'

        executor = OverrideExecutor()
        workflow = PlanWorkflow(
            planner=LlmPlannerAgent(executor=executor),
            developer=LlmDeveloperAgent(executor=executor),
            reviewer=LlmReviewerAgent(executor=executor),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )
        session = self._session()
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {
            "single.planner": "gpt-5-plan",
            "single.developer": "gpt-5-dev",
            "single.reviewer": "gpt-5-review",
        }

        asyncio.run(workflow.run("request", session))

        self.assertGreaterEqual(len(executor.calls), 3)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-plan")
        self.assertEqual(executor.calls[1]["model"], "gpt-5-dev")
        self.assertEqual(executor.calls[2]["model"], "gpt-5-review")

    def test_plan_workflow_can_skip_developer_by_planner_gate(self) -> None:
        planner_json = (
            '{'
            '"need_planner_handoff": true,'
            '"need_developer": false,'
            '"need_reviewer": true,'
            '"reason": "question only",'
            '"execution_handoff": "No code change required."'
            '}'
        )
        workflow = PlanWorkflow(
            planner=FakePlanner(planner_json),
            developer=FailIfCalledDeveloper(),
            reviewer=FailIfCalledReviewer(),
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("answer only", self._session()))

        self.assertEqual(result["review_round"], 0)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("No code change required.", result["output_text"])
        self.assertIn("rounds=0/3", result["output_text"])

    def test_plan_workflow_can_skip_reviewer_by_planner_gate(self) -> None:
        planner_json = (
            '{'
            '"need_planner_handoff": true,'
            '"need_developer": true,'
            '"need_reviewer": false,'
            '"reason": "simple implementation",'
            '"execution_handoff": "Do it directly."'
            '}'
        )
        reviewer = FailIfCalledReviewer()
        workflow = PlanWorkflow(
            planner=FakePlanner(planner_json),
            developer=FakeDeveloper(),
            reviewer=reviewer,
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(result["review_round"], 1)
        self.assertEqual(result["review_result"], "approved")
        self.assertIn("rounds=1/3", result["output_text"])
        self.assertEqual(reviewer.calls, 0)

    def test_plan_workflow_uses_single_fallback_for_simple_request(self) -> None:
        planner_json = (
            '{'
            '"use_single_agent": true,'
            '"need_planner_handoff": false,'
            '"need_developer": true,'
            '"need_reviewer": false,'
            '"reason": "simple request",'
            '"execution_handoff": ""'
            '}'
        )
        single_fallback = FakeSingleFallbackWorkflow()
        workflow = PlanWorkflow(
            planner=FakePlanner(planner_json),
            developer=FailIfCalledDeveloper(),
            reviewer=FailIfCalledReviewer(),
            single_fallback_workflow=single_fallback,
            max_review_rounds=3,
            review_only_with_artifacts=False,
        )

        result = asyncio.run(workflow.run("quick request", self._session()))

        self.assertEqual(single_fallback.calls, ["quick request"])
        self.assertEqual(result["output_text"], "single:quick request")
        self.assertEqual(result["review_round"], 0)
        self.assertEqual(result["review_result"], "approved")
        metadata = result.get("metadata", {})
        gate = metadata.get("planner_gate", {})
        self.assertEqual(gate.get("use_single_agent"), True)
        self.assertEqual(metadata.get("delegated_to"), "single.developer")


if __name__ == "__main__":
    unittest.main()
