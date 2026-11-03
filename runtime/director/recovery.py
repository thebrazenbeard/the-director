"""Consent-gated, data-only local recovery records for The Director.

This reference stores immutable checkpoints and atomically persists *recovered
data records* with their replay receipts. It never rewrites a coordinator DB,
activates an agent, starts a process, opens a listener, or restores authority.

Local owner names and consent booleans are **claims**, not authenticated host
identities. SHA-256 checks accidental corruption; a privileged database writer
can rewrite contents and checksums. All saved material is plaintext, including
goal descriptions and any explicitly included agent memory summaries. Operators
must restrict filesystem access, redact confidential text, and back up the
SQLite file independently if actual disaster recovery is needed.

The recovery transaction is atomic *within this store*. Future activation of
an external runtime would require a separate independently enforced, atomic
host handoff; a persisted recovery record alone is not such activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "director.data-recovery.v1"
_DOMAIN = b"director-recovery-reference-v1\x00"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_MAX_RECORD_BYTES = 256 * 1024
_MAX_RECORDS = 256
_MAX_DEPTH = 10
_MAX_ITEMS = 2048
_MAX_TEXT = 16 * 1024
_CONSENT_FIELDS = frozenset({"approved", "owner_id", "target_domain", "allow_agent_state"})
_APPROVAL_FIELDS = frozenset({"approved", "owner_id", "target_domain", "allow_rollback"})
_COORDINATOR_FIELDS = frozenset({
    "goal", "world", "assignments", "reports", "operator_control", "status",
    "reason", "effect_status", "control_state", "cancellations", "graph",
    "worker_stopped_verified", "authentication_mode", "recovery_quarantine",
})
_AGENT_FIELDS = frozenset({
    "new_instance_id", "agent_id", "restored_from_instance_id", "capsule_id",
    "target_domain", "subject_kind", "state", "memory_manifest",
    "authority", "effects", "continuity_claim",
})


class RecoveryIntegrityError(ValueError):
    """Stored metadata or content fails a local consistency check."""


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _json_value(value: object, depth: int = 0) -> object:
    """Reject arbitrary Python objects and denial-of-service-sized structures."""
    if depth > _MAX_DEPTH:
        raise ValueError("recovery input is nested too deeply")
    if value is None or type(value) in (bool, int):
        if type(value) is int and abs(value) > 10**15:
            raise ValueError("unbounded recovery integer")
        return value
    if type(value) is float:
        # JSON canonicalization below rejects NaN and infinities.
        if not (-1e15 <= value <= 1e15):
            raise ValueError("nonfinite or unbounded recovery number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT:
            raise ValueError("unbounded recovery text")
        return value
    if isinstance(value, (tuple, list)):
        if len(value) > _MAX_ITEMS:
            raise ValueError("too many recovery list items")
        return [_json_value(item, depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            raise ValueError("too many recovery mapping items")
        if any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ValueError("recovery keys must be bounded strings")
        return {key: _json_value(val, depth + 1) for key, val in value.items()}
    raise ValueError("recovery state must contain only bounded JSON data")


def _canonical(value: object) -> str:
    clean = _json_value(value)
    try:
        encoded = json.dumps(clean, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":"), allow_nan=False)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("invalid recovery JSON") from exc
    if len(encoded.encode("ascii")) > _MAX_RECORD_BYTES:
        raise ValueError("recovery record exceeds the 256 KiB bound")
    return encoded


def _digest(encoded: str) -> str:
    return hashlib.sha256(_DOMAIN + encoded.encode("ascii")).hexdigest()


def _permission(raw: Mapping[str, object], *, approval: bool,
                owner_id: str, target_domain: str) -> dict[str, object]:
    fields = _APPROVAL_FIELDS if approval else _CONSENT_FIELDS
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ValueError("explicit owner consent/approval with exact fields is required")
    if raw["approved"] is not True:
        raise ValueError("owner consent/approval was not granted")
    if raw["owner_id"] != owner_id or raw["target_domain"] != target_domain:
        raise ValueError("owner approval is not scoped to this owner/domain")
    flag = "allow_rollback" if approval else "allow_agent_state"
    if type(raw[flag]) is not bool:
        raise ValueError(f"{flag} must be a boolean")
    return dict(raw)


def _snapshot(raw: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(raw, Mapping) or not {"goal", "world", "operator_control", "effect_status"} <= set(raw):
        raise ValueError("coordinator snapshot is missing required reference fields")
    if not set(raw) <= _COORDINATOR_FIELDS:
        raise ValueError("coordinator snapshot includes unsupported fields")
    if raw["effect_status"] != "NOT_EXECUTED":
        raise ValueError("cannot recover an effect or an effect authority grant")
    if "worker_stopped_verified" in raw and raw["worker_stopped_verified"] is not False:
        raise ValueError("cannot restore a claim that a worker actually stopped")
    if "authentication_mode" in raw and raw["authentication_mode"] not in (
        "ENFORCED_LOCAL_HMAC", "ENFORCED_LOCAL_HMAC_BIDIRECTIONAL",
        "UNAUTHENTICATED_LOCAL"
    ):
        raise ValueError("unsupported coordinator authentication mode")
    if "graph" in raw and raw["graph"] is not None:
        graph = raw["graph"]
        if not isinstance(graph, Mapping) or graph.get("configured") is not True:
            raise ValueError("coordinator graph snapshot has invalid reference metadata")
    if "recovery_quarantine" in raw and raw["recovery_quarantine"] is not None:
        quarantine = raw["recovery_quarantine"]
        if (not isinstance(quarantine, Mapping) or quarantine.get("locked") != 1
                or quarantine.get("schema_version") != "director.recovery-quarantine.v1"
                or not isinstance(quarantine.get("source_goal_id"), str)
                or not isinstance(quarantine.get("source_digest"), str)
                or not isinstance(quarantine.get("source_auth_mode"), str)):
            raise ValueError("coordinator quarantine snapshot is invalid")
    world = raw["world"]
    if not isinstance(world, Mapping) or type(world.get("revision")) is not int or world["revision"] < 0:
        raise ValueError("coordinator world revision is invalid")
    control = raw["operator_control"]
    if not isinstance(control, Mapping) or type(control.get("paused")) is not bool or type(control.get("stop_requested")) is not bool:
        raise ValueError("pause/stop state must be explicit booleans")
    return json.loads(_canonical(raw))


def _agent(raw: Mapping[str, object], target_domain: str) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _AGENT_FIELDS:
        raise ValueError("only a restored software-agent data record is accepted")
    if (raw["subject_kind"] != "SOFTWARE_AGENT"
            or raw["target_domain"] != target_domain
            or raw["authority"] != "NOT_GRANTED"
            or raw["effects"] != "NOT_EXECUTED"
            or raw["continuity_claim"] != "INFORMATION_STATE_COPY_ONLY"):
        raise ValueError("agent state cannot transfer authority, effects, or a wrong domain")
    for field in ("agent_id", "new_instance_id", "restored_from_instance_id", "capsule_id"):
        _identifier(raw[field], field)
    if raw["new_instance_id"] == raw["restored_from_instance_id"]:
        raise ValueError("agent recovery cannot overwrite its source instance")
    if not isinstance(raw["state"], Mapping) or set(raw["state"]) != {"goal", "memories"}:
        raise ValueError("agent state must be goal/memories data only")
    if not isinstance(raw["state"]["goal"], Mapping) or not isinstance(raw["state"]["memories"], list):
        raise ValueError("agent goal/memories have invalid data types")
    manifest = raw["memory_manifest"]
    if not isinstance(manifest, Mapping) or set(manifest) != {"input_count", "transferred_count", "omitted_count"}:
        raise ValueError("agent recovery memory manifest is invalid")
    if any(type(manifest[field]) is not int or not 0 <= manifest[field] <= 32 for field in manifest):
        raise ValueError("agent recovery memory manifest contains invalid counts")
    if manifest["input_count"] != manifest["transferred_count"] + manifest["omitted_count"] or manifest["transferred_count"] != len(raw["state"]["memories"]):
        raise ValueError("agent recovery memory manifest disagrees with state")
    return json.loads(_canonical(raw))


def _database(path: str | Path) -> sqlite3.Connection:
    if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
        raise ValueError("a durable local recovery file path is required")
    file = Path(path)
    if file.is_symlink() or not file.parent.is_dir():
        raise ValueError("recovery path must have an existing parent and not be a symlink")
    # SQLite WAL and shared-memory sidecars can contain plaintext memory, too.
    # Validate them *before* opening SQLite; requiring a trusted parent
    # directory remains necessary against concurrent pathname replacement.
    for candidate in (file, Path(f"{file}-wal"), Path(f"{file}-shm")):
        if candidate.is_symlink():
            raise ValueError("recovery database or sidecar cannot be a symlink")
        if not candidate.exists():
            continue
        mode = candidate.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("recovery database or sidecar must be a regular file")
        if os.name == "posix" and mode & 0o077:
            raise ValueError("existing plaintext recovery database or sidecar must be owner-only")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(file, flags, 0o600)
    except FileExistsError:
        if file.is_symlink():
            raise ValueError("recovery file cannot be a symlink")
    else:
        os.close(handle)
    conn = sqlite3.connect(file, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_recovery_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            generation INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            record_digest TEXT NOT NULL,
            UNIQUE(owner_id,target_domain,generation)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_recovery_restores (
            restore_id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            target_domain TEXT NOT NULL,
            generation INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            record_digest TEXT NOT NULL,
            FOREIGN KEY(checkpoint_id) REFERENCES director_recovery_checkpoints(checkpoint_id),
            UNIQUE(owner_id,target_domain,generation)
        )""")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


