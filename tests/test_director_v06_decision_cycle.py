"""v0.6 grounded read-only assessment across reasoner, graph and coordinator."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.director.coordinator import DirectorCoordinator
from runtime.director.decision_cycle import assess_decision_cycle
from runtime.director.messenger import DirectorMailbox
from runtime.director.reasoning import LocalReasoner
from runtime.director.recovery_copy import stage_recovery_copy
from runtime.director.world_state import WorldState


GOAL = {"goal_id": "cycle.goal", "objective": "Review local quality reports",
        "metric": "quality", "target_value": 8, "direction": "INCREASE"}
CAPS = [
    {"capability_id": "quality_probe", "agent_id": "worker.quality", "metric": "quality",
     "expected_delta": 1, "cost_units": 0},
    {"capability_id": "signal_probe", "agent_id": "worker.signal", "metric": "signal",
     "expected_delta": 0, "cost_units": 0},
]
RIVALS = [
    {"hypothesis_id": "blue", "predictions": {"signal": "blue"}},
    {"hypothesis_id": "red", "predictions": {"signal": "red"}},
]


def node(identifier: str, depends=()):
    return {"mission_id": identifier, "kind": "OBSERVE",
            "depends_on": list(depends), "resources": {"reader": 1},
            "priority": 30, "cost_units": 0}


class DecisionCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.mailbox = DirectorMailbox(root / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.path = root / "director.db"
        self.coordinator = DirectorCoordinator(self.path, self.mailbox)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPS)

    def graph(self, *, block_signal=False):
        self.coordinator.configure_graph(
            [node("quality_probe"), node("signal_probe", ["quality_probe"] if block_signal else ())],
            {"reader": 2})

    def test_registered_graph_ready_probe_is_advisory_and_sends_nothing(self):
        self.graph()
        baseline = self.coordinator.inspect()
        before = self.mailbox.poll("worker.signal")
        assessed = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        self.assertEqual(assessed["status"], "ADVISORY_PROBES_MAPPED")
        self.assertEqual(assessed["effect_status"], "NOT_EXECUTED")
        self.assertEqual(assessed["authorization"], "NOT_GRANTED")
        self.assertEqual(len(assessed["input_sha256"]), 64)
        self.assertEqual(assessed["goal_assessment"]["decision"], "NEEDS_EVIDENCE")
        self.assertEqual(assessed["probe_proposals"][0]["metric"], "signal")
        self.assertEqual(assessed["probe_proposals"][0]["registered_capability_ids"], ["signal_probe"])
        self.assertEqual(assessed["probe_proposals"][0]["graph_ready_capability_ids"], ["signal_probe"])
        self.assertEqual(assessed["probe_proposals"][0]["mapping_status"], "REGISTERED_DAG_READY")
        self.assertEqual(assessed["probe_proposals"][0]["dispatch_status"], "ADVISORY_ONLY_NOT_ISSUED")
        self.assertEqual(self.coordinator.inspect(), baseline)
        self.assertEqual(self.mailbox.poll("worker.signal"), before)
        self.assertEqual(self.mailbox.poll("worker.quality"), [])
        self.assertFalse(self.coordinator._connection().in_transaction)

    def test_unmapped_probe_requires_operator_configuration(self):
        self.graph()
        different = [{"hypothesis_id": "a", "predictions": {"missing": True}},
                     {"hypothesis_id": "b", "predictions": {"missing": False}}]
        assessed = self.coordinator.assess_decision_cycle(hypotheses=different)
        self.assertEqual(assessed["status"], "OPERATOR_CONFIGURATION_REQUIRED")
        self.assertEqual(assessed["probe_proposals"][0]["mapping_status"],
                         "OPERATOR_CONFIGURATION_REQUIRED")
        self.assertEqual(assessed["probe_proposals"][0]["registered_capability_ids"], [])
        self.assertEqual(self.mailbox.poll("worker.signal"), [])

    def test_graph_missing_or_dependency_blocked_does_not_admit_probe(self):
        without_graph = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        self.assertEqual(without_graph["graph_status"], "UNCONFIGURED")
        self.assertEqual(without_graph["probe_proposals"][0]["mapping_status"],
                         "OPERATOR_CONFIGURATION_REQUIRED")
        self.graph(block_signal=True)
        blocked = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        self.assertEqual(blocked["probe_proposals"][0]["mapping_status"], "DAG_BLOCKED")
        self.assertEqual(blocked["probe_proposals"][0]["graph_ready_capability_ids"], [])
        self.assertEqual(blocked["status"], "OPERATOR_CONFIGURATION_REQUIRED")

    def test_reorder_does_not_change_assessment_and_duplicate_profile_cannot_game_ranking(self):
        self.graph()
        first = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        second = self.coordinator.assess_decision_cycle(hypotheses=list(reversed(RIVALS)))
        self.assertEqual(first, second)

        hypothesis_profiles = [
            {"hypothesis_id": "a", "predictions": {"m": 0, "z": 0}},
            {"hypothesis_id": "b", "predictions": {"m": 0, "z": 1}},
            {"hypothesis_id": "c", "predictions": {"m": 1, "z": 0}},
        ]
        initial = self.coordinator.assess_decision_cycle(hypotheses=hypothesis_profiles)
        duplicate = {"hypothesis_id": "d", "predictions": {"m": 0, "z": 1}}
        copied = self.coordinator.assess_decision_cycle(hypotheses=hypothesis_profiles + [duplicate])
        self.assertEqual([(p["metric"], p["distinguished_pair_count"])
                          for p in initial["probe_proposals"]],
                         [(p["metric"], p["distinguished_pair_count"])
                          for p in copied["probe_proposals"]])
        self.assertNotEqual(initial["input_sha256"], copied["input_sha256"])
        self.assertEqual(copied["hypothesis_assessment"]["distinct_prediction_profiles"], 3)
        self.assertIn(["b", "d"], copied["hypothesis_assessment"]["observational_equivalence_groups"])
        self.assertEqual(copied["hypothesis_assessment"]["viable_hypothesis_ids"], ["a", "b", "c", "d"])

    def test_observed_only_prediction_difference_cannot_spoof_probe_ranking(self):
        self.graph()
        self.coordinator.observe({"event_id": "obs.gate", "claim_id": "gate",
                                  "value": 1, "evidence_class": "OBSERVATION",
                                  "source_ref": "operator.declared", "lineage_id": "local.fixture"})
        rivals = [
            {"hypothesis_id": "a", "predictions": {"m": 0, "z": 0}},
            {"hypothesis_id": "b", "predictions": {"m": 0, "z": 1}},
            {"hypothesis_id": "c", "predictions": {"m": 1, "z": 0}},
        ]
        before = self.coordinator.assess_decision_cycle(hypotheses=rivals)
        # Distinct entire profiles but identical *still-testable* predictions:
        # the extra gate=1 is already OBSERVED and cannot provide a new probe.
        spoof = {"hypothesis_id": "d", "predictions": {"m": 0, "z": 1, "gate": 1}}
        after = self.coordinator.assess_decision_cycle(hypotheses=rivals + [spoof])
        self.assertEqual([(p["metric"], p["distinguished_pair_count"])
                          for p in before["probe_proposals"]], [("m", 2), ("z", 2)])
        self.assertEqual([(p["metric"], p["distinguished_pair_count"])
                          for p in before["probe_proposals"]],
                         [(p["metric"], p["distinguished_pair_count"])
                          for p in after["probe_proposals"]])
        self.assertEqual(after["hypothesis_assessment"]["distinct_prediction_profiles"], 3)
        self.assertIn(["b", "d"], after["hypothesis_assessment"]["observational_equivalence_groups"])
        self.assertNotEqual(before["input_sha256"], after["input_sha256"])

    def test_only_one_effective_rival_profile_has_no_discriminating_probe(self):
        facts = {"revision": 1, "facts": {"known": {"value": True, "status": "OBSERVED",
                                                   "evidence_ids": ["known.event"]}}}
        result = LocalReasoner().suggest_discriminating_observations(facts, [
            {"hypothesis_id": "a", "predictions": {"unknown": 2}},
            {"hypothesis_id": "b", "predictions": {"unknown": 2, "known": True}},
        ])
        self.assertEqual(result["viable_hypothesis_ids"], ["a", "b"])
        self.assertEqual(result["observational_equivalence_groups"], [["a", "b"]])
        self.assertEqual(result["distinct_prediction_profiles"], 1)
        self.assertEqual(result["status"], "NO_DISCRIMINATING_OBSERVATION")
        self.assertEqual(result["observations"], [])

    def test_maximum_worldstate_history_no_longer_rejected_by_digest_bound(self):
        world = WorldState()
        # Every event meets WorldState's own limits; non-BMP content exercises
        # JSON ASCII escaping, which can multiply the serialized byte count.
        long_value = "🚀" * 512
        for index in range(1024):
            world.ingest({"event_id": f"event.{index:04d}", "claim_id": "archive",
                          "value": long_value, "evidence_class": "OBSERVATION",
                          "source_ref": "fixture.source", "lineage_id": "fixture.lineage"})
        snapshot = world.snapshot()
        self.assertEqual(snapshot["revision"], 1024)
        self.assertGreater(len(json.dumps(snapshot, ensure_ascii=True).encode("ascii")),
                           4 * 1024 * 1024)
        assessed = assess_decision_cycle(
            GOAL, CAPS, snapshot, [], graph_ready=None, graph_snapshot=None,
            operator_control={"paused": False, "stop_requested": False,
                              "confirmed_success_evidence_ids": []}, quarantined=False)
        self.assertEqual(assessed["world_revision"], 1024)
        self.assertEqual(len(assessed["input_sha256"]), 64)
        self.assertEqual(assessed["status"], "NO_SUPPORTED_MEASUREMENT")
        self.assertEqual(assessed["effect_status"], "NOT_EXECUTED")

    def test_new_declared_observation_changes_digest_and_revises_probe(self):
        self.graph()
        before = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        self.coordinator.observe({"event_id": "obs.signal", "claim_id": "signal",
                                  "value": "blue", "evidence_class": "OBSERVATION",
                                  "source_ref": "operator.declared", "lineage_id": "local.fixture"})
        after = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        self.assertEqual(after["world_revision"], before["world_revision"] + 1)
        self.assertNotEqual(before["input_sha256"], after["input_sha256"])
        self.assertEqual(after["status"], "NO_SUPPORTED_MEASUREMENT")
        self.assertEqual(after["probe_proposals"], [])
        self.assertEqual(after["hypothesis_assessment"]["excluded_hypothesis_ids"], ["red"])
        self.assertEqual(self.mailbox.poll("worker.signal"), [])

    def test_unrelated_metric_capability_cannot_be_recommended_for_goal(self):
        unrelated = self.path.with_name("unrelated.db")
        roster = [dict(cap) for cap in CAPS]
        roster[1]["expected_delta"] = 100  # signal, never quality
        with DirectorCoordinator(unrelated, self.mailbox) as director:
            director.configure(GOAL, roster)
            director.observe({"event_id": "baseline.quality", "claim_id": "quality",
                              "value": 1, "evidence_class": "OBSERVATION",
                              "source_ref": "operator.declared", "lineage_id": "local.fixture"})
            assessed = director.assess_decision_cycle(hypotheses=RIVALS)
            self.assertEqual(assessed["goal_assessment"]["recommended_option_id"],
                             "quality_probe")
            self.assertEqual([o["option_id"] for o in assessed["goal_assessment"]["alternatives"]],
                             ["quality_probe"])
            self.assertEqual(assessed["probe_proposals"][0]["registered_capability_ids"],
                             ["signal_probe"])

    def test_quarantined_recovery_and_operator_pause_remain_inert(self):
        self.graph()
        self.coordinator.set_paused(True)
        paused = self.coordinator.assess_decision_cycle(hypotheses=RIVALS)
        self.assertEqual(paused["status"], "OPERATOR_CONTROLLED_ANALYSIS_ONLY")
        destination = self.path.with_name("quarantine.db")
        approval = {"approved": True, "purpose": "PAUSED_QUARANTINE_DATA_ONLY",
                    "source_goal_id": GOAL["goal_id"], "destination_path": str(destination.absolute()),
                    "allow_historical_inflight": False}
        stage_recovery_copy(self.path, destination, operator_approval=approval)
        with DirectorCoordinator(destination, self.mailbox) as recovery:
            baseline = recovery.inspect()
            result = recovery.assess_decision_cycle(hypotheses=RIVALS)
            self.assertTrue(result["recovery_quarantined"])
            self.assertEqual(result["status"], "QUARANTINED_ANALYSIS_ONLY")
            self.assertEqual(result["effect_status"], "NOT_EXECUTED")
            self.assertEqual(recovery.inspect(), baseline)
        self.assertEqual(self.mailbox.poll("worker.signal"), [])

    def test_malformed_hypotheses_fail_without_side_effects(self):
        self.graph()
        baseline = self.coordinator.inspect()
        with self.assertRaisesRegex(ValueError, "duplicate hypothesis_id"):
            self.coordinator.assess_decision_cycle(hypotheses=RIVALS + RIVALS)
        self.assertEqual(self.coordinator.inspect(), baseline)
        self.assertFalse(self.coordinator._connection().in_transaction)
        self.assertEqual(self.mailbox.poll("worker.signal"), [])


if __name__ == "__main__":
    unittest.main()
