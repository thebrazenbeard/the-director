"""Contract and adversarial tests for the offline declared-evidence reducer."""

import json
import unittest
from unittest.mock import patch

from runtime.director import world_state
from runtime.director.world_state import WorldState


def event(
    event_id="e1",
    *,
    claim_id="target.progress",
    value=1,
    evidence_class="OBSERVATION",
    source_ref="worker.alpha",
    lineage_id="run.1",
):
    return {
        "event_id": event_id,
        "claim_id": claim_id,
        "value": value,
        "evidence_class": evidence_class,
        "source_ref": source_ref,
        "lineage_id": lineage_id,
    }


class WorldStateTests(unittest.TestCase):
    def test_empty_snapshot_is_deterministic(self):
        state = WorldState()
        self.assertEqual(state.revision, 0)
        self.assertEqual(state.snapshot(), {"revision": 0, "facts": {}, "events": []})

    def test_observation_records_provenance_but_is_not_authenticated(self):
        state = WorldState()
        snapshot = state.ingest(event())
        self.assertEqual(snapshot["revision"], 1)
        fact = snapshot["facts"]["target.progress"]
        self.assertEqual(fact["status"], "OBSERVED")
        self.assertEqual(fact["value"], 1)
        self.assertEqual(fact["evidence_ids"], ["e1"])
        self.assertEqual(fact["source_refs"], ["worker.alpha"])
        self.assertEqual(fact["lineage_ids"], ["run.1"])
        self.assertEqual(fact["evidence_classes"], ["OBSERVATION"])

    def test_hypothesis_is_not_observation(self):
        state = WorldState()
        fact = state.ingest(event(evidence_class="HYPOTHESIS"))["facts"]["target.progress"]
        self.assertEqual(fact["status"], "HYPOTHETICAL")

    def test_inference_is_not_observation(self):
        state = WorldState()
        fact = state.ingest(event(evidence_class="INFERENCE"))["facts"]["target.progress"]
        self.assertEqual(fact["status"], "INFERRED")

    def test_hypothetical_repetition_does_not_promote(self):
        state = WorldState()
        state.ingest(event(evidence_class="HYPOTHESIS"))
        fact = state.ingest(
            event("e2", evidence_class="HYPOTHESIS", source_ref="worker.beta", lineage_id="r2")
        )["facts"]["target.progress"]
        self.assertEqual(fact["status"], "HYPOTHETICAL")
        self.assertEqual(fact["value"], 1)

    def test_conflicting_observations_are_contested(self):
        state = WorldState()
        state.ingest(event())
        fact = state.ingest(
            event("e2", value=2, source_ref="worker.beta", lineage_id="run.2")
        )["facts"]["target.progress"]
        self.assertEqual(fact["status"], "CONTESTED")
        self.assertIsNone(fact["value"])
        self.assertEqual(fact["evidence_ids"], ["e1", "e2"])

    def test_even_same_claimed_source_conflicting_observation_is_contested(self):
        state = WorldState()
        state.ingest(event())
        fact = state.ingest(event("e2", value=2))["facts"]["target.progress"]
        self.assertEqual(fact["status"], "CONTESTED")

    def test_hypothesis_cannot_overwrite_observation(self):
        state = WorldState()
        state.ingest(event(value=10))
        fact = state.ingest(event("e2", value=0, evidence_class="HYPOTHESIS"))[
            "facts"
        ]["target.progress"]
        self.assertEqual(fact["status"], "OBSERVED")
        self.assertEqual(fact["value"], 10)
        self.assertEqual(fact["evidence_classes"], ["HYPOTHESIS", "OBSERVATION"])

    def test_inference_supersedes_only_hypothesis(self):
        state = WorldState()
        state.ingest(event(evidence_class="HYPOTHESIS", value=8))
        fact = state.ingest(event("e2", evidence_class="INFERENCE", value=9))[
            "facts"
        ]["target.progress"]
        self.assertEqual((fact["status"], fact["value"]), ("INFERRED", 9))

    def test_conflicting_hypotheses_are_not_silently_resolved(self):
        state = WorldState()
        state.ingest(event(evidence_class="HYPOTHESIS"))
        fact = state.ingest(event("e2", evidence_class="HYPOTHESIS", value=5))[
            "facts"
        ]["target.progress"]
        self.assertEqual((fact["status"], fact["value"]), ("CONTESTED", None))
        self.assertEqual(fact["evidence_classes"], ["HYPOTHESIS"])

    def test_duplicate_id_exact_replay_is_idempotent(self):
        state = WorldState()
        first = state.ingest(event())
        self.assertEqual(state.ingest(event()), first)
        self.assertEqual(state.revision, 1)

    def test_duplicate_id_changed_payload_rejected(self):
        state = WorldState()
        state.ingest(event())
        with self.assertRaisesRegex(ValueError, "conflicting content"):
            state.ingest(event(value=2))
        self.assertEqual(state.revision, 1)

    def test_duplicate_id_boolean_integer_ambiguity_rejected(self):
        state = WorldState()
        state.ingest(event(value=True))
        with self.assertRaisesRegex(ValueError, "conflicting content"):
            state.ingest(event(value=1))

    def test_duplicate_id_integer_float_ambiguity_rejected(self):
        state = WorldState()
        state.ingest(event(value=1))
        with self.assertRaisesRegex(ValueError, "conflicting content"):
            state.ingest(event(value=1.0))

    def test_observation_boolean_vs_integer_is_contested(self):
        state = WorldState()
        state.ingest(event(value=True))
        fact = state.ingest(event("e2", value=1))["facts"]["target.progress"]
        self.assertEqual(fact["status"], "CONTESTED")

    def test_unsupported_keys_invalid_class_and_identifiers_rejected(self):
        for bad in (
            dict(event(), extra="not_allowed"),
            dict(event(), event_id=""),
            dict(event(), claim_id="!invalid!"),
            dict(event(), evidence_class="SIMULATED_AS_TRUE"),
            dict(event(), source_ref=""),
            dict(event(), lineage_id="-bad"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                WorldState().ingest(bad)

    def test_reject_non_json_oversized_nonfinite_and_null_values(self):
        for value in (None, [], {}, object(), "x" * 513, 10**13, -(10**13), float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                WorldState().ingest(event(value=value))

    def test_prevents_unbounded_events_and_claims(self):
        with patch.object(world_state, "_MAX_EVENTS", 2):
            state = WorldState()
            state.ingest(event("e1"))
            state.ingest(event("e2"))
            with self.assertRaisesRegex(ValueError, "event limit"):
                state.ingest(event("e3"))
        with patch.object(world_state, "_MAX_CLAIMS", 1):
            state = WorldState()
            state.ingest(event("e1", claim_id="claim.a"))
            state.ingest(event("e2", claim_id="claim.a"))
            with self.assertRaisesRegex(ValueError, "claim limit"):
                state.ingest(event("e3", claim_id="claim.b"))

    def test_snapshots_detached_from_internal_history_and_input(self):
        state = WorldState()
        original = event()
        snapshot = state.ingest(original)
        original["value"] = 999
        snapshot["events"][0]["value"] = 888
        snapshot["facts"]["target.progress"]["evidence_ids"].append("forged")
        self.assertEqual(state.snapshot()["events"][0]["value"], 1)
        self.assertEqual(state.snapshot()["facts"]["target.progress"]["evidence_ids"], ["e1"])

    def test_snapshot_roundtrip_restart_preserves_revision_and_future_updates(self):
        state = WorldState()
        state.ingest(event())
        state.ingest(event("e2", claim_id="resource.ready", value=True))
        canonical = json.loads(json.dumps(state.snapshot()))
        restored = WorldState.from_snapshot(canonical)
        self.assertEqual(restored.snapshot(), state.snapshot())
        self.assertEqual(restored.revision, 2)
        self.assertEqual(restored.ingest(event("e3"))["revision"], 3)

    def test_tampered_projection_rejected(self):
        state = WorldState()
        snapshot = state.ingest(event(value=1))
        snapshot["facts"]["target.progress"]["status"] = "OBSERVED_BY_ORACLE"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            WorldState.from_snapshot(snapshot)

    def test_tampered_snapshot_boolean_integer_ambiguity_rejected(self):
        state = WorldState()
        snapshot = state.ingest(event(value=1))
        snapshot["facts"]["target.progress"]["value"] = True
        with self.assertRaisesRegex(ValueError, "conflicts"):
            WorldState.from_snapshot(snapshot)

    def test_invalid_snapshot_shape_rejected(self):
        with self.assertRaises(ValueError):
            WorldState.from_snapshot({"events": []})
        with self.assertRaises(ValueError):
            WorldState.from_snapshot({"revision": 0, "events": {}, "facts": {}})


if __name__ == "__main__":
    unittest.main()
