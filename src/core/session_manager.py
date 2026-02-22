from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from core.models import BotSession


class SessionManager:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir or (Path.home() / ".codex-orchestrator" / "sessions")
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @staticmethod
    def session_id(chat_id: str | int, user_id: str | int) -> str:
        return f"tg:{chat_id}:{user_id}"

    def _session_path(self, chat_id: str | int, user_id: str | int) -> Path:
        return self._base_dir / f"{chat_id}-{user_id}.json"

    def _new_session(self, chat_id: str | int, user_id: str | int) -> BotSession:
        return BotSession(
            session_id=self.session_id(chat_id=chat_id, user_id=user_id),
            chat_id=str(chat_id),
            user_id=str(user_id),
            mode="single",
            history=[],
            run_lock=False,
        )

    def _ensure_base_dir(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._base_dir, 0o700)
        except OSError:
            pass

    @asynccontextmanager
    async def lock(self, chat_id: str | int, user_id: str | int):
        key = f"{chat_id}:{user_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    async def load(self, chat_id: str | int, user_id: str | int) -> BotSession:
        self._ensure_base_dir()
        path = self._session_path(chat_id=chat_id, user_id=user_id)
        if not path.exists():
            return self._new_session(chat_id=chat_id, user_id=user_id)

        try:
            with path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            session = BotSession.from_dict(payload)
            session.run_lock = False
            return session
        except (json.JSONDecodeError, KeyError, OSError, ValueError):
            session = self._new_session(chat_id=chat_id, user_id=user_id)
            session.last_error = "session_load_failed"
            return session

    async def save(self, session: BotSession) -> None:
        self._ensure_base_dir()
        session.touch()
        path = self._session_path(chat_id=session.chat_id, user_id=session.user_id)
        tmp_path = path.with_name(f"{path.name}.tmp")

        payload = session.to_dict()
        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    async def reset(self, chat_id: str | int, user_id: str | int) -> BotSession:
        session = self._new_session(chat_id=chat_id, user_id=user_id)
        await self.save(session)
        return session
