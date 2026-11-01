"""v0.5 protocol integration through real coordinator, mailbox and HMAC ledgers."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox
from runtime.director.recovery_copy import stage_recovery_copy
from runtime.director.secure_worker import VerifiedWorkerInbox


GOAL = {"goal_id": "quality.check", "objective": "Investigate local quality reports",
        "metric": "quality", "target_value": 8, "direction": "INCREASE"}
CAPS = [
    {"capability_id": "alpha", "agent_id": "worker_alpha", "metric": "quality",
     "expected_delta": 2, "cost_units": 0},
    {"capability_id": "beta", "agent_id": "worker_beta", "metric": "quality",
     "expected_delta": 1, "cost_units": 0},
]
KEYS = {"worker_alpha": b"A" * 32, "worker_beta": b"B" * 32,
        "director.worker_alpha": b"C" * 32, "director.worker_beta": b"D" * 32}


class V05AuthenticatedCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.mailbox = DirectorMailbox(root / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.director_auth = AuthenticatedChannel(root / "director_auth.db", KEYS)
        self.addCleanup(self.director_auth.close)
        self.worker_auth = AuthenticatedChannel(root / "worker_auth.db",
                                                 {"worker_alpha": KEYS["worker_alpha"],
                                                  "director.worker_alpha": KEYS["director.worker_alpha"]})
        self.addCleanup(self.worker_auth.close)
        self.worker = VerifiedWorkerInbox(self.mailbox, self.worker_auth, "worker_alpha")
        self.path = root / "coordinator.db"
        self.coordinator = DirectorCoordinator(self.path, self.mailbox,
                                               auth_channel=self.director_auth,
                                               sign_outbound=True)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPS, max_missions=4)

    def issue(self):
        result = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(len(result["new_missions"]), 1)
        message = self.mailbox.poll("worker_alpha")[0]
        self.assertEqual(message["kind"], "SIGNED_ENVELOPE")
        return message

    def report(self, assignment_id, *, signed_id="worker.report.1", raw=None):
        if raw is None:
            raw = self.worker_auth.seal(
                sender_id="worker_alpha", recipient_id="director", kind="MISSION_REPORT",
                payload={"in_reply_to": assignment_id, "body": {"finding": "needs review"}},
                issued_at=100, expires_at=300, message_id=signed_id)
        return self.mailbox.send(sender_id="forged_outer_label", recipient_id="director",
                                 kind="SIGNED_ENVELOPE", payload={"envelope": raw.decode("ascii")})

    def admission_count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM director_authenticated_admissions").fetchone()[0]

    def test_signed_outbound_worker_verifies_but_cannot_execute(self):
        assignment = self.issue()
        received = self.worker.verify_message(assignment, now_utc=150)
        self.assertEqual(received["kind"], "MISSION_ASSIGNMENT")
        self.assertEqual(received["body"]["assigned_agent_id"], "worker_alpha")
        self.assertEqual(received["worker_action_status"], "NOT_EXECUTED")
        self.assertFalse(received["mailbox_acked"])
        with self.assertRaisesRegex(ValueError, "replay|reused"):
            self.worker.verify_message(assignment, now_utc=150)

    def test_atomic_signed_report_then_replay_rejected_under_new_wrapper(self):
        assignment = self.issue()
        raw = self.worker_auth.seal(
            sender_id="worker_alpha", recipient_id="director", kind="MISSION_REPORT",
            payload={"in_reply_to": assignment["message_id"], "body": {"result": "unverified"}},
            issued_at=100, expires_at=300, message_id="worker.report.stable")
        self.report(assignment["message_id"], raw=raw)
        response = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(response["inbound_reports"][0]["review_status"],
                         "MATCHED_AUTHENTICATED_UNVERIFIED")
        self.assertEqual(self.admission_count(), 1)
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "REPORTED")
        # The coordinator did not consume the *separate* auth-ledger marker;
        # its replay marker and report were committed in the same DB instead.
        self.director_auth.verify(raw, expected_recipient="director", now_utc=150)
        self.report(assignment["message_id"], raw=raw)
        replay = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(replay["inbound_reports"][0]["review_status"],
                         "REJECTED_REPLAY_OR_NONCE")
        self.assertEqual(self.admission_count(), 1)
        self.assertEqual(self.coordinator.inspect()["world"]["facts"], {})

    def test_unconsumed_inspection_before_restart_does_not_lose_report(self):
        assignment = self.issue()
        raw = self.worker_auth.seal(
            sender_id="worker_alpha", recipient_id="director", kind="MISSION_REPORT",
            payload={"in_reply_to": assignment["message_id"], "body": {}},
            issued_at=100, expires_at=300)
        self.director_auth.inspect_signature(raw, expected_recipient="director", now_utc=150)
        self.coordinator.close()
        self.coordinator = DirectorCoordinator(self.path, self.mailbox,
                                               auth_channel=self.director_auth,
                                               sign_outbound=True)
        self.report(assignment["message_id"], raw=raw)
        response = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(response["inbound_reports"][0]["review_status"],
                         "MATCHED_AUTHENTICATED_UNVERIFIED")
        self.assertEqual(self.admission_count(), 1)

    def test_crash_after_report_commit_before_mailbox_ack_retries_idempotently(self):
        assignment = self.issue()
        self.report(assignment["message_id"])
        original_ack = self.mailbox.ack
        def lose_ack(*_args, **_kwargs):
            raise RuntimeError("injected crash before mailbox acknowledgement")
        self.mailbox.ack = lose_ack
        try:
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.coordinator.tick(auth_now_utc=150)
        finally:
            self.mailbox.ack = original_ack
        self.assertEqual(self.admission_count(), 1)
        self.coordinator.close()
        self.coordinator = DirectorCoordinator(self.path, self.mailbox,
                                               auth_channel=self.director_auth,
                                               sign_outbound=True)
        result = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(result["inbound_reports"][0]["review_status"],
                         "MATCHED_AUTHENTICATED_UNVERIFIED")
        self.assertEqual(self.admission_count(), 1)
        self.assertEqual(len(self.coordinator.inspect()["reports"]), 1)
        self.assertFalse(self.mailbox.poll("director"))

    def test_report_transaction_failure_rolls_back_replay_admission(self):
        assignment = self.issue()
        self.report(assignment["message_id"])
        # Deterministic fault at the report INSERT *after* admission INSERT:
        # a single coordinator transaction must roll back both records.
        with self.coordinator._transaction() as conn:
            conn.execute("""CREATE TRIGGER inject_report_failure
                BEFORE INSERT ON director_strategy_reports
                BEGIN SELECT RAISE(ABORT,'injected report-write crash'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected report-write crash"):
            self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(self.admission_count(), 0)
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")
        with self.coordinator._transaction() as conn:
            conn.execute("DROP TRIGGER inject_report_failure")
        accepted = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(accepted["inbound_reports"][0]["review_status"],
                         "MATCHED_AUTHENTICATED_UNVERIFIED")
        self.assertEqual(self.admission_count(), 1)

    def test_crash_after_signed_mailbox_send_uses_identical_persisted_envelope(self):
        original_send = self.mailbox.send
        def send_then_crash(**kwargs):
            receipt = original_send(**kwargs)
            if kwargs["kind"] == "SIGNED_ENVELOPE":
                raise RuntimeError("injected crash after mailbox commit")
            return receipt
        self.mailbox.send = send_then_crash
        try:
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                self.coordinator.tick(auth_now_utc=150)
        finally:
            self.mailbox.send = original_send
        before = self.mailbox.poll("worker_alpha")[0]["payload"]["envelope"]
        self.coordinator.close()
        self.coordinator = DirectorCoordinator(self.path, self.mailbox,
                                               auth_channel=self.director_auth,
                                               sign_outbound=True)
        self.coordinator.tick(auth_now_utc=160)
        after = self.mailbox.poll("worker_alpha")[0]["payload"]["envelope"]
        self.assertEqual(before, after)
        self.assertEqual(len(self.mailbox.poll("worker_alpha")), 1)
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")

    def test_operator_cancel_is_signed_on_next_tick_even_during_pause(self):
        assignment = self.issue()
        self.coordinator.set_paused(True)
        queued = self.coordinator.cancel_mission(assignment["message_id"])
        self.assertEqual(queued["state"], "QUEUED")
        self.assertEqual(len(self.mailbox.poll("worker_alpha")), 1)
        self.coordinator.tick(auth_now_utc=151)
        cancel = self.mailbox.poll("worker_alpha")[-1]
        verified = self.worker.verify_message(cancel, now_utc=151)
        self.assertEqual(verified["kind"], "MISSION_CANCEL")
        self.assertFalse(verified["body"]["worker_stopped_verified"])

    def test_expired_queued_envelope_fails_closed_without_resigning(self):
        original_send = self.mailbox.send
        def fail_send(**_kwargs):
            raise RuntimeError("injected mailbox outage")
        self.mailbox.send = fail_send
        try:
            with self.assertRaisesRegex(RuntimeError, "injected mailbox outage"):
                self.coordinator.tick(auth_now_utc=150)
        finally:
            self.mailbox.send = original_send
        with sqlite3.connect(self.path) as conn:
            persisted = conn.execute("""SELECT outbound_envelope_json FROM
                director_strategy_assignments""").fetchone()[0]
        self.assertIsInstance(persisted, str)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.coordinator.tick(auth_now_utc=150 + 86400)
        self.assertEqual(self.mailbox.poll("worker_alpha"), [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("""SELECT outbound_envelope_json FROM
                director_strategy_assignments""").fetchone()[0], persisted)

    def test_unsigned_director_and_plain_worker_claims_fail_closed(self):
        assignment = self.issue()
        forged = self.mailbox.send(sender_id="director", recipient_id="worker_alpha",
                                   kind="MISSION_CANCEL", payload={"reason": "forged"})
        with self.assertRaisesRegex(ValueError, "unsigned"):
            self.worker.verify_message(forged, now_utc=150)
        self.mailbox.reply(sender_id="worker_alpha", in_reply_to=assignment["message_id"],
                           kind="MISSION_REPORT", payload={"claim": "done"})
        result = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(result["inbound_reports"][0]["review_status"],
                         "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE")
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")

    def test_auth_mode_downgrade_and_missing_route_key_rejected(self):
        self.coordinator.close()
        with self.assertRaisesRegex(ValueError, "immutable"):
            DirectorCoordinator(self.path, self.mailbox, auth_channel=self.director_auth)
        with self.assertRaisesRegex(ValueError, "immutable"):
            DirectorCoordinator(self.path, self.mailbox)
        other = self.path.with_name("missing_route.db")
        with AuthenticatedChannel(self.path.with_name("missing_auth.db"),
                                  {"worker_alpha": KEYS["worker_alpha"]}) as auth:
            with DirectorCoordinator(other, self.mailbox, auth_channel=auth,
                                     sign_outbound=True) as coordinator:
                with self.assertRaisesRegex(ValueError, "missing independently configured"):
                    coordinator.configure(GOAL, CAPS)

    def test_quarantined_signed_coordinator_rejects_cancel_and_stop_bypasses(self):
        assignment = self.issue()
        self.coordinator.set_paused(True)
        destination = self.path.with_name("quarantined.db")
        approval = {"approved": True, "purpose": "PAUSED_QUARANTINE_DATA_ONLY",
                    "source_goal_id": GOAL["goal_id"],
                    "destination_path": str(destination.resolve()),
                    "allow_historical_inflight": False}
        receipt = stage_recovery_copy(self.path, destination, operator_approval=approval)
        self.assertTrue(receipt["quarantined"])
        with DirectorMailbox(self.path.with_name("recovery-mailbox.db")) as inert_mailbox:
            with DirectorCoordinator(destination, inert_mailbox,
                                     auth_channel=self.director_auth, sign_outbound=True) as restored:
                self.assertEqual(restored.tick()["status"], "QUARANTINED")
                self.assertEqual(restored.inspect()["status"], "QUARANTINED")
                with self.assertRaisesRegex(RuntimeError, "recovery-quarantined"):
                    restored.request_stop()
                with self.assertRaisesRegex(RuntimeError, "recovery-quarantined"):
                    restored.cancel_mission(assignment["message_id"])
                with self.assertRaisesRegex(RuntimeError, "recovery-quarantined"):
                    restored.set_paused(False)
                self.assertEqual(restored._flush_outbox(auth_now_utc=151), [])
                self.assertEqual(restored._flush_cancellations(auth_now_utc=151), [])
                self.assertEqual(inert_mailbox.poll("worker_alpha"), [])


if __name__ == "__main__":
    unittest.main()
