"""Opt-in local integration: advisory input identity never conveys authority."""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runtime.director.authority import evaluate_reference_authority
from runtime.director.custody import CustodyStore
from runtime.director.decision import evaluate
from runtime.director.dossier import (
    build_dossier,
    parse_dossier,
    replay_dossier,
    serialize_dossier,
)
from runtime.director.task_store import TaskStore


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def inputs():
    mission = {
        "mission_id": "local-mission", "revision": 1,
        "objective": "Compare two bounded, private plans",
        "allowed_actions": ["analyze"], "allowed_targets": ["document"],
        "forbidden_targets": [], "max_cost_cents": 0,
        "min_independent_evidence": 1,
        "expires_at_utc": "2026-11-03T00:00:00Z",
    }
    proposals = [{
        "proposal_id": "option-a", "action": "analyze", "target": "document",
        "cost_cents": 0, "benefit_points": 50, "evidence_ids": ["obs-a"],
    }]
    evidence = [{
        "evidence_id": "obs-a", "evidence_class": "OBSERVATION",
        "source_ref": "declared-local-a", "lineage_id": "declared-lineage-a",
    }]
    return mission, proposals, evidence


class DirectorLocalFoundationsIntegration(unittest.TestCase):
    def test_advisory_dossier_to_opt_in_custody_to_fenced_analytical_task(self):
        mission, proposals, evidence = inputs()
        # Disallow all ordinary process launches and socket creation during
        # this flow. Only the explicitly chosen temporary SQLite files change.
        with TemporaryDirectory() as directory, \
             patch("subprocess.run", side_effect=AssertionError("process effect")), \
             patch("socket.socket", side_effect=AssertionError("network effect")):
            base = Path(directory)
            dossier = build_dossier(mission, proposals, evidence, now_utc=NOW)
            raw = serialize_dossier(dossier)
            self.assertEqual(replay_dossier(parse_dossier(raw)), "MATCH")
            self.assertEqual(dossier["decision"]["status"], "RECOMMENDATION_ONLY")

            custody_path = base / "custody.sqlite3"
            self.assertFalse(custody_path.exists())
            with CustodyStore(custody_path, max_records=2) as custody:
                saved = custody.append("dossier-a", "dossier", raw, "2026-11-01T12:00:00Z")
                self.assertTrue(custody.verify_chain())
            with CustodyStore(custody_path, max_records=2) as reopened:
                recovered = reopened.get("dossier-a")
                self.assertEqual(recovered.receipt, saved)
                self.assertEqual(recovered.content, raw)
                self.assertEqual(replay_dossier(parse_dossier(recovered.content)), "MATCH")

            queue_path = base / "tasks.sqlite3"
            self.assertFalse(queue_path.exists())
            digest = hashlib.sha256(raw).hexdigest()
            result_digest = hashlib.sha256(
                json.dumps(dossier["decision"], sort_keys=True).encode("utf-8")
            ).hexdigest()
            with TaskStore(queue_path) as tasks:
                tasks.submit("analysis-a", mission["mission_id"], mission["revision"], digest)
                lease = tasks.claim("local-analyst", NOW, 60)
                self.assertIsNotNone(lease)
                result = tasks.complete(lease, result_digest, NOW + timedelta(seconds=1))
                self.assertEqual(result["status"], "DONE")
                self.assertEqual(result["effect_authority"], "NOT_GRANTED")
                self.assertIsNone(tasks.claim("next-analyst", NOW + timedelta(seconds=2), 60))
            with TaskStore(queue_path) as reopened:
                self.assertEqual(reopened.inspect("analysis-a")["result_sha256"], result_digest)

            for claimed_grant in (None, {"role": "ADMIN", "approved": True}):
                refusal = evaluate_reference_authority(
                    {"dossier_sha256": dossier["dossier_sha256"]}, claimed_grant,
                    now_utc=NOW, declared_revocation_epoch=0,
                )
                self.assertEqual(refusal["status"], "DENIED")
                self.assertEqual(refusal["authorization_status"], "NOT_GRANTED")
                self.assertEqual(refusal["effect_status"], "NOT_EXECUTED")

            self.assertTrue(custody_path.exists())
            self.assertTrue(queue_path.exists())

    def test_legacy_summary_collision_is_fixed_by_private_dossier_binding(self):
        mission, proposals, evidence = inputs()
        before = evaluate(mission, proposals, evidence, now_utc=NOW)
        dossier = build_dossier(mission, proposals, evidence, now_utc=NOW)
        mission["objective"] = "The other objective altogether"
        after = evaluate(mission, proposals, evidence, now_utc=NOW)
        changed = build_dossier(mission, proposals, evidence, now_utc=NOW)
        self.assertEqual(before, after)
        self.assertNotEqual(dossier["dossier_sha256"], changed["dossier_sha256"])
        evidence[0]["source_ref"] = "claimed-origin-b"
        changed_again = build_dossier(mission, proposals, evidence, now_utc=NOW)
        self.assertNotEqual(changed["input_sha256"], changed_again["input_sha256"])


if __name__ == "__main__":
    unittest.main()
