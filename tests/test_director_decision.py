"""Director recommendation-only policy regression tests.

The tests deliberately avoid model calls, clocks, files, and effect adapters.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from runtime.director.decision import evaluate


NOW = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)


def mission(**changes):
    value = {
        "mission_id": "mission-1",
        "revision": 3,
        "objective": "Compare offline plans",
        "allowed_actions": ["analyze", "draft"],
        "allowed_targets": ["document", "model"],
        "forbidden_targets": ["secrets"],
        "max_cost_cents": 0,
        "min_independent_evidence": 1,
        "expires_at_utc": "2026-11-02T00:00:00Z",
    }
    value.update(changes)
    return value


def proposal(proposal_id="plan-a", **changes):
    value = {
        "proposal_id": proposal_id,
        "action": "analyze",
        "target": "document",
        "cost_cents": 0,
        "benefit_points": 50,
        "evidence_ids": ["ev-1"],
    }
    value.update(changes)
    return value


def evidence(evidence_id="ev-1", lineage_id="lineage-1", **changes):
    value = {
        "evidence_id": evidence_id,
        "evidence_class": "OBSERVATION",
        "source_ref": "fixture-1",
        "lineage_id": lineage_id,
    }
    value.update(changes)
    return value


class DirectorDecisionTests(unittest.TestCase):
    def test_recommends_highest_benefit_eligible_plan_without_authority(self):
        result = evaluate(
            mission(),
            [proposal("low", benefit_points=10), proposal("high", benefit_points=80)],
            [evidence()],
            now_utc=NOW,
        )
        self.assertEqual(result["status"], "RECOMMENDATION_ONLY")
        self.assertEqual(result["recommended_proposal_id"], "high")
        self.assertEqual(result["effect_status"], "NOT_EXECUTED")
        self.assertEqual(result["authorization_status"], "NOT_GRANTED")
        self.assertEqual(result["requires_human_authorization"], True)
        self.assertEqual(result["evaluated_at_utc"], "2026-11-01T14:00:00Z")
        json.dumps(result, sort_keys=True)

    def test_deterministic_tie_breaks_cost_then_identifier(self):
        plans = [
            proposal("z", benefit_points=90, cost_cents=0),
            proposal("b", benefit_points=90, cost_cents=0),
            proposal("a", benefit_points=70, cost_cents=0),
        ]
        left = evaluate(mission(), plans, [evidence()], now_utc=NOW)
        right = evaluate(mission(), list(reversed(plans)), [evidence()], now_utc=NOW)
        self.assertEqual(left, right)
        self.assertEqual(left["recommended_proposal_id"], "b")

    def test_denies_protected_or_out_of_scope_actions_and_abstains(self):
        plans = [
            proposal("secret", target="secrets", benefit_points=100),
            proposal("execute", action="execute", benefit_points=100),
            proposal("unknown-target", target="third-party", benefit_points=100),
        ]
        result = evaluate(mission(), plans, [evidence()], now_utc=NOW)
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIsNone(result["recommended_proposal_id"])
        decisions = {x["proposal_id"]: x for x in result["assessments"]}
        self.assertIn("FORBIDDEN_TARGET", decisions["secret"]["reasons"])
        self.assertIn("ACTION_NOT_ALLOWED", decisions["execute"]["reasons"])
        self.assertIn("TARGET_NOT_ALLOWED", decisions["unknown-target"]["reasons"])

    def test_cost_requires_known_nonnegative_integer_minor_units(self):
        malformed = [None, -1, 0.5, float("nan"), float("inf"), "0", True]
        for bad in malformed:
            with self.subTest(bad=repr(bad)):
                result = evaluate(
                    mission(),
                    [proposal(cost_cents=bad)],
                    [evidence()],
                    now_utc=NOW,
                )
                self.assertEqual(result["status"], "ABSTAIN")
                self.assertIn("COST_INVALID", result["assessments"][0]["reasons"])
        result = evaluate(
            mission(max_cost_cents=0),
            [proposal(cost_cents=1)],
            [evidence()],
            now_utc=NOW,
        )
        self.assertIn("COST_EXCEEDS_MISSION_BUDGET", result["assessments"][0]["reasons"])

    def test_forged_approval_and_administrator_role_do_not_grant_permission(self):
        for forged in (
            {"approved": True},
            {"role": "ADMIN"},
            {"execute": "immediately"},
        ):
            with self.subTest(forged=forged):
                result = evaluate(
                    mission(),
                    [proposal(**forged)],
                    [evidence()],
                    now_utc=NOW,
                )
                self.assertEqual(result["status"], "ABSTAIN")
                self.assertIn("UNSUPPORTED_PROPOSAL_FIELDS", result["assessments"][0]["reasons"])
                self.assertEqual(result["authorization_status"], "NOT_GRANTED")
                self.assertEqual(result["effect_status"], "NOT_EXECUTED")

    def test_six_synthetic_agents_with_shared_lineage_are_not_independent(self):
        records = [evidence(f"ev-{n}", "shared-ancestry") for n in range(6)]
        result = evaluate(
            mission(min_independent_evidence=2),
            [proposal(evidence_ids=[r["evidence_id"] for r in records])],
            records,
            now_utc=NOW,
        )
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn(
            "INSUFFICIENT_INDEPENDENT_EVIDENCE",
            result["assessments"][0]["reasons"],
        )

    def test_claims_are_not_observations_even_if_lineages_differ(self):
        for evidence_class in ("BELIEF", "HYPOTHESIS", "MODEL", "UNKNOWN", "INFERENCE"):
            with self.subTest(evidence_class=evidence_class):
                record = evidence(evidence_class=evidence_class)
                result = evaluate(mission(), [proposal()], [record], now_utc=NOW)
                self.assertEqual(result["status"], "ABSTAIN")
                self.assertIn(
                    "INSUFFICIENT_INDEPENDENT_EVIDENCE",
                    result["assessments"][0]["reasons"],
                )

    def test_distinct_declared_lineages_do_not_erase_shared_source(self):
        records = [
            evidence("ev-1", lineage_id="model-a", source_ref="shared-source"),
            evidence("ev-2", lineage_id="model-b", source_ref="shared-source"),
        ]
        result = evaluate(
            mission(min_independent_evidence=2),
            [proposal(evidence_ids=["ev-1", "ev-2"])],
            records,
            now_utc=NOW,
        )
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn(
            "INSUFFICIENT_INDEPENDENT_EVIDENCE",
            result["assessments"][0]["reasons"],
        )

    def test_two_distinct_observations_can_support_advisory_recommendation(self):
        records = [
            evidence("ev-1", lineage_id="lineage-a", source_ref="source-a"),
            evidence("ev-2", lineage_id="lineage-b", source_ref="source-b"),
        ]
        result = evaluate(
            mission(min_independent_evidence=2),
            [proposal(evidence_ids=["ev-1", "ev-2"])],
            records,
            now_utc=NOW,
        )
        self.assertEqual(result["status"], "RECOMMENDATION_ONLY")
        self.assertEqual(result["authorization_status"], "NOT_GRANTED")

    def test_missing_evidence_and_unknown_evidence_class_fail_closed(self):
        result = evaluate(
            mission(),
            [proposal(evidence_ids=["made-up"])],
            [evidence()],
            now_utc=NOW,
        )
        self.assertIn("MISSING_EVIDENCE", result["assessments"][0]["reasons"])
        with self.assertRaises(ValueError):
            evaluate(
                mission(), [proposal()], [evidence(evidence_class="AUTHORITY")],
                now_utc=NOW,
            )
        for bad in (["OBSERVATION"], {"label": "OBSERVATION"}, None, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    evaluate(
                        mission(), [proposal()], [evidence(evidence_class=bad)],
                        now_utc=NOW,
                    )

    def test_mission_expires_and_does_not_recommend_stale_plan(self):
        result = evaluate(
            mission(expires_at_utc="2026-11-01T13:59:59Z"),
            [proposal()],
            [evidence()],
            now_utc=NOW,
        )
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn("MISSION_EXPIRED", result["assessments"][0]["reasons"])

    def test_trusted_now_is_explicit_timezone_aware_utc(self):
        for now in (datetime(2026, 11, 1), NOW.astimezone(timezone(timedelta(hours=-5))), "now"):
            with self.subTest(now=now):
                with self.assertRaises((TypeError, ValueError)):
                    evaluate(mission(), [proposal()], [evidence()], now_utc=now)

    def test_duplicate_ids_and_untrusted_evidence_metadata_fail_closed(self):
        result = evaluate(
            mission(),
            [proposal("same"), proposal("same")],
            [evidence()],
            now_utc=NOW,
        )
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertIn("DUPLICATE_PROPOSAL_ID", result["assessments"][0]["reasons"])
        with self.assertRaises(ValueError):
            evaluate(mission(), [proposal()], [evidence(approved=True)], now_utc=NOW)
        with self.assertRaises(ValueError):
            evaluate(mission(), [proposal()], [evidence(), evidence()], now_utc=NOW)

    def test_invalid_proposal_id_cannot_alias_valid_id(self):
        with self.assertRaises(ValueError):
            evaluate(
                mission(),
                [proposal(proposal_id=None), proposal("invalid-proposal-0")],
                [evidence()],
                now_utc=NOW,
            )
        with self.assertRaises(ValueError):
            evaluate(mission(), [None], [evidence()], now_utc=NOW)

    def test_duplicate_id_assessments_are_canonical_under_permutation(self):
        plans = [
            proposal("same", cost_cents=4),
            proposal("same", target="secrets"),
        ]
        left = evaluate(mission(), plans, [evidence()], now_utc=NOW)
        right = evaluate(mission(), list(reversed(plans)), [evidence()], now_utc=NOW)
        self.assertEqual(left, right)
        self.assertEqual(left["status"], "ABSTAIN")

    def test_invalid_mission_policy_cannot_be_relaxed_by_bad_types(self):
        for override in (
            {"max_cost_cents": True},
            {"max_cost_cents": "100"},
            {"allowed_actions": ["analyze", "analyze"]},
            {"admin_approved": True},
            {"expires_at_utc": "not-a-timestamp"},
        ):
            with self.subTest(override=override):
                with self.assertRaises((TypeError, ValueError)):
                    evaluate(mission(**override), [proposal()], [evidence()], now_utc=NOW)


if __name__ == "__main__":
    unittest.main()
