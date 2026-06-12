from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CopilotTurn:
    conversation_id: str
    client_session_id: str
    is_start_of_session: bool


@dataclass
class PersistentSession:
    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    turn_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    label: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reserve_turn(self) -> CopilotTurn:
        turn = CopilotTurn(
            conversation_id=self.conversation_id,
            client_session_id=self.client_session_id,
            is_start_of_session=self.turn_count == 0,
        )
        self.turn_count += 1
        self.last_used = time.time()
        return turn


class PersistentSessionStore:
    """Maps a key -> PersistentSession. When db_path is given, the conversation mapping is
    persisted to SQLite so chats survive proxy restarts; otherwise it is purely in-memory."""

    def __init__(self, db_path: str | None = None):
        self._lock = threading.RLock()
        self._cache: dict[str, PersistentSession] = {}
        self._db_path = str(db_path) if db_path else None
        if self._db_path:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS sessions ("
                    "key TEXT PRIMARY KEY, conversation_id TEXT, client_session_id TEXT, "
                    "created_at REAL, last_used REAL, turn_count INTEGER, label TEXT)"
                )

    def get(self, key: str) -> PersistentSession:
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            session = self._load(key) or PersistentSession()
            self._cache[key] = session
            if self._db_path:
                self._save(key, session)
            return session

    def persist(self, key: str, session: PersistentSession) -> None:
        if not self._db_path:
            return
        with self._lock:
            self._save(key, session)

    def items(self) -> list[tuple[str, PersistentSession]]:
        with self._lock:
            return list(self._cache.items())

    def find(self, key: str) -> PersistentSession | None:
        """Read a session WITHOUT creating it (unlike get). Returns None if unknown."""
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            session = self._load(key)
            if session is not None:
                self._cache[key] = session
            return session

    def create(self, key: str, label: str = "") -> PersistentSession:
        """Create (or overwrite) a session under key with a fresh conversation."""
        with self._lock:
            session = PersistentSession(label=label)
            self._cache[key] = session
            if self._db_path:
                self._save(key, session)
            return session

    def update(
        self, key: str, label: str | None = None, rotate: bool = False
    ) -> PersistentSession | None:
        """Mutate an existing session: set its label and/or rotate to a fresh substrate
        conversation (new ids, turn_count reset so the next message starts a new chat).
        Returns None if the key is unknown."""
        with self._lock:
            session = self.find(key)
            if session is None:
                return None
            if label is not None:
                session.label = label
            if rotate:
                session.conversation_id = str(uuid.uuid4())
                session.client_session_id = str(uuid.uuid4())
                session.turn_count = 0
            session.last_used = time.time()
            if self._db_path:
                self._save(key, session)
            return session

    def delete(self, key: str) -> bool:
        """Forget a session (cache + DB). Returns True if it existed."""
        with self._lock:
            existed = self._cache.pop(key, None) is not None
            if self._db_path:
                with sqlite3.connect(self._db_path) as conn:
                    cur = conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
                    existed = existed or cur.rowcount > 0
            return existed

    def _load(self, key: str) -> PersistentSession | None:
        if not self._db_path:
            return None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT conversation_id, client_session_id, created_at, turn_count, label "
                "FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return PersistentSession(
            conversation_id=row[0],
            client_session_id=row[1],
            created_at=row[2] or time.time(),
            # >= 1 so a resumed conversation is not flagged isStartOfSession again.
            turn_count=max(int(row[3] or 0), 1),
            label=row[4] or "",
        )

    def _save(self, key: str, s: PersistentSession) -> None:
        assert self._db_path is not None
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(key, conversation_id, client_session_id, created_at, last_used, turn_count, label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, s.conversation_id, s.client_session_id, s.created_at, s.last_used, s.turn_count, s.label),
            )
