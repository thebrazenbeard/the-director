"""Authenticated, replay-resistant *local envelope* reference for The Director.

This is not a network service, credential issuer, or effect authorization gate.
It authenticates possession of an **operator-configured per-sender symmetric
secret**; any other holder of that same secret can impersonate that sender.
Identity and consent beyond key custody are not established by HMAC, and
authenticated data must still be independently authorized before any effects.

There are no sockets, workers, shell commands, or background activity. Time is
an integer POSIX second provided by the caller; this module does not establish
that the clock is trusted. A durable local SQLite replay/revocation ledger is
created only by explicit constructor call. It is plaintext; secret key bytes
are never written to it. Protect the directory and keys at the host boundary.

``DirectorCoordinator`` may opt into authenticated incoming worker reports.
In fresh ``sign_outbound=True`` mode, it also persists signed local mission
instructions and commits incoming replay admission together with the report
in its OWN SQLite transaction, using this class's non-consuming
``inspect_signature`` verifier. Existing v0.4 incoming-only coordinator mode
still uses ``verify`` below, whose separate replay commit can lose a report
on a downstream crash. Even in the new mode, worker-side ``verify`` consumes
its own replay marker before application handling. Neither mode provides
exactly-once end-to-end transport, independent Director identity, or effect
authority. Importing this module changes no mailbox trust level by itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROTOCOL = "director.authenticated-envelope.v1"
_DOMAIN = b"the-director/authenticated-envelope/v1\x00"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_KIND = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_MAC = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({"protocol", "sender_id", "recipient_id", "kind", "payload",
                     "message_id", "nonce", "issued_at", "expires_at", "mac"})
_MAX_PAYLOAD_BYTES = 16 * 1024
_MAX_ENVELOPE_BYTES = 32 * 1024
_MAX_TEXT = 4096
_MAX_DEPTH = 10
_MAX_ITEMS = 100
_MAX_TTL_SECONDS = 86400
_MAX_EPOCH = 4102444800  # Through the beginning of 2100, explicit upper bound.


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _kind(value: object) -> str:
    if not isinstance(value, str) or _KIND.fullmatch(value) is None:
        raise ValueError("kind must be an uppercase bounded message type")
    return value


def _epoch(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_EPOCH:
        raise ValueError(f"{label} must be bounded POSIX UTC seconds")
    return value


def _time_span(issued: object, expires: object) -> tuple[int, int]:
    start = _epoch(issued, "issued_at")
    end = _epoch(expires, "expires_at")
    if not start < end <= start + _MAX_TTL_SECONDS:
        raise ValueError("message expiration must be within 1..86400 seconds")
    return start, end


def _payload_value(value: object, depth: int = 0) -> object:
    if depth > _MAX_DEPTH:
        raise ValueError("payload nested too deeply")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError("payload integer outside supported range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be signed")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            raise ValueError("payload string too long")
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("payload contains malformed Unicode") from exc
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            raise ValueError("payload object has too many fields")
        if any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in value):
            raise ValueError("payload field names must be bounded strings")
        try:
            for key in value:
                key.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("payload field name contains malformed Unicode") from exc
        return {key: _payload_value(item, depth + 1) for key, item in value.items()}
    if type(value) in (list, tuple):
        if len(value) > _MAX_ITEMS:
            raise ValueError("payload array has too many items")
        return [_payload_value(item, depth + 1) for item in value]
    raise ValueError("payload must be bounded, JSON-compatible data")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise ValueError("invalid canonical JSON") from exc


def _validated_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a JSON object")
    normalized = _payload_value(payload)
    if len(_canonical(normalized)) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload too large")
    return normalized


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON envelope key")
        result[key] = value
    return result


def _parse(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or len(raw) > _MAX_ENVELOPE_BYTES or not raw:
        raise ValueError("envelope must be bounded raw JSON bytes")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid authenticated envelope JSON") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise ValueError("envelope must contain exactly the signed fields")
    if data["protocol"] != PROTOCOL:
        raise ValueError("unsupported authenticated protocol")
    _identifier(data["sender_id"], "sender_id")
    _identifier(data["recipient_id"], "recipient_id")
    _identifier(data["message_id"], "message_id")
    _kind(data["kind"])
    if type(data["nonce"]) is not str or _NONCE.fullmatch(data["nonce"]) is None:
        raise ValueError("nonce must be 128-bit lowercase hexadecimal")
    if type(data["mac"]) is not str or _MAC.fullmatch(data["mac"]) is None:
        raise ValueError("MAC must be lowercase SHA-256 hex")
    _time_span(data["issued_at"], data["expires_at"])
    _validated_payload(data["payload"])
    return data


def _key(value: object) -> bytes:
    # A length check is not a proof of entropy. The operator must generate and
    # protect random keys outside this software's evidence boundary.
    if type(value) is not bytes or not 32 <= len(value) <= 128:
        raise ValueError("each sender requires a 32..128-byte operator-supplied secret")
    return bytes(value)


def _check_existing_file_security(path: Path) -> None:
    """Reject mutable or readable POSIX ledger files outside the owner account.

    SQLite WAL/SHM siblings are part of the durable database boundary, so an
    insecure pre-existing sidecar is rejected too. This is a static check,
    not a defense against a malicious actor able to swap files in the parent
    directory concurrently; the operator must own/protect that directory.
    """
    if os.name != "posix":
        return
    for member in (path, path.with_name(path.name + "-wal"),
                   path.with_name(path.name + "-shm")):
        if member.is_symlink():
            raise ValueError("replay database files cannot be symlinks")
        try:
            metadata = member.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("replay database files must be regular files")
        if metadata.st_mode & 0o077:
            raise ValueError("pre-existing replay database files must be owner-only (0600)")


class AuthenticatedChannel:
    """Local HMAC envelope encoder/verifier with durable at-most-once admission.

    ``seal`` is data-only and does not reserve nonce IDs; duplicate IDs are
    rejected when ``verify`` atomically admits the first authenticated copy.
    A rotated key immediately invalidates unverified messages signed under the
    old key. No secret material or rotation history is persisted in SQLite.
    """

    def __init__(self, path: str | Path, keys: Mapping[str, bytes]):
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable local replay database path is required")
        self.path = Path(path)
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("replay database parent must exist and path must not be a symlink")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("replay database must be a regular file")
        _check_existing_file_security(self.path)
        if not isinstance(keys, Mapping) or not 1 <= len(keys) <= 64:
            raise ValueError("keys must map 1..64 sender IDs to secret bytes")
        self._keys: dict[str, bytes] = {
            _identifier(sender, "key sender_id"): _key(secret) for sender, secret in keys.items()
        }
        if len(set(self._keys.values())) != len(self._keys):
            raise ValueError("each sender must have distinct symmetric key material")

        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("replay database path must not be a symlink")
        else:
            os.close(descriptor)
        _check_existing_file_security(self.path)

        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        try:
            conn = self._connection()
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS authenticated_replay (
                sender_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                nonce TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                verified_at INTEGER NOT NULL,
                PRIMARY KEY (sender_id, message_id),
                UNIQUE (sender_id, nonce)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS authenticated_revocation (
                sender_id TEXT PRIMARY KEY,
                revoked INTEGER NOT NULL CHECK(revoked=1)
            )""")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            connection.close()
        self._keys.clear()  # Does not guarantee secure erasure of immutable bytes.

    def __enter__(self) -> "AuthenticatedChannel":
        self._connection()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("AuthenticatedChannel is closed")
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

    def _secret_for(self, sender: str) -> bytes:
        secret = self._keys.get(sender)
        if secret is None:
            raise ValueError("sender has no configured authentication key")
        return secret

    def has_sender_key(self, sender_id: str) -> bool:
        """Whether a route-specific signing key is configured and not revoked.

        This is an operational preflight, NOT proof of exclusive control of
        the key or authority of the named party. It exposes no key bytes.
        """
        self._connection()
        sender = _identifier(sender_id, "sender_id")
        return (sender in self._keys
                and not self._is_revoked(self._connection(), sender))

    @staticmethod
    def _is_revoked(conn: sqlite3.Connection, sender: str) -> bool:
        row = conn.execute("SELECT 1 FROM authenticated_revocation WHERE sender_id=?", (sender,)).fetchone()
        return row is not None

    def rotate_key(self, sender_id: str, new_secret: bytes) -> None:
        """Replace an in-memory sender secret; old pending envelopes stop working.

        This is an operator-controlled local API, not a credential admin system.
        Key provenance, distribution, and revocation across devices are NOT
        solved by this method.
        """
        self._connection()
        sender = _identifier(sender_id, "sender_id")
        if sender not in self._keys:
            raise ValueError("sender has no configured authentication key")
        validated = _key(new_secret)
        if any(other != sender and secret == validated for other, secret in self._keys.items()):
            raise ValueError("rotated key must remain unique to this sender")
        self._keys[sender] = validated

    def revoke_sender(self, sender_id: str) -> None:
        """Durably stop future admissions under the sender's configured ID."""
        sender = _identifier(sender_id, "sender_id")
        self._secret_for(sender)
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO authenticated_revocation(sender_id,revoked) VALUES(?,1)", (sender,))

    def seal(
        self,
        *,
        sender_id: str,
        recipient_id: str,
        kind: str,
        payload: Mapping[str, object],
        issued_at: int,
        expires_at: int,
        message_id: str | None = None,
        nonce: str | None = None,
    ) -> bytes:
        """Create a deterministic canonical HMAC envelope without sending it."""
        self._connection()
        sender = _identifier(sender_id, "sender_id")
        recipient = _identifier(recipient_id, "recipient_id")
        kind = _kind(kind)
        start, end = _time_span(issued_at, expires_at)
        identifier = _identifier(uuid.uuid4().hex if message_id is None else message_id, "message_id")
        nonce = secrets.token_hex(16) if nonce is None else nonce
        if type(nonce) is not str or _NONCE.fullmatch(nonce) is None:
            raise ValueError("nonce must be 128-bit lowercase hexadecimal")
        payload_dict = _validated_payload(payload)
        secret = self._secret_for(sender)
        if self._is_revoked(self._connection(), sender):
            raise ValueError("sender has been revoked")
        body = {"protocol": PROTOCOL, "sender_id": sender, "recipient_id": recipient,
                "kind": kind, "payload": payload_dict, "message_id": identifier,
                "nonce": nonce, "issued_at": start, "expires_at": end}
        mac = hmac.new(secret, _DOMAIN + _canonical(body), hashlib.sha256).hexdigest()
        serialized = _canonical({**body, "mac": mac})
        if len(serialized) > _MAX_ENVELOPE_BYTES:
            raise ValueError("envelope exceeds the size limit")
        return serialized

    def verify(self, envelope: bytes, *, expected_recipient: str, now_utc: int) -> dict[str, object]:
        """Verify and atomically consume a fresh, authenticated envelope.

        On success the returned authenticated sender is ONLY as trustworthy as
        exclusive custody of the sender's configured symmetric key. The replay
        ledger records ID/nonce/route/time, *not* payload or secret keys.
        """
        checked = self.inspect_signature(envelope, expected_recipient=expected_recipient,
                                         now_utc=now_utc)
        now = _epoch(now_utc, "now_utc")
        sender = checked["authenticated_sender_id"]
        with self._transaction() as conn:
            if self._is_revoked(conn, sender):
                raise ValueError("sender has been revoked")
            try:
                conn.execute("""INSERT INTO authenticated_replay(
                    sender_id,message_id,nonce,recipient_id,verified_at)
                    VALUES(?,?,?,?,?)""",
                    (sender, checked["message_id"], checked["nonce"],
                     checked["recipient_id"], now))
            except sqlite3.IntegrityError as exc:
                raise ValueError("replay or reused sender message identifier/nonce") from exc
        return {**checked, "replay_admitted": True}

    def inspect_signature(
        self, envelope: bytes, *, expected_recipient: str, now_utc: int,
    ) -> dict[str, object]:
        """Check MAC, route, validity window and revocation WITHOUT replay admission.

        This is deliberately **not** a sufficient authorization or freshness
        receipt. Repeated calls on the same bytes succeed while a signature is
        valid; callers MUST atomically enforce replay uniqueness in the same
        durable database transaction as their own downstream state change.
        The Director coordinator's signed-ingress flow is such a caller.

        Never use this method alone to authorize or execute worker effects.
        When no shared transactional replay ledger exists, use ``verify``
        instead and accept its explicitly narrower cross-database guarantee.
        """
        self._connection()
        expected_recipient = _identifier(expected_recipient, "expected_recipient")
        now = _epoch(now_utc, "now_utc")
        parsed = _parse(envelope)
        if parsed["recipient_id"] != expected_recipient:
            raise ValueError("envelope recipient differs from the expected recipient")
        if now < parsed["issued_at"]:
            raise ValueError("envelope is not yet valid")
        if now >= parsed["expires_at"]:
            raise ValueError("envelope expired")
        sender = parsed["sender_id"]
        secret = self._secret_for(sender)
        body = {key: value for key, value in parsed.items() if key != "mac"}
        expected_mac = hmac.new(secret, _DOMAIN + _canonical(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(parsed["mac"], expected_mac):
            raise ValueError("envelope MAC/signature verification failed")
        if self._is_revoked(self._connection(), sender):
            raise ValueError("sender has been revoked")
        return {"authenticated_sender_id": sender,
                "sender_authenticated": True,
                "authentication_scope": "OPERATOR_SUPPLIED_SYMMETRIC_KEY",
                "recipient_id": expected_recipient, "message_id": parsed["message_id"],
                "nonce": parsed["nonce"], "kind": parsed["kind"], "payload": parsed["payload"],
                "issued_at": parsed["issued_at"], "expires_at": parsed["expires_at"],
                "effect_authority": "NOT_GRANTED", "effect_status": "NOT_EXECUTED",
                "replay_admitted": False}


__all__ = ["AuthenticatedChannel", "PROTOCOL"]
