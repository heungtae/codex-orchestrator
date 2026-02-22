import tempfile
import unittest
from pathlib import Path

from core.profiles import load_profiles_from_conf


class ProfilesTests(unittest.TestCase):
    def test_load_profiles_from_conf_uses_fallback_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "missing.toml"
            registry = load_profiles_from_conf(
                conf,
                fallback_model="gpt-5",
                fallback_working_directory="/tmp/default",
            )

            profile = registry.default_profile()
            self.assertEqual(profile.name, "default")
            self.assertEqual(profile.model, "gpt-5")
            self.assertEqual(profile.working_directory, "/tmp/default")

    def test_load_profiles_from_conf_parses_profiles_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "conf.toml"
            conf.write_text(
                """
[profile]
default = "bridge"

[profiles.default]
model = "gpt-5"
working_directory = "./default"

[profiles.bridge]
model = "gpt-5-codex"
workingdirectory = "./bridge"
""".strip(),
                encoding="utf-8",
            )

            registry = load_profiles_from_conf(conf)
            default_profile = registry.default_profile()
            bridge = registry.get("bridge")

            self.assertEqual(default_profile.name, "bridge")
            self.assertIsNotNone(bridge)
            assert bridge is not None
            self.assertEqual(bridge.model, "gpt-5-codex")
            self.assertTrue(bridge.working_directory.endswith("/bridge"))

    def test_load_profiles_from_conf_parses_agent_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_file = root / "prompts" / "developer.txt"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text("Developer custom system prompt", encoding="utf-8")

            conf = root / "conf.toml"
            conf.write_text(
                """
[profiles.default]
model = "gpt-5"

[profiles.bridge]
model = "gpt-5-codex"

[agents.single.developer]
model = "gpt-5-codex"
system_prompt_file = "./prompts/developer.txt"

[agents.single.reviewer]
system_prompt = "Reviewer custom prompt"

[agents.multi.frontend.developer]
model = "gpt-5-front"
""".strip(),
                encoding="utf-8",
            )

            registry = load_profiles_from_conf(conf)
            profile = registry.default_profile()
            bridge = registry.get("bridge")

            developer = profile.agent_overrides.get("single.developer")
            reviewer = profile.agent_overrides.get("single.reviewer")
            frontend = profile.agent_overrides.get("multi.frontend.developer")

            self.assertIsNotNone(developer)
            assert developer is not None
            self.assertEqual(developer.model, "gpt-5-codex")
            self.assertEqual(developer.system_prompt, "Developer custom system prompt")

            self.assertIsNotNone(reviewer)
            assert reviewer is not None
            self.assertIsNone(reviewer.model)
            self.assertEqual(reviewer.system_prompt, "Reviewer custom prompt")

            self.assertIsNotNone(frontend)
            assert frontend is not None
            self.assertEqual(frontend.model, "gpt-5-front")

            self.assertIsNotNone(bridge)
            assert bridge is not None
            self.assertIn("single.developer", bridge.agent_overrides)
            self.assertIn("single.reviewer", bridge.agent_overrides)

    def test_system_prompt_file_takes_precedence_over_inline_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_file = root / "prompts" / "planner.txt"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text("Planner prompt from file", encoding="utf-8")

            conf = root / "conf.toml"
            conf.write_text(
                """
[profiles.default]
model = "gpt-5"

[agents.single.planner]
system_prompt = "Inline planner prompt"
system_prompt_file = "./prompts/planner.txt"
""".strip(),
                encoding="utf-8",
            )

            registry = load_profiles_from_conf(conf)
            profile = registry.default_profile()
            planner = profile.agent_overrides.get("single.planner")

            self.assertIsNotNone(planner)
            assert planner is not None
            self.assertEqual(planner.system_prompt, "Planner prompt from file")

    def test_load_profiles_from_conf_with_agents_only_uses_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "conf.toml"
            conf.write_text(
                """
[agents.single.developer]
model = "gpt-5-dev"
system_prompt = "Developer prompt"
""".strip(),
                encoding="utf-8",
            )

            registry = load_profiles_from_conf(
                conf,
                fallback_model="gpt-5",
                fallback_working_directory="/tmp/default",
            )
            default_profile = registry.default_profile()
            developer = default_profile.agent_overrides.get("single.developer")

            self.assertEqual(default_profile.name, "default")
            self.assertEqual(default_profile.model, "gpt-5")
            self.assertEqual(default_profile.working_directory, "/tmp/default")
            self.assertIsNotNone(developer)
            assert developer is not None
            self.assertEqual(developer.model, "gpt-5-dev")
            self.assertEqual(developer.system_prompt, "Developer prompt")


if __name__ == "__main__":
    unittest.main()
