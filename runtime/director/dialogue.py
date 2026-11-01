"""Bounded, durable, *local* two-way dialogue with an injected reasoning engine.

This is an explicitly opened same-host conversation journal. It neither starts
a background service nor opens a network port. A reasoning engine can interpret
declared world facts, goals, and prior turns; it is not given executable tools.
Speaker IDs and supplied observations are local claims, not authentication.
The SQLite database is plaintext and must not be fed secrets by default.

``ask`` persists a user message before invoking the reasoner. If reasoning
fails or the program crashes, ``resume`` may complete that pending message
after restart; no duplicate reply is written. A reply is generated text,
never permission to alter the mission or operate a host.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .strategy import generate_candidates


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_STATUS = re.compile(r"[A-Z][A-Z0-9_]{0,39}\Z")
_MAX_QUESTION = 2048
_MAX_ANSWER = 2048
_MAX_WORLD_BYTES = 128 * 1024
_MAX_GOAL_BYTES = 16 * 1024
_MAX_ALTERNATIVES_BYTES = 32 * 1024
_MAX_THREADS = 64
_MAX_MESSAGES_PER_THREAD = 256
_HISTORY_WINDOW = 40


def _identifier(raw: object, label: str) -> str:
    if not isinstance(raw, str) or _ID.fullmatch(raw) is None:
        raise ValueError(f"{label} must be a bounded identifier")
    return raw


def _text(raw: object, label: str, limit: int) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > limit:
        raise ValueError(f"{label} must be nonempty text of at most {limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in raw):
        raise ValueError(f"{label} contains unsupported control characters")
    return raw


def _json(raw: object, label: str, limit: int) -> str:
    try:
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be strictly JSON-compatible") from exc
    if len(encoded.encode("ascii")) > limit:
        raise ValueError(f"{label} exceeds size limit")
    return encoded


def _utc() -> str:
    # A locally supplied clock is for ordering convenience only; chronology is
    # determined by the SQLite sequence and timestamps are not authenticated.
    return datetime.now(timezone.utc).isoformat()


class DirectorDialogue:
    """Persistent session wrapper around a provided synchronous local reasoner.

    The reasoner must provide ``respond(prompt, *, context) -> dict`` with a
    bounded textual ``answer`` and optional ``status/basis/uncertainty``.
    The caller owns the goal configuration, filesystem, and time of invocation.
    No model or agent process is installed or launched automatically.
    """

    def __init__(self, path: str | Path, reasoner: object):
        if not hasattr(reasoner, "respond") or not callable(reasoner.respond):
            raise TypeError("reasoner must have a callable respond(prompt, *, context)")
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable local dialogue SQLite path is required")
        self.path = Path(path)
        self.reasoner = reasoner
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("dialogue path must have an existing parent and not be a symlink")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("existing dialogue path must be a regular file")
        self._assert_private_files()
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("dialogue path cannot be a symlink")
            self._assert_private_files()
        else:
            os.close(descriptor)
        self._conn: sqlite3.Connection | None = sqlite3.connect(self.path, timeout=5.0,
                                                                 isolation_level=None)
        try:
            conn = self._connection()
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""CREATE TABLE IF NOT EXISTS director_dialogue_threads (
                thread_id TEXT PRIMARY KEY,
                goal_json TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS director_dialogue_messages (
                thread_id TEXT NOT NULL REFERENCES director_dialogue_threads(thread_id),
                seq INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('USER','DIRECTOR')),
                speaker_id TEXT NOT NULL,
                content TEXT NOT NULL,
                reply_to_seq INTEGER,
                status TEXT NOT NULL,
                basis_json TEXT NOT NULL,
                uncertainty TEXT NOT NULL,
                world_revision INTEGER,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(thread_id,seq),
                UNIQUE(thread_id,reply_to_seq)
            )""")
        except BaseException:
            self.close()
            raise

    def _assert_private_files(self) -> None:
        # The transcript can hold personal text. Existing files must already
        # be private, including journal/lock sidecars. Do not silently chmod
        # an operator-owned file or follow a symlink to another location.
        for item in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if item.is_symlink():
                raise ValueError("dialogue SQLite paths cannot be symlinks")
            if item.exists():
                if not item.is_file():
                    raise ValueError("dialogue SQLite paths must be regular files")
                if os.name == "posix" and item.stat().st_mode & 0o077:
                    raise ValueError("dialogue SQLite files must be private (mode 0600 or stricter)")

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DirectorDialogue is closed")
        return self._conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "DirectorDialogue":
        self._connection()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def configure_thread(self, thread_id: str, goal: Mapping[str, object]) -> dict[str, object]:
        thread_id = _identifier(thread_id, "thread_id")
        if not isinstance(goal, Mapping):
            raise ValueError("goal must be a mapping")
        # The strategy validator keeps this objective compatible with local
        # strategic analysis; no free-form prompt can modify it afterwards.
        generate_candidates(goal, [], {"revision": 0, "facts": {}}, max_candidates=1)
        goal_json = _json(dict(goal), "goal", _MAX_GOAL_BYTES)
        with self._transaction() as conn:
            row = conn.execute("SELECT goal_json FROM director_dialogue_threads WHERE thread_id=?",
                               (thread_id,)).fetchone()
            if row is None:
                count = conn.execute("SELECT COUNT(*) FROM director_dialogue_threads").fetchone()[0]
                if count >= _MAX_THREADS:
                    raise ValueError("dialogue thread capacity exhausted")
                conn.execute("""INSERT INTO director_dialogue_threads(thread_id,goal_json,created_at_utc)
                    VALUES(?,?,?)""", (thread_id, goal_json, _utc()))
            elif row["goal_json"] != goal_json:
                raise ValueError("thread goal is immutable; start a new thread to change it")
        return {"thread_id": thread_id, "goal": json.loads(goal_json),
                "sender_authenticated": False, "effect_authority": "NOT_GRANTED"}

    def set_paused(self, thread_id: str, paused: bool) -> None:
        thread_id = _identifier(thread_id, "thread_id")
        if type(paused) is not bool:
            raise ValueError("paused must be bool")
        with self._transaction() as conn:
            cursor = conn.execute("UPDATE director_dialogue_threads SET paused=? WHERE thread_id=?",
                                  (int(paused), thread_id))
            if cursor.rowcount != 1:
                raise KeyError(thread_id)

    def _thread(self, conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
        thread = conn.execute("SELECT * FROM director_dialogue_threads WHERE thread_id=?",
                              (thread_id,)).fetchone()
        if thread is None:
            raise KeyError(thread_id)
        return thread

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, object]:
        return {"thread_id": row["thread_id"], "seq": row["seq"],
                "role": row["role"], "speaker_id": row["speaker_id"],
                "content": row["content"], "reply_to_seq": row["reply_to_seq"],
                "status": row["status"], "basis": json.loads(row["basis_json"]),
                "uncertainty": row["uncertainty"], "world_revision": row["world_revision"],
                "created_at_utc": row["created_at_utc"],
                "sender_authenticated": False, "effect_authority": "NOT_GRANTED",
                "effect_status": "NOT_EXECUTED"}

    def history(self, thread_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        thread_id = _identifier(thread_id, "thread_id")
        if type(limit) is not int or not 1 <= limit <= _MAX_MESSAGES_PER_THREAD:
            raise ValueError("history limit is invalid")
        conn = self._connection()
        self._thread(conn, thread_id)
        return [self._message(row) for row in conn.execute("""SELECT * FROM (
            SELECT * FROM director_dialogue_messages WHERE thread_id=? ORDER BY seq DESC LIMIT ?
            ) ORDER BY seq""", (thread_id, limit))]

    @staticmethod
    def _pending(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
        return conn.execute("""SELECT q.* FROM director_dialogue_messages q
            LEFT JOIN director_dialogue_messages r ON r.thread_id=q.thread_id
                AND r.reply_to_seq=q.seq
            WHERE q.thread_id=? AND q.role='USER' AND r.seq IS NULL
            ORDER BY q.seq LIMIT 1""", (thread_id,)).fetchone()

    def ask(self, thread_id: str, prompt: str, *, world_snapshot: Mapping[str, object],
            speaker_id: str = "operator",
            alternatives: Sequence[Mapping[str, object]] = ()) -> dict[str, object]:
        thread_id = _identifier(thread_id, "thread_id")
        speaker_id = _identifier(speaker_id, "speaker_id")
        prompt = _text(prompt, "prompt", _MAX_QUESTION)
        world_json, revision = self._world(world_snapshot)
        alternatives_json = self._alternatives(alternatives)
        with self._transaction() as conn:
            thread = self._thread(conn, thread_id)
            if thread["paused"]:
                raise RuntimeError("dialogue thread is paused")
            if self._pending(conn, thread_id) is not None:
                raise RuntimeError("thread has a pending question; call resume first")
            row = conn.execute("""SELECT COUNT(*) AS n, COALESCE(MAX(seq),0) AS s
                FROM director_dialogue_messages WHERE thread_id=?""", (thread_id,)).fetchone()
            if row["n"] + 2 > _MAX_MESSAGES_PER_THREAD:
                raise ValueError("dialogue thread capacity exhausted")
            conn.execute("""INSERT INTO director_dialogue_messages
                (thread_id,seq,role,speaker_id,content,reply_to_seq,status,basis_json,
                 uncertainty,world_revision,created_at_utc)
                VALUES(?,?,'USER',?,?,NULL,'DECLARED_UNVERIFIED','[]','',?,?)""",
                (thread_id, row["s"] + 1, speaker_id, prompt, revision, _utc()))
        return self._complete(thread_id, world_json, revision, alternatives_json)

    @staticmethod
    def _world(world_snapshot: Mapping[str, object]) -> tuple[str, int]:
        if not isinstance(world_snapshot, Mapping):
            raise ValueError("world_snapshot must be a mapping")
        revision = world_snapshot.get("revision")
        if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
            raise ValueError("world_snapshot must have a bounded integer revision")
        return _json(dict(world_snapshot), "world_snapshot", _MAX_WORLD_BYTES), revision

    @staticmethod
    def _alternatives(alternatives: Sequence[Mapping[str, object]]) -> str:
        if isinstance(alternatives, (str, bytes)) or not isinstance(alternatives, (list, tuple)) or len(alternatives) > 32:
            raise ValueError("alternatives must be a bounded sequence")
        if any(not isinstance(alternative, Mapping) for alternative in alternatives):
            raise ValueError("alternatives must contain mappings")
        return _json([dict(item) for item in alternatives], "alternatives", _MAX_ALTERNATIVES_BYTES)

    def resume(self, thread_id: str, *, world_snapshot: Mapping[str, object],
               alternatives: Sequence[Mapping[str, object]] = ()) -> dict[str, object]:
        thread_id = _identifier(thread_id, "thread_id")
        world_json, revision = self._world(world_snapshot)
        alternatives_json = self._alternatives(alternatives)
        return self._complete(thread_id, world_json, revision, alternatives_json)

    def _complete(self, thread_id: str, world_json: str, revision: int,
                  alternatives_json: str) -> dict[str, object]:
        conn = self._connection()
        thread = self._thread(conn, thread_id)
        if thread["paused"]:
            raise RuntimeError("dialogue thread is paused")
        pending = self._pending(conn, thread_id)
        if pending is None:
            raise ValueError("no pending question to resume")
        prior_rows = list(conn.execute("""SELECT role,content FROM director_dialogue_messages
            WHERE thread_id=? AND seq<? ORDER BY seq DESC LIMIT ?""",
            (thread_id, pending["seq"], _HISTORY_WINDOW)))
        prior_rows.reverse()
        # The durable ledger uses its own uppercase roles, while the reasoning
        # engine's conversation contract accepts normalized user/assistant.
        history = [{"role": "user" if row["role"] == "USER" else "assistant",
                    "content": row["content"]} for row in prior_rows]
        response = self.reasoner.respond(pending["content"], context={
            "goal": json.loads(thread["goal_json"]),
            "world_snapshot": json.loads(world_json),
            "history": history,
            "thread_id": thread_id,
            "turn_index": pending["seq"],
            "history_truncated": len(prior_rows) == _HISTORY_WINDOW,
            "alternatives": json.loads(alternatives_json),
        })
        if not isinstance(response, Mapping):
            raise ValueError("reasoner response must be a mapping")
        answer = _text(response.get("answer"), "reasoner answer", _MAX_ANSWER)
        status = response.get("status", "ANALYTIC_RESPONSE")
        if not isinstance(status, str) or _STATUS.fullmatch(status) is None:
            raise ValueError("reasoner response status is invalid")
        # Engine-provided labels are not permission or outcome receipts. Avoid
        # making an untrusted natural-language response look like a grant.
        if status in {"GRANTED", "APPROVED", "AUTHORIZED", "EXECUTED",
                      "OBJECTIVE_MET", "SUCCESS"}:
            status = "UNTRUSTED_AUTHORITY_CLAIM"
        basis = response.get("basis", [])
        if not isinstance(basis, list) or len(basis) > 16 or any(
            not isinstance(item, str) or len(item) > 512 for item in basis
        ):
            raise ValueError("reasoner basis must be a bounded list of text")
        uncertainty = response.get("uncertainty", "NOT_INDEPENDENTLY_VERIFIED")
        if not isinstance(uncertainty, str) or len(uncertainty) > 512:
            raise ValueError("reasoner uncertainty must be bounded text")
        basis_json = _json(basis, "reasoner basis", 8192)
        with self._transaction() as conn:
            thread = self._thread(conn, thread_id)
            if thread["paused"]:
                raise RuntimeError("dialogue thread paused during response generation")
            # Another process may have completed this prompt while the engine
            # was reasoning. Return the committed answer without duplicating it.
            existing = conn.execute("""SELECT * FROM director_dialogue_messages
                WHERE thread_id=? AND reply_to_seq=?""",
                (thread_id, pending["seq"])).fetchone()
            if existing is None:
                count = conn.execute("SELECT COUNT(*) FROM director_dialogue_messages WHERE thread_id=?",
                                     (thread_id,)).fetchone()[0]
                if count >= _MAX_MESSAGES_PER_THREAD:
                    raise ValueError("dialogue thread capacity exhausted")
                max_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM director_dialogue_messages WHERE thread_id=?",
                                       (thread_id,)).fetchone()[0]
                conn.execute("""INSERT INTO director_dialogue_messages
                    (thread_id,seq,role,speaker_id,content,reply_to_seq,status,basis_json,
                     uncertainty,world_revision,created_at_utc)
                    VALUES(?,?,'DIRECTOR','director',?,?,?,?,?,?,?)""",
                    (thread_id, max_seq + 1, answer, pending["seq"], status,
                     basis_json, uncertainty, revision, _utc()))
                existing = conn.execute("""SELECT * FROM director_dialogue_messages
                    WHERE thread_id=? AND reply_to_seq=?""",
                    (thread_id, pending["seq"])).fetchone()
            question = conn.execute("""SELECT * FROM director_dialogue_messages
                WHERE thread_id=? AND seq=?""", (thread_id, pending["seq"])).fetchone()
        return {"question": self._message(question), "reply": self._message(existing),
                "effect_status": "NOT_EXECUTED", "sender_authenticated": False}


__all__ = ["DirectorDialogue"]
