"""Durable, recipient-addressed *local* messages for the Director prototype.

This is a transport ledger, not an agent, network listener, authentication
service, or execution authority. ``sender_id`` is a caller-supplied claim;
neither it nor message content proves who originated an instruction. Consumers
MUST treat messages as untrusted input and separately authorize any effects.

The explicit SQLite file is local plaintext. Do not send credentials, private
correspondence, or other secrets through it. Use one host; SQLite WAL is not
a distributed queue. Delivery is at-least-once until the recipient acknowledges
a message, so handlers should be idempotent. No automatic external effects,
polling threads, processes, sockets, or paid services are started here.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_KIND = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_MAX_PAYLOAD_BYTES = 32 * 1024
_MAX_DEPTH = 12
_MAX_COLLECTION_ITEMS = 200
_MAX_STRING_CHARS = 16 * 1024
_MAX_POLL = 500


class MessageConflict(ValueError):
    """A message ID was reused with a contradictory immutable definition."""


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _message_kind(value: object) -> str:
    if not isinstance(value, str) or _KIND.fullmatch(value) is None:
        raise ValueError("kind must be a bounded uppercase message type")
    return value


def _json_value(value: object, depth: int = 0) -> object:
    if depth > _MAX_DEPTH:
        raise ValueError("message payload nesting exceeds limit")
    if value is None or type(value) in (bool, int):
        if type(value) is int and not -(2**63) <= value < 2**63:
            raise ValueError("integer exceeds bounded JSON range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid message payloads")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            raise ValueError("message payload string exceeds limit")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("message payload object exceeds limit")
        if any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ValueError("message payload keys must be bounded strings")
        return {key: _json_value(item, depth + 1) for key, item in value.items()}
    if type(value) in (list, tuple):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ValueError("message payload array exceeds limit")
        return [_json_value(item, depth + 1) for item in value]
    raise ValueError("message payload must contain JSON-compatible values only")


def _payload_json(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("message payload must be a JSON object")
    normalized = _json_value(payload)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    try:
        byte_count = len(encoded.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("message payload must contain valid Unicode text") from exc
    if byte_count > _MAX_PAYLOAD_BYTES:
        raise ValueError("message payload exceeds byte limit")
    return encoded


class DirectorMailbox:
    """Explicit single-host SQLite inbox/outbox with durable acknowledgments.

    ``send`` is idempotent for an explicit ``message_id`` only if every
    immutable field matches. A generated ID is returned when one is omitted.
    ``poll`` always selects an exact recipient; ``ack`` cannot acknowledge
    someone else's message. The claimed sender is NEVER authenticated.
    """

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable local SQLite file path is required")
        self.path = Path(path)
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("database must have a local parent and not be a symlink")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("existing mailbox path must be a regular file")

        # Create new POSIX databases owner-only. Existing file permissions
        # remain the operator's responsibility; messages are unencrypted.
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("database must not be a symlink")
        else:
            os.close(descriptor)

        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        try:
            conn = self._connection()
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS director_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    in_reply_to TEXT REFERENCES director_messages(message_id),
                    created_at_utc TEXT NOT NULL,
                    acked_at_utc TEXT
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS director_messages_recipient_seq
                   ON director_messages(recipient_id, seq)"""
            )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            connection.close()

    def __enter__(self) -> "DirectorMailbox":
        self._connection()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DirectorMailbox is closed")
        return self._conn

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

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, object]:
        return {
            "seq": row["seq"],
            "message_id": row["message_id"],
            "sender_id": row["sender_id"],  # Explicitly unauthenticated claim.
            "recipient_id": row["recipient_id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "correlation_id": row["correlation_id"],
            "in_reply_to": row["in_reply_to"],
            "created_at_utc": row["created_at_utc"],
            "acked_at_utc": row["acked_at_utc"],
            "acked": row["acked_at_utc"] is not None,
            "sender_authenticated": False,
            "effect_authority": "NOT_GRANTED",
        }

    def send(
        self,
        *,
        sender_id: str,
        recipient_id: str,
        kind: str,
        payload: Mapping[str, object],
        message_id: str | None = None,
        correlation_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, object]:
        sender_id = _identifier(sender_id, "sender_id")
        recipient_id = _identifier(recipient_id, "recipient_id")
        kind = _message_kind(kind)
        message_id = _identifier(
            uuid.uuid4().hex if message_id is None else message_id, "message_id"
        )
        in_reply_to = (
            None if in_reply_to is None else _identifier(in_reply_to, "in_reply_to")
        )
        correlation_id = (
            None if correlation_id is None else _identifier(correlation_id, "correlation_id")
        )
        payload_json = _payload_json(payload)

        with self._transaction() as conn:
            if in_reply_to is not None:
                original = conn.execute(
                    "SELECT * FROM director_messages WHERE message_id=?", (in_reply_to,)
                ).fetchone()
                if original is None:
                    raise ValueError("reply parent message does not exist")
                if (original["recipient_id"], original["sender_id"]) != (
                    sender_id, recipient_id
                ):
                    raise ValueError("reply participants must reverse the parent route")
                if correlation_id is None:
                    correlation_id = original["correlation_id"]
                if correlation_id != original["correlation_id"]:
                    raise ValueError("reply must preserve the parent correlation")
            if correlation_id is None:
                correlation_id = message_id

            immutable = (
                sender_id, recipient_id, kind,
                payload_json, correlation_id, in_reply_to,
            )
            existing = conn.execute(
                "SELECT * FROM director_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if existing is not None:
                if immutable != (
                    existing["sender_id"], existing["recipient_id"],
                    existing["kind"], existing["payload_json"],
                    existing["correlation_id"], existing["in_reply_to"],
                ):
                    raise MessageConflict("message ID contradicts its immutable definition")
                return self._snapshot(existing)

            conn.execute(
                """INSERT INTO director_messages(
                    message_id, sender_id, recipient_id, kind, payload_json,
                    correlation_id, in_reply_to, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, *immutable, datetime.now(timezone.utc).isoformat()),
            )
            inserted = conn.execute(
                "SELECT * FROM director_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            return self._snapshot(inserted)

    def poll(
        self,
        recipient_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        include_acked: bool = False,
    ) -> list[dict[str, object]]:
        recipient_id = _identifier(recipient_id, "recipient_id")
        if type(after_seq) is not int or not 0 <= after_seq < 2**63:
            raise ValueError("after_seq must be a nonnegative bounded integer")
        if type(limit) is not int or not 1 <= limit <= _MAX_POLL:
            raise ValueError("limit must be in [1,500]")
        if type(include_acked) is not bool:
            raise ValueError("include_acked must be a boolean")
        rows = self._connection().execute(
            """SELECT * FROM director_messages
               WHERE recipient_id=? AND seq>?
                 AND (? OR acked_at_utc IS NULL)
               ORDER BY seq ASC LIMIT ?""",
            (recipient_id, after_seq, include_acked, limit),
        ).fetchall()
        return [self._snapshot(row) for row in rows]

    def ack(self, recipient_id: str, message_id: str) -> dict[str, object]:
        recipient_id = _identifier(recipient_id, "recipient_id")
        message_id = _identifier(message_id, "message_id")
        with self._transaction() as conn:
            original = conn.execute(
                """SELECT * FROM director_messages
                   WHERE recipient_id=? AND message_id=?""",
                (recipient_id, message_id),
            ).fetchone()
            if original is None:
                raise KeyError(message_id)
            if original["acked_at_utc"] is None:
                conn.execute(
                    """UPDATE director_messages SET acked_at_utc=?
                       WHERE recipient_id=? AND message_id=?""",
                    (datetime.now(timezone.utc).isoformat(), recipient_id, message_id),
                )
                original = conn.execute(
                    "SELECT * FROM director_messages WHERE message_id=?", (message_id,)
                ).fetchone()
            return self._snapshot(original)

    def reply(
        self,
        *,
        sender_id: str,
        in_reply_to: str,
        kind: str,
        payload: Mapping[str, object],
        message_id: str | None = None,
    ) -> dict[str, object]:
        sender_id = _identifier(sender_id, "sender_id")
        in_reply_to = _identifier(in_reply_to, "in_reply_to")
        parent = self._connection().execute(
            "SELECT * FROM director_messages WHERE message_id=?", (in_reply_to,)
        ).fetchone()
        if parent is None:
            raise KeyError(in_reply_to)
        if parent["recipient_id"] != sender_id:
            raise ValueError("only the declared recipient may reply")
        return self.send(
            sender_id=sender_id,
            recipient_id=parent["sender_id"],
            kind=kind,
            payload=payload,
            message_id=message_id,
            correlation_id=parent["correlation_id"],
            in_reply_to=in_reply_to,
        )
