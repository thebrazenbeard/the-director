"""Complete-input advisory dossier tests: equality is not attestation."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from runtime.director.dossier import (
    build_dossier,
    parse_dossier,
    public_receipt,
    replay_dossier,
    serialize_dossier,
)


NOW = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)


def fixture():
    mission = {
        "mission_id": "mission-1", "revision": 1,
        "objective": "Compare two plans, privately",
        "allowed_actions": ["draft", "analyze"],
        "allowed_targets": ["model", "document"],
        "forbidden_targets": ["secrets"],
        "max_cost_cents": 0, "min_independent_evidence": 1,
        "expires_at_utc": "2026-11-02T00:00:00Z",
    }
    proposals = [
        {"proposal_id": "b", "action": "draft", "target": "document",
         "cost_cents": 0, "benefit_points": 60, "evidence_ids": ["e1", "e2"]},
        {"proposal_id": "a", "action": "analyze", "target": "model",
         "cost_cents": 0, "benefit_points": 70, "evidence_ids": ["e2", "e1"]},
    ]
    evidence = [
        {"evidence_id": "e2", "evidence_class": "OBSERVATION", "source_ref": "source2", "lineage_id": "l2"},
        {"evidence_id": "e1", "evidence_class": "OBSERVATION", "source_ref": "source1", "lineage_id": "l1"},
    ]
    return mission, proposals, evidence


def build(m=None, p=None, e=None, now=NOW):
    baseline = fixture()
    return build_dossier(
        baseline[0] if m is None else m,
        baseline[1] if p is None else p,
        baseline[2] if e is None else e,
        now_utc=now,
    )


class DirectorDossierTests(unittest.TestCase):
    def test_digest_vectors_independently_recomputed(self):
        dossier = build()
        canonical = lambda data: json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        self.assertEqual(
            dossier["input_sha256"],
            hashlib.sha256(b"director-input-v1\x00" + canonical(dossier["input"])).hexdigest(),
        )
        self.assertEqual(
            dossier["decision_sha256"],
            hashlib.sha256(b"director-decision-v1\x00" + canonical(dossier["decision"])).hexdigest(),
        )
        record_without_digest = {k: v for k, v in dossier.items() if k != "dossier_sha256"}
        self.assertEqual(
            dossier["dossier_sha256"],
            hashlib.sha256(b"director-dossier-v1\x00" + canonical(record_without_digest)).hexdigest(),
        )

    def test_roundtrip_canonical_and_replay(self):
        dossier = build()
        raw = serialize_dossier(dossier)
        self.assertEqual(parse_dossier(raw), dossier)
        self.assertEqual(replay_dossier(dossier), "MATCH")
        self.assertEqual(serialize_dossier(parse_dossier(raw)), raw)
        self.assertEqual(dossier["decision"]["authorization_status"], "NOT_GRANTED")
        self.assertEqual(dossier["decision"]["effect_status"], "NOT_EXECUTED")

    def test_objective_only_mutation_changes_input_and_dossier_not_summary(self):
        baseline = build()
        mission, p, e = fixture()
        mission["objective"] = "An entirely different objective"
        changed = build(mission, p, e)
        self.assertEqual(baseline["decision"], changed["decision"])
        self.assertNotEqual(baseline["input_sha256"], changed["input_sha256"])
        self.assertNotEqual(baseline["dossier_sha256"], changed["dossier_sha256"])

    def test_source_only_mutation_changes_bound_digest(self):
        baseline = build()
        m, p, e = fixture()
        e[0]["source_ref"] = "other-source"
        changed = build(m, p, e)
        self.assertEqual(baseline["decision"], changed["decision"])
        self.assertNotEqual(baseline["input_sha256"], changed["input_sha256"])

    def test_lineage_and_class_are_bound(self):
        baseline = build()
        for field, replacement in (("lineage_id", "other-lineage"), ("evidence_class", "INFERENCE")):
            m, p, e = fixture()
            e[0][field] = replacement
            with self.subTest(field=field):
                self.assertNotEqual(baseline["input_sha256"], build(m, p, e)["input_sha256"])

    def test_every_material_mission_field_bound(self):
        baseline = build()
        for field, replacement in (("revision", 2), ("max_cost_cents", 1),
                                   ("allowed_targets", ["document"]),
                                   ("forbidden_targets", ["secrets", "model"]),
                                   ("min_independent_evidence", 2),
                                   ("expires_at_utc", "2026-11-03T00:00:00Z")):
            m, p, e = fixture()
            m[field] = replacement
            with self.subTest(field=field):
                self.assertNotEqual(baseline["input_sha256"], build(m, p, e)["input_sha256"])

    def test_every_material_proposal_field_bound(self):
        baseline = build()
        for field, replacement in (("cost_cents", 1), ("benefit_points", 90),
                                   ("action", "draft"), ("target", "document"),
                                   ("evidence_ids", ["e1"]), ("proposal_id", "newid")):
            m, p, e = fixture()
            p[1][field] = replacement
            with self.subTest(field=field):
                self.assertNotEqual(baseline["input_sha256"], build(m, p, e)["input_sha256"])

    def test_semantic_list_permutations_canonical(self):
        m, p, e = fixture()
        baseline = build(m, p, e)
        m["allowed_actions"].reverse()
        m["allowed_targets"].reverse()
        p.reverse()
        p[0]["evidence_ids"].reverse()
        e.reverse()
        self.assertEqual(baseline, build(m, p, e))

    def test_duplicate_proposals_are_stably_ordered(self):
        m, p, e = fixture()
        p.append(copy.deepcopy(p[0]))
        p[-1]["benefit_points"] = 0
        baseline = build(m, p, e)
        duplicate_rows = [
            assessment for assessment in baseline["decision"]["assessments"]
            if assessment["proposal_id"] == "b"
        ]
        self.assertEqual(len(duplicate_rows), 2)
        self.assertTrue(all("DUPLICATE_PROPOSAL_ID" in row["reasons"] for row in duplicate_rows))
        self.assertEqual(baseline, build(m, list(reversed(p)), e))

    def test_invalid_extra_fields_are_still_bound(self):
        m, p, e = fixture()
        p[0]["claimed_authority"] = {"role": "ADMIN", "note": "untrusted"}
        first = build(m, p, e)
        self.assertEqual(first["decision"]["authorization_status"], "NOT_GRANTED")
        self.assertIn("UNSUPPORTED_PROPOSAL_FIELDS", first["decision"]["assessments"][1]["reasons"])
        p[0]["claimed_authority"]["note"] = "different untrusted text"
        self.assertNotEqual(first["input_sha256"], build(m, p, e)["input_sha256"])

    def test_expiry_time_equivalence_and_microsecond_change(self):
        m, p, e = fixture()
        first = build(m, p, e)
        m["expires_at_utc"] = "2026-11-02T00:00:00+00:00"
        self.assertEqual(first, build(m, p, e))
        self.assertNotEqual(first["input_sha256"], build(m, p, e, now=NOW.replace(microsecond=1))["input_sha256"])
        self.assertEqual(first, build(m, p, e, now=NOW.astimezone(timezone(timedelta(0)))))

    def test_caller_mutation_does_not_change_stored_dossier(self):
        m, p, e = fixture()
        p[0]["extra"] = {"items": ["one", "two"]}
        dossier = build(m, p, e)
        immutable_bytes = serialize_dossier(dossier)
        m["objective"] = "Replaced"
        p[0]["extra"]["items"].append("three")
        e[0]["source_ref"] = "forged"
        self.assertEqual(serialize_dossier(dossier), immutable_bytes)

    def test_replay_rejects_content_or_decision_tamper(self):
        for label, field, change in (("input", "input", lambda d: d["mission"].update(objective="forged")),
                                     ("decision", "decision", lambda d: d.update(status="GRANTED")),
                                     ("digest", "input_sha256", lambda d: None),
                                     ("protocol", "policy_protocol", lambda d: None)):
            dossier = copy.deepcopy(build())
            if label == "digest":
                dossier[field] = "0" * 64
            elif label == "protocol":
                dossier[field] = "the-director.decision/9.9"
            else:
                change(dossier[field])
            with self.subTest(label=label), self.assertRaises(ValueError):
                replay_dossier(dossier)

    def test_parse_rejects_duplicate_keys_nonfinite_and_unknown_schema(self):
        for bad in (b'{"schema":"x","schema":"y"}', b'{"x": NaN}', b'{"x": Infinity}', b'[]', b'not json'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_dossier(bad)
        dossier = build()
        dossier["schema"] = "the-director.dossier/2"
        with self.assertRaises(ValueError):
            parse_dossier(json.dumps(dossier).encode("utf-8"))

    def test_parse_rejects_noncanonical_tamper_and_limits(self):
        with self.assertRaises(ValueError):
            parse_dossier(b" " + serialize_dossier(build()))
        with self.assertRaises(ValueError):
            parse_dossier(b" " * 300_000)
        with self.assertRaises(ValueError):
            serialize_dossier({"untrusted": True})

    def test_invalid_types_nan_cycles_surrogates_and_oversize_rejected(self):
        for extra in (float("nan"), float("inf"), object(), "x" * 500_000, "\ud800"):
            m, p, e = fixture()
            p[0]["extra"] = extra
            with self.subTest(extra=repr(extra)[:20]), self.assertRaises(ValueError):
                build(m, p, e)
        m, p, e = fixture()
        cycle = []
        cycle.append(cycle)
        p[0]["extra"] = cycle
        with self.assertRaises(ValueError):
            build(m, p, e)

    def test_valid_evaluator_inputs_can_exceed_custody_bound_and_fail_closed(self):
        mission, _, _ = fixture()
        references = [f"ev{n:02d}" + "z" * 60 for n in range(100)]
        records = [
            {"evidence_id": item, "evidence_class": "OBSERVATION",
             "source_ref": f"source{n}", "lineage_id": f"lineage{n}"}
            for n, item in enumerate(references)
        ]
        proposals = [
            {"proposal_id": f"plan{n}", "action": "analyze", "target": "model",
             "cost_cents": 0, "benefit_points": 1, "evidence_ids": references}
            for n in range(100)
        ]
        # A bounded dossier has a tighter limit than v0.1's evaluator alone.
        # Oversized but evaluator-valid input must be rejected before custody.
        with self.assertRaisesRegex(ValueError, "byte budget"):
            build(mission, proposals, records)

    def test_abstain_remains_valid_and_never_grants(self):
        m, p, e = fixture()
        m["expires_at_utc"] = "2026-10-31T00:00:00Z"
        dossier = build(m, p, e)
        self.assertEqual(dossier["decision"]["status"], "ABSTAIN")
        self.assertEqual(replay_dossier(parse_dossier(serialize_dossier(dossier))), "MATCH")
        self.assertEqual(dossier["decision"]["authorization_status"], "NOT_GRANTED")
        self.assertEqual(dossier["decision"]["effect_status"], "NOT_EXECUTED")

    def test_public_receipt_redacts_free_text_and_preserves_digests(self):
        dossier = build()
        receipt = public_receipt(dossier)
        self.assertEqual(receipt["dossier_sha256"], dossier["dossier_sha256"])
        self.assertEqual(receipt["status"], "RECOMMENDATION_ONLY")
        text = json.dumps(receipt)
        self.assertNotIn("Compare two plans", text)
        self.assertNotIn("source1", text)
        self.assertNotIn("lineage_id", text)
        self.assertNotIn("input", receipt)
        self.assertEqual(receipt["authorization_status"], "NOT_GRANTED")


if __name__ == "__main__":
    unittest.main()
