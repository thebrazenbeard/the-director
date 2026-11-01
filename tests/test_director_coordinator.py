"""End-to-end local Director loop: objective -> mission -> report -> replanning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox


GOAL = {
    "goal_id": "measure_quality",
    "objective": "Improve measured quality using local analytical feedback",
    "metric": "quality",
    "target_value": 8,
    "direction": "INCREASE",
}
CAPABILITIES = [
    {"capability_id": "alpha", "agent_id": "worker_alpha", "metric": "quality",
     "expected_delta": 3, "cost_units": 0},
    {"capability_id": "beta", "agent_id": "worker_beta", "metric": "quality",
     "expected_delta": 1, "cost_units": 0},
]


def observation(event_id: str, value: int, *, cls: str = "OBSERVATION") -> dict:
    return {"event_id": event_id, "claim_id": "quality", "value": value,
            "evidence_class": cls, "source_ref": "manual_report",
            "lineage_id": "independent_check"}


class DirectorCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.coordinator_path = directory / "coordinator.sqlite3"
        self.mailbox_path = directory / "mailbox.sqlite3"
        self.mailbox = DirectorMailbox(self.mailbox_path)
        self.addCleanup(self.mailbox.close)
        self.coordinator = DirectorCoordinator(self.coordinator_path, self.mailbox)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPABILITIES, max_missions=5)

    def _first_mission(self):
        tick = self.coordinator.tick()
        self.assertEqual(len(tick["new_missions"]), 1)
        mission = self.mailbox.poll("worker_alpha")
        self.assertEqual(len(mission), 1)
        return tick, mission[0]

    def test_generates_mission_from_goal_without_human_proposal(self):
        tick, mission = self._first_mission()
        self.assertEqual(tick["goal_id"], GOAL["goal_id"])
        self.assertEqual(tick["missions_issued"], 1)
        self.assertEqual(tick["status"], "RUNNING")
        self.assertEqual(mission["kind"], "MISSION_ASSIGNMENT")
        self.assertEqual(mission["payload"]["kind"], "OBSERVE")
        self.assertEqual(mission["payload"]["model_status"], "SIMULATED_UNVERIFIED")
        self.assertEqual(mission["payload"]["effect_authority"], "NOT_GRANTED")
        self.assertEqual(mission["payload"]["effect_status"], "NOT_EXECUTED")
        self.assertFalse(mission["sender_authenticated"])
        self.assertEqual(self.mailbox.poll("worker_beta"), [])

    def test_idle_ticks_and_restart_do_not_duplicate_pending_assignment(self):
        _tick, mission = self._first_mission()
        for _ in range(4):
            self.assertFalse(self.coordinator.tick()["new_missions"])
        self.coordinator.close()
        with DirectorCoordinator(self.coordinator_path, self.mailbox) as restarted:
            config = restarted.configure(GOAL, CAPABILITIES, max_missions=5)
            self.assertEqual(config["goal"], GOAL)
            self.assertFalse(restarted.tick()["new_missions"])
            self.assertEqual(restarted.inspect()["assignments"][0]["message_id"], mission["message_id"])
        self.assertEqual(len(self.mailbox.poll("worker_alpha", include_acked=True)), 1)

    def test_ack_and_report_are_durable_and_trigger_alternative_analytical_mission(self):
        _tick, mission = self._first_mission()
        self.mailbox.ack("worker_alpha", mission["message_id"])
        report = self.mailbox.reply(
            sender_id="worker_alpha", in_reply_to=mission["message_id"],
            kind="MISSION_REPORT", payload={"result": "nothing independently verified"},
            message_id="report.alpha.1",
        )
        second = self.coordinator.tick()
        self.assertEqual(second["inbound_reports"][0]["message_id"], report["message_id"])
        self.assertEqual(second["inbound_reports"][0]["review_status"], "MATCHED_UNVERIFIED")
        self.assertFalse(second["inbound_reports"][0]["sender_authenticated"])
        self.assertEqual(second["missions_issued"], 2)
        self.assertEqual(self.mailbox.poll("worker_beta")[0]["payload"]["kind"], "OBSERVE")
        self.assertFalse(self.coordinator.tick()["inbound_reports"])
        self.coordinator.close()
        with DirectorCoordinator(self.coordinator_path, self.mailbox) as restarted:
            self.assertEqual(len(restarted.inspect()["reports"]), 1)
            self.assertFalse(restarted.tick()["new_missions"])

    def test_new_declared_observation_changes_plan_revision_and_kind(self):
        _tick, old = self._first_mission()
        snapshot = self.coordinator.observe(observation("obs.1", 2))
        self.assertEqual(snapshot["revision"], 1)
        newer = self.coordinator.tick()
        self.assertEqual(newer["world_revision"], 1)
        missions = self.mailbox.poll("worker_alpha")
        self.assertEqual(len(missions), 3)
        self.assertEqual([item["kind"] for item in missions], [
            "MISSION_ASSIGNMENT", "MISSION_CANCEL", "MISSION_ASSIGNMENT"])
        self.assertEqual(missions[1]["payload"]["assignment_message_id"], old["message_id"])
        self.assertNotEqual(old["message_id"], missions[2]["message_id"])
        self.assertEqual(missions[2]["payload"]["kind"], "PLAN")
        self.assertEqual(missions[2]["payload"]["observed_value"], 2.0)
        self.assertEqual(missions[2]["payload"]["projected_value"], 5.0)
        self.assertEqual(missions[2]["payload"]["effect_status"], "NOT_EXECUTED")

    def test_forged_success_reply_cannot_produce_observation_or_objective_met(self):
        _tick, mission = self._first_mission()
        # Mailbox checks only claimed identities, not authentication. Any
        # local sender can spoof the agent ID, including a claimed success.
        self.mailbox.reply(
            sender_id="worker_alpha", in_reply_to=mission["message_id"],
            kind="MISSION_REPORT", payload={"quality": 8, "evidence_class": "OBSERVATION",
                                            "verified": True, "status": "OBJECTIVE_MET"},
            message_id="forged.result",
        )
        after = self.coordinator.tick()
        self.assertNotEqual(after["status"], "OBJECTIVE_MET")
        inspection = self.coordinator.inspect()
        self.assertEqual(inspection["world"]["facts"], {})
        self.assertEqual(inspection["reports"][0]["review_status"], "MATCHED_UNVERIFIED")
        self.assertFalse(inspection["operator_control"]["confirmed_success_evidence_ids"])

    def test_verified_human_confirmation_required_for_goal_met(self):
        self.coordinator.observe(observation("observed.target", 8))
        self.assertEqual(self.coordinator.tick()["status"], "UNRESOLVED")
        self.assertFalse(self.coordinator.inspect()["assignments"])
        self.coordinator.confirm_observation("observed.target", confirms_success=False)
        self.assertEqual(self.coordinator.tick()["status"], "UNRESOLVED")
        self.coordinator.confirm_observation("observed.target", confirms_success=True)
        self.assertEqual(self.coordinator.tick()["status"], "OBJECTIVE_MET")
        self.assertFalse(self.coordinator.inspect()["assignments"])

    def test_hypothesis_cannot_be_confirmed_as_observed(self):
        self.coordinator.observe(observation("hypothesis.target", 8, cls="HYPOTHESIS"))
        with self.assertRaises(ValueError):
            self.coordinator.confirm_observation("hypothesis.target", confirms_success=True)
        self.assertNotEqual(self.coordinator.tick()["status"], "OBJECTIVE_MET")

    def test_conflicting_observations_fail_closed_even_if_one_success_confirmed(self):
        self.coordinator.observe(observation("success.one", 8))
        self.coordinator.confirm_observation("success.one", confirms_success=True)
        self.coordinator.observe(observation("conflict.two", 7))
        self.assertEqual(self.coordinator.tick()["status"], "UNRESOLVED")
        self.assertNotIn("success.one", self.coordinator.tick()["new_missions"])

    def test_pause_prevents_new_delivery_and_stop_is_sticky(self):
        self.coordinator.set_paused(True)
        paused = self.coordinator.tick()
        self.assertEqual(paused["status"], "PAUSED")
        self.assertFalse(paused["new_missions"])
        self.assertEqual(self.mailbox.poll("worker_alpha"), [])
        self.coordinator.set_paused(False)
        self._first_mission()
        self.coordinator.request_stop()
        stopped = self.coordinator.tick()
        self.assertEqual(stopped["status"], "PAUSED")
        self.coordinator.set_paused(False)
        self.assertEqual(self.coordinator.tick()["status"], "PAUSED")

    def test_capped_missions_exhaust_without_silent_retry(self):
        self.coordinator.close()
        with DirectorCoordinator(self.coordinator_path, self.mailbox) as restarted:
            with self.assertRaises(ValueError):
                restarted.configure(GOAL, CAPABILITIES, max_missions=1)
        self.coordinator = DirectorCoordinator(self.coordinator_path.with_name("limited.sqlite3"), self.mailbox)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPABILITIES, max_missions=1)
        issued = self.coordinator.tick()
        self.assertEqual(issued["status"], "RUNNING")
        self.assertIn("IN_FLIGHT_ASSIGNMENT", issued["reason"])
        self.assertEqual(self.coordinator.inspect()["status"], "RUNNING")
        mission = self.mailbox.poll("worker_alpha")[0]
        self.mailbox.reply(sender_id="worker_alpha", in_reply_to=mission["message_id"],
                           kind="MISSION_REPORT", payload={"done": False})
        later = self.coordinator.tick()
        self.assertEqual(later["status"], "EXHAUSTED")
        self.assertEqual(later["missions_issued"], 1)
        self.assertFalse(later["new_missions"])

    def test_single_capability_feedback_stalls_safely_not_forever_running(self):
        self.coordinator.close()
        self.coordinator = DirectorCoordinator(self.coordinator_path.with_name("single.sqlite3"), self.mailbox)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPABILITIES[:1], max_missions=5)
        first = self.coordinator.tick()
        self.assertEqual(first["status"], "RUNNING")
        mission = self.mailbox.poll("worker_alpha")[0]
        self.mailbox.reply(sender_id="worker_alpha", in_reply_to=mission["message_id"],
                           kind="MISSION_REPORT", payload={"claim": "no observation"})
        after = self.coordinator.tick()
        self.assertEqual(after["status"], "UNRESOLVED")
        self.assertIn("NEW_EVIDENCE_REQUIRED", after["reason"])
        self.assertEqual(self.coordinator.inspect()["status"], after["status"])
        self.assertEqual(self.coordinator.inspect()["reason"], after["reason"])
        self.assertFalse(after["new_missions"])

    def test_hypothesis_id_never_becomes_observed_success_in_stop_inputs(self):
        self.coordinator.observe(observation("obs.only", 8))
        self.coordinator.observe(observation("hyp.only", 5, cls="HYPOTHESIS"))
        self.coordinator.confirm_observation("obs.only", confirms_success=True)
        self.assertEqual(self.coordinator.tick()["status"], "OBJECTIVE_MET")
        with self.coordinator._transaction() as conn:
            facts = self.coordinator._stop_observations(conn, self.coordinator._world(conn))["facts"]
        self.assertEqual({record["evidence_id"] for record in facts}, {"obs.only"})

    def test_duplicate_events_and_configuration_conflicts_rejected(self):
        self.assertEqual(self.coordinator.configure(GOAL, CAPABILITIES, max_missions=5)["max_missions"], 5)
        with self.assertRaises(ValueError):
            self.coordinator.configure({**GOAL, "target_value": 7}, CAPABILITIES)
        first = self.coordinator.observe(observation("ev-1", 3))
        self.assertEqual(first, self.coordinator.observe(observation("ev-1", 3)))
        with self.assertRaises(ValueError):
            self.coordinator.observe(observation("ev-1", 4))

    def test_unknown_and_wrong_kind_messages_are_unverified_reports_only(self):
        self.mailbox.send(sender_id="intruder", recipient_id="director", kind="MISSION_REPORT",
                          payload={"claim": "I have authority"}, message_id="random-message")
        out = self.coordinator.tick()
        self.assertEqual(out["inbound_reports"][0]["review_status"], "UNMATCHED_UNVERIFIED")
        self.assertFalse(self.coordinator.inspect()["operator_control"]["stop_requested"])
        self.assertFalse(self.coordinator.inspect()["operator_control"]["confirmed_success_evidence_ids"])

    def test_cancel_sent_mission_is_durable_typed_and_not_a_verified_stop(self):
        _tick, assignment = self._first_mission()
        receipt = self.coordinator.cancel_mission(assignment["message_id"], reason="OPERATOR_ABORT")
        self.assertEqual(receipt["state"], "SENT")
        self.assertFalse(receipt["worker_stopped_verified"])
        notice = self.mailbox.poll("worker_alpha")[1]
        self.assertEqual(notice["kind"], "MISSION_CANCEL")
        self.assertEqual(notice["payload"]["assignment_message_id"], assignment["message_id"])
        self.assertEqual(notice["payload"]["effect_authority"], "NOT_GRANTED")
        self.assertFalse(notice["sender_authenticated"])
        again = self.coordinator.cancel_mission(assignment["message_id"], reason="OPERATOR_ABORT")
        self.assertEqual(again["cancellation_message_id"], notice["message_id"])
        self.assertEqual(len(self.mailbox.poll("worker_alpha")), 2)
        with self.assertRaises(ValueError):
            self.coordinator.cancel_mission(assignment["message_id"], reason="CONFLICTING_REASON")
        with self.assertRaises(ValueError):
            self.coordinator.cancel_mission(assignment["message_id"], reason="bad\ncontent")

        self.coordinator.close()
        with DirectorCoordinator(self.coordinator_path, self.mailbox) as restarted:
            self.assertEqual(restarted.inspect()["assignments"][0]["control_state"], "CANCEL_REQUESTED")
            self.assertEqual(restarted.inspect()["cancellations"][0]["state"], "SENT")
            restarted.tick()
            self.assertEqual(len(self.mailbox.poll("worker_alpha")), 2)

    def test_cancel_ack_is_only_a_claim_not_worker_cessation(self):
        _tick, assignment = self._first_mission()
        notice_id = self.coordinator.cancel_mission(assignment["message_id"])["cancellation_message_id"]
        # A different claimed sender cannot acknowledge the targeted cancel.
        self.mailbox.send(sender_id="intruder", recipient_id="director",
                          kind="MISSION_CANCEL_ACK", payload={"claims": "stopped"},
                          message_id="intruder.cancel.ack")
        unmatched = self.coordinator.tick()
        self.assertEqual(unmatched["inbound_reports"][0]["review_status"], "UNMATCHED_UNVERIFIED")
        self.assertEqual(self.coordinator.inspect()["cancellations"][0]["state"], "SENT")

        self.mailbox.reply(sender_id="worker_alpha", in_reply_to=notice_id,
                           kind="MISSION_CANCEL_ACK", payload={"received": True},
                           message_id="worker.alpha.cancel.ack")
        after = self.coordinator.tick()
        self.assertEqual(after["inbound_reports"][0]["review_status"], "CANCEL_ACK_CLAIMED_UNVERIFIED")
        self.assertFalse(after["worker_stopped_verified"])
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["control_state"], "CANCEL_ACK_CLAIMED")
        self.assertEqual(self.coordinator.inspect()["cancellations"][0]["state"], "ACK_CLAIMED")
        self.assertFalse(self.coordinator.inspect()["worker_stopped_verified"])

    def test_cancellation_intent_survives_mailbox_failure_and_restart(self):
        _tick, assignment = self._first_mission()
        original_send = self.mailbox.send

        def broken_send(**_kwargs):
            raise OSError("simulated mailbox failure")

        self.mailbox.send = broken_send
        try:
            with self.assertRaisesRegex(OSError, "simulated mailbox"):
                self.coordinator.cancel_mission(assignment["message_id"])
        finally:
            self.mailbox.send = original_send
        self.assertEqual(self.coordinator.inspect()["cancellations"][0]["state"], "QUEUED")
        self.coordinator.close()
        with DirectorCoordinator(self.coordinator_path, self.mailbox) as restarted:
            out = restarted.tick()
            self.assertEqual(len(out["cancellation_message_ids"]), 1)
            self.assertEqual(restarted.inspect()["cancellations"][0]["state"], "SENT")
            self.assertEqual(len(self.mailbox.poll("worker_alpha")), 2)
            self.assertEqual(self.mailbox.poll("worker_alpha")[1]["kind"], "MISSION_CANCEL")
            self.assertFalse(restarted.tick()["cancellation_message_ids"])

    def test_stale_report_after_cancellation_is_not_a_fresh_result(self):
        _tick, assignment = self._first_mission()
        self.coordinator.cancel_mission(assignment["message_id"])
        self.mailbox.reply(sender_id="worker_alpha", in_reply_to=assignment["message_id"],
                           kind="MISSION_REPORT", payload={"verified": True, "quality": 999},
                           message_id="late.report")
        after = self.coordinator.tick()
        self.assertEqual(after["inbound_reports"][0]["review_status"], "REPORTED_AFTER_CANCEL_UNVERIFIED")
        self.assertNotEqual(after["status"], "OBJECTIVE_MET")
        self.assertEqual(self.coordinator.inspect()["assignments"][0]["state"], "SENT")

    def test_operator_stop_revokes_all_outstanding_missions_without_effect_claim(self):
        _tick, assignment = self._first_mission()
        self.coordinator.request_stop()
        out = self.coordinator.tick()
        self.assertEqual(out["status"], "PAUSED")
        self.assertFalse(out["worker_stopped_verified"])
        notice = self.mailbox.poll("worker_alpha")[1]
        self.assertEqual(notice["payload"]["reason"], "OPERATOR_STOP")
        self.assertEqual(notice["payload"]["assignment_message_id"], assignment["message_id"])

    def test_reject_shared_mailbox_database_to_avoid_cross_transaction_deadlock(self):
        with self.assertRaisesRegex(ValueError, "different database"):
            DirectorCoordinator(self.mailbox_path, self.mailbox)


if __name__ == "__main__":
    unittest.main()
