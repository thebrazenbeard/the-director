"""Opt-in, local strategic coordination with durable analytical mission delivery.

The coordinator consumes an operator-supplied objective and a registered catalog
of *analytical* agent capabilities. It generates missions from the current
world-state snapshot, deposits assignments into a recipient-specific local
mailbox, and records replies as UNVERIFIED claims. It never launches agents,
uses a network, executes commands, or converts messages into trusted evidence.

The two SQLite files (coordinator and mailbox) cannot commit atomically.
Durable assignment intents and deterministic mailbox message IDs make delivery
idempotently recoverable after a crash; there is no cross-host guarantee.
The operator methods here are a local *interface*, not an authentication or
secure-approval boundary. Restrict filesystem/process access accordingly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .messenger import DirectorMailbox
from .stop_policy import evaluate_stop
from .strategy import generate_candidates
from .world_state import WorldState


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_MAX_MISSIONS = 100
_MAX_EVENTS = 1024
_MAX_REPORTS = 4096
_MAX_CONFIG_BYTES = 65536
_MAX_EVENT_BYTES = 4096
_CANCEL_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:;/-]{0,127}\Z")


def _id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _json(value: object, *, limit: int) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("value must be strictly JSON serializable") from exc
    if len(payload.encode("utf-8")) > limit:
        raise ValueError("value exceeds the local storage bound")
    return payload


def _make_database(path: str | Path) -> sqlite3.Connection:
    if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
        raise ValueError("a durable coordinator SQLite file path is required")
    filename = Path(path)
    if filename.is_symlink() or not filename.parent.is_dir():
        raise ValueError("database path must have an existing parent and not be a symlink")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(filename, flags, 0o600)
    except FileExistsError:
        if filename.is_symlink():
            raise ValueError("database file must not be a symlink")
    else:
        os.close(handle)
    conn = sqlite3.connect(filename, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_config (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            config_json TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_control (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            paused INTEGER NOT NULL DEFAULT 0,
            stop_requested INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("INSERT OR IGNORE INTO director_strategy_control(singleton) VALUES(1)")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_events (
            event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE NOT NULL,
            event_json TEXT NOT NULL,
            operator_attested INTEGER NOT NULL DEFAULT 0,
            success_confirmed INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_assignments (
            message_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            world_revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('QUEUED','SENT','REPORTED')),
            reply_message_id TEXT UNIQUE
        )""")
        # Additive migration: earlier local databases have this table but lack
        # a separately persisted control state. Do not rewrite the assignment
        # state CHECK constraint or alter historical assignment identities.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(director_strategy_assignments)")}
        if "control_state" not in columns:
            conn.execute("""ALTER TABLE director_strategy_assignments ADD COLUMN
                control_state TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK(control_state IN ('ACTIVE','CANCEL_REQUESTED','CANCEL_ACK_CLAIMED'))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_cancellations (
            assignment_message_id TEXT PRIMARY KEY REFERENCES director_strategy_assignments(message_id),
            cancellation_message_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('QUEUED','SENT','ACK_CLAIMED'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_reports (
            message_id TEXT PRIMARY KEY,
            request_id TEXT,
            reported_sender_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            review_status TEXT NOT NULL
        )""")
    except BaseException:
        conn.close()
        raise
    return conn


