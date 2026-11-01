"""Explicit, full-fidelity *paused quarantine* copies of Director coordinator DBs.

Unlike ``RecoveryStore`` (which archives an inspection projection), this API
copies a complete, consistent SQLite coordinator database. It validates the
reference schema, requires the source to be paused, inserts a durable lock in
the destination, and publishes to a NEW path without overwriting anything.
It never opens a mailbox, sends/cancels missions, starts workers, restores keys,
unpauses a coordinator, or confers host authority. The coordinator must enforce
``director_recovery_quarantine.locked`` on every dispatch path.

This is a trusted, operator-owned same-host file workflow, not an untrusted DB
loader, malicious-host defense, or cross-device live failover. The source must
stay quiescent throughout copying. Integrity digests catch accidental changes,
not tampering by a privileged writer. POSIX protection requires a private
parent directory as well as owner-only database/WAL/SHM files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from .mission_graph import MissionGraph
from .strategy import generate_candidates
from .world_state import WorldState


SCHEMA = "director.recovery-quarantine.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_APPROVAL = frozenset({"approved", "purpose", "source_goal_id", "destination_path",
                       "allow_historical_inflight"})
_REQUIRED_TABLES = frozenset({
    "director_strategy_config", "director_strategy_control",
    "director_strategy_events", "director_strategy_assignments",
    "director_strategy_cancellations", "director_strategy_reports",
    "director_plan_graph", "director_plan_completions",
    "director_strategy_auth_policy",
})
_MAX_DB_BYTES = 64 * 1024 * 1024
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _id(value: object, label: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _private_regular(file: Path, *, required: bool) -> bool:
    """Inspect the named DB or SQLite sidecar before any SQLite file opens."""
    if file.is_symlink():
        raise ValueError("recovery path or SQLite journal must not be a symlink")
    try:
        info = file.stat()
    except FileNotFoundError:
        if required:
            raise ValueError("source coordinator database is missing") from None
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("recovery path or SQLite journal must be a regular file")
    if os.name == "posix" and info.st_mode & 0o077:
        raise ValueError("coordinator database and journal must be owner-only")
    return True


def _source_path(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
        raise ValueError("existing source coordinator path required")
    source = Path(path).absolute()
    if not source.parent.is_dir() or source.parent.is_symlink():
        raise ValueError("source database parent must be an existing directory")
    if os.name == "posix" and source.parent.stat().st_mode & 0o022:
        raise ValueError("source coordinator parent must not be writable by other users")
    _private_regular(source, required=True)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if _private_regular(sidecar, required=False) and sidecar.stat().st_size > _MAX_DB_BYTES:
            raise ValueError("source SQLite journal exceeds bounded size")
    if source.stat().st_size > _MAX_DB_BYTES:
        raise ValueError("source coordinator database exceeds bounded size")
    return source


def _destination_path(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
        raise ValueError("new destination path required")
    destination = Path(path).absolute()
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("destination must have an existing, nonsymlink parent")
    if os.name == "posix" and parent.stat().st_mode & 0o077:
        raise ValueError("destination parent must be private to its owner")
    # No overwrite or reuse; reject even a dangling symlink or sidecars.
    for candidate in (destination, Path(str(destination) + "-wal"),
                      Path(str(destination) + "-shm")):
        if candidate.is_symlink() or candidate.exists():
            raise FileExistsError("destination coordinator or SQLite journal already exists")
    return destination


def _approval(raw: Mapping[str, object], destination: Path) -> str:
    if not isinstance(raw, Mapping) or set(raw) != _APPROVAL:
        raise ValueError("exact operator approval fields are required")
    if (raw["approved"] is not True or raw["purpose"] != "PAUSED_QUARANTINE_DATA_ONLY"
            or raw["allow_historical_inflight"] is not False):
        raise ValueError("recovery requires explicit no-activation approval")
    approved_path = raw["destination_path"]
    # Windows may spell the same temporary directory using its short (8.3)
    # component or expanded name. Bind to the same absolute filesystem target,
    # not to an incidental spelling produced by Path.absolute()/resolve().
    if (type(approved_path) is not str or not Path(approved_path).is_absolute()
            or Path(approved_path).resolve(strict=False) != destination.resolve(strict=False)):
        raise ValueError("approval must bind the exact absolute destination path")
    return _id(raw["source_goal_id"], "source_goal_id")


def _row(conn: sqlite3.Connection, table: str, fields: str = "*") -> sqlite3.Row:
    row = conn.execute(f"SELECT {fields} FROM {table} WHERE singleton=1").fetchone()
    if row is None:
        raise ValueError(f"missing single-instance {table} configuration")
    return row


def _signed_intent(
    encoded: object, *, recipient: str, message_id: str, kind: str,
    expected_link: str, expected_body: Mapping[str, object],
) -> None:
    """Check archived envelope shape/binding, not cryptographic validity."""
    if type(encoded) is not str or len(encoded) > 16384:
        raise ValueError("persisted signed intent must be bounded text")
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
        raise ValueError("persisted signed intent is malformed")
    if (raw.get("protocol") != "director.authenticated-envelope.v1"
            or raw.get("recipient_id") != recipient or raw.get("sender_id") != "director." + recipient
            or raw.get("message_id") != message_id or raw.get("kind") != kind
            or raw["payload"].get(expected_link) != message_id
            or raw["payload"].get("body") != dict(expected_body)
            or type(raw.get("mac")) is not str or not _HEX64.fullmatch(raw["mac"])):
        raise ValueError("persisted signed intent mismatches destination/mission")


def _validate_snapshot(conn: sqlite3.Connection, expected_goal_id: str) -> tuple[str, str]:
    """Validate known contracts on the staged copy, never live source state."""
    check = conn.execute("PRAGMA quick_check").fetchall()
    if len(check) != 1 or check[0][0] != "ok":
        raise ValueError("source SQLite integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source coordinator has invalid foreign keys")
    schema = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not _REQUIRED_TABLES <= schema:
        raise ValueError("source is missing Director coordinator schema")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
        raise ValueError("source coordinator contains unreviewed SQL trigger/view")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='director_recovery_quarantine'").fetchone():
        if conn.execute("SELECT 1 FROM director_recovery_quarantine LIMIT 1").fetchone():
            raise ValueError("a quarantined database cannot be used as a new live source")

    config = json.loads(_row(conn, "director_strategy_config", "config_json")[0])
    if not isinstance(config, dict) or set(config) != {"goal", "capabilities", "max_missions"}:
        raise ValueError("source coordinator configuration has unsupported fields")
    if _id(config["goal"].get("goal_id"), "goal_id") != expected_goal_id:
        raise ValueError("recovery approval does not match source goal")
    caps = config["capabilities"]
    quota = config["max_missions"]
    if (not isinstance(caps, list) or not 1 <= len(caps) <= 32
            or type(quota) is not int or not 1 <= quota <= 100):
        raise ValueError("invalid coordinator capability catalog or quota")
    generate_candidates(config["goal"], caps, WorldState().snapshot(), max_candidates=8)
    capability_ids = {cap["capability_id"] for cap in caps}
    agents = {cap["capability_id"]: cap["agent_id"] for cap in caps}
    control = _row(conn, "director_strategy_control", "paused,stop_requested")
    if control["paused"] != 1 or control["stop_requested"] not in (0, 1):
        raise ValueError("source coordinator must be explicitly paused")
    auth = _row(conn, "director_strategy_auth_policy", "mode")[0]
    if auth not in ("LOCAL_HMAC", "LOCAL_HMAC_BIDIRECTIONAL", "LOCAL_UNAUTHENTICATED"):
        raise ValueError("invalid persisted source authentication policy")

    world = WorldState()
    events: set[str] = set()
    for entry in conn.execute("""SELECT event_id,event_json,operator_attested,success_confirmed
        FROM director_strategy_events ORDER BY event_seq"""):
        event = json.loads(entry["event_json"])
        if event["event_id"] != entry["event_id"]:
            raise ValueError("event ID/record mismatch")
        if entry["operator_attested"] not in (0, 1) or entry["success_confirmed"] not in (0, 1):
            raise ValueError("invalid recorded operator attestation")
        if entry["success_confirmed"] and not entry["operator_attested"]:
            raise ValueError("success cannot be confirmed without operator review")
        world.ingest(event)
        events.add(event["event_id"])
    graph = conn.execute("SELECT graph_json,unavailable_json,now_tick FROM director_plan_graph WHERE singleton=1").fetchone()
    graph_ids: set[str] = set()
    if graph:
        declared = json.loads(graph["graph_json"])
        model = MissionGraph(declared["nodes"], declared["resource_capacities"])
        graph_ids = {node["mission_id"] for node in model.snapshot()["nodes"]}
        if not graph_ids <= capability_ids:
            raise ValueError("graph capabilities differ from configured catalog")
        unavailable = json.loads(graph["unavailable_json"])
        if (not isinstance(unavailable, list) or len(set(unavailable)) != len(unavailable)
                or not set(unavailable) <= set(declared["resource_capacities"])):
            raise ValueError("invalid graph resource outage")
        if type(graph["now_tick"]) is not int or not 0 <= graph["now_tick"] <= 1_000_000_000:
            raise ValueError("invalid graph logical clock")

    assignment_columns = {row["name"] for row in conn.execute(
        "PRAGMA table_info(director_strategy_assignments)")}
    cancellation_columns = {row["name"] for row in conn.execute(
        "PRAGMA table_info(director_strategy_cancellations)")}
    assigned: dict[str, dict[str, object]] = {}
    for entry in conn.execute("SELECT * FROM director_strategy_assignments"):
        payload = json.loads(entry["payload_json"])
        cap = payload.get("capability_id")
        if (cap not in capability_ids or entry["recipient_id"] != agents[cap]
                or entry["candidate_id"] != payload.get("mission_id")
                or type(entry["world_revision"]) is not int
                or not 0 <= entry["world_revision"] <= world.snapshot()["revision"]
                or entry["state"] not in ("QUEUED", "SENT", "REPORTED")
                or entry["control_state"] not in ("ACTIVE", "CANCEL_REQUESTED", "CANCEL_ACK_CLAIMED")):
            raise ValueError("source assignment/capability contract invalid")
        assigned[entry["message_id"]] = dict(entry)
        if "outbound_envelope_json" in assignment_columns:
            envelope = entry["outbound_envelope_json"]
            if envelope is not None:
                if auth != "LOCAL_HMAC_BIDIRECTIONAL":
                    raise ValueError("legacy assignment unexpectedly contains a signed intent")
                _signed_intent(envelope, recipient=entry["recipient_id"],
                               message_id=entry["message_id"], kind="MISSION_ASSIGNMENT",
                               expected_link="assignment_message_id", expected_body=payload)
            elif auth == "LOCAL_HMAC_BIDIRECTIONAL" and entry["state"] != "QUEUED":
                raise ValueError("sent signed assignment is missing its durable envelope")
    for entry in conn.execute("SELECT * FROM director_strategy_cancellations"):
        if (entry["assignment_message_id"] not in assigned
                or entry["state"] not in ("QUEUED", "SENT", "ACK_CLAIMED")):
            raise ValueError("source cancellation invariant invalid")
        if "outbound_envelope_json" in cancellation_columns:
            encoded = entry["outbound_envelope_json"]
            if encoded is not None:
                if auth != "LOCAL_HMAC_BIDIRECTIONAL":
                    raise ValueError("legacy cancellation unexpectedly contains a signed intent")
                linked = assigned[entry["assignment_message_id"]]
                _signed_intent(encoded, recipient=linked["recipient_id"],
                               message_id=entry["cancellation_message_id"], kind="MISSION_CANCEL",
                               expected_link="cancellation_message_id",
                               expected_body={"assignment_message_id": entry["assignment_message_id"],
                                              "reason": entry["reason"],
                                              "effect_authority": "NOT_GRANTED",
                                              "worker_stopped_verified": False})
            elif auth == "LOCAL_HMAC_BIDIRECTIONAL" and entry["state"] != "QUEUED":
                raise ValueError("sent signed cancellation is missing durable envelope")
    for entry in conn.execute("""SELECT node_id,assignment_message_id,observation_event_id
        FROM director_plan_completions"""):
        if (entry["node_id"] not in graph_ids or entry["assignment_message_id"] not in assigned
                or entry["observation_event_id"] not in events):
            raise ValueError("source graph completion references unknown evidence")
    if "director_authenticated_admissions" in schema:
        for entry in conn.execute("""SELECT sender_id,signed_message_id,nonce,
            wrapper_message_id,envelope_sha256,admitted_at FROM director_authenticated_admissions"""):
            if (not _ID.fullmatch(entry["sender_id"]) or not _ID.fullmatch(entry["signed_message_id"])
                    or not _HEX32.fullmatch(entry["nonce"])
                    or not _ID.fullmatch(entry["wrapper_message_id"])
                    or not _HEX64.fullmatch(entry["envelope_sha256"])
                    or type(entry["admitted_at"]) is not int):
                raise ValueError("invalid persisted authenticated receipt admission")

    # A content-level digest is an accidental-integrity aid; it is NOT a
    # signature or hostile database-author verification. Include future
    # coordinator-owned append-only tables (e.g., authenticated receipt
    # admissions), not merely the historical v0.4 schema list.
    digest = hashlib.sha256()
    for table in sorted(schema):
        if not table.startswith("director_") or table == "director_recovery_quarantine":
            continue
        if not re.fullmatch(r"director_[A-Za-z0-9_]{1,80}", table):
            raise ValueError("unsupported coordinator table name")
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(table.encode("ascii") + b"\0")
            digest.update(json.dumps(tuple(row), ensure_ascii=True, allow_nan=False,
                                     separators=(",", ":")).encode("ascii"))
            digest.update(b"\n")
    return auth, digest.hexdigest()


def stage_recovery_copy(
    source_path: str | Path,
    destination_path: str | Path,
    *, operator_approval: Mapping[str, object],
) -> dict[str, object]:
    """Copy a paused coordinator into a NEW, permanently quarantined database.

    This function does not turn a checkpoint into active authority. No unlock
    operation exists. The original mailbox/auth ledgers are not restored and
    pending assignment rows are historical, inert information only.
    """
    source = _source_path(source_path)
    destination = _destination_path(destination_path)
    if source.resolve() == destination.resolve(strict=False):
        raise ValueError("source and destination must be distinct coordinator files")
    goal_id = _approval(operator_approval, destination)

    # Use a private staging file in the destination's own directory. A hard
    # link is then an atomic no-clobber publish; crashes before publish leave
    # only an orphaned staging file, never an activated coordinator.
    descriptor, temporary = tempfile.mkstemp(
        prefix=".director-quarantine-", suffix=".sqlite3", dir=destination.parent)
    os.close(descriptor)
    staging = Path(temporary)
    published = False
    try:
        source_uri = source.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as src:
            src.row_factory = sqlite3.Row
            with closing(sqlite3.connect(staging, timeout=5.0)) as target:
                target.row_factory = sqlite3.Row
                src.backup(target)

        if staging.stat().st_size > _MAX_DB_BYTES:
            raise ValueError("staged coordinator database exceeds bounded size")
        with closing(sqlite3.connect(staging, timeout=5.0, isolation_level=None)) as stage:
            stage.row_factory = sqlite3.Row
            stage.execute("PRAGMA foreign_keys=ON")
            auth_mode, digest = _validate_snapshot(stage, goal_id)
            stage.execute("BEGIN IMMEDIATE")
            try:
                stage.execute("""CREATE TABLE IF NOT EXISTS director_recovery_quarantine (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    locked INTEGER NOT NULL CHECK(locked=1),
                    schema_version TEXT NOT NULL,
                    source_goal_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    source_auth_mode TEXT NOT NULL
                )""")
                stage.execute("""INSERT INTO director_recovery_quarantine(
                    singleton,locked,schema_version,source_goal_id,source_digest,source_auth_mode)
                    VALUES(1,1,?,?,?,?)""", (SCHEMA, goal_id, digest, auth_mode))
                stage.execute("COMMIT")
            except BaseException:
                stage.execute("ROLLBACK")
                raise
            mode = stage.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise ValueError("staged recovery must checkpoint into a single closed SQLite file")
            stage.execute("PRAGMA synchronous=FULL")

        # sqlite3.close() above must precede link on Windows. On POSIX hard
        # linking a private regular file is atomic and never replaces an
        # existing path. If the filesystem cannot hard-link, fail closed.
        if _destination_path(destination) != destination:
            raise ValueError("destination path changed during staging")
        os.link(staging, destination)
        published = True
        if os.name == "posix":
            with open(destination, "rb") as saved:
                os.fsync(saved.fileno())
            try:
                dirfd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            except OSError:
                # Data remains quarantined. Directory durability depends on
                # host/filesystem behavior and is not an absolute guarantee.
                pass
        return {"destination_path": str(destination), "source_goal_id": goal_id,
                "quarantined": True, "recovery_schema": SCHEMA,
                "snapshot_digest": digest, "auth_mode": auth_mode,
                "effect_status": "NOT_EXECUTED", "sender_authenticated": False}
    finally:
        # Only clean the staging file created by this invocation, never the
        # destination (including when the caller's path already existed).
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            if not published:
                raise


__all__ = ["SCHEMA", "stage_recovery_copy"]
