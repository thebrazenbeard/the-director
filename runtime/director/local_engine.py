"""Opt-in finite local JSON acquisition; assertions, never execution authority.

Only operator registration selects paths and scalar keys. POSIX no-follow walks
are supported; Windows is deliberately unsupported. Deadlines are cooperative:
Python cannot interrupt a kernel-stalled filesystem syscall. No hard real-time
or hostile-host isolation guarantee is made.
Worst-case wall time is unbounded if the filesystem or SQLite kernel I/O
stalls; assessment and final reporting also require synchronous database reads.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time

from .world_state import _identifier, _scalar
from .reasoning import LocalReasoner, _parse_hypotheses


ADAPTER = "director.local-json-scalar.v1"
CLASSIFICATION = "UNVERIFIED_SOURCE_CONTENT"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path, limit, deadline):
    """Read exactly one opened regular descriptor, never follow any symlink."""
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or os.open not in os.supports_dir_fd):
        raise ValueError("safe local adapter unsupported on this platform")
    if time.monotonic() >= deadline:
        raise ValueError("local read deadline expired")
    parts = path.split("/")[1:]
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    data = bytearray()
    try:
        for component in parts[:-1]:
            if time.monotonic() >= deadline:
                raise ValueError("local read deadline expired")
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("source is not a bounded regular file")
        while len(data) < limit:
            if time.monotonic() >= deadline:
                raise ValueError("local read deadline expired")
            chunk = os.read(fd, min(4096, limit - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(before, f) != getattr(after, f) for f in fields)
                or len(data) != before.st_size):
            raise ValueError("source changed during acquisition")
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)
    # Bound nesting before parsing (including ignored/unselected content).
    depth = 0
    quoted = escaped = False
    for char in data.decode("utf-8"):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 16:
                raise ValueError("JSON nesting exceeds bound")
        elif char in "]}":
            depth -= 1
    def reject_constant(_):
        raise ValueError("nonfinite JSON value")
    parsed = json.loads(data, object_pairs_hook=_pairs, parse_constant=reject_constant)
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        elif isinstance(value, list):
            for child in value:
                finite(child)
    finite(parsed)
    if not isinstance(parsed, dict):
        raise ValueError("JSON source must be an object")
    return parsed, hashlib.sha256(data).hexdigest(), len(data)


class LocalReasoningEngine:
    """Trusted operator API, not a worker/model tool or background service."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        # Explicit opt-in schema, never installed by legacy coordinator opens.
        with coordinator._transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS director_local_sources (
                source_id TEXT PRIMARY KEY, registration_json TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS director_local_receipts (
                semantic_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                receipt_json TEXT NOT NULL)""")

    def register_source(self, *, source_id, capability_id, metric, path, key):
        """Register immutable operator-selected mapping; never consumes JSON policy."""
        for label, value in (("source_id", source_id), ("capability_id", capability_id),
                             ("metric", metric), ("key", key)):
            _identifier(value, label)
        path = os.fspath(path)
        if (not isinstance(path, str) or len(path) > 4096 or not path.startswith("/")
                or any(p in ("", ".", "..") for p in path.split("/")[1:])):
            raise ValueError("source requires a normalized absolute POSIX path")
        registration = dict(source_id=source_id, capability_id=capability_id,
                            metric=metric, path=path, key=key, adapter=ADAPTER)
        encoded = _json(registration)
        with self.coordinator._transaction() as conn:
            caps = self.coordinator._config(conn)["capabilities"]
            if not any(c["capability_id"] == capability_id and c["metric"] == metric for c in caps):
                raise ValueError("source must match registered capability and metric")
            old = conn.execute("SELECT registration_json FROM director_local_sources WHERE source_id=?",
                               (source_id,)).fetchone()
            if old:
                if old[0] != encoded:
                    raise ValueError("source registration is immutable")
                return
            if conn.execute("SELECT COUNT(*) FROM director_local_sources").fetchone()[0] >= 32:
                raise ValueError("source registry full")
            conn.execute("INSERT INTO director_local_sources VALUES (?,?)", (source_id, encoded))

    @staticmethod
    def _blocked(assessment):
        control = assessment["operator_control"]
        return (assessment["recovery_quarantined"] or control["paused"]
                or control["stop_requested"])

    def run(self, *, hypotheses=(), max_steps=4, max_bytes=65536, max_seconds=2):
        """Finite read/reassess loop; no tick, dispatch, ACK, attestation or DAG completion."""
        if type(max_steps) is not int or not 1 <= max_steps <= 16:
            raise ValueError("max_steps must be 1..16")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
            raise ValueError("max_bytes must be 1..1048576")
        if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 30:
            raise ValueError("max_seconds must be positive and at most 30")
        started = time.monotonic()
        deadline = started + max_seconds
        hypotheses = _parse_hypotheses(hypotheses)
        coordinator = self.coordinator
        before = coordinator.assess_decision_cycle(hypotheses=hypotheses)
        current = before
        reads = byte_count = 0
        acquisitions = []
        stop = "STEP_BUDGET_EXHAUSTED"
        for _ in range(max_steps):
            if self._blocked(current):
                stop = "CONTROL_BLOCKED"
                break
            if time.monotonic() >= deadline or byte_count >= max_bytes:
                stop = "RESOURCE_BUDGET_EXHAUSTED"
                break
            conn = coordinator._connection()
            sources = [json.loads(row[0]) for row in conn.execute(
                "SELECT registration_json FROM director_local_sources ORDER BY source_id")]
            selected = None
            for probe in current["probe_proposals"]:
                for source in sources:
                    if (source["metric"] == probe["metric"] and source["capability_id"]
                            in probe["graph_ready_capability_ids"]):
                        selected = source
                        break
                if selected:
                    break
            if selected is None:
                stop = ("DEPENDENCY_BLOCKED_OPERATOR_REVIEW_REQUIRED" if any(
                    p["mapping_status"] == "DAG_BLOCKED" for p in current["probe_proposals"])
                    else "NO_SUPPORTED_NEW_MEASUREMENT")
                break
            # A fresh snapshot fences the decision immediately before any file I/O.
            fresh = coordinator.assess_decision_cycle(hypotheses=hypotheses)
            if self._blocked(fresh) or fresh["input_sha256"] != current["input_sha256"]:
                stop = "STALE_OR_CONTROL_BLOCKED"
                break
            if time.monotonic() >= deadline:
                stop = "RESOURCE_BUDGET_EXHAUSTED"
                break
            reads += 1
            try:
                parsed, raw_hash, size = _read_json(selected["path"], max_bytes-byte_count, deadline)
                byte_count += size
                value = _scalar(parsed[selected["key"]])
            except (ValueError, OSError, KeyError, UnicodeError, RecursionError):
                # Conservatively charge the entire remaining budget on failed I/O.
                byte_count = max_bytes
                stop = "SOURCE_REFUSED"
                break
            semantic_value = float(value) if type(value) in (int, float) else value
            semantic = _digest([selected["metric"], type(semantic_value).__name__, semantic_value])
            event_id = "local." + semantic[:58]
            receipt = dict(source_registration=selected, registration_sha256=_digest(selected),
                           acquisition_sha256=raw_hash, raw_bytes_sha256=raw_hash,
                           bytes_read=size, selected_value=value, adapter_version=ADAPTER,
                           classification=CLASSIFICATION, event_id=event_id,
                           assessment_sha256=current["input_sha256"],
                           before_revision=current["world_revision"], admission="NOT_ADMITTED")
            acquisitions.append(receipt)
            if time.monotonic() >= deadline:
                stop = "RESOURCE_BUDGET_EXHAUSTED"
                break
            # Event and receipt share one atomic transaction, AFTER filesystem I/O.
            try:
                with coordinator._transaction() as conn:
                    check = coordinator._assess_decision_cycle_in_transaction(conn, hypotheses=hypotheses)
                    if self._blocked(check) or check["input_sha256"] != current["input_sha256"]:
                        stop = "STALE_OR_CONTROL_BLOCKED"
                        break
                    if time.monotonic() >= deadline:
                        stop = "RESOURCE_BUDGET_EXHAUSTED"
                        break
                    if conn.execute("SELECT 1 FROM director_local_receipts WHERE semantic_id=?",
                                    (semantic,)).fetchone():
                        stop = "UNCHANGED_METRIC_VALUE"
                        break
                    # Older ledgers may contain externally preempted IDs. Never
                    # overwrite, retry forever or adopt an unreceipted event.
                    if conn.execute("SELECT 1 FROM director_strategy_events WHERE event_id=?",
                                    (event_id,)).fetchone():
                        stop = "REFUSED_ID_COLLISION"
                        break
                    world = coordinator._world(conn)
                    event = dict(event_id=event_id, claim_id=selected["metric"], value=value,
                                 evidence_class="OBSERVATION", source_ref=selected["source_id"],
                                 lineage_id=selected["source_id"])
                    world.ingest(event)
                    receipt.update(admission="ADMITTED", after_revision=world.revision)
                    conn.execute("INSERT INTO director_strategy_events(event_id,event_json) VALUES (?,?)",
                                 (event_id, _json(event)))
                    conn.execute("INSERT INTO director_local_receipts VALUES (?,?,?)",
                                 (semantic, event_id, _json(receipt)))
            except RuntimeError:
                stop = "CONTROL_BLOCKED_AFTER_READ"
                break
            except (sqlite3.DatabaseError, ValueError):
                receipt["admission"] = "NOT_ADMITTED"
                receipt.pop("after_revision", None)
                stop = "ADMISSION_REFUSED"
                break
            current = coordinator.assess_decision_cycle(hypotheses=hypotheses)
            if coordinator._world(conn).snapshot()["facts"][selected["metric"]]["status"] == "CONTESTED":
                stop = "CONFLICT_REQUIRES_OPERATOR_REVIEW"
                break
            if time.monotonic() >= deadline:
                stop = "RESOURCE_BUDGET_EXHAUSTED"
                break
        after = coordinator.assess_decision_cycle(hypotheses=hypotheses)
        world = coordinator._world(coordinator._connection()).snapshot()
        return dict(before=before, after=after, stop_reason=stop, read_attempts=reads,
                    bytes_charged=byte_count, acquisitions=acquisitions,
                    conditional_hypotheses=LocalReasoner().compare_hypotheses(world, hypotheses),
                    all_hypothesis_ids=[h["hypothesis_id"] for h in hypotheses],
                    classification=CLASSIFICATION, truth_status="NOT_ESTABLISHED",
                    success_confirmed=False, authorization="NOT_GRANTED",
                    local_io_status=("LOCAL_JSON_READ_COMPLETED" if acquisitions
                                     else "READ_ATTEMPTED" if reads else "NOT_READ"),
                    host_execution="NOT_EXECUTED", deadline_kind="COOPERATIVE_NOT_HARD_REALTIME",
                    elapsed_seconds=time.monotonic()-started,
                    worst_case_blocking="UNBOUNDED_KERNEL_OR_DATABASE_IO")

    def receipts(self):
        return [json.loads(row[0]) for row in self.coordinator._connection().execute(
            "SELECT receipt_json FROM director_local_receipts ORDER BY event_id")]
