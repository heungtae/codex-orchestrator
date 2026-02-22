from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import TraceRecord

_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(token|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"),
]
_SENSITIVE_KEYS = {"token", "api_key", "apikey", "authorization", "access_token", "refresh_token"}


class TraceLogger:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or (Path.home() / ".codex-orchestrator" / "traces")

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _ensure_base_dir(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._base_dir, 0o700)
        except OSError:
            pass

    @staticmethod
    def _mask_text(value: str) -> str:
        masked = value
        for pattern in _SENSITIVE_PATTERNS:
            if "bearer" in pattern.pattern.lower():
                masked = pattern.sub("Bearer ***", masked)
            else:
                masked = pattern.sub(lambda m: f"{m.group(1)}=***", masked)
        return masked

    def _mask_payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            masked: dict[str, Any] = {}
            for key, current in value.items():
                key_lower = key.lower()
                if key_lower in _SENSITIVE_KEYS:
                    masked[key] = "***"
                else:
                    masked[key] = self._mask_payload(current)
            return masked

        if isinstance(value, list):
            return [self._mask_payload(item) for item in value]

        if isinstance(value, str):
            return self._mask_text(value)

        return value

    def _trace_path(self) -> Path:
        date_part = datetime.now(timezone.utc).date().isoformat()
        return self._base_dir / f"{date_part}.jsonl"

    def append(self, record: TraceRecord) -> None:
        self._ensure_base_dir()
        payload = self._mask_payload(dict(record))
        payload.setdefault(
            "timestamp",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        path = self._trace_path()
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
