"""Crash-recoverable, data-only agent handoff without persisting memories."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from runtime.director.agent_state import (
    AgentStateLedger,
    cancel_agent_state_restore,
    export_agent_state,
    restore_agent_state,
    resume_agent_state_restore,
)


GOAL = {"goal_id": "quality", "objective": "Review a declared metric",
        "metric": "quality", "target_value": 8, "direction": "INCREASE"}
AGENT = "agent.resumable"
SOURCE = "instance.old"
TARGET = "instance.new"
DOMAIN = "private.domain"


def capsule(*, capsule_id="capsule.resumable", summary="top-secret-example"):
    return export_agent_state(
        capsule_id=capsule_id, agent_id=AGENT, source_instance_id=SOURCE,
        target_domain=DOMAIN,
        state={"goal": GOAL, "memories": [{"memory_id": "memo.one", "summary": summary}]},
        consent={"approved": True, "agent_id": AGENT,
                 "source_instance_id": SOURCE, "target_domain": DOMAIN,
                 "allow_memories": True, "operator_ref": "test.operator"},
    )


def approval(**changes):
    return {"approved": True, "agent_id": AGENT, "new_instance_id": TARGET,
            "target_domain": DOMAIN, **changes}


def cancellation_approval(**changes):
    return {**approval(), "purpose": "CANCEL_DATA_ONLY_RESTORE", **changes}


def handoff(fn, raw, ledger, *, permission=None, **changes):
    return fn(raw, target_domain=DOMAIN, new_instance_id=TARGET,
              operator_approval=approval() if permission is None else permission,
              ledger=ledger, **changes)


class RestoredStateResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accepted.sqlite3"

    def _admit_and_drop_response(self, raw):
        with AgentStateLedger(self.path) as ledger:
            handoff(restore_agent_state, raw, ledger)
        # Simulate a caller crash *after* SQLite COMMIT and before retaining
        # the returned record. The only surviving inputs are DB and capsule.

    def test_reconstructs_exact_inert_record_after_crash_and_ledger_reopen(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as ledger:
            with patch("subprocess.Popen", side_effect=AssertionError("unexpected process")):
                restored = handoff(resume_agent_state_restore, raw, ledger)
                self.assertEqual(restored, handoff(resume_agent_state_restore, raw, ledger))
            self.assertEqual(restored["new_instance_id"], TARGET)
            self.assertEqual(restored["state"]["memories"][0]["summary"], "top-secret-example")
            self.assertEqual(restored["authority"], "NOT_GRANTED")
            self.assertEqual(restored["effects"], "NOT_EXECUTED")
            self.assertEqual(restored["continuity_claim"], "INFORMATION_STATE_COPY_ONLY")
            with self.assertRaisesRegex(ValueError, "already restored"):
                handoff(restore_agent_state, raw, ledger)

    def test_ledger_never_persists_private_state_or_memory_text(self):
        raw = capsule(summary="PRIVATE-MEMORY-DO-NOT-STORE")
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as ledger:
            handoff(resume_agent_state_restore, raw, ledger)
            rows = ledger._conn.execute("SELECT sql FROM sqlite_master").fetchall()
            self.assertFalse(any("state_json" in str(row) for row in rows))
        self.assertNotIn(b"PRIVATE-MEMORY-DO-NOT-STORE", self.path.read_bytes())
        self.assertNotIn(b"Review a declared metric", self.path.read_bytes())

    def test_changed_but_valid_capsule_cannot_recover_existing_receipt(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        different = capsule(summary="another value")
        with AgentStateLedger(self.path) as ledger:
            with self.assertRaisesRegex(ValueError, "receipt|digest|match"):
                handoff(resume_agent_state_restore, different, ledger)
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                handoff(resume_agent_state_restore,
                        raw.replace(b"top-secret-example", b"bit-flipped-memory"), ledger)

    def test_wrong_or_unapproved_target_cannot_resume(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as ledger:
            for invalid in (approval(approved=False), approval(agent_id="different"),
                            approval(new_instance_id="different"),
                            approval(target_domain="different"),
                            {**approval(), "host_authority": True}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    handoff(resume_agent_state_restore, raw, ledger, permission=invalid)
            with self.assertRaisesRegex(ValueError, "target domain"):
                resume_agent_state_restore(raw, target_domain="wrong.domain",
                                           new_instance_id=TARGET,
                                           operator_approval=approval(), ledger=ledger)

    def test_unadmitted_capsule_cannot_resume_or_activate_anything(self):
        with AgentStateLedger(self.path) as ledger:
            with patch("subprocess.Popen", side_effect=AssertionError("unexpected process")):
                with self.assertRaisesRegex(ValueError, "no committed restore receipt"):
                    handoff(resume_agent_state_restore, capsule(), ledger)

    def test_modified_ledger_binding_is_not_accepted_by_same_capsule(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as ledger:
            ledger._conn.execute("UPDATE restored_capsules SET source_instance_id='not.original'")
            with self.assertRaisesRegex(ValueError, "receipt does not match"):
                handoff(resume_agent_state_restore, raw, ledger)
            with self.assertRaisesRegex(ValueError, "receipt does not match"):
                handoff(cancel_agent_state_restore, raw, ledger,
                        permission=cancellation_approval())

    def test_legacy_v1_ledger_row_migrates_additively_without_state_copies(self):
        raw = capsule()
        digest = json.loads(raw)["sha256"]
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""CREATE TABLE restored_capsules (
                capsule_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                source_instance_id TEXT NOT NULL, new_instance_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL, target_domain TEXT NOT NULL)""")
            conn.execute("INSERT INTO restored_capsules VALUES (?,?,?,?,?,?)",
                         ("capsule.resumable", digest, SOURCE, TARGET, AGENT, DOMAIN))
            conn.commit()
        with AgentStateLedger(self.path) as ledger:
            restored = handoff(resume_agent_state_restore, raw, ledger)
            self.assertEqual(restored["capsule_id"], "capsule.resumable")
            with self.assertRaises(ValueError):
                handoff(restore_agent_state, raw, ledger)

    def test_terminal_cancel_blocks_resume_across_restart_but_keeps_replay(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as ledger:
            with self.assertRaises(ValueError):
                handoff(cancel_agent_state_restore, raw, ledger, permission=approval())
            receipt = handoff(cancel_agent_state_restore, raw, ledger,
                              permission=cancellation_approval())
            self.assertEqual(receipt["status"], "CANCELLED_DATA_ONLY")
            self.assertEqual(receipt["effects"], "NOT_EXECUTED")
            self.assertEqual(handoff(cancel_agent_state_restore, raw, ledger,
                                     permission=cancellation_approval()), receipt)
        with AgentStateLedger(self.path) as ledger:
            with self.assertRaisesRegex(ValueError, "cancelled"):
                handoff(resume_agent_state_restore, raw, ledger)
            with self.assertRaisesRegex(ValueError, "already restored"):
                handoff(restore_agent_state, raw, ledger)
            with self.assertRaisesRegex(ValueError, "already restored|already used"):
                handoff(restore_agent_state, capsule(capsule_id="another.capsule"), ledger)

    def test_cannot_cancel_unadmitted_or_different_capsule(self):
        raw = capsule()
        with AgentStateLedger(self.path) as ledger:
            with self.assertRaises(ValueError):
                handoff(cancel_agent_state_restore, raw, ledger,
                        permission=cancellation_approval())
            handoff(restore_agent_state, raw, ledger)
            with self.assertRaises(ValueError):
                handoff(cancel_agent_state_restore, capsule(summary="changed"), ledger,
                        permission=cancellation_approval())
            handoff(resume_agent_state_restore, raw, ledger)

    def test_failed_begin_preserves_original_database_lock_error(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as lock_owner, AgentStateLedger(self.path) as contender:
            contender._conn.execute("PRAGMA busy_timeout=0")
            lock_owner._conn.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    handoff(cancel_agent_state_restore, raw, contender,
                            permission=cancellation_approval())
                self.assertFalse(contender._conn.in_transaction)
            finally:
                lock_owner._conn.execute("ROLLBACK")
            receipt = handoff(cancel_agent_state_restore, raw, contender,
                              permission=cancellation_approval())
            self.assertEqual(receipt["status"], "CANCELLED_DATA_ONLY")
            with self.assertRaisesRegex(ValueError, "cancelled"):
                handoff(resume_agent_state_restore, raw, contender)

    def test_restore_trigger_auto_rollback_preserves_original_failure(self):
        """SQLite can end the transaction itself before Python cleanup runs."""
        raw = capsule()
        with AgentStateLedger(self.path) as ledger:
            ledger._conn.execute(
                "CREATE TRIGGER rollback_restore BEFORE INSERT ON restored_capsules "
                "BEGIN SELECT RAISE(ROLLBACK, 'original restore failure'); END"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "original restore failure"):
                handoff(restore_agent_state, raw, ledger)
            self.assertFalse(ledger._conn.in_transaction)
            self.assertEqual(
                0, ledger._conn.execute("SELECT count(*) FROM restored_capsules").fetchone()[0]
            )
            ledger._conn.execute("DROP TRIGGER rollback_restore")
            restored = handoff(restore_agent_state, raw, ledger)
            self.assertEqual("NOT_EXECUTED", restored["effects"])

    def test_restore_failed_begin_preserves_database_lock_error(self):
        raw = capsule()
        with AgentStateLedger(self.path) as owner, AgentStateLedger(self.path) as contender:
            contender._conn.execute("PRAGMA busy_timeout=0")
            owner._conn.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    handoff(restore_agent_state, raw, contender)
                self.assertFalse(contender._conn.in_transaction)
            finally:
                owner._conn.execute("ROLLBACK")
            restored = handoff(restore_agent_state, raw, contender)
            self.assertEqual("NOT_EXECUTED", restored["effects"])

    def test_restore_never_acknowledges_missing_or_modified_receipt(self):
        raw = capsule()
        triggers = {
            "ignored": (
                "CREATE TRIGGER faulty_insert BEFORE INSERT ON restored_capsules "
                "BEGIN SELECT RAISE(IGNORE); END"
            ),
            "deleted": (
                "CREATE TRIGGER faulty_insert AFTER INSERT ON restored_capsules "
                "BEGIN DELETE FROM restored_capsules WHERE capsule_id=NEW.capsule_id; END"
            ),
            "mutated": (
                "CREATE TRIGGER faulty_insert AFTER INSERT ON restored_capsules "
                "BEGIN UPDATE restored_capsules SET digest='invalid' "
                "WHERE capsule_id=NEW.capsule_id; END"
            ),
        }
        for failure, statement in triggers.items():
            with self.subTest(failure=failure):
                database = Path(self.temp.name) / f"{failure}.sqlite3"
                with AgentStateLedger(database) as ledger:
                    ledger._conn.execute(statement)
                    with self.assertRaisesRegex(ValueError, "restore receipt"):
                        handoff(restore_agent_state, raw, ledger)
                    self.assertFalse(ledger._conn.in_transaction)
                    self.assertEqual(
                        0, ledger._conn.execute(
                            "SELECT count(*) FROM restored_capsules"
                        ).fetchone()[0]
                    )
                    ledger._conn.execute("DROP TRIGGER faulty_insert")
                    restored = handoff(restore_agent_state, raw, ledger)
                    resumed = handoff(resume_agent_state_restore, raw, ledger)
                    self.assertEqual(restored, resumed)
                    self.assertEqual("NOT_EXECUTED", restored["effects"])

    def test_cancel_after_resume_does_not_revoke_returned_data(self):
        raw = capsule()
        self._admit_and_drop_response(raw)
        with AgentStateLedger(self.path) as ledger:
            previously_returned = handoff(resume_agent_state_restore, raw, ledger)
            cancellation = handoff(cancel_agent_state_restore, raw, ledger,
                                   permission=cancellation_approval())
            with self.assertRaisesRegex(ValueError, "cancelled"):
                handoff(resume_agent_state_restore, raw, ledger)
            # This deterministic order proves only future resume refusal.
            # A copy previously returned to a caller cannot be revoked.
            self.assertEqual(previously_returned["state"]["goal"], GOAL)
            self.assertEqual(previously_returned["effects"], "NOT_EXECUTED")
            self.assertFalse(cancellation["runtime_stopped_verified"])


if __name__ == "__main__":
    unittest.main()
