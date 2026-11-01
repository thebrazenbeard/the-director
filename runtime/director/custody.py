"""Local, offline integrity-only custody for opaque reference evidence bytes.

This is an append-only *API*, not an authenticated or tamper-proof ledger. A
privileged SQLite writer can rewrite entries, counters, and their hashes. A
digest receipt establishes only consistency with this local database; it does
not verify source identity, real-world events, approval, or authorization.

The caller supplies already-canonical dossier/evidence bytes. This module
neither interprets them nor imports the dossier/decision implementation.
Restrict filesystem permissions: stored content is plaintext, not encrypted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}\Z")
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z"
)
_GENESIS_HASH = "0" * 64
_MAX_CONTENT_BYTES = 256 * 1024
_DEFAULT_MAX_RECORDS = 256
_MAX_ALLOWED_RECORDS = 10_000
_DIGEST_DOMAIN = b"the-director-reference-custody-v1\x00"


class CustodyError(Exception):
    """Base class for custody-specific errors."""


class CustodyConflictError(CustodyError, ValueError):
    """A record ID was reused with a different kind or exact content bytes."""


class CustodyIntegrityError(CustodyError):
    """The locally held database no longer has a self-consistent chain."""


class CustodyClosedError(CustodyError):
    """An operation was requested after the store was closed."""


@dataclass(frozen=True)
class CustodyReceipt:
    seq: int
    record_id: str
    kind: str
    observed_at_utc: str
    content_sha256: str
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class CustodyRecord:
    receipt: CustodyReceipt
    content: bytes


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be an identifier of 1–64 ASCII characters")
    return value


def _utc(value: object) -> str:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("observed_at_utc must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at_utc must be a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("observed_at_utc must be UTC, not a local timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _entry_hash(
    seq: int,
    record_id: str,
    kind: str,
    observed_at_utc: str,
    content_sha256: str,
    prev_hash: str,
) -> str:
    metadata = {
        "seq": seq,
        "record_id": record_id,
        "kind": kind,
        "observed_at_utc": observed_at_utc,
        "content_sha256": content_sha256,
        "prev_hash": prev_hash,
    }
    serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(_DIGEST_DOMAIN + serialized.encode("ascii")).hexdigest()


def _receipt(row: sqlite3.Row) -> CustodyReceipt:
    return CustodyReceipt(
        seq=row["seq"],
        record_id=row["record_id"],
        kind=row["kind"],
        observed_at_utc=row["observed_at_utc"],
        content_sha256=row["content_sha256"],
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )


def _verify_connection(conn: sqlite3.Connection) -> tuple[int, str]:
    """Verify a consistent transaction snapshot; return (tail seq, tail hash)."""
    last_hash = _GENESIS_HASH
    last_seq = 0
    for row in conn.execute("SELECT * FROM entries ORDER BY seq"):
        seq = row["seq"]
        if type(seq) is not int or seq != last_seq + 1:
            raise CustodyIntegrityError("custody sequence gap or duplicate")
        if (
            not isinstance(row["content"], bytes)
            or not isinstance(row["record_id"], str)
            or not isinstance(row["kind"], str)
            or not isinstance(row["observed_at_utc"], str)
            or not isinstance(row["content_sha256"], str)
            or not isinstance(row["prev_hash"], str)
            or not isinstance(row["entry_hash"], str)
        ):
            raise CustodyIntegrityError("invalid custody entry field types")
        if hashlib.sha256(row["content"]).hexdigest() != row["content_sha256"]:
            raise CustodyIntegrityError(f"content checksum mismatch at sequence {seq}")
        if row["prev_hash"] != last_hash:
            raise CustodyIntegrityError(f"predecessor hash mismatch at sequence {seq}")
        expected = _entry_hash(
            seq, row["record_id"], row["kind"], row["observed_at_utc"],
            row["content_sha256"], row["prev_hash"],
        )
        if expected != row["entry_hash"]:
            raise CustodyIntegrityError(f"entry hash mismatch at sequence {seq}")
        last_seq = seq
        last_hash = row["entry_hash"]

    # AUTOINCREMENT's high-water mark catches deleted trailing entries when
    # sqlite_sequence itself is untouched. It is not an external trust anchor.
    marker = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'entries'").fetchone()
    if (0 if marker is None else marker["seq"]) != last_seq:
        raise CustodyIntegrityError("custody tail missing or sequence counter altered")
    return last_seq, last_hash


class CustodyStore:
    """A bounded, same-host SQLite append-only reference interface.

    Each operation owns and explicitly closes its SQLite connection, including
    on Windows. `BEGIN IMMEDIATE` serializes two independent writers. Local
    hash verification is O(number of records); this store is intentionally
    small, not an unbounded high-throughput event stream. Default capacity is
    256 entries of at most 256 KiB each (64 MiB maximum payload, plus SQLite
    overhead); a caller must explicitly opt in to a higher cap up to 10000.

    Re-appending an existing ID with exactly matching kind and raw bytes is
    idempotent: it returns the *original* immutable receipt, including its
    first observed_at_utc. A later timestamp never overwrites the first.
    """

    def __init__(self, path: str | os.PathLike[str], *, max_records: int = _DEFAULT_MAX_RECORDS):
        if type(max_records) is not int or not 1 <= max_records <= _MAX_ALLOWED_RECORDS:
            raise ValueError("max_records must be an integer between 1 and 10000")
        self._path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("in-memory SQLite cannot provide restart persistence")
        self._max_records = max_records
        self._closed = False
        # A new POSIX database starts private, regardless of caller umask.
        # Existing DB permissions are the caller's responsibility.
        if os.name == "posix":
            try:
                descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    observed_at_utc TEXT NOT NULL,
                    content BLOB NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise CustodyClosedError("custody store is closed")
        conn = sqlite3.connect(str(self._path), timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA synchronous = FULL")
            return conn
        except BaseException:
            conn.close()
            raise

    def append(
        self,
        record_id: str,
        kind: str,
        content: bytes,
        observed_at_utc: str,
    ) -> CustodyReceipt:
        if self._closed:
            raise CustodyClosedError("custody store is closed")
        record_id = _identifier(record_id, "record_id")
        kind = _identifier(kind, "kind")
        if type(content) is not bytes or len(content) > _MAX_CONTENT_BYTES:
            raise ValueError("content must be bytes of at most 256 KiB")
        timestamp = _utc(observed_at_utc)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            last_seq, last_hash = _verify_connection(conn)
            existing = conn.execute("SELECT * FROM entries WHERE record_id = ?", (record_id,)).fetchone()
            if existing is not None:
                if existing["kind"] != kind or existing["content"] != content:
                    raise CustodyConflictError("record_id already exists with different bytes or kind")
                conn.execute("COMMIT")
                return _receipt(existing)
            if last_seq >= self._max_records:
                raise ValueError("custody record limit reached")

            seq = last_seq + 1
            content_digest = hashlib.sha256(content).hexdigest()
            entry_digest = _entry_hash(seq, record_id, kind, timestamp, content_digest, last_hash)
            conn.execute(
                """INSERT INTO entries
                   (seq, record_id, kind, observed_at_utc, content, content_sha256, prev_hash, entry_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (seq, record_id, kind, timestamp, content, content_digest, last_hash, entry_digest),
            )
            conn.execute("COMMIT")
            return CustodyReceipt(seq, record_id, kind, timestamp, content_digest, last_hash, entry_digest)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    def get(self, record_id: str) -> CustodyRecord | None:
        if self._closed:
            raise CustodyClosedError("custody store is closed")
        record_id = _identifier(record_id, "record_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            _verify_connection(conn)
            row = conn.execute("SELECT * FROM entries WHERE record_id = ?", (record_id,)).fetchone()
            return None if row is None else CustodyRecord(_receipt(row), row["content"])
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    def verify_chain(self) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            _verify_connection(conn)
            return True
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    def close(self) -> None:
        """Idempotent; all operation-scoped connections are already closed."""
        self._closed = True

    def __enter__(self) -> CustodyStore:
        if self._closed:
            raise CustodyClosedError("custody store is closed")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

