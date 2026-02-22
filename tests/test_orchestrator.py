import asyncio
import tempfile
import unittest
from pathlib import Path

from core.command_router import CommandRouter
from core.orchestrator import BotOrchestrator
from core.profiles import ExecutionProfile, ProfileRegistry
from core.session_manager import SessionManager
from core.trace_logger import TraceLogger
from integrations.codex_executor import CodexExecutionError
from integrations.codex_mcp import CodexMcpServer


class FakeSingleWorkflow:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, input_text, session):
        self.calls += 1
        return {
            "output_text": f"single:{input_text}",
            "next_history": [*session.history, {"role": "assistant", "content": input_text}],
            "review_round": 2,
            "review_result": "approved",
        }


class FakeMultiWorkflow:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, input_text, session):
        self.calls += 1
        return {
            "output_text": f"multi:{input_text}",
            "next_history": session.history,
        }


class FailingWorkflow:
    async def run(self, input_text, session):
        raise CodexExecutionError("executor returned prompt-like output")


class OrchestratorTests(unittest.TestCase):
    @staticmethod
    def _build(tmp_path: Path) -> BotOrchestrator:
        mcp = CodexMcpServer()
        mcp.mark_running(pid=12345, ready=True)
        profiles = ProfileRegistry(
            profiles={
                "default": ExecutionProfile(name="default", model="gpt-5", working_directory="/tmp/default"),
                "bridge": ExecutionProfile(name="bridge", model="gpt-5", working_directory="/tmp/bridge"),
            },
            default_name="default",
        )
        return BotOrchestrator(
            router=CommandRouter(),
            session_manager=SessionManager(base_dir=tmp_path / "sessions"),
            trace_logger=TraceLogger(base_dir=tmp_path / "traces"),
            single_workflow=FakeSingleWorkflow(),
            multi_workflow=FakeMultiWorkflow(),
            codex_mcp=mcp,
            profile_registry=profiles,
        )

    def test_default_mode_is_single_and_status_includes_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._build(Path(tmp))

            output = asyncio.run(orchestrator.handle_message("1", "2", "add a textbox to the file"))
            self.assertTrue(output.startswith("single:"))

            status = asyncio.run(orchestrator.handle_message("1", "2", "/status"))
            self.assertIn("mode: single", status)
            self.assertIn("profile: default, model=gpt-5, working_directory=/tmp/default", status)
            self.assertIn("single_review: rounds=2/3, result=approved", status)
            self.assertIn("codex_mcp: running=true, ready=true, pid=12345", status)

    def test_start_command_includes_session_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._build(Path(tmp))
            orchestrator.working_directory = tmp

            output = asyncio.run(orchestrator.handle_message("1", "2", "/start"))
            self.assertIn("/profile list|<name>", output)
            self.assertIn(f"session_working_directory: {Path(tmp).resolve()}", output)

    def test_new_command_resets_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._build(Path(tmp))

            asyncio.run(orchestrator.handle_message("1", "2", "/mode multi"))
            asyncio.run(orchestrator.handle_message("1", "2", "/profile bridge"))
            status_before = asyncio.run(orchestrator.handle_message("1", "2", "/status"))
            self.assertIn("mode: multi", status_before)
            self.assertIn("profile: bridge", status_before)

            reset_output = asyncio.run(orchestrator.handle_message("1", "2", "/new"))
            self.assertIn("mode=single", reset_output)

            status_after = asyncio.run(orchestrator.handle_message("1", "2", "/status"))
            self.assertIn("mode: single", status_after)
            self.assertIn("profile: default", status_after)

    def test_codex_literal_is_forwarded_to_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._build(Path(tmp))
            response = asyncio.run(orchestrator.handle_message("1", "2", "/codex"))
            self.assertEqual(response, "single:/codex")

    def test_codex_execution_error_returns_configuration_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mcp = CodexMcpServer()
            orchestrator = BotOrchestrator(
                router=CommandRouter(),
                session_manager=SessionManager(base_dir=Path(tmp) / "sessions"),
                trace_logger=TraceLogger(base_dir=Path(tmp) / "traces"),
                single_workflow=FailingWorkflow(),
                multi_workflow=FakeMultiWorkflow(),
                codex_mcp=mcp,
            )

            response = asyncio.run(orchestrator.handle_message("1", "2", "test"))
            self.assertIn("CODEX_MCP_COMMAND", response)
            self.assertIn("detail:", response)

    def test_profile_list_and_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._build(Path(tmp))

            listed = asyncio.run(orchestrator.handle_message("1", "2", "/profile list"))
            self.assertIn("profiles:", listed)
            self.assertIn("* default (default): model=gpt-5, working_directory=/tmp/default", listed)
            self.assertIn("- bridge: model=gpt-5, working_directory=/tmp/bridge", listed)

            switched = asyncio.run(orchestrator.handle_message("1", "2", "/profile bridge"))
            self.assertIn("profile set to bridge", switched)
            self.assertIn("working_directory: /tmp/bridge", switched)

            status = asyncio.run(orchestrator.handle_message("1", "2", "/status"))
            self.assertIn("profile: bridge, model=gpt-5, working_directory=/tmp/bridge", status)

    def test_profile_usage_and_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = self._build(Path(tmp))

            usage = asyncio.run(orchestrator.handle_message("1", "2", "/profile"))
            self.assertEqual(usage, "usage: /profile list|<name>")

            not_found = asyncio.run(orchestrator.handle_message("1", "2", "/profile unknown"))
            self.assertIn("profile not found: unknown", not_found)


if __name__ == "__main__":
    unittest.main()
