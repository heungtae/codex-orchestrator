from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True)
class AgentProfile:
    model: str | None = None
    system_prompt: str | None = None


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    model: str | None = None
    working_directory: str | None = None
    agent_overrides: dict[str, AgentProfile] = field(default_factory=dict)


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
        agent_overrides: dict[str, AgentProfile] | None = None,
    ) -> "ProfileRegistry":
        default_profile = ExecutionProfile(
            name=_DEFAULT_PROFILE_NAME,
            model=model,
            working_directory=working_directory,
            agent_overrides=dict(agent_overrides or {}),
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
    global_agents = _parse_agents_table(
        conf_path=path,
        raw_agents=payload.get("agents"),
        table_key="agents",
    )
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
            raw_agents = raw_profile.get("agents")
            profile_agents = _parse_agents_table(
                conf_path=path,
                raw_agents=raw_agents,
                table_key=f"profiles.{name}.agents",
            )
            merged_agents = dict(global_agents)
            merged_agents.update(profile_agents)
            parsed_profiles[name] = ExecutionProfile(
                name=name,
                model=model,
                working_directory=resolved_working_directory,
                agent_overrides=merged_agents,
            )

    if not parsed_profiles:
        return ProfileRegistry.build_default(
            model=fallback_model,
            working_directory=fallback_working_directory,
            agent_overrides=global_agents,
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


_AGENT_CONFIG_KEYS = {"model", "system_prompt", "system_prompt_file", "system_prompt_path"}


def _parse_agents_table(
    *,
    conf_path: Path,
    raw_agents: dict[str, Any] | None,
    table_key: str,
) -> dict[str, AgentProfile]:
    if raw_agents is None:
        return {}
    if not isinstance(raw_agents, dict):
        raise ValueError(f"{conf_path}: [{table_key}] must be a table")

    parsed: dict[str, AgentProfile] = {}
    _collect_agents(
        conf_path=conf_path,
        node=raw_agents,
        table_key=table_key,
        path_parts=[],
        parsed=parsed,
    )
    return parsed


def _collect_agents(
    *,
    conf_path: Path,
    node: dict[str, Any],
    table_key: str,
    path_parts: list[str],
    parsed: dict[str, AgentProfile],
) -> None:
    has_leaf_setting = False
    child_tables: list[tuple[str, dict[str, Any]]] = []

    for raw_key, raw_value in node.items():
        key = str(raw_key).strip()
        if not key:
            raise ValueError(f"{conf_path}: {table_key} contains empty key")
        lowered_key = key.lower()
        if lowered_key in _AGENT_CONFIG_KEYS:
            has_leaf_setting = True
            continue
        if not isinstance(raw_value, dict):
            dotted = ".".join(path_parts).strip()
            location = f"{table_key}.{dotted}" if dotted else table_key
            raise ValueError(f"{conf_path}: {location}.{lowered_key} must be a table")
        child_tables.append((lowered_key, raw_value))

    if has_leaf_setting:
        if child_tables:
            dotted = ".".join(path_parts).strip()
            location = f"{table_key}.{dotted}" if dotted else table_key
            raise ValueError(
                f"{conf_path}: {location} cannot mix model/prompt keys with nested agent tables"
            )
        if not path_parts:
            raise ValueError(f"{conf_path}: [{table_key}] must define nested agent tables")

        agent_key = ".".join(path_parts)
        agent_profile = _parse_agent_profile_leaf(
            conf_path=conf_path,
            table_key=table_key,
            agent_key=agent_key,
            raw_leaf=node,
        )
        if agent_profile is not None:
            parsed[agent_key] = agent_profile
        return

    for child_name, child_node in child_tables:
        next_parts = [*path_parts, child_name]
        _collect_agents(
            conf_path=conf_path,
            node=child_node,
            table_key=table_key,
            path_parts=next_parts,
            parsed=parsed,
        )


def _parse_agent_profile_leaf(
    *,
    conf_path: Path,
    table_key: str,
    agent_key: str,
    raw_leaf: dict[str, Any],
) -> AgentProfile | None:
    normalized_leaf = {str(k).strip().lower(): v for k, v in raw_leaf.items()}
    full_key = f"{table_key}.{agent_key}"
    model = _optional_string(
        value=normalized_leaf.get("model"),
        path=conf_path,
        key_name=f"{full_key}.model",
        default=None,
    )
    inline_prompt = _optional_string(
        value=normalized_leaf.get("system_prompt"),
        path=conf_path,
        key_name=f"{full_key}.system_prompt",
        default=None,
    )
    prompt_file = normalized_leaf.get("system_prompt_file")
    if prompt_file is None:
        prompt_file = normalized_leaf.get("system_prompt_path")
    prompt_file = _optional_string(
        value=prompt_file,
        path=conf_path,
        key_name=f"{full_key}.system_prompt_file",
        default=None,
    )
    system_prompt = _resolve_agent_system_prompt(
        conf_path=conf_path,
        full_key=full_key,
        inline_prompt=inline_prompt,
        prompt_file=prompt_file,
    )
    if model is None and system_prompt is None:
        return None
    return AgentProfile(model=model, system_prompt=system_prompt)


def _resolve_agent_system_prompt(
    *,
    conf_path: Path,
    full_key: str,
    inline_prompt: str | None,
    prompt_file: str | None,
) -> str | None:
    if prompt_file is None:
        return inline_prompt

    candidate = Path(prompt_file).expanduser()
    if not candidate.is_absolute():
        candidate = conf_path.parent / candidate
    resolved = candidate.resolve()

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            (
                f"{conf_path}: failed to read "
                f"{full_key}.system_prompt_file "
                f"({resolved}): {exc}"
            )
        ) from exc

    cleaned = raw.strip()
    if not cleaned:
        raise ValueError(
            (
                f"{conf_path}: {full_key}.system_prompt_file "
                f"points to empty file: {resolved}"
            )
        )
    return cleaned
