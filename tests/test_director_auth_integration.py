"""Authenticated local Director reports must pass HMAC verification before matching."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox

GOAL = {
    "goal_id": "measure_quality", "objective": "Improve measured quality using local feedback",
    "metric": "quality", "target_value": 8, "direction": "INCREASE",
}
CAPABILITIES = [
    {"capability_id": "alpha", "agent_id": "worker_alpha", "metric": "quality",
     "expected_delta": 3, "cost_units": 0},
    {"capability_id": "beta", "agent_id": "worker_beta", "metric": "quality",
     "expected_delta": 1, "cost_units": 0},
]


class AuthenticatedCoordinatorIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.mailbox_path = directory / "mailbox.sqlite3"
        self.coordinator_path = directory / "coordinator.sqlite3"
        self.auth_path = directory / "auth.sqlite3"
        self.keys = {"worker_alpha": b"A" * 32, "worker_beta": b"B" * 32}
        self.mailbox = DirectorMailbox(self.mailbox_path)
        self.addCleanup(self.mailbox.close)
        self.auth = AuthenticatedChannel(self.auth_path, self.keys)
        self.addCleanup(self.auth.close)
        self.coordinator = DirectorCoordinator(self.coordinator_path, self.mailbox, auth_channel=self.auth)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPABILITIES, max_missions=3)

    def assignment(self):
        self.coordinator.tick(auth_now_utc=150)
        return self.mailbox.poll("worker_alpha")[0]

    def signed_reply(self, *, request_id, worker="worker_alpha", recipient="director",
                     kind="MISSION_REPORT", body=None, issued=100, expires=200,
                     envelope_id="signed.1", claimed_sender="intruder", raw=None):
        if raw is None:
            raw = self.auth.seal(
                sender_id=worker, recipient_id=recipient, kind=kind,
                payload={"in_reply_to": request_id, "body": body or {"finding": "new evidence needed"}},
                issued_at=issued, expires_at=expires, message_id=envelope_id,
            )
        return self.mailbox.send(
            sender_id=claimed_sender, recipient_id="director", kind="SIGNED_ENVELOPE",
            payload={"envelope": raw.decode("ascii")},
        )

    def test_signed_sender_not_mailbox_claim_can_complete_report_leg(self):
        assignment = self.assignment()
        self.signed_reply(request_id=assignment["message_id"], claimed_sender="intruder")
        response = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(response["inbound_reports"][0]["review_status"],
                         "MATCHED_AUTHENTICATED_UNVERIFIED")
        self.assertTrue(response["inbound_reports"][0]["sender_authenticated"])
        recorded = self.coordinator.inspect()["reports"][0]
        self.assertEqual(recorded["reported_sender_id"], "worker_alpha")
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "REPORTED")
        self.assertEqual(self.coordinator.inspect()["world"]["facts"], {})
        self.assertEqual(response["authentication_mode"], "ENFORCED_LOCAL_HMAC")

    def test_plaintext_worker_spoof_cannot_mark_assignment_reported(self):
        assignment = self.assignment()
        self.mailbox.reply(sender_id="worker_alpha", in_reply_to=assignment["message_id"],
                           kind="MISSION_REPORT", payload={"status": "OBJECTIVE_MET"})
        response = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(response["inbound_reports"][0]["review_status"],
                         "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE")
        self.assertFalse(response["inbound_reports"][0]["sender_authenticated"])
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")

    def test_tampered_envelope_wrong_recipient_expiry_and_replay_fail_closed(self):
        assignment = self.assignment()
        target = assignment["message_id"]
        raw = self.auth.seal(sender_id="worker_alpha", recipient_id="director",
                             kind="MISSION_REPORT", payload={"in_reply_to": target,
                                                               "body": {"claim": "legit"}},
                             issued_at=100, expires_at=200, message_id="signed.good")
        tampered = raw.replace(b"legit", b"false")
        self.signed_reply(request_id=target, raw=tampered)
        self.signed_reply(request_id=target, raw=self.auth.seal(
            sender_id="worker_alpha", recipient_id="wrong", kind="MISSION_REPORT",
            payload={"in_reply_to": target, "body": {}}, issued_at=100, expires_at=200,
            message_id="signed.wrong"))
        self.signed_reply(request_id=target, raw=self.auth.seal(
            sender_id="worker_alpha", recipient_id="director", kind="MISSION_REPORT",
            payload={"in_reply_to": target, "body": {}}, issued_at=100, expires_at=140,
            message_id="signed.expired"))
        result = self.coordinator.tick(auth_now_utc=150)
        self.assertTrue(all(row["review_status"] == "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE"
                            for row in result["inbound_reports"]))
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")
        self.signed_reply(request_id=target, raw=raw)
        ok = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(ok["inbound_reports"][0]["review_status"],
                         "MATCHED_AUTHENTICATED_UNVERIFIED")
        self.signed_reply(request_id=target, raw=raw)
        replay = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(replay["inbound_reports"][0]["review_status"],
                         "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE")

    def test_wrong_worker_key_and_signed_inner_shape_do_not_match(self):
        assignment = self.assignment()
        self.signed_reply(request_id=assignment["message_id"], worker="worker_beta")
        self.signed_reply(request_id=assignment["message_id"], raw=self.auth.seal(
            sender_id="worker_alpha", recipient_id="director", kind="MISSION_REPORT",
            payload={"wrong": "shape"}, issued_at=100, expires_at=200,
            message_id="invalid.inner"))
        rows = self.coordinator.tick(auth_now_utc=150)["inbound_reports"]
        self.assertEqual([row["review_status"] for row in rows],
                         ["UNMATCHED_UNVERIFIED", "REJECTED_SIGNED_PAYLOAD_SHAPE"])
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")

    def test_signed_cancel_ack_only_claims_receipt_not_worker_stop(self):
        assignment = self.assignment()
        cancel = self.coordinator.cancel_mission(assignment["message_id"])
        self.signed_reply(request_id=cancel["cancellation_message_id"],
                          kind="MISSION_CANCEL_ACK", body={"received": True})
        result = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(result["inbound_reports"][0]["review_status"],
                         "CANCEL_ACK_AUTHENTICATED_UNVERIFIED")
        self.assertFalse(result["worker_stopped_verified"])

    def test_signed_report_then_separate_operator_review_unlocks_graph_dependency(self):
        def node(name, predecessors):
            return {"mission_id": name, "kind": "ANALYZE", "depends_on": predecessors,
                    "resources": {"analyst": 1}, "priority": 20, "cost_units": 0}
        self.coordinator.configure_graph([node("alpha", []), node("beta", ["alpha"])],
                                         {"analyst": 1})
        alpha = self.assignment()
        self.signed_reply(request_id=alpha["message_id"])
        self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(self.mailbox.poll("worker_beta"), [])
        self.coordinator.observe({
            "event_id": "operator.confirm.alpha", "claim_id": "completed.alpha",
            "value": True, "evidence_class": "OBSERVATION",
            "source_ref": "independent.local.review", "lineage_id": "operator.local",
        })
        self.coordinator.confirm_observation("operator.confirm.alpha")
        self.coordinator.confirm_graph_node_completed(
            "alpha", alpha["message_id"], "operator.confirm.alpha")
        after = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(len(after["new_missions"]), 1)
        self.assertEqual(self.mailbox.poll("worker_beta")[0]["payload"]["capability_id"], "beta")

    def test_auth_mode_sticky_and_time_required_even_when_no_messages(self):
        with self.assertRaises(ValueError):
            self.coordinator.tick()
        self.coordinator.close()
        with self.assertRaisesRegex(ValueError, "authentication mode is immutable"):
            DirectorCoordinator(self.coordinator_path, self.mailbox)
        with DirectorCoordinator(self.coordinator_path, self.mailbox,
                                 auth_channel=self.auth) as reopened:
            reopened.tick(auth_now_utc=150)

    def test_pre_policy_database_with_unauthenticated_history_cannot_be_upgraded(self):
        legacy_path = self.coordinator_path.with_name("legacy.sqlite3")
        with DirectorCoordinator(legacy_path, self.mailbox) as legacy:
            legacy.configure(GOAL, CAPABILITIES, max_missions=3)
            legacy.tick()
            self.assertEqual(len(legacy.inspect()["assignments"]), 1)
            # Mimic a pre-v0.4 database predating auth-policy migration.
            with legacy._transaction() as conn:
                conn.execute("DELETE FROM director_strategy_auth_policy")
        with self.assertRaisesRegex(ValueError, "legacy unauthenticated history"):
            DirectorCoordinator(legacy_path, self.mailbox, auth_channel=self.auth)

    def test_revoked_sender_cannot_enter_coordinator(self):
        assignment = self.assignment()
        raw = self.auth.seal(sender_id="worker_alpha", recipient_id="director",
                             kind="MISSION_REPORT",
                             payload={"in_reply_to": assignment["message_id"], "body": {}},
                             issued_at=100, expires_at=200)
        self.auth.revoke_sender("worker_alpha")
        self.signed_reply(request_id=assignment["message_id"], raw=raw)
        result = self.coordinator.tick(auth_now_utc=150)
        self.assertEqual(result["inbound_reports"][0]["review_status"],
                         "REJECTED_UNSIGNED_OR_INVALID_ENVELOPE")


if __name__ == "__main__":
    unittest.main()
