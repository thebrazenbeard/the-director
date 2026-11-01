"""Real SQLite recovery-copy qualification: full-fidelity, paused, no activation."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox
from runtime.director.recovery_copy import SCHEMA, stage_recovery_copy


GOAL = {"goal_id": "recovery.goal", "objective": "Review a declared quality metric",
        "metric": "quality", "target_value": 9, "direction": "INCREASE"}
CAPS = [{"capability_id": "inspect", "agent_id": "worker.inspect", "metric": "quality",
         "expected_delta": 2, "cost_units": 0},
        {"capability_id": "assess", "agent_id": "worker.assess", "metric": "quality",
         "expected_delta": 1, "cost_units": 0}]


def approval(path: Path, *, goal_id: str = "recovery.goal", approved: bool = True) -> dict:
    return {"approved": approved, "purpose": "PAUSED_QUARANTINE_DATA_ONLY",
            "source_goal_id": goal_id, "destination_path": str(path.absolute()),
            "allow_historical_inflight": False}


def rowset(path: Path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


class PausedRecoveryCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        parent = Path(self.temp.name)
        self.source_path = parent / "director.sqlite3"
        self.destination = parent / "recovered.sqlite3"
        self.mailbox = DirectorMailbox(parent / "mailbox.sqlite3")
        self.addCleanup(self.mailbox.close)
        self.coordinator = DirectorCoordinator(self.source_path, self.mailbox)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPS, max_missions=5)

    def _populate(self):
        self.coordinator.configure_graph([
            {"mission_id": "inspect", "kind": "OBSERVE", "depends_on": [],
             "resources": {"analyst": 1}, "priority": 10, "cost_units": 0},
            {"mission_id": "assess", "kind": "ANALYZE", "depends_on": ["inspect"],
             "resources": {"analyst": 1}, "priority": 10, "cost_units": 0},
        ], {"analyst": 1})
        self.coordinator.tick()
        self.coordinator.observe({"event_id": "first.quality", "claim_id": "quality",
                                  "value": 4, "evidence_class": "OBSERVATION",
                                  "source_ref": "manual.metric", "lineage_id": "test.lineage"})
        self.coordinator.confirm_observation("first.quality")
        self.coordinator.set_graph_logical_tick(4)

    def test_full_fidelity_paused_quarantine_preserves_pending_missions_evidence_and_graph(self):
        self._populate()
        self.coordinator.set_paused(True)
        source_before = self.coordinator.inspect()
        mailbox_before = self.mailbox.poll("worker.inspect", include_acked=True)
        receipt = stage_recovery_copy(self.source_path, self.destination,
                                      operator_approval=approval(self.destination))
        self.assertEqual(receipt["recovery_schema"], SCHEMA)
        self.assertTrue(receipt["quarantined"])
        self.assertEqual(receipt["auth_mode"], "LOCAL_UNAUTHENTICATED")
        self.assertEqual(receipt["effect_status"], "NOT_EXECUTED")
        self.assertEqual(len(receipt["snapshot_digest"]), 64)
        self.assertTrue(self.destination.is_file())
        self.assertEqual(self.mailbox.poll("worker.inspect", include_acked=True), mailbox_before)
        self.assertEqual(self.coordinator.inspect(), source_before)
        for table in ("director_strategy_config", "director_strategy_control",
                      "director_strategy_events", "director_strategy_assignments",
                      "director_strategy_reports", "director_strategy_cancellations",
                      "director_plan_graph", "director_plan_completions",
                      "director_strategy_auth_policy"):
            self.assertEqual(rowset(self.destination, table), rowset(self.source_path, table), table)
        record = rowset(self.destination, "director_recovery_quarantine")
        self.assertEqual(record[0][0:4], (1, 1, SCHEMA, GOAL["goal_id"]))
        self.assertEqual(record[0][4], receipt["snapshot_digest"])
        # Copy retains actual full payload/config, not just inspect projection.
        config = json.loads(rowset(self.destination, "director_strategy_config")[0][1])
        self.assertEqual(config["capabilities"], CAPS)
        self.assertEqual(config["max_missions"], 5)
        self.assertTrue(rowset(self.destination, "director_strategy_assignments")[0][4])

        # Root Coordinator must treat the copied DB as permanently inert even
        # with historical QUEUED/SENT assignments and pending cancellations.
        before = self.mailbox.poll("worker.inspect", include_acked=True)
        with DirectorCoordinator(self.destination, self.mailbox) as recovered:
            status = recovered.tick()
            self.assertEqual(status["status"], "QUARANTINED")
            self.assertEqual(status["new_missions"], [])
            self.assertEqual(status["cancellation_message_ids"], [])
            self.assertEqual(recovered.inspect()["recovery_quarantine"]["locked"], 1)
            with self.assertRaisesRegex(RuntimeError, "quarantined"):
                recovered.set_paused(False)
            with self.assertRaisesRegex(RuntimeError, "quarantined"):
                recovered.observe({"event_id": "after.copy", "claim_id": "quality", "value": 8,
                                   "evidence_class": "OBSERVATION", "source_ref": "manual",
                                   "lineage_id": "manual"})
        self.assertEqual(self.mailbox.poll("worker.inspect", include_acked=True), before)

    def test_source_must_be_paused_and_approval_must_bind_exact_goal_target(self):
        for permission in (approval(self.destination), approval(self.destination, approved=False),
                           approval(self.destination, goal_id="wrong.goal"),
                           {**approval(self.destination), "allow_historical_inflight": True},
                           {**approval(self.destination), "destination_path": self.destination.name},
                           {**approval(self.destination), "destination_path": str(self.destination) + "-other"}):
            with self.assertRaises(ValueError):
                stage_recovery_copy(self.source_path, self.destination,
                                    operator_approval=permission)
            self.assertFalse(self.destination.exists())
        self.coordinator.set_paused(True)
        with self.assertRaises(ValueError):
            stage_recovery_copy(self.source_path, self.destination,
                                operator_approval=approval(self.destination, goal_id="wrong.goal"))
        self.assertFalse(self.destination.exists())

    def test_equivalent_absolute_destination_spelling_is_approved(self):
        self.coordinator.set_paused(True)
        # On Windows Path.resolve may expand a RUNNER~1 component whereas
        # Path.absolute retains the short spelling. Both bind to one target.
        alternate = (self.destination.parent / ".." /
                     self.destination.parent.name / self.destination.name)
        self.assertTrue(alternate.is_absolute())
        receipt = stage_recovery_copy(self.source_path, self.destination,
                                      operator_approval={**approval(self.destination),
                                                         "destination_path": str(alternate)})
        self.assertTrue(receipt["quarantined"])

    def test_existing_destination_never_overwritten_and_no_partial_copy(self):
        self.coordinator.set_paused(True)
        self.destination.write_bytes(b"USER-OWNED-SENTINEL")
        with self.assertRaises(FileExistsError):
            stage_recovery_copy(self.source_path, self.destination,
                                operator_approval=approval(self.destination))
        self.assertEqual(self.destination.read_bytes(), b"USER-OWNED-SENTINEL")
        self.assertFalse(list(self.destination.parent.glob(".director-quarantine-*.sqlite3")))

    def test_failed_publish_never_creates_a_destination_or_leaks_a_staging_file(self):
        self.coordinator.set_paused(True)
        with patch("runtime.director.recovery_copy.os.link", side_effect=OSError("no hardlinks")):
            with self.assertRaisesRegex(OSError, "no hardlinks"):
                stage_recovery_copy(self.source_path, self.destination,
                                    operator_approval=approval(self.destination))
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.destination.parent.glob(".director-quarantine-*.sqlite3")))

    def test_rejects_malformed_source_schema_and_corrupt_local_attestation(self):
        bad_path = self.destination.with_name("bad.sqlite3")
        with closing(sqlite3.connect(bad_path)) as conn:
            conn.execute("CREATE TABLE unrelated (some_text TEXT)")
        if os.name == "posix":
            bad_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "missing Director coordinator schema"):
            stage_recovery_copy(bad_path, self.destination,
                                operator_approval=approval(self.destination))
        self.assertFalse(self.destination.exists())
        self.coordinator.observe({"event_id": "event.a", "claim_id": "quality", "value": 5,
                                  "evidence_class": "OBSERVATION", "source_ref": "source.a",
                                  "lineage_id": "lineage.a"})
        self.coordinator.set_paused(True)
        # Simulate corrupted external state. Source remains untouched.
        with self.coordinator._transaction() as conn:
            conn.execute("UPDATE director_strategy_events SET success_confirmed=1,operator_attested=0")
        with self.assertRaisesRegex(ValueError, "operator review"):
            stage_recovery_copy(self.source_path, self.destination,
                                operator_approval=approval(self.destination))
        self.assertFalse(self.destination.exists())

    def test_quarantined_copy_cannot_be_recopied_as_live_source(self):
        self.coordinator.set_paused(True)
        stage_recovery_copy(self.source_path, self.destination,
                            operator_approval=approval(self.destination))
        next_path = self.destination.with_name("second-generation.sqlite3")
        with self.assertRaisesRegex(ValueError, "quarantined database"):
            stage_recovery_copy(self.destination, next_path,
                                operator_approval=approval(next_path))
        self.assertFalse(next_path.exists())

    def test_authenticated_source_copy_preserves_policy_without_importing_keys(self):
        auth_path = self.source_path.with_name("auth.sqlite3")
        with AuthenticatedChannel(auth_path, {"worker.inspect": b"W" * 32}) as channel:
            alternate = self.source_path.with_name("authenticated.sqlite3")
            with DirectorCoordinator(alternate, self.mailbox, auth_channel=channel) as secured:
                secured.configure(GOAL, CAPS, max_missions=3)
                secured.set_paused(True)
                copy = self.destination
                receipt = stage_recovery_copy(alternate, copy, operator_approval=approval(copy))
                self.assertEqual(receipt["auth_mode"], "LOCAL_HMAC")
                self.assertEqual(rowset(copy, "director_strategy_auth_policy")[0][1], "LOCAL_HMAC")
                self.assertEqual(list(Path(self.temp.name).glob("*key*")), [])

    def test_bidirectional_authenticated_source_keeps_mode_but_does_not_import_keys(self):
        auth_path = self.source_path.with_name("auth-bidirectional.sqlite3")
        with AuthenticatedChannel(auth_path, {
            "director.worker.inspect": b"D" * 32,
            "director.worker.assess": b"E" * 32,
            "worker.inspect": b"W" * 32,
        }) as channel:
            alternate = self.source_path.with_name("bidi.sqlite3")
            with DirectorCoordinator(alternate, self.mailbox, auth_channel=channel,
                                     sign_outbound=True) as secured:
                secured.configure(GOAL, CAPS, max_missions=3)
                secured.tick(auth_now_utc=150)
                self.assertIsNotNone(rowset(alternate, "director_strategy_assignments")[0][-1])
                secured.set_paused(True)
                receipt = stage_recovery_copy(alternate, self.destination,
                                              operator_approval=approval(self.destination))
                self.assertEqual(receipt["auth_mode"], "LOCAL_HMAC_BIDIRECTIONAL")
                self.assertEqual(rowset(self.destination, "director_strategy_auth_policy")[0][1],
                                 "LOCAL_HMAC_BIDIRECTIONAL")
                self.assertEqual(rowset(self.destination, "director_strategy_assignments"),
                                 rowset(alternate, "director_strategy_assignments"))
                with DirectorCoordinator(self.destination, self.mailbox, auth_channel=channel,
                                         sign_outbound=True) as restored:
                    self.assertEqual(restored.tick()["status"], "QUARANTINED")

    @unittest.skipUnless(os.name == "posix", "POSIX file-mode qualification")
    def test_source_and_destination_require_private_local_permissions(self):
        self.coordinator.set_paused(True)
        prior = self.source_path.stat().st_mode & 0o777
        try:
            self.source_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                stage_recovery_copy(self.source_path, self.destination,
                                    operator_approval=approval(self.destination))
        finally:
            self.source_path.chmod(prior)
        insecure_parent = self.destination.parent / "public-parent"
        insecure_parent.mkdir(mode=0o755)
        insecure_parent.chmod(0o755)
        public_target = insecure_parent / "recovered.sqlite3"
        with self.assertRaisesRegex(ValueError, "parent must be private"):
            stage_recovery_copy(self.source_path, public_target,
                                operator_approval=approval(public_target))

        destination_link = self.destination.parent / "linked.sqlite3"
        destination_link.symlink_to(self.source_path)
        with self.assertRaises(FileExistsError):
            stage_recovery_copy(self.source_path, destination_link,
                                operator_approval=approval(destination_link))

    def test_source_is_preserved_if_staged_database_has_unsupported_trigger(self):
        # A syntactically valid but unexpected source SQL trigger must not be
        # copied into a future writable coordinator lifecycle.
        self.coordinator.set_paused(True)
        with self.coordinator._transaction() as conn:
            conn.execute("""CREATE TRIGGER unexpected_after_config AFTER UPDATE
                ON director_strategy_config BEGIN SELECT 1; END""")
        with self.assertRaisesRegex(ValueError, "unreviewed SQL trigger"):
            stage_recovery_copy(self.source_path, self.destination,
                                operator_approval=approval(self.destination))
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
