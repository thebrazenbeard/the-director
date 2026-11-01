"""v0.6 readiness assessment is read-only and never qualifies a Lappy daemon."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox
from runtime.director.recovery_copy import stage_recovery_copy
from runtime.director.unattended_qualification import assess_unattended_readiness


GOAL = {"goal_id": "readiness.goal", "objective": "Review declared quality",
        "metric": "quality", "target_value": 8, "direction": "INCREASE"}
CAPS = [{"capability_id": "observe", "agent_id": "worker_a", "metric": "quality",
         "expected_delta": 2, "cost_units": 0}]
KEYS = {"worker_a": b"A" * 32, "director.worker_a": b"B" * 32}


class UnattendedReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mailbox = DirectorMailbox(self.root / "messages.sqlite3")
        self.addCleanup(self.mailbox.close)
        self.auth = AuthenticatedChannel(self.root / "auth.sqlite3", KEYS)
        self.addCleanup(self.auth.close)
        self.path = self.root / "coordinator.sqlite3"
        self.coordinator = DirectorCoordinator(self.path, self.mailbox,
                                               auth_channel=self.auth, sign_outbound=True)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPS)

    @staticmethod
    def codes(receipt):
        return {item["code"] for item in receipt["blockers"]}

    def test_signed_ready_looking_coordinator_is_still_host_unqualified(self):
        receipt = assess_unattended_readiness(self.coordinator, now_utc=150)
        self.assertEqual(receipt["schema"], "director.unattended-readiness.v1")
        self.assertEqual(receipt["status"], "UNQUALIFIED_HOST")
        self.assertFalse(receipt["qualified"])
        self.assertEqual(receipt["effect_authority"], "NOT_GRANTED")
        self.assertEqual(receipt["effect_status"], "NOT_EXECUTED")
        self.assertEqual(receipt["host_probe"], "NOT_PERFORMED")
        self.assertEqual(receipt["authentication_mode"], "ENFORCED_LOCAL_HMAC_BIDIRECTIONAL")
        self.assertEqual(self.codes(receipt), {
            "HOST_PRINCIPAL_ISOLATION_UNVERIFIED", "WORKER_ACK_RECOVERY_UNVERIFIED",
            "LAPPY_UNATTENDED_RUNTIME_UNVERIFIED",
        })

    def test_assessment_does_not_tick_send_ack_or_change_database_bytes(self):
        self.coordinator.tick(auth_now_utc=150)
        before = {file.name: file.read_bytes() for file in self.root.iterdir() if file.is_file()}
        with (patch.object(self.coordinator, "tick", side_effect=AssertionError("tick forbidden")),
              patch.object(self.mailbox, "send", side_effect=AssertionError("send forbidden")),
              patch.object(self.mailbox, "ack", side_effect=AssertionError("ack forbidden"))):
            assess_unattended_readiness(self.coordinator, now_utc=151)
        after = {file.name: file.read_bytes() for file in self.root.iterdir() if file.is_file()}
        self.assertEqual(after, before)

    def test_unauthenticated_coordinator_is_blocked(self):
        with DirectorCoordinator(self.root / "plain.sqlite3", self.mailbox) as plain:
            plain.configure(GOAL, CAPS)
            receipt = assess_unattended_readiness(plain, now_utc=150)
            self.assertEqual(receipt["status"], "BLOCKED")
            self.assertEqual(receipt["authentication_mode"], "UNAUTHENTICATED_LOCAL")
            self.assertIn("BIDIRECTIONAL_AUTH_NOT_ENFORCED", self.codes(receipt))

    def test_paused_and_operator_stopped_coordinator_are_blocked(self):
        self.coordinator.set_paused(True)
        paused = assess_unattended_readiness(self.coordinator, now_utc=150)
        self.assertEqual(paused["status"], "BLOCKED")
        self.assertIn("OPERATOR_PAUSED", self.codes(paused))
        self.coordinator.request_stop()
        stopped = assess_unattended_readiness(self.coordinator, now_utc=150)
        self.assertIn("OPERATOR_STOP_REQUESTED", self.codes(stopped))
        self.assertEqual(stopped["effect_status"], "NOT_EXECUTED")

    def test_quarantined_copy_cannot_be_qualified(self):
        self.coordinator.set_paused(True)
        destination = self.root / "quarantined.sqlite3"
        stage_recovery_copy(self.path, destination, operator_approval={
            "approved": True, "purpose": "PAUSED_QUARANTINE_DATA_ONLY",
            "source_goal_id": GOAL["goal_id"], "destination_path": str(destination.absolute()),
            "allow_historical_inflight": False,
        })
        with DirectorCoordinator(destination, self.mailbox,
                                 auth_channel=self.auth, sign_outbound=True) as recovered:
            result = assess_unattended_readiness(recovered, now_utc=150)
            self.assertEqual(result["coordinator_status"], "QUARANTINED")
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("RECOVERY_QUARANTINED", self.codes(result))
            self.assertFalse(result["qualified"])

    def test_bounds_declared_time_and_rejects_noninteger_boolean(self):
        for timestamp in (True, False, 1.5, -1, 4102444801, "150", None):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                assess_unattended_readiness(self.coordinator, now_utc=timestamp)
        valid = assess_unattended_readiness(self.coordinator, now_utc=4102444800)
        self.assertEqual(valid["declared_now_utc"], 4102444800)
        self.assertFalse(valid["qualified"])

    def test_unavailable_or_malformed_projection_fails_closed(self):
        class Closed:
            def inspect(self):
                raise RuntimeError("secret-path-cannot-appear-in-receipt")

        class Malformed:
            def inspect(self):
                return {"status": "RUNNING", "authentication_mode": "ENFORCED_LOCAL_HMAC_BIDIRECTIONAL",
                        "operator_control": {"paused": "false", "stop_requested": False}}

        closed = assess_unattended_readiness(Closed(), now_utc=150)
        self.assertEqual(closed["status"], "BLOCKED")
        self.assertIn("COORDINATOR_INSPECTION_UNAVAILABLE", self.codes(closed))
        self.assertNotIn("secret-path", repr(closed))
        malformed = assess_unattended_readiness(Malformed(), now_utc=150)
        self.assertIn("OPERATOR_CONTROL_UNAVAILABLE", self.codes(malformed))
        self.assertFalse(malformed["qualified"])


if __name__ == "__main__":
    unittest.main()
