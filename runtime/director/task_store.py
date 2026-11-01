"""Local, durable coordination for *analytical* Director tasks only.

This module is deliberately not a worker, scheduler loop, host adapter, or
authorization service. Its only effects are writes to the explicitly supplied
local SQLite database. A lease controls which analytical worker may *record*
an outcome; it grants no permission to call providers, alter hosts, or spend.

Use one database on one machine. SQLite WAL is not a cross-host coordination
protocol. Timestamps are caller supplied; the persisted monotonic watermark
rejects obvious clock rollback but does not authenticate wall-clock time.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FUTURE_CLOCK_SKEW_US = 5 * 60 * 1_000_000


class TaskConflict(ValueError):
    """An immutable task definition or recorded outcome contradicts the store."""


class LeaseRejected(ValueError):
    """A stale, expired, replaced, paused, or cancelled lease cannot mutate a task."""


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    mission_id: str
    revision: int
    payload_sha256: str
    worker_id: str
    fencing_token: int
    expires_at_utc: datetime
    lease_seconds: int


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _reason(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or not value.isprintable()
    ):
        raise ValueError("control reason must be bounded printable text")
    return value


def _clock(value: object) -> int:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("now_utc must be a timezone-aware UTC datetime")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000) + delta.microseconds


def _from_us(microseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=microseconds)


class TaskStore:
    """A single-host, opt-in SQLite ledger with persisted fencing generations.

    The caller owns and must close the handle (or use ``with TaskStore(...)``).
    Each separate worker/process should open its own connection. Every state
    change is serialized with ``BEGIN IMMEDIATE``; database lock errors fail
    without fabricating an accepted task or result.
    """

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ValueError("a durable local SQLite file path is required")
        self.path = Path(path)
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("database path must have an existing local parent and not be a symlink")
        # sqlite3.connect() otherwise creates a file with the process umask
        # (often 0644). These records can include mission identifiers and
        # operator control reasons; make *new* POSIX databases owner-only.
        # Pre-existing database permissions are an operator responsibility.
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            new_file = os.open(self.path, flags, 0o600)
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("database must not be a symlink")
        else:
            os.close(new_file)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS director_tasks (
                    task_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    depends_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    expires_us INTEGER,
                    lease_seconds INTEGER,
                    next_eligible_us INTEGER,
                    result_sha256 TEXT,
                    failure_code TEXT,
                    control_reason TEXT
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS director_task_meta (
                    key TEXT PRIMARY KEY, int_value INTEGER NOT NULL
                )"""
            )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            connection.close()

    def __enter__(self) -> "TaskStore":
        self._connection()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TaskStore is closed")
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
    def _row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM director_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, object]:
        return {
            "task_id": row["task_id"],
            "mission_id": row["mission_id"],
            "revision": row["revision"],
            "payload_sha256": row["payload_sha256"],
            "max_attempts": row["max_attempts"],
            "depends_on": tuple(json.loads(row["depends_json"])),
            "status": row["status"],
            "attempts": row["attempts"],
            "fencing_token": row["fencing_token"],
            "worker_id": row["worker_id"],
            "lease_expires_at_utc": (
                _from_us(row["expires_us"]) if row["expires_us"] is not None else None
            ),
            "next_eligible_at_utc": (
                _from_us(row["next_eligible_us"])
                if row["next_eligible_us"] is not None else None
            ),
            "result_sha256": row["result_sha256"],
            "failure_code": row["failure_code"],
            "control_reason": row["control_reason"],
            # Even when PAUSED or CANCELLED, this ledger does not control or
            # attest whether arbitrary processes have actually stopped.
            "worker_stop_confirmed": False,
            "effect_authority": "NOT_GRANTED",
            "effect_status": "NOT_EXECUTED_BY_TASK_STORE",
        }

    @staticmethod
    def _advance_clock(conn: sqlite3.Connection, now_us: int) -> None:
        # Reject wildly future-dated injected timestamps before persisting a
        # global watermark that would lock out all normal future calls. This
        # host-wall-clock *sanity bound* is not a trusted time attestation.
        if now_us > _clock(datetime.now(timezone.utc)) + _FUTURE_CLOCK_SKEW_US:
            raise ValueError("UTC clock exceeds the host sanity window")
        previous = conn.execute(
            "SELECT int_value FROM director_task_meta WHERE key='last_clock_us'"
        ).fetchone()
        if previous is not None and now_us < previous[0]:
            raise ValueError("UTC clock rollback relative to persisted task ledger")
        conn.execute(
            """INSERT INTO director_task_meta(key,int_value)
               VALUES ('last_clock_us',?)
               ON CONFLICT(key) DO UPDATE SET int_value=excluded.int_value""",
            (now_us,),
        )

    def submit(
        self,
        task_id: str,
        mission_id: str,
        revision: int,
        payload_sha256: str,
        *,
        max_attempts: int = 3,
        depends_on: tuple[str, ...] | list[str] = (),
    ) -> dict[str, object]:
        task_id = _identifier(task_id, "task_id")
        mission_id = _identifier(mission_id, "mission_id")
        if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
            raise ValueError("revision must be a positive bounded integer")
        payload_sha256 = _digest(payload_sha256, "payload_sha256")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be a bounded positive integer")
        if not isinstance(depends_on, (list, tuple)) or len(depends_on) > 32:
            raise ValueError("depends_on must be a bounded list")
        dependencies = tuple(sorted(_identifier(dep, "dependency ID") for dep in depends_on))
        if len(dependencies) != len(set(dependencies)) or task_id in dependencies:
            raise ValueError("duplicate or self-referential dependencies are forbidden")
        signature = (mission_id, revision, payload_sha256, max_attempts, json.dumps(dependencies))
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM director_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is not None:
                if signature != (
                    existing["mission_id"], existing["revision"],
                    existing["payload_sha256"], existing["max_attempts"],
                    existing["depends_json"],
                ):
                    raise TaskConflict("task identity has a contradictory immutable definition")
                return self._snapshot(existing)
            for dependency in dependencies:
                try:
                    self._row(conn, dependency)
                except KeyError as exc:
                    raise ValueError("every dependency must be registered first") from exc
            # Because all dependencies must already exist, registration edges
            # strictly point to earlier tasks and cycles are impossible.
            conn.execute(
                """INSERT INTO director_tasks
                (task_id,mission_id,revision,payload_sha256,max_attempts,depends_json,status)
                VALUES (?,?,?,?,?,?,'PENDING')""",
                (task_id, *signature),
            )
            return self._snapshot(self._row(conn, task_id))

    def inspect(self, task_id: str) -> dict[str, object]:
        return self._snapshot(self._row(self._connection(), _identifier(task_id, "task_id")))

    @staticmethod
    def _lease(row: sqlite3.Row) -> TaskLease:
        return TaskLease(
            task_id=row["task_id"],
            mission_id=row["mission_id"],
            revision=row["revision"],
            payload_sha256=row["payload_sha256"],
            worker_id=row["worker_id"],
            fencing_token=row["fencing_token"],
            expires_at_utc=_from_us(row["expires_us"]),
            lease_seconds=row["lease_seconds"],
        )

    def claim(self, worker_id: str, now_utc: datetime, lease_seconds: int) -> TaskLease | None:
        worker_id = _identifier(worker_id, "worker_id")
        now_us = _clock(now_utc)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be 1..86400")
        with self._transaction() as conn:
            self._advance_clock(conn, now_us)
            candidates = conn.execute(
                """SELECT * FROM director_tasks
                   WHERE status IN ('PENDING','LEASED') ORDER BY task_id"""
            ).fetchall()
            for row in candidates:
                if row["status"] == "LEASED":
                    if row["expires_us"] > now_us:
                        continue
                    if row["attempts"] >= row["max_attempts"]:
                        conn.execute(
                            """UPDATE director_tasks SET status='UNRESOLVED',worker_id=NULL,
                               expires_us=NULL,lease_seconds=NULL,fencing_token=fencing_token+1
                               WHERE task_id=?""", (row["task_id"],)
                        )
                        continue
                else:
                    if row["attempts"] >= row["max_attempts"]:
                        conn.execute(
                            "UPDATE director_tasks SET status='UNRESOLVED' WHERE task_id=?",
                            (row["task_id"],),
                        )
                        continue
                    if row["next_eligible_us"] is not None and row["next_eligible_us"] > now_us:
                        continue
                dependencies = json.loads(row["depends_json"])
                if dependencies:
                    states = [self._row(conn, dep)["status"] for dep in dependencies]
                    if any(state in ("DEAD", "CANCELLED", "UNRESOLVED") for state in states):
                        conn.execute(
                            "UPDATE director_tasks SET status='UNRESOLVED' WHERE task_id=?",
                            (row["task_id"],),
                        )
                        continue
                    if any(state != "DONE" for state in states):
                        continue
                conn.execute(
                    """UPDATE director_tasks
                       SET status='LEASED',attempts=attempts+1,
                           fencing_token=fencing_token+1,worker_id=?,expires_us=?,
                           lease_seconds=?,next_eligible_us=NULL,failure_code=NULL
                       WHERE task_id=?""",
                    (worker_id, now_us + lease_seconds * 1_000_000,
                     lease_seconds, row["task_id"]),
                )
                return self._lease(self._row(conn, row["task_id"]))
        return None

    @staticmethod
    def _require_lease(row: sqlite3.Row, lease: TaskLease, now_us: int) -> None:
        if (
            not isinstance(lease, TaskLease)
            or row["status"] != "LEASED"
            or row["task_id"] != lease.task_id
            or row["mission_id"] != lease.mission_id
            or row["revision"] != lease.revision
            or row["payload_sha256"] != lease.payload_sha256
            or row["worker_id"] != lease.worker_id
            or row["fencing_token"] != lease.fencing_token
            or row["expires_us"] != _clock(lease.expires_at_utc)
            or row["lease_seconds"] != lease.lease_seconds
            or now_us >= row["expires_us"]
        ):
            raise LeaseRejected("lease expired, changed, or no longer held")

    def renew(self, lease: TaskLease, now_utc: datetime) -> TaskLease:
        now_us = _clock(now_utc)
        if not isinstance(lease, TaskLease):
            raise LeaseRejected("invalid lease")
        with self._transaction() as conn:
            self._advance_clock(conn, now_us)
            row = self._row(conn, lease.task_id)
            self._require_lease(row, lease, now_us)
            conn.execute(
                "UPDATE director_tasks SET expires_us=? WHERE task_id=?",
                (now_us + row["lease_seconds"] * 1_000_000, lease.task_id),
            )
            return self._lease(self._row(conn, lease.task_id))

    def complete(
        self, lease: TaskLease, result_sha256: str, now_utc: datetime
    ) -> dict[str, object]:
        result_sha256 = _digest(result_sha256, "result_sha256")
        now_us = _clock(now_utc)
        if not isinstance(lease, TaskLease):
            raise LeaseRejected("invalid lease")
        with self._transaction() as conn:
            self._advance_clock(conn, now_us)
            row = self._row(conn, lease.task_id)
            if row["status"] == "DONE":
                if (
                    row["worker_id"] != lease.worker_id
                    or row["fencing_token"] != lease.fencing_token
                    or row["mission_id"] != lease.mission_id
                    or row["revision"] != lease.revision
                    or row["payload_sha256"] != lease.payload_sha256
                    or row["expires_us"] != _clock(lease.expires_at_utc)
                    or row["lease_seconds"] != lease.lease_seconds
                ):
                    raise LeaseRejected("completion belongs to a different owner or generation")
                if row["result_sha256"] != result_sha256:
                    raise TaskConflict("already completed with a contradictory result")
                return self._snapshot(row)
            self._require_lease(row, lease, now_us)
            conn.execute(
                "UPDATE director_tasks SET status='DONE', result_sha256=? WHERE task_id=?",
                (result_sha256, lease.task_id),
            )
            return self._snapshot(self._row(conn, lease.task_id))

    def fail(
        self, lease: TaskLease, reason_code: str, retryable: bool, now_utc: datetime
    ) -> dict[str, object]:
        reason_code = _identifier(reason_code, "reason_code")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a bool")
        now_us = _clock(now_utc)
        if not isinstance(lease, TaskLease):
            raise LeaseRejected("invalid lease")
        with self._transaction() as conn:
            self._advance_clock(conn, now_us)
            row = self._row(conn, lease.task_id)
            self._require_lease(row, lease, now_us)
            again = retryable and row["attempts"] < row["max_attempts"]
            wait_us = min(2 ** (row["attempts"] - 1), 3600) * 1_000_000
            conn.execute(
                """UPDATE director_tasks SET status=?,worker_id=NULL,expires_us=NULL,
                   lease_seconds=NULL,next_eligible_us=?,failure_code=?,
                   fencing_token=fencing_token+1 WHERE task_id=?""",
                ("PENDING" if again else "DEAD", now_us + wait_us if again else None,
                 reason_code, lease.task_id),
            )
            return self._snapshot(self._row(conn, lease.task_id))

    def request_pause(
        self, task_id: str, reason: str, now_utc: datetime
    ) -> dict[str, object]:
        return self._control(task_id, reason, now_utc, operation="pause")

    def resume(self, task_id: str, now_utc: datetime) -> dict[str, object]:
        task_id = _identifier(task_id, "task_id")
        now_us = _clock(now_utc)
        with self._transaction() as conn:
            self._advance_clock(conn, now_us)
            row = self._row(conn, task_id)
            if row["status"] != "PAUSED":
                raise TaskConflict("only a paused task can resume")
            # A paused in-flight claim already consumed an attempt and its
            # outcome is unknown. Reopening it cannot bypass the ceiling.
            if row["attempts"] >= row["max_attempts"]:
                conn.execute(
                    """UPDATE director_tasks SET status='UNRESOLVED',control_reason=NULL
                       WHERE task_id=?""", (task_id,),
                )
                return self._snapshot(self._row(conn, task_id))
            conn.execute(
                """UPDATE director_tasks SET status='PENDING',control_reason=NULL,
                   next_eligible_us=NULL WHERE task_id=?""", (task_id,),
            )
            return self._snapshot(self._row(conn, task_id))

    def cancel(self, task_id: str, reason: str, now_utc: datetime) -> dict[str, object]:
        return self._control(task_id, reason, now_utc, operation="cancel")

    def _control(
        self, task_id: str, reason: str, now_utc: datetime, *, operation: str
    ) -> dict[str, object]:
        task_id = _identifier(task_id, "task_id")
        reason = _reason(reason)
        now_us = _clock(now_utc)
        new_status = "PAUSED" if operation == "pause" else "CANCELLED"
        with self._transaction() as conn:
            self._advance_clock(conn, now_us)
            row = self._row(conn, task_id)
            if row["status"] == new_status and row["control_reason"] == reason:
                return self._snapshot(row)
            if row["status"] in ("DONE", "DEAD", "UNRESOLVED", "CANCELLED"):
                raise TaskConflict("cannot change a terminal task")
            conn.execute(
                """UPDATE director_tasks
                   SET status=?,control_reason=?,worker_id=NULL,expires_us=NULL,
                       lease_seconds=NULL,next_eligible_us=NULL,
                       fencing_token=fencing_token+1 WHERE task_id=?""",
                (new_status, reason, task_id),
            )
            return self._snapshot(self._row(conn, task_id))
