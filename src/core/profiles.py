from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    model: str | None = None
    working_directory: str | None = None


@dataclass(frozen=True)
class ProfileRegistry:
    profiles: dict[str, ExecutionProfile]
    default_name: str = _DEFAULT_PROFILE_NAME

    @classmethod
    def build_default(
        cls,
        *,
        model: str | None = None,
        working_directory: str | None = None,
    ) -> "ProfileRegistry":
        default_profile = ExecutionProfile(
            name=_DEFAULT_PROFILE_NAME,
            model=model,
            working_directory=working_directory,
        )
        return cls(profiles={default_profile.name: default_profile}, default_name=default_profile.name)

    def get(self, name: str | None) -> ExecutionProfile | None:
        if not isinstance(name, str):
            return None
        normalized = name.strip()
        if not normalized:
            return None

        direct = self.profiles.get(normalized)
        if direct is not None:
            return direct

        lowered = normalized.lower()
        for profile_name, profile in self.profiles.items():
            if profile_name.lower() == lowered:
                return profile
        return None

    def default_profile(self) -> ExecutionProfile:
        selected = self.get(self.default_name)
        if selected is not None:
            return selected
        if self.profiles:
            return next(iter(self.profiles.values()))
        return ExecutionProfile(name=_DEFAULT_PROFILE_NAME)


def resolve_conf_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def load_profiles_from_conf(
    conf_path: str | Path,
    *,
    fallback_model: str | None = None,
    fallback_working_directory: str | None = None,
) -> ProfileRegistry:
    path = resolve_conf_path(conf_path)
    if not path.exists():
        return ProfileRegistry.build_default(
            model=fallback_model,
            working_directory=fallback_working_directory,
        )

    payload = _load_toml(path)
    profile_table = payload.get("profile")
    if profile_table is not None and not isinstance(profile_table, dict):
        raise ValueError(f"{path}: [profile] must be a table")

    default_name = _DEFAULT_PROFILE_NAME
    if isinstance(profile_table, dict):
        default_name = _optional_string(
            value=profile_table.get("default"),
            path=path,
            key_name="profile.default",
            default=_DEFAULT_PROFILE_NAME,
        )

    profiles_table = payload.get("profiles")
    if profiles_table is not None and not isinstance(profiles_table, dict):
        raise ValueError(f"{path}: [profiles] must be a table")

    parsed_profiles: dict[str, ExecutionProfile] = {}
    if isinstance(profiles_table, dict):
        for raw_name, raw_profile in profiles_table.items():
            name = str(raw_name).strip()
            if not name:
                raise ValueError(f"{path}: profile name must not be empty")

            if name == "default" and isinstance(raw_profile, str) and raw_profile.strip():
                # Compatible with `[profiles] default = "bridge"` style.
                default_name = raw_profile.strip()
                continue

            if not isinstance(raw_profile, dict):
                raise ValueError(f"{path}: profiles.{name} must be a table")

            model = _optional_string(
                value=raw_profile.get("model"),
                path=path,
                key_name=f"profiles.{name}.model",
                default=None,
            )
            raw_working_directory = raw_profile.get("working_directory")
            if raw_working_directory is None:
                raw_working_directory = raw_profile.get("workingdirectory")

            working_directory = _optional_string(
                value=raw_working_directory,
                path=path,
                key_name=f"profiles.{name}.working_directory",
                default=None,
            )
            resolved_working_directory = _resolve_profile_working_directory(
                path,
                working_directory,
            )
            parsed_profiles[name] = ExecutionProfile(
                name=name,
                model=model,
                working_directory=resolved_working_directory,
            )

    if not parsed_profiles:
        return ProfileRegistry.build_default(
            model=fallback_model,
            working_directory=fallback_working_directory,
        )

    if default_name not in parsed_profiles:
        if _DEFAULT_PROFILE_NAME in parsed_profiles:
            default_name = _DEFAULT_PROFILE_NAME
        else:
            default_name = next(iter(parsed_profiles))

    return ProfileRegistry(profiles=parsed_profiles, default_name=default_name)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except Exception as exc:
        raise RuntimeError("Python 3.11+ is required for conf.toml parsing (tomllib).") from exc

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read {path}: {exc}") from exc

    try:
        loaded = tomllib.loads(raw)
    except Exception as exc:
        raise ValueError(f"failed to parse {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: root must be a table")
    return loaded


def _optional_string(
    *,
    value: Any,
    path: Path,
    key_name: str,
    default: str | None,
) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{path}: {key_name} must be a string")

    cleaned = value.strip()
    if not cleaned:
        return default
    return cleaned


def _resolve_profile_working_directory(conf_path: Path, raw_working_directory: str | None) -> str | None:
    if raw_working_directory is None:
        return None

    candidate = Path(raw_working_directory).expanduser()
    if not candidate.is_absolute():
        candidate = conf_path.parent / candidate
    return str(candidate.resolve())
