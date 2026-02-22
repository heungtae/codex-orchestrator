import asyncio
import unittest

from core.models import BotSession
from workflows.single_agent_workflow import LlmSingleDeveloperAgent, SingleAgentWorkflow


class FakeDeveloper:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None, str]] = []

    async def develop(
        self,
        *,
        user_input: str,
        session: BotSession,
        round_index: int,
        review_feedback: str | None,
    ) -> str:
        self.calls.append((round_index, review_feedback, user_input))
        return "implemented"


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
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "cwd": cwd,
                "system_instructions": system_instructions,
            }
        )
        return "developer output"


class PromptLikeExecutor:
    async def run(
        self,
        prompt: str,
        history=None,
        *,
        system_instructions=None,
        model=None,
        cwd=None,
    ) -> str:
        del prompt, history, system_instructions, model, cwd
        return "You are Developer Agent. Return only the improved developer response."


class SingleWorkflowTests(unittest.TestCase):
    @staticmethod
    def _session() -> BotSession:
        return BotSession(session_id="tg:1:1", chat_id="1", user_id="1")

    def test_single_workflow_executes_developer_once(self) -> None:
        developer = FakeDeveloper()
        workflow = SingleAgentWorkflow(developer=developer)

        result = asyncio.run(workflow.run("request", self._session()))

        self.assertEqual(len(developer.calls), 1)
        self.assertEqual(developer.calls[0], (1, None, "request"))
        self.assertEqual(result["output_text"], "implemented")
        self.assertEqual(result["review_round"], 0)
        self.assertEqual(result["review_result"], "approved")
        self.assertNotIn("[plan-review]", result["output_text"])

    def test_single_workflow_fails_fast_on_prompt_echo(self) -> None:
        workflow = SingleAgentWorkflow(developer=LlmSingleDeveloperAgent(executor=PromptLikeExecutor()))

        with self.assertRaisesRegex(Exception, "prompt-like developer output"):
            asyncio.run(workflow.run("show me the full file list", self._session()))

    def test_history_is_sanitized_before_single_flow(self) -> None:
        workflow = SingleAgentWorkflow(developer=FakeDeveloper())
        session = self._session()
        session.history = [
            {"role": "reviewer", "content": "should be removed"},
            {
                "role": "assistant",
                "content": "You are Developer Agent. Return only the improved developer response.",
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
        workflow = SingleAgentWorkflow(developer=LlmSingleDeveloperAgent(executor=executor))
        session = self._session()
        session.profile_model = "gpt-5"
        session.profile_working_directory = "/tmp/bridge"

        asyncio.run(workflow.run("request", session))

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["model"], "gpt-5")
        self.assertEqual(executor.calls[0]["cwd"], "/tmp/bridge")

    def test_agent_specific_model_and_prompt_overrides_are_forwarded(self) -> None:
        executor = CapturingExecutor()
        workflow = SingleAgentWorkflow(developer=LlmSingleDeveloperAgent(executor=executor))
        session = self._session()
        session.profile_model = "gpt-5-default"
        session.profile_working_directory = "/tmp/bridge"
        session.profile_agent_models = {"single.developer": "gpt-5-dev"}
        session.profile_agent_system_prompts = {"single.developer": "Developer custom prompt"}

        asyncio.run(workflow.run("request", session))

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0]["model"], "gpt-5-dev")
        self.assertEqual(executor.calls[0]["system_instructions"], "Developer custom prompt")

    def test_generic_developer_override_is_used_as_fallback(self) -> None:
        executor = CapturingExecutor()
        workflow = SingleAgentWorkflow(developer=LlmSingleDeveloperAgent(executor=executor))
        session = self._session()
        session.profile_model = "gpt-5-default"
        session.profile_agent_models = {"developer": "gpt-5-generic"}

        asyncio.run(workflow.run("request", session))

        self.assertEqual(executor.calls[0]["model"], "gpt-5-generic")


if __name__ == "__main__":
    unittest.main()