class RecoveryStore:
    """Bounded local checkpoints and crash-recoverable *records*, not effects.

    A caller deliberately selects a private file. The API owns one SQLite
    handle, and ``close`` / context management is required on Windows before
    removing its parent temporary directory.
    """

    def __init__(self, path: str | Path):
        self._conn: sqlite3.Connection | None = _database(path)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("RecoveryStore is closed")
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            # SQLite RAISE(ROLLBACK) can finish the transaction itself. Never
            # conceal the original database exception with a cleanup failure.
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
            raise

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "RecoveryStore":
        self._connection()
        return self

    def __exit__(self, _type: object, _value: object, _trace: object) -> None:
        self.close()

    @staticmethod
    def _read_checkpoint(conn: sqlite3.Connection, checkpoint_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM director_recovery_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
        if row is None:
            raise KeyError(checkpoint_id)
        try:
            if _digest(row["record_json"]) != row["record_digest"]:
                raise RecoveryIntegrityError("checkpoint checksum mismatch")
            record = json.loads(row["record_json"])
            if (record["schema_version"] != SCHEMA_VERSION
                    or record["checkpoint_id"] != checkpoint_id
                    or record["owner_id"] != row["owner_id"]
                    or record["target_domain"] != row["target_domain"]
                    or record["generation"] != row["generation"]
                    or _canonical(record) != row["record_json"]):
                raise RecoveryIntegrityError("checkpoint metadata inconsistent")
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, RecoveryIntegrityError):
                raise
            raise RecoveryIntegrityError("checkpoint is malformed") from exc
        return record

    @staticmethod
    def _read_restore(conn: sqlite3.Connection, restore_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM director_recovery_restores WHERE restore_id=?", (restore_id,)).fetchone()
        if row is None:
            raise KeyError(restore_id)
        try:
            if _digest(row["record_json"]) != row["record_digest"]:
                raise RecoveryIntegrityError("recovered record checksum mismatch")
            record = json.loads(row["record_json"])
            if (record["schema_version"] != SCHEMA_VERSION
                    or record["restore_id"] != restore_id
                    or record["checkpoint_id"] != row["checkpoint_id"]
                    or record["owner_id"] != row["owner_id"]
                    or record["target_domain"] != row["target_domain"]
                    or record["recovery_generation"] != row["generation"]
                    or _canonical(record) != row["record_json"]):
                raise RecoveryIntegrityError("recovered record metadata inconsistent")
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, RecoveryIntegrityError):
                raise
            raise RecoveryIntegrityError("recovered record is malformed") from exc
        return record

    @classmethod
    def _require_exact_receipt(cls, conn: sqlite3.Connection, kind: str,
                               record_id: str, expected: dict[str, Any]) -> None:
        """Reject ignored, deleted, or mutated rows before reporting success."""
        try:
            if kind == "checkpoint":
                actual = cls._read_checkpoint(conn, record_id)
            else:
                actual = cls._read_restore(conn, record_id)
        except (KeyError, RecoveryIntegrityError) as exc:
            raise RecoveryIntegrityError(f"{kind} receipt missing or corrupt") from exc
        if actual != expected:
            raise RecoveryIntegrityError(f"{kind} receipt does not match requested record")

    @staticmethod
    def _head_generation(conn: sqlite3.Connection, owner_id: str, target_domain: str) -> int:
        checkpoint = conn.execute("""SELECT COALESCE(MAX(generation),0)
            FROM director_recovery_checkpoints WHERE owner_id=? AND target_domain=?""",
            (owner_id, target_domain)).fetchone()[0]
        restore = conn.execute("""SELECT COALESCE(MAX(generation),0)
            FROM director_recovery_restores WHERE owner_id=? AND target_domain=?""",
            (owner_id, target_domain)).fetchone()[0]
        return max(checkpoint, restore)

    def checkpoint(
        self, checkpoint_id: str, *, owner_id: str, target_domain: str,
        generation: int, coordinator_snapshot: Mapping[str, object],
        agent_state: Mapping[str, object] | None = None,
        consent: Mapping[str, object],
    ) -> dict[str, object]:
        """Atomically store a bounded versioned snapshot, explicit opt-in.

        New checkpoints must advance the owner's domain generation. Repeating
        exactly the same checkpoint ID and content is idempotent; conflicting
        reuse is rejected. The selected content is plaintext.
        """
        checkpoint_id = _identifier(checkpoint_id, "checkpoint_id")
        owner_id = _identifier(owner_id, "owner_id")
        target_domain = _identifier(target_domain, "target_domain")
        if type(generation) is not int or not 1 <= generation <= 2**31 - 1:
            raise ValueError("generation must be a positive bounded integer")
        permission = _permission(consent, approval=False, owner_id=owner_id, target_domain=target_domain)
        if agent_state is not None and not permission["allow_agent_state"]:
            raise ValueError("agent-state backup requires separate explicit opt-in")
        record = {
            "schema_version": SCHEMA_VERSION, "checkpoint_id": checkpoint_id,
            "owner_id": owner_id, "target_domain": target_domain,
            "generation": generation, "coordinator_snapshot": _snapshot(coordinator_snapshot),
            "agent_state": None if agent_state is None else _agent(agent_state, target_domain),
            "consent_scope": {"allow_agent_state": permission["allow_agent_state"]},
        }
        encoded = _canonical(record)
        digest = _digest(encoded)
        with self._transaction() as conn:
            row = conn.execute("SELECT record_json, record_digest FROM director_recovery_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            if row is not None:
                if row["record_json"] != encoded or row["record_digest"] != digest:
                    raise ValueError("conflicting checkpoint_id")
            else:
                count = conn.execute("SELECT COUNT(*) FROM director_recovery_checkpoints").fetchone()[0]
                if count >= _MAX_RECORDS:
                    raise ValueError("recovery checkpoint limit reached")
                if generation <= self._head_generation(conn, owner_id, target_domain):
                    raise ValueError("checkpoint generation is not newer than local history")
                inserted = conn.execute("""INSERT INTO director_recovery_checkpoints
                    (checkpoint_id,owner_id,target_domain,generation,record_json,record_digest)
                    VALUES(?,?,?,?,?,?)""",
                    (checkpoint_id, owner_id, target_domain, generation, encoded, digest))
                if inserted.rowcount != 1:
                    raise RecoveryIntegrityError("checkpoint receipt insert was suppressed")
            self._require_exact_receipt(conn, "checkpoint", checkpoint_id, record)
        self._require_exact_receipt(self._connection(), "checkpoint", checkpoint_id, record)
        return {"checkpoint_id": checkpoint_id, "owner_id": owner_id,
                "target_domain": target_domain, "generation": generation,
                "sha256": digest, "agent_state_included": agent_state is not None,
                "effect_status": "NOT_EXECUTED", "source_authenticated": False}

    def preview_restore(self, checkpoint_id: str, *, approval: Mapping[str, object]) -> dict[str, object]:
        """Read-only preflight; never consumes or activates a snapshot."""
        checkpoint_id = _identifier(checkpoint_id, "checkpoint_id")
        conn = self._connection()
        record = self._read_checkpoint(conn, checkpoint_id)
        permission = _permission(approval, approval=True,
                                 owner_id=record["owner_id"], target_domain=record["target_domain"])
        head = self._head_generation(conn, record["owner_id"], record["target_domain"])
        latest_checkpoint = conn.execute("""SELECT MAX(generation) FROM director_recovery_checkpoints
            WHERE owner_id=? AND target_domain=?""", (record["owner_id"], record["target_domain"])).fetchone()[0]
        previous = conn.execute("""SELECT 1 FROM director_recovery_restores
            WHERE checkpoint_id=? LIMIT 1""", (checkpoint_id,)).fetchone()
        needs_rollback = record["generation"] < latest_checkpoint or previous is not None
        if needs_rollback and not permission["allow_rollback"]:
            raise ValueError("restoring an older or previously restored checkpoint requires explicit rollback approval")
        return {"status": "ELIGIBLE_PREVIEW", "checkpoint_id": checkpoint_id,
                "owner_id": record["owner_id"], "target_domain": record["target_domain"],
                "source_generation": record["generation"],
                "next_recovery_generation": head + 1, "requires_rollback": needs_rollback,
                "agent_state_included": record["agent_state"] is not None,
                "authority": "NOT_GRANTED", "effect_status": "NOT_EXECUTED",
                "owner_authenticated": False, "runtime_activated": False}

    def restore(
        self, checkpoint_id: str, *, restore_id: str,
        approval: Mapping[str, object],
    ) -> dict[str, object]:
        """Atomically persist a new recovered data record and replay receipt.

        Retrieve by ``get_restore`` after a caller crash. The record itself is
        not applied to any active coordinator or agent; a future host-owned
        effect boundary must authorize and atomically activate it separately.
        """
        restore_id = _identifier(restore_id, "restore_id")
        checkpoint_id = _identifier(checkpoint_id, "checkpoint_id")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM director_recovery_restores WHERE restore_id=?", (restore_id,)).fetchone():
                raise ValueError("restore_id already consumed; use get_restore to recover its receipt")
            snapshot = self._read_checkpoint(conn, checkpoint_id)
            permission = _permission(approval, approval=True,
                                     owner_id=snapshot["owner_id"], target_domain=snapshot["target_domain"])
            latest_cp = conn.execute("""SELECT MAX(generation) FROM director_recovery_checkpoints
                WHERE owner_id=? AND target_domain=?""", (snapshot["owner_id"], snapshot["target_domain"])).fetchone()[0]
            needs_rollback = snapshot["generation"] < latest_cp
            if needs_rollback and not permission["allow_rollback"]:
                raise ValueError("explicit rollback approval required for older source checkpoint")
            previously_restored = conn.execute("""SELECT 1 FROM director_recovery_restores
                WHERE checkpoint_id=? LIMIT 1""", (checkpoint_id,)).fetchone()
            if previously_restored and not permission["allow_rollback"]:
                raise ValueError("checkpoint already restored; repeat requires explicit rollback approval")
            count = conn.execute("SELECT COUNT(*) FROM director_recovery_restores").fetchone()[0]
            if count >= _MAX_RECORDS:
                raise ValueError("recovery restore limit reached")
            generation = self._head_generation(conn, snapshot["owner_id"], snapshot["target_domain"]) + 1
            if generation > 2**31 - 1:
                raise ValueError("recovery generation exhausted")
            recovered = {
                "schema_version": SCHEMA_VERSION, "restore_id": restore_id,
                "checkpoint_id": checkpoint_id, "owner_id": snapshot["owner_id"],
                "target_domain": snapshot["target_domain"],
                "source_generation": snapshot["generation"],
                "recovery_generation": generation,
                "rollback_requested": needs_rollback or bool(previously_restored),
                "coordinator_snapshot": snapshot["coordinator_snapshot"],
                "agent_state": snapshot["agent_state"],
                "authority": "NOT_GRANTED", "effect_status": "NOT_EXECUTED",
                "runtime_activated": False, "owner_authenticated": False,
            }
            encoded = _canonical(recovered)
            digest = _digest(encoded)
            inserted = conn.execute("""INSERT INTO director_recovery_restores
                (restore_id,checkpoint_id,owner_id,target_domain,generation,record_json,record_digest)
                VALUES(?,?,?,?,?,?,?)""",
                (restore_id, checkpoint_id, snapshot["owner_id"], snapshot["target_domain"],
                 generation, encoded, digest))
            if inserted.rowcount != 1:
                raise RecoveryIntegrityError("restore receipt insert was suppressed")
            self._require_exact_receipt(conn, "restore", restore_id, recovered)
        self._require_exact_receipt(self._connection(), "restore", restore_id, recovered)
        return json.loads(encoded)

    def get_restore(self, restore_id: str) -> dict[str, object]:
        """Recover the exact admitted record after restart or lost response."""
        restore_id = _identifier(restore_id, "restore_id")
        return self._read_restore(self._connection(), restore_id)


__all__ = ["RecoveryStore", "RecoveryIntegrityError", "SCHEMA_VERSION"]