class DirectorCoordinator:
    """Single-machine strategy loop; all effects are explicitly local SQLite writes.

    ``configure`` accepts the complete objective and an *analytical* capability
    catalog once; reconfiguration is not silently permitted. ``observe`` only
    admits declared evidence and does not attest its provenance. ``confirm``
    requires an operator to manually mark observed evidence as reviewed; this
    library cannot authenticate that operator or the original observer.
    """

    def __init__(self, path: str | Path, mailbox: DirectorMailbox):
        if not isinstance(mailbox, DirectorMailbox):
            raise TypeError("mailbox must be an open DirectorMailbox instance")
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable coordinator SQLite file path is required")
        # Two independent databases are required. Their SQLite write
        # transactions cannot nest on the same file when a mission is sent.
        if Path(path).resolve(strict=False) == mailbox.path.resolve(strict=False):
            raise ValueError("coordinator and mailbox must use different database files")
        self.mailbox = mailbox
        self._conn: sqlite3.Connection | None = _make_database(path)

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            connection.close()

    def __enter__(self) -> "DirectorCoordinator":
        self._connection()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DirectorCoordinator is closed")
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def configure(
        self,
        goal: Mapping[str, object],
        capabilities: Sequence[Mapping[str, object]],
        *,
        max_missions: int = 8,
    ) -> dict[str, object]:
        """Persist one declared objective and bounded local analytical roster.

        Idempotent with exactly identical configuration; changes require a new
        coordinator database (or an explicit future versioned migration).
        """
        if type(max_missions) is not int or not 1 <= max_missions <= _MAX_MISSIONS:
            raise ValueError("max_missions must be a bounded positive integer")
        if not isinstance(goal, Mapping) or not isinstance(capabilities, (list, tuple)):
            raise ValueError("goal and capabilities must be declared mappings and sequence")
        if not 1 <= len(capabilities) <= 32 or any(not isinstance(cap, Mapping) for cap in capabilities):
            raise ValueError("capability catalog must contain 1..32 mappings")
        _id(goal.get("goal_id"), "goal_id")
        # Validate input by exercising the generator with an empty reference
        # world, but do not emit or assume a generated mission here.
        generate_candidates(goal, capabilities, WorldState().snapshot(), max_candidates=8)
        config = {"goal": dict(goal), "capabilities": [dict(cap) for cap in capabilities],
                  "max_missions": max_missions}
        encoded = _json(config, limit=_MAX_CONFIG_BYTES)
        with self._transaction() as conn:
            row = conn.execute("SELECT config_json FROM director_strategy_config WHERE singleton=1").fetchone()
            if row is None:
                conn.execute("INSERT INTO director_strategy_config(singleton,config_json) VALUES(1,?)", (encoded,))
            elif row["config_json"] != encoded:
                raise ValueError("coordinator configuration already exists and is immutable")
        return json.loads(encoded)

    @staticmethod
    def _config(conn: sqlite3.Connection) -> dict[str, object]:
        row = conn.execute("SELECT config_json FROM director_strategy_config WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("configure an objective before stepping")
        return json.loads(row["config_json"])

    @staticmethod
    def _world(conn: sqlite3.Connection) -> WorldState:
        world = WorldState()
        for row in conn.execute("SELECT event_json FROM director_strategy_events ORDER BY event_seq"):
            world.ingest(json.loads(row["event_json"]))
        return world

    def observe(self, event: Mapping[str, object]) -> dict[str, object]:
        """Record declared evidence; never infer source authentication from it."""
        if not isinstance(event, Mapping):
            raise ValueError("event must be a mapping")
        encoded = _json(dict(event), limit=_MAX_EVENT_BYTES)
        _id(event.get("event_id"), "event_id")
        # Validate full schema and consistency against persisted events before
        # committing; conflicts are never silently promoted to observations.
        with self._transaction() as conn:
            row = conn.execute("SELECT event_json FROM director_strategy_events WHERE event_id=?", (event["event_id"],)).fetchone()
            if row is not None:
                if row["event_json"] != encoded:
                    raise ValueError("conflicting content for existing event_id")
            else:
                size = conn.execute("SELECT COUNT(*) FROM director_strategy_events").fetchone()[0]
                if size >= _MAX_EVENTS:
                    raise ValueError("event ledger is full")
                world = self._world(conn)
                world.ingest(dict(event))
                conn.execute("INSERT INTO director_strategy_events(event_id,event_json) VALUES(?,?)", (event["event_id"], encoded))
            snapshot = self._world(conn).snapshot()
        return snapshot

    def confirm_observation(self, event_id: str, *, confirms_success: bool = False) -> None:
        """Record a *declared* human review; this is not cryptographic attestation.

        Hypothetical evidence can never satisfy success, even if confirmed.
        An explicit second condition (``confirms_success``) is required for
        the stop policy to consider an operator-verified metric value.
        """
        event_id = _id(event_id, "event_id")
        if type(confirms_success) is not bool:
            raise ValueError("confirms_success must be bool")
        with self._transaction() as conn:
            row = conn.execute("SELECT event_json FROM director_strategy_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            event = json.loads(row["event_json"])
            if event.get("evidence_class") != "OBSERVATION":
                raise ValueError("only observed evidence can be confirmed")
            conn.execute("""UPDATE director_strategy_events
                SET operator_attested=1,
                    success_confirmed=MAX(success_confirmed, ?)
                WHERE event_id=?""", (int(confirms_success), event_id))

    def set_paused(self, paused: bool) -> None:
        if type(paused) is not bool:
            raise ValueError("paused must be bool")
        with self._transaction() as conn:
            conn.execute("UPDATE director_strategy_control SET paused=? WHERE singleton=1", (int(paused),))

    def request_stop(self) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE director_strategy_control SET stop_requested=1 WHERE singleton=1")
            active = conn.execute("""SELECT message_id FROM director_strategy_assignments
                WHERE state IN ('QUEUED','SENT') AND control_state='ACTIVE'""").fetchall()
            for row in active:
                self._queue_cancel_intent(conn, row["message_id"], "OPERATOR_STOP")
        # A stop also attempts cooperative revocation of already sent local
        # assignments. Neither delivery nor ACK proves the worker has halted.
        self._flush_cancellations()

    @staticmethod
    def _queue_cancel_intent(conn: sqlite3.Connection, message_id: str, reason: str) -> dict[str, object]:
        row = conn.execute("""SELECT message_id, state, control_state FROM director_strategy_assignments
            WHERE message_id=?""", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        existing = conn.execute("""SELECT * FROM director_strategy_cancellations
            WHERE assignment_message_id=?""", (message_id,)).fetchone()
        if existing is not None:
            if existing["reason"] != reason:
                raise ValueError("a cancellation with another reason already exists")
            return {"assignment_message_id": message_id,
                    "cancellation_message_id": existing["cancellation_message_id"],
                    "state": existing["state"]}
        if row["state"] == "REPORTED":
            raise ValueError("cannot cancel an already reported assignment")
        cancel_id = "cancel." + hashlib.sha256(message_id.encode("ascii")).hexdigest()[:40]
        conn.execute("""INSERT INTO director_strategy_cancellations(
            assignment_message_id,cancellation_message_id,reason,state)
            VALUES(?,?,?,'QUEUED')""", (message_id, cancel_id, reason))
        conn.execute("""UPDATE director_strategy_assignments
            SET control_state='CANCEL_REQUESTED' WHERE message_id=?""", (message_id,))
        return {"assignment_message_id": message_id,
                "cancellation_message_id": cancel_id, "state": "QUEUED"}

    def cancel_mission(self, message_id: str, *, reason: str = "OPERATOR_ABORT") -> dict[str, object]:
        """Cooperatively revoke an analytical assignment by typed local notice.

        A cancellation *request* is durable. Sending/acknowledging it cannot
        prove the receiver stopped a worker; this reference never runs one.
        The caller here is not an authenticated external operator.
        """
        message_id = _id(message_id, "message_id")
        if not isinstance(reason, str) or not _CANCEL_REASON.fullmatch(reason):
            raise ValueError("reason must be bounded and contain no control characters")
        with self._transaction() as conn:
            receipt = self._queue_cancel_intent(conn, message_id, reason)
        self._flush_cancellations()
        with self._transaction() as conn:
            row = conn.execute("""SELECT state FROM director_strategy_cancellations
                WHERE assignment_message_id=?""", (message_id,)).fetchone()
        return {**receipt, "state": row["state"], "effect_status": "NOT_EXECUTED",
                "worker_stopped_verified": False}

    def _enqueue_superseded(self) -> None:
        """An updated world invalidates old analytic missions, not their audit."""
        with self._transaction() as conn:
            revision = self._world(conn).revision
            rows = conn.execute("""SELECT message_id FROM director_strategy_assignments
                WHERE world_revision<? AND state IN ('QUEUED','SENT')
                  AND control_state='ACTIVE' ORDER BY rowid""", (revision,)).fetchall()
            for row in rows:
                self._queue_cancel_intent(conn, row["message_id"], "SUPERSEDED_BY_WORLD_REVISION")

    def _flush_cancellations(self) -> list[str]:
        """Send crash-recoverable revocations, even during a global pause."""
        sent: list[str] = []
        with self._transaction() as conn:
            rows = conn.execute("""SELECT c.assignment_message_id,
                c.cancellation_message_id,c.reason,a.recipient_id,a.payload_json
                FROM director_strategy_cancellations c
                JOIN director_strategy_assignments a ON a.message_id=c.assignment_message_id
                WHERE c.state='QUEUED' ORDER BY c.rowid""").fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                self.mailbox.send(
                    sender_id="director", recipient_id=row["recipient_id"],
                    kind="MISSION_CANCEL", message_id=row["cancellation_message_id"],
                    correlation_id=_id(payload["goal_id"], "goal_id"),
                    payload={"assignment_message_id": row["assignment_message_id"],
                             "reason": row["reason"],
                             "effect_authority": "NOT_GRANTED",
                             "worker_stopped_verified": False},
                )
                conn.execute("""UPDATE director_strategy_cancellations SET state='SENT'
                    WHERE assignment_message_id=? AND state='QUEUED'""",
                    (row["assignment_message_id"],))
                sent.append(row["cancellation_message_id"])
        return sent

    @staticmethod
    def _operator_control(conn: sqlite3.Connection) -> dict[str, object]:
        control = conn.execute("SELECT * FROM director_strategy_control WHERE singleton=1").fetchone()
        confirmed = [row[0] for row in conn.execute("""SELECT event_id FROM director_strategy_events
            WHERE success_confirmed=1 ORDER BY event_seq""")]
        return {"paused": bool(control["paused"]), "stop_requested": bool(control["stop_requested"]),
                "confirmed_success_evidence_ids": confirmed}

    @staticmethod
    def _stop_observations(conn: sqlite3.Connection, world: WorldState) -> dict[str, object]:
        snapshot = world.snapshot()
        verified = {row[0] for row in conn.execute("SELECT event_id FROM director_strategy_events WHERE operator_attested=1")}
        observed_events = {
            event["event_id"]: event for event in snapshot["events"]
            if event["evidence_class"] == "OBSERVATION"
        }
        facts: list[dict[str, object]] = []
        contested: list[str] = []
        for metric, record in snapshot["facts"].items():
            if record["status"] == "CONTESTED":
                contested.append(metric)
            elif record["status"] == "OBSERVED":
                for evidence_id in record["evidence_ids"]:
                    if evidence_id not in observed_events:
                        continue
                    facts.append({"evidence_id": evidence_id, "metric": metric,
                                  "value": record["value"], "evidence_class": "OBSERVATION",
                                  "status": "OBSERVED", "verified": evidence_id in verified})
        return {"facts": facts, "contested_metrics": contested}

    @staticmethod
    def _stop(conn: sqlite3.Connection, config: dict[str, object], world: WorldState):
        goal = config["goal"]
        used = conn.execute("SELECT COUNT(*) FROM director_strategy_assignments").fetchone()[0]
        return evaluate_stop(
            {"goal_id": goal["goal_id"], "success_metric": goal["metric"],
             "success_value": goal["target_value"],
             "remaining_attempts": max(0, config["max_missions"] - used)},
            DirectorCoordinator._stop_observations(conn, world),
            DirectorCoordinator._operator_control(conn),
        )

    @staticmethod
    def _display_status(conn: sqlite3.Connection, config: dict[str, object], world: WorldState) -> tuple[str, str]:
        """Use one consistent lifecycle view for ``tick`` and ``inspect``.

        Assignment quotas limit *issuance*. They do not establish that a
        still-unanswered mission was completed, and absence of further
        candidates must not masquerade as an indefinitely running plan.
        """
        decision = DirectorCoordinator._stop(conn, config, world)
        status, reason = decision.status.value, decision.reason
        if status not in ("RUNNING", "EXHAUSTED"):
            return status, reason

        pending = conn.execute("""SELECT 1 FROM director_strategy_assignments
            WHERE state IN ('QUEUED','SENT') AND control_state='ACTIVE' LIMIT 1""").fetchone()
        if pending is not None:
            return "RUNNING", "IN_FLIGHT_ASSIGNMENT; ISSUANCE_BUDGET_MAY_BE_SPENT"
        cancellation_pending = conn.execute("""SELECT 1 FROM director_strategy_cancellations
            WHERE state IN ('QUEUED','SENT') LIMIT 1""").fetchone()
        if cancellation_pending is not None:
            return "UNRESOLVED", "CANCELLATION_NOTICE_AWAITING_ACK; WORKER_STOP_UNVERIFIED"
        if status == "EXHAUSTED":
            return status, reason

        snapshot = world.snapshot()
        candidates = generate_candidates(config["goal"], config["capabilities"], snapshot,
                                         max_candidates=min(32, config["max_missions"]))
        if not candidates:
            return "UNRESOLVED", "AWAITING_OPERATOR_CONFIRMATION_OR_NEW_EVIDENCE"
        for candidate in candidates:
            msg_id = DirectorCoordinator._mission_id(candidate)
            issued = conn.execute("SELECT 1 FROM director_strategy_assignments WHERE message_id=?", (msg_id,)).fetchone()
            if issued is None:
                return status, reason
        return "UNRESOLVED", "NO_NEW_ANALYTICAL_ALTERNATIVE; NEW_EVIDENCE_REQUIRED"

    @staticmethod
    def _mission_id(candidate: Mapping[str, object]) -> str:
        # Stable regardless of retry, so a crash between the SQLite files
        # cannot produce a duplicate recipient assignment.
        identity = _json(candidate, limit=_MAX_EVENT_BYTES)
        return "mission." + hashlib.sha256(identity.encode("ascii")).hexdigest()[:40]

    def _flush_outbox(self) -> list[str]:
        """Idempotently publish precommitted intents; do not dispatch while paused."""
        sent: list[str] = []
        # Keep the coordinator's transaction open across the short local
        # mailbox send so a concurrent pause cannot slip between the policy
        # check and dispatch. Cross-DB commit atomicity is still unavailable:
        # idempotent message IDs make a retry safe after partial success.
        with self._transaction() as conn:
            rows = list(conn.execute("""SELECT message_id, recipient_id, payload_json
                FROM director_strategy_assignments
                WHERE state='QUEUED' AND control_state='ACTIVE' ORDER BY rowid"""))
            for row in rows:
                control = self._operator_control(conn)
                if control["paused"] or control["stop_requested"]:
                    break
                if self._stop(conn, self._config(conn), self._world(conn)).status == "OBJECTIVE_MET":
                    break
                payload = json.loads(row["payload_json"])
                self.mailbox.send(
                    sender_id="director", recipient_id=row["recipient_id"],
                    kind="MISSION_ASSIGNMENT", payload=payload,
                    message_id=row["message_id"],
                    correlation_id=_id(payload["goal_id"], "goal_id"),
                )
                conn.execute("UPDATE director_strategy_assignments SET state='SENT' WHERE message_id=? AND state='QUEUED'", (row["message_id"],))
                sent.append(row["message_id"])
        return sent

    def _accept_reports(self) -> list[dict[str, object]]:
        """Record unverified declared results; messages cannot become evidence."""
        accepted = []
        # Our own inbox only: agent claims are not authenticated.
        inbound = self.mailbox.poll("director", after_seq=0, limit=100)
        for msg in inbound:
            msg_id = _id(msg["message_id"], "message_id")
            request_id = msg.get("in_reply_to")
            reported_sender = _id(msg["sender_id"], "sender_id")
            kind = msg["kind"]
            encoded = _json(msg["payload"], limit=_MAX_EVENT_BYTES)
            with self._transaction() as conn:
                already = conn.execute("SELECT * FROM director_strategy_reports WHERE message_id=?", (msg_id,)).fetchone()
                if already is None:
                    count = conn.execute("SELECT COUNT(*) FROM director_strategy_reports").fetchone()[0]
                    if count >= _MAX_REPORTS:
                        raise ValueError("report ledger is full")
                    assigned = None
                    if isinstance(request_id, str):
                        assigned = conn.execute("""SELECT recipient_id, state FROM director_strategy_assignments
                            WHERE message_id=?""", (request_id,)).fetchone()
                    review_status = "UNMATCHED_UNVERIFIED"
                    cancel = None
                    if isinstance(request_id, str):
                        cancel = conn.execute("""SELECT c.assignment_message_id,c.state,a.recipient_id
                            FROM director_strategy_cancellations c
                            JOIN director_strategy_assignments a ON a.message_id=c.assignment_message_id
                            WHERE c.cancellation_message_id=?""", (request_id,)).fetchone()
                    if (kind == "MISSION_CANCEL_ACK" and cancel is not None
                            and cancel["recipient_id"] == reported_sender
                            and cancel["state"] in ("SENT", "ACK_CLAIMED")):
                        # A claimed local acknowledgment is not proof that a
                        # remote worker has ceased executing or that its sender
                        # was independently authenticated.
                        review_status = "CANCEL_ACK_CLAIMED_UNVERIFIED"
                        conn.execute("""UPDATE director_strategy_cancellations
                            SET state='ACK_CLAIMED' WHERE assignment_message_id=?""",
                            (cancel["assignment_message_id"],))
                        conn.execute("""UPDATE director_strategy_assignments
                            SET control_state='CANCEL_ACK_CLAIMED' WHERE message_id=?""",
                            (cancel["assignment_message_id"],))
                    elif (kind == "MISSION_REPORT" and assigned is not None
                            and assigned["recipient_id"] == reported_sender
                            and assigned["state"] in ("SENT", "REPORTED")):
                        control = conn.execute("""SELECT control_state FROM director_strategy_assignments
                            WHERE message_id=?""", (request_id,)).fetchone()["control_state"]
                        if control == "ACTIVE":
                            review_status = "MATCHED_UNVERIFIED"
                            conn.execute("""UPDATE director_strategy_assignments
                                SET state='REPORTED', reply_message_id=COALESCE(reply_message_id, ?)
                                WHERE message_id=?""", (msg_id, request_id))
                        else:
                            review_status = "REPORTED_AFTER_CANCEL_UNVERIFIED"
                    conn.execute("""INSERT INTO director_strategy_reports
                        (message_id,request_id,reported_sender_id,kind,payload_json,review_status)
                        VALUES(?,?,?,?,?,?)""",
                        (msg_id, request_id, reported_sender, kind, encoded, review_status))
                else:
                    review_status = already["review_status"]
            # Ack AFTER committing the report ledger; duplicates on restart
            # are recognized by primary key and never trusted twice.
            self.mailbox.ack("director", msg_id)
            accepted.append({"message_id": msg_id, "review_status": review_status,
                             "effect_status": "NOT_EXECUTED", "sender_authenticated": False,
                             "worker_stopped_verified": False})
        return accepted

    def tick(self) -> dict[str, object]:
        """Run one finite, deterministic coordination pass.

        No background loop is started. Callers can schedule ticks explicitly.
        Existing undelivered intents are retried before reply ingestion. A
        changing world revision or newly reported result can cause another
        analytical mission to be generated, within a strict global bound.
        """
        self._config(self._connection())
        self._enqueue_superseded()
        cancellations_sent = self._flush_cancellations()
        sent = self._flush_outbox()
        replies = self._accept_reports()
        queued = []
        with self._transaction() as conn:
            config = self._config(conn)
            world = self._world(conn)
            snapshot = world.snapshot()
            decision = self._stop(conn, config, world)
            if decision.status == "RUNNING":
                # Reported missions no longer block alternative analysis.
                # Unanswered missions at the same revision do, preventing
                # runaway queues while a worker is still considering work.
                pending = conn.execute("""SELECT 1 FROM director_strategy_assignments
                    WHERE world_revision=? AND state IN ('QUEUED','SENT')
                      AND control_state='ACTIVE' LIMIT 1""",
                    (snapshot["revision"],)).fetchone()
                if pending is None:
                    candidates = generate_candidates(
                        config["goal"], config["capabilities"], snapshot,
                        max_candidates=min(32, config["max_missions"]),
                    )
                    for candidate in candidates:
                        msg_id = self._mission_id(candidate)
                        exists = conn.execute("""SELECT 1 FROM director_strategy_assignments
                            WHERE message_id=?""", (msg_id,)).fetchone()
                        if exists:
                            continue
                        payload = dict(candidate)
                        payload["evidence_status"] = "DECLARED_UNVERIFIED"
                        payload["effect_authority"] = "NOT_GRANTED"
                        payload["effect_status"] = "NOT_EXECUTED"
                        payload_json = _json(payload, limit=_MAX_EVENT_BYTES)
                        conn.execute("""INSERT INTO director_strategy_assignments
                            (message_id,candidate_id,recipient_id,world_revision,payload_json,state)
                            VALUES(?,?,?,?,?,'QUEUED')""",
                            (msg_id, _id(candidate["mission_id"], "mission_id"),
                             _id(candidate["assigned_agent_id"], "agent_id"),
                             snapshot["revision"], payload_json))
                        queued.append(msg_id)
                        break
        if queued:
            sent.extend(self._flush_outbox())
        with self._transaction() as conn:
            config = self._config(conn)
            world = self._world(conn)
            status, reason = self._display_status(conn, config, world)
            emitted = conn.execute("SELECT COUNT(*) FROM director_strategy_assignments").fetchone()[0]
        return {"goal_id": config["goal"]["goal_id"], "status": status,
                "reason": reason, "world_revision": world.snapshot()["revision"],
                "missions_issued": emitted, "mission_message_ids": sent,
                "new_missions": queued, "cancellation_message_ids": cancellations_sent,
                "inbound_reports": replies, "worker_stopped_verified": False,
                "effect_status": "NOT_EXECUTED", "sender_authenticated": False}

    def inspect(self) -> dict[str, object]:
        """Read-only diagnostic snapshot, including unverified report metadata."""
        conn = self._connection()
        config = self._config(conn)
        world = self._world(conn)
        assignments = [dict(row) for row in conn.execute("""SELECT message_id,candidate_id,
            recipient_id,world_revision,state,control_state,reply_message_id
            FROM director_strategy_assignments ORDER BY rowid""")]
        reports = [dict(row) for row in conn.execute("""SELECT message_id,request_id,
            reported_sender_id,kind,review_status FROM director_strategy_reports ORDER BY rowid""")]
        cancellations = [dict(row) for row in conn.execute("""SELECT assignment_message_id,
            cancellation_message_id,reason,state FROM director_strategy_cancellations ORDER BY rowid""")]
        status, reason = self._display_status(conn, config, world)
        return {"goal": config["goal"], "world": world.snapshot(),
                "assignments": assignments, "reports": reports, "cancellations": cancellations,
                "operator_control": self._operator_control(conn),
                "status": status, "reason": reason,
                "effect_status": "NOT_EXECUTED", "worker_stopped_verified": False}
