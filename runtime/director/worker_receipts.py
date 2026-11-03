"""Optional durable, data-only admission of signed local worker commands.

This worker-owned SQLite ledger records ONLY the authenticated command route,
message identity, nonce and immutable envelope digest. It stores neither the
command payload nor HMAC keys and never executes, ACKs, or authorizes effects.

Inspection and the replay/receipt INSERT use one SQLite transaction in this
ledger: if a worker restarts after committing, the same signed envelope
recovers its pending-review receipt without consuming AuthenticatedChannel's separate
replay marker. This is NOT a trusted local broker or same-UID process sandbox.
Anyone allowed to mutate the separate shared mailbox can still suppress it.

The envelope digest binds the *canonical signed JSON object*, including its
MAC, so harmless whitespace reserialization cannot create a second identity.

Signature/revocation inspection happens in the *separate* authentication
ledger before this receipt transaction; concurrent key revocation can race
that admission. No cross-database atomicity or authorization is claimed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .auth_channel import _canonical, _identifier, _parse
from .secure_worker import VerifiedWorkerInbox


_SCHEMA = "director.worker-receipts.v1"
_MAX_RECEIPTS = 2048


def _check_receipt_files(path: Path) -> None:
    """Reject unsafe main, WAL, SHM *and rollback-journal* aliases pre-open.

    A link to another active SQLite journal can modify that other database's
    pages when this ledger enters WAL mode. Owner-only mode alone is not
    enough: hardlinks preserve permissions. This is a static local pathname
    check, not race-proof defense against another same-UID file writer.
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        member = Path(str(path) + suffix)
        if member.is_symlink():
            raise ValueError("worker receipt database or sidecar cannot be a symlink")
        try:
            metadata = member.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("worker receipt database or sidecar must be a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("worker receipt database or sidecar must not have hardlink aliases")
        if os.name == "posix" and metadata.st_mode & 0o077:
            raise ValueError("worker receipt database and sidecars must be owner-only")


class DurableWorkerReceipts:
    """Explicit, owner-only, per-worker journal of *unexecuted* commands.

    An untrusted caller cannot use this class to grant effects or ACK the
    shared mailbox. Correct operation still relies on operator-controlled
    key custody, storage isolation and trusted local time. Worker account
    isolation and peer authentication are separate, unimplemented concerns.
    """

    def __init__(self, path: str | Path, inbox: VerifiedWorkerInbox) -> None:
        if not isinstance(inbox, VerifiedWorkerInbox):
            raise TypeError("inbox must be a VerifiedWorkerInbox")
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable private worker receipt path is required")
        self.path = Path(path)
        if self.path.resolve(strict=False) in (
                inbox.mailbox.path.resolve(strict=False),
                inbox.channel.path.resolve(strict=False)):
            raise ValueError("worker receipt ledger must be separate from mailbox/auth databases")
        if (self.path.is_symlink() or not self.path.parent.is_dir()
                or self.path.parent.is_symlink()):
            raise ValueError("worker receipt parent must exist and path cannot be a symlink")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("worker receipt database must be a regular file")
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("worker receipt parent directory must be owner-only")
        _check_receipt_files(self.path)

        self.inbox = inbox
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("worker receipt database cannot be a symlink") from None
        else:
            os.close(descriptor)
        _check_receipt_files(self.path)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None)
        try:
            conn = self._connection()
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS director_worker_receipt_binding (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_version TEXT NOT NULL,
                worker_id TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS director_worker_receipts (
                signed_message_id TEXT PRIMARY KEY,
                nonce TEXT NOT NULL UNIQUE,
                worker_id TEXT NOT NULL,
                signed_route_id TEXT NOT NULL,
                command_kind TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                envelope_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state='PENDING_REVIEW')
            )""")
            with self._transaction() as tx:
                tx.execute("""INSERT OR IGNORE INTO director_worker_receipt_binding
                    (singleton,schema_version,worker_id) VALUES(1,?,?)""",
                           (_SCHEMA, self.inbox.worker_id))
                bound = tx.execute("""SELECT schema_version,worker_id FROM
                    director_worker_receipt_binding WHERE singleton=1""").fetchone()
                if bound is None or bound["schema_version"] != _SCHEMA:
                    raise ValueError("unsupported worker receipt ledger schema")
                if bound["worker_id"] != self.inbox.worker_id:
                    raise ValueError("worker receipt ledger is bound to a different worker")
        except BaseException:
            self.close()
            raise

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DurableWorkerReceipts is closed")
        return self._conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "DurableWorkerReceipts":
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
            # SQLite RAISE(ROLLBACK) may already have ended the transaction.
            # A redundant cleanup error must not mask the original failure.
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
            raise

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, object]:
        if row["state"] != "PENDING_REVIEW":
            raise ValueError("worker receipt ledger has unsupported state")
        return {
            "signed_message_id": row["signed_message_id"],
            "nonce": row["nonce"],
            "worker_id": row["worker_id"],
            "signed_route_id": row["signed_route_id"],
            "kind": row["command_kind"],
            "expires_at": row["expires_at"],
            "envelope_sha256": row["envelope_sha256"],
            "state": "PENDING_REVIEW",
            "effect_authority": "NOT_GRANTED",
            "effect_status": "NOT_EXECUTED",
            "mailbox_acked": False,
        }

    def receipt(self, signed_message_id: str) -> dict[str, object] | None:
        """Read metadata of a previously admitted receipt, not executable work."""
        identifier = _identifier(signed_message_id, "signed_message_id")
        row = self._connection().execute("""SELECT * FROM director_worker_receipts
            WHERE signed_message_id=?""", (identifier,)).fetchone()
        return None if row is None else self._receipt(row)

    def accept(self, message: Mapping[str, object], *, now_utc: int) -> dict[str, object]:
        """Atomically admit an inspected signed command as pending review.

        An exact retry returns the same receipt after restart. A changed
        envelope reusing the signed ID or nonce fails closed. No worker action
        or mailbox acknowledgment occurs, regardless of return or exception.
        Fresh verification still requires unexpired, nonrevoked route keys.
        """
        signed_raw, checked = self.inbox._inspect_parts(message, now_utc=now_utc)
        # inspect_signature authenticates canonical signed fields. Formatting
        # differences (e.g. legal JSON whitespace) must not create distinct
        # durable receipt identities for exactly the same signed envelope.
        digest = hashlib.sha256(_canonical(_parse(signed_raw))).hexdigest()
        with self._transaction() as conn:
            matching_id = conn.execute("""SELECT * FROM director_worker_receipts
                WHERE signed_message_id=?""", (checked["message_id"],)).fetchone()
            matching_nonce = conn.execute("""SELECT * FROM director_worker_receipts
                WHERE nonce=?""", (checked["nonce"],)).fetchone()
            if matching_id is not None or matching_nonce is not None:
                if (matching_id is None or matching_nonce is None
                        or matching_id["signed_message_id"] != matching_nonce["signed_message_id"]
                        or matching_id["envelope_sha256"] != digest
                        or matching_id["worker_id"] != self.inbox.worker_id
                        or matching_id["signed_route_id"] != checked["authenticated_sender_id"]
                        or matching_id["command_kind"] != checked["kind"]
                        or matching_id["expires_at"] != checked["expires_at"]):
                    raise ValueError("signed command replay identity/nonce conflict")
                return self._receipt(matching_id)
            total = conn.execute("SELECT COUNT(*) FROM director_worker_receipts").fetchone()[0]
            if total >= _MAX_RECEIPTS:
                raise ValueError("worker receipt ledger capacity exceeded")
            conn.execute("""INSERT INTO director_worker_receipts (
                signed_message_id,nonce,worker_id,signed_route_id,command_kind,
                expires_at,envelope_sha256,state) VALUES (?,?,?,?,?,?,?,'PENDING_REVIEW')""",
                         (checked["message_id"], checked["nonce"], self.inbox.worker_id,
                          checked["authenticated_sender_id"], checked["kind"],
                          checked["expires_at"], digest))
            row = conn.execute("""SELECT * FROM director_worker_receipts
                WHERE signed_message_id=?""", (checked["message_id"],)).fetchone()
            return self._receipt(row)


__all__ = ["DurableWorkerReceipts"]
