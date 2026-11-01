"""Opt-in, local strategic coordination with durable analytical mission delivery.

The coordinator consumes an operator-supplied objective and a registered catalog
of *analytical* agent capabilities. It generates missions from the current
world-state snapshot, deposits assignments into a recipient-specific local
mailbox, and records replies as UNVERIFIED evidence claims. A fresh opt-in
bidirectional mode persists signed command envelopes and atomically admits
HMAC worker replies into its own replay/report ledger. It never launches agents,
uses a network, executes commands, or upgrades reports into trusted evidence.

The coordinator and mailbox SQLite files cannot commit atomically. Durable
assignment intents, immutable signed bytes and deterministic mailbox IDs make
local delivery idempotently recoverable after a partial crash; there is no
cross-host or worker-side exactly-once guarantee.
The operator methods here are a local *interface*, not an authentication or
secure-approval boundary. Restrict filesystem/process access accordingly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .auth_channel import AuthenticatedChannel
from .messenger import DirectorMailbox
from .mission_graph import MissionGraph
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


def _check_existing_coordinator_files(path: Path) -> None:
    """Reject exposed plaintext coordinator/main/WAL/SHM paths on POSIX.

    Static checks require an operator-controlled private parent directory to
    guard against same-UID or pathname-replacement attacks. Windows ACLs are
    the host operator's responsibility.
    """
    if os.name != "posix":
        return
    for member in (path, path.with_name(path.name + "-wal"),
                   path.with_name(path.name + "-shm")):
        if member.is_symlink():
            raise ValueError("coordinator database files cannot be symlinks")
        try:
            info = member.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("coordinator database files must be regular files")
        if info.st_mode & 0o077:
            raise ValueError("existing plaintext coordinator database files must be owner-only (0600)")


def _make_database(path: str | Path) -> sqlite3.Connection:
    if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
        raise ValueError("a durable coordinator SQLite file path is required")
    filename = Path(path)
    if filename.is_symlink() or not filename.parent.is_dir():
        raise ValueError("database path must have an existing parent and not be a symlink")
    _check_existing_coordinator_files(filename)
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(filename, flags, 0o600)
    except FileExistsError:
        if filename.is_symlink():
            raise ValueError("database file must not be a symlink")
    else:
        os.close(handle)
    _check_existing_coordinator_files(filename)
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
            reply_message_id TEXT UNIQUE,
            outbound_envelope_json TEXT
        )""")
        # Additive migration: earlier local databases have this table but lack
        # a separately persisted control state. Do not rewrite the assignment
        # state CHECK constraint or alter historical assignment identities.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(director_strategy_assignments)")}
        if "control_state" not in columns:
            conn.execute("""ALTER TABLE director_strategy_assignments ADD COLUMN
                control_state TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK(control_state IN ('ACTIVE','CANCEL_REQUESTED','CANCEL_ACK_CLAIMED'))""")
        if "outbound_envelope_json" not in columns:
            conn.execute("ALTER TABLE director_strategy_assignments ADD COLUMN outbound_envelope_json TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_cancellations (
            assignment_message_id TEXT PRIMARY KEY REFERENCES director_strategy_assignments(message_id),
            cancellation_message_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('QUEUED','SENT','ACK_CLAIMED')),
            outbound_envelope_json TEXT
        )""")
        cancellation_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(director_strategy_cancellations)")}
        if "outbound_envelope_json" not in cancellation_columns:
            conn.execute("ALTER TABLE director_strategy_cancellations ADD COLUMN outbound_envelope_json TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_reports (
            message_id TEXT PRIMARY KEY,
            request_id TEXT,
            reported_sender_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            review_status TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_plan_graph (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            graph_json TEXT NOT NULL,
            unavailable_json TEXT NOT NULL DEFAULT '[]',
            now_tick INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_plan_completions (
            node_id TEXT PRIMARY KEY,
            assignment_message_id TEXT NOT NULL REFERENCES director_strategy_assignments(message_id),
            observation_event_id TEXT NOT NULL REFERENCES director_strategy_events(event_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_strategy_auth_policy (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            mode TEXT NOT NULL CHECK(mode IN ('LOCAL_HMAC','LOCAL_UNAUTHENTICATED',
                                              'LOCAL_HMAC_BIDIRECTIONAL'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_authenticated_admissions (
            sender_id TEXT NOT NULL,
            signed_message_id TEXT NOT NULL,
            nonce TEXT NOT NULL,
            wrapper_message_id TEXT NOT NULL UNIQUE,
            envelope_sha256 TEXT NOT NULL,
            admitted_at INTEGER NOT NULL,
            PRIMARY KEY(sender_id,signed_message_id),
            UNIQUE(sender_id,nonce)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS director_recovery_quarantine (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            locked INTEGER NOT NULL CHECK(locked=1),
            schema_version TEXT NOT NULL,
            source_goal_id TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            source_auth_mode TEXT NOT NULL
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

    def __init__(
        self, path: str | Path, mailbox: DirectorMailbox,
        *, auth_channel: AuthenticatedChannel | None = None,
        sign_outbound: bool = False,
    ):
        if not isinstance(mailbox, DirectorMailbox):
            raise TypeError("mailbox must be an open DirectorMailbox instance")
        if auth_channel is not None and not isinstance(auth_channel, AuthenticatedChannel):
            raise TypeError("auth_channel must be an AuthenticatedChannel instance")
        if type(sign_outbound) is not bool or (sign_outbound and auth_channel is None):
            raise ValueError("signed outbound requires an explicit authenticated channel")
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable coordinator SQLite file path is required")
        # Two independent databases are required. Their SQLite write
        # transactions cannot nest on the same file when a mission is sent.
        if Path(path).resolve(strict=False) == mailbox.path.resolve(strict=False):
            raise ValueError("coordinator and mailbox must use different database files")
        if auth_channel is not None and (Path(path).resolve(strict=False) == auth_channel.path.resolve(strict=False)
                                         or mailbox.path.resolve(strict=False) == auth_channel.path.resolve(strict=False)):
            raise ValueError("coordinator, mailbox, and authentication ledgers require separate files")
        self.mailbox = mailbox
        # In authenticated mode unsigned local mailbox claims are rejected;
        # a verified HMAC never grants independent action/effect authority.
        self.auth_channel = auth_channel
        self.sign_outbound = sign_outbound
        self._conn: sqlite3.Connection | None = _make_database(path)
        # Prevent an inadvertent insecure reopen of a previously authenticated
        # coordinator (or retroactive 'upgrade' of already untrusted reports).
        self._initializing = True
        try:
            with self._transaction() as conn:
                existing = conn.execute("SELECT mode FROM director_strategy_auth_policy WHERE singleton=1").fetchone()
                requested = ("LOCAL_HMAC_BIDIRECTIONAL" if sign_outbound else
                             "LOCAL_HMAC" if auth_channel is not None else "LOCAL_UNAUTHENTICATED")
                if existing is None:
                    # A pre-v0.4 SQLite file may contain legacy unverified
                    # history, despite having no policy row. It must not be
                    # relabeled 'authenticated' during an additive migration.
                    has_legacy = conn.execute("""SELECT
                        (SELECT COUNT(*) FROM director_strategy_assignments) +
                        (SELECT COUNT(*) FROM director_strategy_reports) +
                        (SELECT COUNT(*) FROM director_strategy_cancellations)
                    """).fetchone()[0] > 0
                    if has_legacy and requested != "LOCAL_UNAUTHENTICATED":
                        raise ValueError("legacy unauthenticated history cannot be upgraded; use a new database")
                    conn.execute("INSERT INTO director_strategy_auth_policy(singleton,mode) VALUES(1,?)",
                                 (requested,))
                elif existing["mode"] != requested:
                    raise ValueError("coordinator authentication mode is immutable; use a new database")
        except BaseException:
            self.close()
            raise
        finally:
            self._initializing = False

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

    def _authentication_mode(self) -> str:
        return ("ENFORCED_LOCAL_HMAC_BIDIRECTIONAL" if self.sign_outbound else
                "ENFORCED_LOCAL_HMAC" if self.auth_channel is not None else
                "UNAUTHENTICATED_LOCAL")

    @staticmethod
    def _director_route_sender(worker_id: str) -> str:
        return _id("director." + _id(worker_id, "worker_id"), "signed director route")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        if not getattr(self, "_initializing", False) and self._recovery_quarantine(connection) is not None:
            raise RuntimeError("coordinator is recovery-quarantined and cannot mutate state")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _recovery_quarantine(conn: sqlite3.Connection) -> dict[str, object] | None:
        """A copied coordinator is permanently inert pending external reconciliation.

        There is deliberately no unquarantine API or historical-worker replay.
        This check runs before public mutations, dispatch and report ingestion.
        """
        row = conn.execute("SELECT * FROM director_recovery_quarantine WHERE singleton=1").fetchone()
        if row is None:
            return None
        if row["locked"] != 1 or row["schema_version"] != "director.recovery-quarantine.v1":
            raise ValueError("invalid recovery quarantine lock state")
        return dict(row)

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
        if self.sign_outbound:
            assert self.auth_channel is not None
            for capability in capabilities:
                route = self._director_route_sender(capability.get("agent_id"))
                if not self.auth_channel.has_sender_key(route):
                    raise ValueError("missing independently configured Director-to-worker route key: " + route)
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

    def configure_graph(
        self, nodes: Sequence[Mapping[str, object]],
        resource_capacities: Mapping[str, int],
    ) -> dict[str, object]:
        """Opt into dependency-aware analytical dispatch before the first tick.

        Each graph node's ``mission_id`` binds to exactly one registered
        ``capability_id``. Nodes are *capability steps*, not authorized work;
        generated proposals remain entirely advisory. Immutable once installed.
        """
        graph = MissionGraph(nodes, resource_capacities)
        snapshot = graph.snapshot()
        with self._transaction() as conn:
            config = self._config(conn)
            available = {cap["capability_id"] for cap in config["capabilities"]}
            ids = {node["mission_id"] for node in snapshot["nodes"]}
            if not ids <= available:
                raise ValueError("graph nodes must bind registered capability IDs")
            encoded = _json({"nodes": snapshot["nodes"],
                             "resource_capacities": snapshot["resource_capacities"]},
                            limit=_MAX_CONFIG_BYTES)
            existing = conn.execute("SELECT graph_json FROM director_plan_graph WHERE singleton=1").fetchone()
            if existing is None:
                if conn.execute("SELECT 1 FROM director_strategy_assignments LIMIT 1").fetchone():
                    raise ValueError("graph must be configured before any assignments")
                conn.execute("INSERT INTO director_plan_graph(singleton,graph_json) VALUES(1,?)", (encoded,))
            elif existing["graph_json"] != encoded:
                raise ValueError("dependency graph is immutable for this coordinator")
        return json.loads(encoded)

    def set_unavailable_graph_resources(self, names: Sequence[str]) -> None:
        """Declare a bounded resource outage; tick emits cooperative cancels.

        These are operator-supplied availability *claims*, not authenticated
        telemetry. Do not interpret removal from the outage list as worker
        cessation or permission for an effect.
        """
        if not isinstance(names, (list, tuple)) or len(names) > 32:
            raise ValueError("unavailable resources must be a bounded list")
        with self._transaction() as conn:
            context = self._graph_context(conn)
            if context is None:
                raise RuntimeError("configure_graph is required")
            graph = context["graph"]
            valid = set(graph.snapshot()["resource_capacities"])
            outage = {_id(name, "resource name") for name in names}
            if len(outage) != len(names) or not outage <= valid:
                raise ValueError("unavailable resources must be distinct declared names")
            conn.execute("UPDATE director_plan_graph SET unavailable_json=? WHERE singleton=1",
                         (_json(sorted(outage), limit=1024),))

    def set_graph_logical_tick(self, now_tick: int) -> None:
        """Advance bounded logical time for deadline-aware graph cancellation.

        This is a caller declaration, not an authenticated wall clock. A
        regressed time would undo deadline states and is therefore rejected.
        """
        if type(now_tick) is not int or not 0 <= now_tick <= 1_000_000_000:
            raise ValueError("logical tick must be a bounded nonnegative integer")
        with self._transaction() as conn:
            row = conn.execute("SELECT now_tick FROM director_plan_graph WHERE singleton=1").fetchone()
            if row is None:
                raise RuntimeError("configure_graph is required")
            if now_tick < row["now_tick"]:
                raise ValueError("logical time cannot move backwards")
            conn.execute("UPDATE director_plan_graph SET now_tick=? WHERE singleton=1", (now_tick,))

    def confirm_graph_node_completed(self, node_id: str, assignment_message_id: str,
                                     observation_event_id: str) -> dict[str, object]:
        """Admit an independently *locally reviewed* analytical completion.

        A worker's MISSION_REPORT alone is **never** completion evidence. This
        requires a distinct operator-attested OBSERVATION with claim_id
        ``completed.<node_id>`` and boolean true; local operator identity is
        still not cryptographically authenticated by this reference.
        """
        node_id = _id(node_id, "node_id")
        assignment_message_id = _id(assignment_message_id, "assignment_message_id")
        observation_event_id = _id(observation_event_id, "observation_event_id")
        with self._transaction() as conn:
            context = self._graph_context(conn)
            if context is None or node_id not in context["node_ids"]:
                raise ValueError("unknown plan node")
            assignment = conn.execute("""SELECT state,control_state,payload_json,world_revision
                FROM director_strategy_assignments WHERE message_id=?""",
                (assignment_message_id,)).fetchone()
            if (assignment is None or assignment["state"] != "REPORTED"
                    or assignment["control_state"] != "ACTIVE"
                    or json.loads(assignment["payload_json"])["capability_id"] != node_id):
                raise ValueError("completion requires an active reported matching assignment")
            record = conn.execute("""SELECT event_json,operator_attested,event_seq
                FROM director_strategy_events WHERE event_id=?""", (observation_event_id,)).fetchone()
            if record is None or not record["operator_attested"]:
                raise ValueError("completion requires a separate reviewed observation")
            if record["event_seq"] <= assignment["world_revision"]:
                raise ValueError("completion observation must be newer than the assigned plan")
            event = json.loads(record["event_json"])
            if (event["evidence_class"] != "OBSERVATION" or
                    event["claim_id"] != "completed." + node_id or event["value"] is not True):
                raise ValueError("completion evidence must be an observed true task outcome")
            current_fact = self._world(conn).snapshot()["facts"].get(event["claim_id"])
            if current_fact is None or current_fact["status"] != "OBSERVED" or current_fact["value"] is not True:
                raise ValueError("contested completion evidence cannot unlock dependencies")
            previous = conn.execute("SELECT * FROM director_plan_completions WHERE node_id=?", (node_id,)).fetchone()
            if previous is None:
                conn.execute("""INSERT INTO director_plan_completions(
                    node_id,assignment_message_id,observation_event_id) VALUES(?,?,?)""",
                    (node_id, assignment_message_id, observation_event_id))
            elif (previous["assignment_message_id"] != assignment_message_id or
                  previous["observation_event_id"] != observation_event_id):
                raise ValueError("conflicting plan completion record")
        return {"node_id": node_id, "completion": "OPERATOR_REVIEWED_LOCAL",
                "source_authenticated": False, "effect_status": "NOT_EXECUTED"}

    @staticmethod
    def _graph_context(conn: sqlite3.Connection) -> dict[str, object] | None:
        row = conn.execute("SELECT graph_json,unavailable_json,now_tick FROM director_plan_graph WHERE singleton=1").fetchone()
        if row is None:
            return None
        declared = json.loads(row["graph_json"])
        graph = MissionGraph(declared["nodes"], declared["resource_capacities"])
        node_ids = {node["mission_id"] for node in declared["nodes"]}
        snapshot = DirectorCoordinator._world(conn).snapshot()
        completed: set[str] = set()
        invalidated: set[str] = set()
        for record in conn.execute("SELECT node_id,observation_event_id FROM director_plan_completions"):
            fact = snapshot["facts"].get("completed." + record["node_id"])
            if fact and fact["status"] == "OBSERVED" and fact["value"] is True:
                completed.add(record["node_id"])
            else:
                invalidated.add(record["node_id"])
        active: set[str] = set()
        for assignment in conn.execute("""SELECT payload_json FROM director_strategy_assignments
                WHERE state IN ('QUEUED','SENT')"""):
            capability = json.loads(assignment["payload_json"])["capability_id"]
            if capability in node_ids:
                active.add(capability)
        unavailable = json.loads(row["unavailable_json"])
        return {"graph": graph, "node_ids": node_ids,
                "completed": completed, "invalidated": invalidated,
                "active": active, "unavailable": unavailable, "now_tick": row["now_tick"]}

    @staticmethod
    def _graph_ready_ids(conn: sqlite3.Connection) -> set[str] | None:
        context = DirectorCoordinator._graph_context(conn)
        if context is None:
            return None
        return {node["mission_id"] for node in context["graph"].ready(
            completed=context["completed"], in_progress=context["active"],
            failed_or_cancelled=context["invalidated"],
            unavailable_resources=context["unavailable"], now_tick=context["now_tick"],
        )}

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
        if not self.sign_outbound:
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
        if not self.sign_outbound:
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

    def _enqueue_graph_blocked(self) -> None:
        """Propagate a declared graph outage/invalidation to issued work."""
        with self._transaction() as conn:
            context = self._graph_context(conn)
            if context is None:
                return
            blocked = context["graph"].cascade_blocked(
                failed_or_cancelled=context["invalidated"],
                unavailable_resources=context["unavailable"],
                now_tick=context["now_tick"],
                completed=context["completed"],
            )["recommended_cancellations"]
            if not blocked:
                return
            for row in conn.execute("""SELECT message_id,payload_json
                    FROM director_strategy_assignments
                    WHERE state IN ('QUEUED','SENT') AND control_state='ACTIVE' ORDER BY rowid""").fetchall():
                node_id = json.loads(row["payload_json"])["capability_id"]
                if node_id in blocked:
                    self._queue_cancel_intent(conn, row["message_id"], "GRAPH_PREREQUISITE_OR_RESOURCE_UNAVAILABLE")

    def _prepare_signed_outgoing(self, *, auth_now_utc: int | None) -> None:
        """Commit immutable HMAC bytes before any independent mailbox write.

        The mailbox and coordinator use separate WAL databases. Signing and
        sending inside one coordinator transaction would lose the nonce on a
        crash after mailbox delivery, making retries conflict. The two phases
        below make a retry publish exactly the original signed bytes.
        """
        if not self.sign_outbound:
            return
        assert self.auth_channel is not None
        if type(auth_now_utc) is not int or not 0 <= auth_now_utc < 4102444800:
            raise ValueError("signed dispatch requires a bounded explicit UTC second")
        expiry = min(auth_now_utc + 86400, 4102444800)
        with self._transaction() as conn:
            for row in conn.execute("""SELECT message_id,recipient_id,payload_json
                FROM director_strategy_assignments
                WHERE state='QUEUED' AND control_state='ACTIVE'
                  AND outbound_envelope_json IS NULL
                  AND (SELECT paused FROM director_strategy_control WHERE singleton=1)=0
                  AND (SELECT stop_requested FROM director_strategy_control WHERE singleton=1)=0
                ORDER BY rowid""").fetchall():
                payload = json.loads(row["payload_json"])
                raw = self.auth_channel.seal(
                    sender_id=self._director_route_sender(row["recipient_id"]),
                    recipient_id=row["recipient_id"], kind="MISSION_ASSIGNMENT",
                    payload={"assignment_message_id": row["message_id"], "body": payload},
                    message_id=row["message_id"], issued_at=auth_now_utc,
                    expires_at=expiry)
                if len(raw) > 16384:
                    raise ValueError("signed outgoing assignment exceeds mailbox envelope bound")
                conn.execute("""UPDATE director_strategy_assignments SET outbound_envelope_json=?
                    WHERE message_id=? AND outbound_envelope_json IS NULL""",
                    (raw.decode("ascii"), row["message_id"]))
            for row in conn.execute("""SELECT c.assignment_message_id,c.cancellation_message_id,
                c.reason,a.recipient_id
                FROM director_strategy_cancellations c
                JOIN director_strategy_assignments a ON a.message_id=c.assignment_message_id
                WHERE c.state='QUEUED' AND c.outbound_envelope_json IS NULL
                ORDER BY c.rowid""").fetchall():
                body = {"assignment_message_id": row["assignment_message_id"],
                        "reason": row["reason"], "effect_authority": "NOT_GRANTED",
                        "worker_stopped_verified": False}
                raw = self.auth_channel.seal(
                    sender_id=self._director_route_sender(row["recipient_id"]),
                    recipient_id=row["recipient_id"], kind="MISSION_CANCEL",
                    payload={"cancellation_message_id": row["cancellation_message_id"],
                             "body": body},
                    message_id=row["cancellation_message_id"], issued_at=auth_now_utc,
                    expires_at=expiry)
                if len(raw) > 16384:
                    raise ValueError("signed cancellation exceeds mailbox envelope bound")
                conn.execute("""UPDATE director_strategy_cancellations SET outbound_envelope_json=?
                    WHERE assignment_message_id=? AND outbound_envelope_json IS NULL""",
                    (raw.decode("ascii"), row["assignment_message_id"]))

    def _outgoing(self, *, row: sqlite3.Row, kind: str,
                  body: dict[str, object], message_id: str,
                  auth_now_utc: int | None) -> tuple[str, dict[str, object]]:
        if not self.sign_outbound:
            return kind, body
        assert self.auth_channel is not None
        encoded = row["outbound_envelope_json"]
        if type(encoded) is not str:
            raise RuntimeError("signed outgoing intent must be durable before delivery")
        inspected = self.auth_channel.inspect_signature(
            encoded.encode("ascii"), expected_recipient=row["recipient_id"],
            now_utc=auth_now_utc)
        if (inspected["authenticated_sender_id"] != self._director_route_sender(row["recipient_id"])
                or inspected["kind"] != kind or inspected["message_id"] != message_id):
            raise ValueError("persisted signed outgoing intent failed route/identity validation")
        return "SIGNED_ENVELOPE", {"envelope": encoded}

    def _flush_cancellations(self, *, auth_now_utc: int | None = None) -> list[str]:
        """Send crash-recoverable revocations, even during a global pause."""
        if self._recovery_quarantine(self._connection()) is not None:
            return []
        self._prepare_signed_outgoing(auth_now_utc=auth_now_utc)
        sent: list[str] = []
        with self._transaction() as conn:
            rows = conn.execute("""SELECT c.assignment_message_id,
                c.cancellation_message_id,c.reason,a.recipient_id,a.payload_json,
                c.outbound_envelope_json
                FROM director_strategy_cancellations c
                JOIN director_strategy_assignments a ON a.message_id=c.assignment_message_id
                WHERE c.state='QUEUED' ORDER BY c.rowid""").fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                kind, body = self._outgoing(
                    row=row, kind="MISSION_CANCEL", message_id=row["cancellation_message_id"],
                    body={"assignment_message_id": row["assignment_message_id"],
                          "reason": row["reason"],
                          "effect_authority": "NOT_GRANTED",
                          "worker_stopped_verified": False}, auth_now_utc=auth_now_utc)
                self.mailbox.send(
                    sender_id="director", recipient_id=row["recipient_id"],
                    kind=kind, message_id=row["cancellation_message_id"],
                    correlation_id=_id(payload["goal_id"], "goal_id"),
                    payload=body,
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
        graph_ready = DirectorCoordinator._graph_ready_ids(conn)
        candidates = generate_candidates(config["goal"], config["capabilities"], snapshot,
                                         max_candidates=32)
        if not candidates:
            return "UNRESOLVED", "AWAITING_OPERATOR_CONFIRMATION_OR_NEW_EVIDENCE"
        for candidate in candidates:
            if graph_ready is not None and candidate["capability_id"] not in graph_ready:
                continue
            msg_id = DirectorCoordinator._mission_id(candidate)
            issued = conn.execute("SELECT 1 FROM director_strategy_assignments WHERE message_id=?", (msg_id,)).fetchone()
            if issued is None:
                return status, reason
        if graph_ready is not None and not graph_ready:
            return "UNRESOLVED", "GRAPH_DEPENDENCIES_OR_RESOURCES_NOT_READY"
        return "UNRESOLVED", "NO_NEW_ANALYTICAL_ALTERNATIVE; NEW_EVIDENCE_REQUIRED"

    @staticmethod
    def _mission_id(candidate: Mapping[str, object]) -> str:
        # Stable regardless of retry, so a crash between the SQLite files
        # cannot produce a duplicate recipient assignment.
        identity = _json(candidate, limit=_MAX_EVENT_BYTES)
        return "mission." + hashlib.sha256(identity.encode("ascii")).hexdigest()[:40]

    def _flush_outbox(self, *, auth_now_utc: int | None = None) -> list[str]:
        """Idempotently publish precommitted intents; do not dispatch while paused."""
        if self._recovery_quarantine(self._connection()) is not None:
            return []
        self._prepare_signed_outgoing(auth_now_utc=auth_now_utc)
        sent: list[str] = []
        # Keep the coordinator's transaction open across the short local
        # mailbox send so a concurrent pause cannot slip between the policy
        # check and dispatch. Cross-DB commit atomicity is still unavailable:
        # idempotent message IDs make a retry safe after partial success.
        with self._transaction() as conn:
            rows = list(conn.execute("""SELECT message_id, recipient_id, payload_json,
                outbound_envelope_json
                FROM director_strategy_assignments
                WHERE state='QUEUED' AND control_state='ACTIVE' ORDER BY rowid"""))
            for row in rows:
                control = self._operator_control(conn)
                if control["paused"] or control["stop_requested"]:
                    break
                if self._stop(conn, self._config(conn), self._world(conn)).status == "OBJECTIVE_MET":
                    break
                payload = json.loads(row["payload_json"])
                kind, body = self._outgoing(
                    row=row, kind="MISSION_ASSIGNMENT", message_id=row["message_id"],
                    body=payload, auth_now_utc=auth_now_utc)
                self.mailbox.send(
                    sender_id="director", recipient_id=row["recipient_id"],
                    kind=kind, payload=body,
                    message_id=row["message_id"],
                    correlation_id=_id(payload["goal_id"], "goal_id"),
                )
                conn.execute("UPDATE director_strategy_assignments SET state='SENT' WHERE message_id=? AND state='QUEUED'", (row["message_id"],))
                sent.append(row["message_id"])
        return sent

    def _accept_reports(self, *, auth_now_utc: int | None = None) -> list[dict[str, object]]:
        """Record reports; verify envelope sender before matching in secure mode.

        Transport-level authentication is conditional on *explicitly* supplying
        a channel and clock to ``tick``; the default legacy mailbox is not
        authenticated. Both modes keep report contents unverified as evidence.
        """
        accepted = []
        # The mailbox sender_id is always untrusted, including in secure mode.
        inbound = self.mailbox.poll("director", after_seq=0, limit=100)
        for msg in inbound:
            msg_id = _id(msg["message_id"], "message_id")
            request_id = msg.get("in_reply_to")
            reported_sender = _id(msg["sender_id"], "sender_id")
            kind = msg["kind"]
            body = msg["payload"]
            authenticated = False
            rejection = None
            verified = None
            signed_raw = None
            if self.auth_channel is not None:
                rejection = "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE"
                if kind == "SIGNED_ENVELOPE" and isinstance(body, dict):
                    raw = body.get("envelope")
                    if set(body) == {"envelope"} and type(raw) is str and len(raw) <= 16384:
                        try:
                            signed_raw = raw.encode("ascii")
                            if self.sign_outbound:
                                # The replay marker is inserted below in the
                                # SAME coordinator transaction as the report.
                                verified = self.auth_channel.inspect_signature(
                                    signed_raw, expected_recipient="director", now_utc=auth_now_utc)
                            else:
                                # Historical v0.4 mode retains its original
                                # separate-ledger at-most-once behavior.
                                verified = self.auth_channel.verify(
                                    signed_raw, expected_recipient="director", now_utc=auth_now_utc)
                            signed = verified["payload"]
                            if (verified["kind"] not in ("MISSION_REPORT", "MISSION_CANCEL_ACK")
                                    or not isinstance(signed, dict)
                                    or set(signed) != {"in_reply_to", "body"}
                                    or not isinstance(signed["body"], dict)):
                                rejection = "REJECTED_SIGNED_PAYLOAD_SHAPE"
                            else:
                                request_id = _id(signed["in_reply_to"], "in_reply_to")
                                reported_sender = verified["authenticated_sender_id"]
                                kind = verified["kind"]
                                body = signed["body"]
                                authenticated = True
                                rejection = None
                        except (ValueError, UnicodeError, TypeError):
                            rejection = "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE"
                if rejection is not None:
                    request_id = None
                    kind = "SIGNED_ENVELOPE_REJECTED"
                    # Never persist a raw untrusted envelope or its claimed
                    # contents into the coordinator's bounded report ledger.
                    body = {"reason": rejection}
            try:
                encoded = _json(body, limit=_MAX_EVENT_BYTES)
            except ValueError:
                request_id = None
                authenticated = False
                rejection = "REJECTED_REPORT_TOO_LARGE"
                kind = "SIGNED_ENVELOPE_REJECTED"
                encoded = _json({"reason": rejection}, limit=_MAX_EVENT_BYTES)
            with self._transaction() as conn:
                already = conn.execute("SELECT * FROM director_strategy_reports WHERE message_id=?", (msg_id,)).fetchone()
                if already is None:
                    count = conn.execute("SELECT COUNT(*) FROM director_strategy_reports").fetchone()[0]
                    if count >= _MAX_REPORTS:
                        raise ValueError("report ledger is full")
                    if self.sign_outbound and authenticated and rejection is None:
                        assert verified is not None and signed_raw is not None
                        try:
                            conn.execute("""INSERT INTO director_authenticated_admissions (
                                sender_id,signed_message_id,nonce,wrapper_message_id,
                                envelope_sha256,admitted_at)
                                VALUES(?,?,?,?,?,?)""", (
                                verified["authenticated_sender_id"], verified["message_id"],
                                verified["nonce"], msg_id,
                                hashlib.sha256(signed_raw).hexdigest(), auth_now_utc,
                            ))
                        except sqlite3.IntegrityError:
                            # A valid signature is still a replay if the same
                            # nonce or signed ID was already admitted under a
                            # different mailbox wrapper. Never update a task.
                            rejection = "REJECTED_REPLAY_OR_NONCE"
                            authenticated = False
                            request_id = None
                            kind = "SIGNED_ENVELOPE_REJECTED"
                            encoded = _json({"reason": rejection}, limit=_MAX_EVENT_BYTES)
                    assigned = None
                    if isinstance(request_id, str):
                        assigned = conn.execute("""SELECT recipient_id, state FROM director_strategy_assignments
                            WHERE message_id=?""", (request_id,)).fetchone()
                    review_status = rejection or "UNMATCHED_UNVERIFIED"
                    cancel = None
                    if isinstance(request_id, str):
                        cancel = conn.execute("""SELECT c.assignment_message_id,c.state,a.recipient_id
                            FROM director_strategy_cancellations c
                            JOIN director_strategy_assignments a ON a.message_id=c.assignment_message_id
                            WHERE c.cancellation_message_id=?""", (request_id,)).fetchone()
                    if (rejection is None and kind == "MISSION_CANCEL_ACK" and cancel is not None
                            and cancel["recipient_id"] == reported_sender
                            and cancel["state"] in ("SENT", "ACK_CLAIMED")):
                        # A claimed local acknowledgment is not proof that a
                        # remote worker has ceased executing or that its sender
                        # was independently authenticated.
                        review_status = ("CANCEL_ACK_AUTHENTICATED_UNVERIFIED" if authenticated
                                         else "CANCEL_ACK_CLAIMED_UNVERIFIED")
                        conn.execute("""UPDATE director_strategy_cancellations
                            SET state='ACK_CLAIMED' WHERE assignment_message_id=?""",
                            (cancel["assignment_message_id"],))
                        conn.execute("""UPDATE director_strategy_assignments
                            SET control_state='CANCEL_ACK_CLAIMED' WHERE message_id=?""",
                            (cancel["assignment_message_id"],))
                    elif (rejection is None and kind == "MISSION_REPORT" and assigned is not None
                            and assigned["recipient_id"] == reported_sender
                            and assigned["state"] in ("SENT", "REPORTED")):
                        control = conn.execute("""SELECT control_state FROM director_strategy_assignments
                            WHERE message_id=?""", (request_id,)).fetchone()["control_state"]
                        if control == "ACTIVE":
                            review_status = ("MATCHED_AUTHENTICATED_UNVERIFIED" if authenticated
                                             else "MATCHED_UNVERIFIED")
                            conn.execute("""UPDATE director_strategy_assignments
                                SET state='REPORTED', reply_message_id=COALESCE(reply_message_id, ?)
                                WHERE message_id=?""", (msg_id, request_id))
                        else:
                            review_status = ("REPORTED_AFTER_CANCEL_AUTHENTICATED_UNVERIFIED" if authenticated
                                             else "REPORTED_AFTER_CANCEL_UNVERIFIED")
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
                             "effect_status": "NOT_EXECUTED",
                             "sender_authenticated": (authenticated and rejection is None),
                             "worker_stopped_verified": False})
        return accepted

    def tick(self, *, auth_now_utc: int | None = None) -> dict[str, object]:
        """Run one finite, deterministic coordination pass.

        No background loop is started. Callers can schedule ticks explicitly.
        Existing undelivered intents are retried before reply ingestion. A
        changing world revision or newly reported result can cause another
        analytical mission to be generated, within a strict global bound.
        """
        self._config(self._connection())
        quarantine = self._recovery_quarantine(self._connection())
        if quarantine is not None:
            config = self._config(self._connection())
            world = self._world(self._connection())
            issued = self._connection().execute(
                "SELECT COUNT(*) FROM director_strategy_assignments").fetchone()[0]
            return {"goal_id": config["goal"]["goal_id"], "status": "QUARANTINED",
                    "reason": "PAUSED_RECOVERY_COPY_REQUIRES_MANUAL_RECONCILIATION",
                    "world_revision": world.snapshot()["revision"], "missions_issued": issued,
                    "mission_message_ids": [], "new_missions": [], "cancellation_message_ids": [],
                    "inbound_reports": [], "worker_stopped_verified": False,
                    "effect_status": "NOT_EXECUTED", "sender_authenticated": False,
                    "recovery_quarantine": quarantine,
                    "authentication_mode": self._authentication_mode()}
        if self.auth_channel is not None and (type(auth_now_utc) is not int or not 0 <= auth_now_utc <= 4102444800):
            raise ValueError("authenticated mode requires a bounded explicit UTC POSIX second")
        self._enqueue_superseded()
        self._enqueue_graph_blocked()
        cancellations_sent = self._flush_cancellations(auth_now_utc=auth_now_utc)
        sent = self._flush_outbox(auth_now_utc=auth_now_utc)
        replies = self._accept_reports(auth_now_utc=auth_now_utc)
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
                        max_candidates=32,
                    )
                    graph_ready = self._graph_ready_ids(conn)
                    for candidate in candidates:
                        if graph_ready is not None and candidate["capability_id"] not in graph_ready:
                            continue
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
            sent.extend(self._flush_outbox(auth_now_utc=auth_now_utc))
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
                "effect_status": "NOT_EXECUTED",
                "authentication_mode": self._authentication_mode(),
                "sender_authenticated": False}

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
        context = self._graph_context(conn)
        graph_state = None if context is None else {
            "configured": True, "completed": sorted(context["completed"]),
            "invalidated": sorted(context["invalidated"]),
            "active": sorted(context["active"]),
            "unavailable_resources": list(context["unavailable"]),
            "logical_tick": context["now_tick"],
            "ready": sorted(self._graph_ready_ids(conn)),
        }
        status, reason = self._display_status(conn, config, world)
        quarantine = self._recovery_quarantine(conn)
        if quarantine is not None:
            status, reason = "QUARANTINED", "PAUSED_RECOVERY_COPY_REQUIRES_MANUAL_RECONCILIATION"
        return {"goal": config["goal"], "world": world.snapshot(),
                "assignments": assignments, "reports": reports, "cancellations": cancellations,
                "graph": graph_state,
                "operator_control": self._operator_control(conn),
                "status": status, "reason": reason,
                "effect_status": "NOT_EXECUTED", "worker_stopped_verified": False,
                "authentication_mode": self._authentication_mode(),
                "recovery_quarantine": quarantine}
