import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import build_orchestrator


class MainBuildOrchestratorTests(unittest.TestCase):
    @staticmethod
    def _executor_of(orchestrator):
        return getattr(getattr(orchestrator.single_workflow, "developer"), "_executor")

    def test_direct_status_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CODEX_CONF_PATH": str(Path(tmp) / "conf.toml")}
            with patch.dict("os.environ", env, clear=True):
                orchestrator = build_orchestrator()

        self.assertIsNone(orchestrator.codex_mcp.status_command)
        self.assertFalse(orchestrator.codex_mcp.auto_detect_process)
        executor = self._executor_of(orchestrator)
        self.assertEqual(executor.approval_policy, "never")
        self.assertEqual(executor.sandbox, "danger-full-access")

    def test_external_status_mode_can_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf_path = Path(tmp) / "conf.toml"
            conf_path.write_text(
                """
[codex]
approval_policy = "on-request"
sandbox = "workspace-write"
mcp_direct_status = false
mcp_status_cmd = "echo running=true"
mcp_auto_detect_process = true
""".strip(),
                encoding="utf-8",
            )
            env = {"CODEX_CONF_PATH": str(conf_path)}
            with patch.dict("os.environ", env, clear=True):
                orchestrator = build_orchestrator()

        self.assertEqual(orchestrator.codex_mcp.status_command, ["echo", "running=true"])
        self.assertTrue(orchestrator.codex_mcp.auto_detect_process)
        executor = self._executor_of(orchestrator)
        self.assertEqual(executor.approval_policy, "on-request")
        self.assertEqual(executor.sandbox, "workspace-write")


if __name__ == "__main__":
    unittest.main()
