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


if __name__ == "__main__":
    unittest.main()
