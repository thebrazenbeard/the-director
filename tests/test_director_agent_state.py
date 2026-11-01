"""Hostile, cross-process-friendly tests for *software-only* state handoff."""

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime.director.agent_state import (
    AgentStateLedger,
    SCHEMA_VERSION,
    export_agent_state,
    preview_agent_state_restore,
    restore_agent_state,
)


def _state():
    return {
        "goal": {
            "goal_id": "g1", "objective": "Investigate a test signal",
            "metric": "quality", "target_value": 10, "direction": "INCREASE",
        },
        "memories": [
            {"memory_id": "m1", "summary": "A prior measurement was uncertain"},
            {"memory_id": "m2", "summary": "An operator preferred bounded trials"},
        ],
    }


def _consent(**changes):
    c = {
        "approved": True, "agent_id": "agentA", "source_instance_id": "source1",
        "target_domain": "isolated-domain", "allow_memories": False,
        "operator_ref": "local-operator-claim",
    }
    c.update(changes)
    return c


def _approval(**changes):
    a = {
        "approved": True, "agent_id": "agentA", "new_instance_id": "restored1",
        "target_domain": "isolated-domain",
    }
    a.update(changes)
    return a


def _export(state=None, consent=None, **changes):
    kwargs = {
        "capsule_id": "capsule1", "agent_id": "agentA",
        "source_instance_id": "source1", "target_domain": "isolated-domain",
        "state": _state() if state is None else state,
        "consent": _consent() if consent is None else consent,
    }
    kwargs.update(changes)
    return export_agent_state(**kwargs)


def _reseal(envelope):
    raw = json.dumps(envelope["body"], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    envelope["sha256"] = hashlib.sha256(raw).hexdigest()
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class DirectorAgentStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = Path(self.tmp.name) / "transfers.sqlite3"

    def _restore(self, capsule, *, target_domain="isolated-domain", new_instance_id="restored1",
                 approval=None, ledger=None):
        if ledger is not None:
            return restore_agent_state(
                capsule, target_domain=target_domain, new_instance_id=new_instance_id,
                operator_approval=_approval() if approval is None else approval, ledger=ledger,
            )
        with AgentStateLedger(self.ledger_path) as new_ledger:
            return restore_agent_state(
                capsule, target_domain=target_domain, new_instance_id=new_instance_id,
                operator_approval=_approval() if approval is None else approval, ledger=new_ledger,
            )

    def test_deterministic_cross_process_canonical_export(self):
        a = _export()
        b = _export(state={"memories": _state()["memories"], "goal": dict(reversed(list(_state()["goal"].items())))})
        self.assertEqual(a, b)
        self.assertLessEqual(len(a), 32768)
        self.assertEqual(json.loads(a)["body"]["schema_version"], SCHEMA_VERSION)

    def test_memory_omitted_without_specific_consent(self):
        capsule = _export()
        self.assertEqual(json.loads(capsule)["body"]["state"]["memories"], [])
        self.assertEqual(json.loads(capsule)["body"]["memory_manifest"], {
            "input_count": 2, "transferred_count": 0, "omitted_count": 2,
        })
        result = self._restore(capsule)
        self.assertEqual(result["state"]["memories"], [])
        self.assertEqual(result["state"]["goal"]["goal_id"], "g1")
        self.assertEqual(result["memory_manifest"]["omitted_count"], 2)

    def test_memory_retained_only_with_consent(self):
        capsule = _export(consent=_consent(allow_memories=True))
        result = self._restore(capsule)
        self.assertEqual(result["state"]["memories"], _state()["memories"])
        self.assertEqual(result["memory_manifest"]["omitted_count"], 0)

    def test_preview_is_read_only_and_restore_still_works_afterwards(self):
        capsule = _export()
        with AgentStateLedger(self.ledger_path) as ledger:
            preview = preview_agent_state_restore(
                capsule, target_domain="isolated-domain", new_instance_id="restored1",
                operator_approval=_approval(), ledger=ledger,
            )
            self.assertEqual(preview["status"], "ELIGIBLE_AT_PREVIEW")
            self.assertEqual(preview["memory_manifest"]["omitted_count"], 2)
            self.assertEqual(preview["executable"], False)
            self.assertEqual(preview["authority"], "NOT_GRANTED")
            self.assertEqual(preview, preview_agent_state_restore(
                capsule, target_domain="isolated-domain", new_instance_id="restored1",
                operator_approval=_approval(), ledger=ledger,
            ))
            count = ledger._conn.execute("SELECT COUNT(*) FROM restored_capsules").fetchone()[0]
            self.assertEqual(count, 0)
            state = self._restore(capsule, ledger=ledger)
            self.assertEqual(state["new_instance_id"], "restored1")
            with self.assertRaisesRegex(ValueError, "already restored"):
                preview_agent_state_restore(
                    capsule, target_domain="isolated-domain", new_instance_id="restored2",
                    operator_approval=_approval(new_instance_id="restored2"), ledger=ledger,
                )

    def test_preview_rejects_invalid_consent_target_capsule_and_instance_collision(self):
        capsule = _export()
        with AgentStateLedger(self.ledger_path) as ledger:
            for kwargs in (
                {"target_domain": "other"},
                {"operator_approval": _approval(approved=False)},
                {"new_instance_id": "source1",
                 "operator_approval": _approval(new_instance_id="source1")},
            ):
                standard = {
                    "target_domain": "isolated-domain", "new_instance_id": "restored1",
                    "operator_approval": _approval(), "ledger": ledger,
                }
                standard.update(kwargs)
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    preview_agent_state_restore(capsule, **standard)
            bad = capsule.replace(b"Investigate", b"Manipulate")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                preview_agent_state_restore(bad, target_domain="isolated-domain",
                                            new_instance_id="restored1",
                                            operator_approval=_approval(), ledger=ledger)

    def test_preview_cannot_expose_opted_in_memory_text(self):
        state = _state()
        state["memories"].append({"memory_id": "private", "summary": "hidden-preview-example-secret"})
        capsule = _export(state=state, consent=_consent(allow_memories=True))
        with AgentStateLedger(self.ledger_path) as ledger:
            preview = preview_agent_state_restore(
                capsule, target_domain="isolated-domain", new_instance_id="restored1",
                operator_approval=_approval(), ledger=ledger,
            )
        self.assertEqual(preview["memory_manifest"], {
            "input_count": 3, "transferred_count": 3, "omitted_count": 0,
        })
        self.assertNotIn("hidden-preview-example-secret", json.dumps(preview))
        self.assertFalse(preview["source_authenticated"])

    def test_preview_rejects_unsupported_schema_and_memory_manifest_forgery(self):
        envelope = json.loads(_export())
        envelope["body"]["schema_version"] = "director.software-agent-state.future"
        with AgentStateLedger(self.ledger_path) as ledger:
            with self.assertRaisesRegex(ValueError, "unsupported"):
                preview_agent_state_restore(
                    _reseal(envelope), target_domain="isolated-domain",
                    new_instance_id="restored1", operator_approval=_approval(), ledger=ledger,
                )
            envelope = json.loads(_export())
            envelope["body"]["memory_manifest"]["transferred_count"] = 1
            with self.assertRaisesRegex(ValueError, "memory manifest"):
                preview_agent_state_restore(
                    _reseal(envelope), target_domain="isolated-domain",
                    new_instance_id="restored1", operator_approval=_approval(), ledger=ledger,
                )

    def test_manifest_tampering_cannot_make_excluded_memories_appear(self):
        envelope = json.loads(_export())
        envelope["body"]["memory_manifest"]["omitted_count"] = 0
        with self.assertRaisesRegex(ValueError, "memory manifest"):
            self._restore(_reseal(envelope))

    def test_free_text_secrets_require_manual_redaction(self):
        state = _state()
        state["memories"].append({"memory_id": "m3", "summary": "API_KEY=example-sensitive-value"})
        excluded = _export(state=state)
        self.assertNotIn(b"API_KEY", excluded)
        # This intentionally proves the LIMITATION: explicit memory consent
        # includes text verbatim and is not an automated secret scanner.
        included = _export(state=state, consent=_consent(allow_memories=True))
        self.assertIn(b"API_KEY", included)

    def test_restores_as_new_instance_without_permission(self):
        result = self._restore(_export())
        self.assertEqual(result["new_instance_id"], "restored1")
        self.assertEqual(result["restored_from_instance_id"], "source1")
        self.assertEqual(result["subject_kind"], "SOFTWARE_AGENT")
        self.assertEqual(result["authority"], "NOT_GRANTED")
        self.assertEqual(result["effects"], "NOT_EXECUTED")
        self.assertEqual(result["continuity_claim"], "INFORMATION_STATE_COPY_ONLY")

    def test_replay_rejected_after_ledger_reopen(self):
        capsule = _export()
        self._restore(capsule)
        with self.assertRaisesRegex(ValueError, "already restored"):
            self._restore(capsule, new_instance_id="restored2", approval=_approval(new_instance_id="restored2"))

    def test_new_instance_id_reuse_rejected_even_with_different_capsule(self):
        self._restore(_export())
        with self.assertRaisesRegex(ValueError, "already used"):
            self._restore(_export(capsule_id="capsule2"))

    def test_rejects_source_instance_overwrite(self):
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self._restore(_export(), new_instance_id="source1",
                          approval=_approval(new_instance_id="source1"))

    def test_rejects_target_domain_mismatch(self):
        with self.assertRaisesRegex(ValueError, "target domain"):
            self._restore(_export(), target_domain="another-domain")
        with sqlite3.connect(self.ledger_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM restored_capsules").fetchone()[0]
        self.assertEqual(count, 0)

    def test_rejects_missing_or_wrong_operator_approval(self):
        for wrong in [
            _approval(approved=False), _approval(agent_id="other"),
            _approval(target_domain="wrong"), _approval(new_instance_id="wrong"),
        ]:
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self._restore(_export(), approval=wrong)

    def test_rejects_missing_or_wrong_source_consent(self):
        for wrong in [
            _consent(approved=False), _consent(agent_id="other"),
            _consent(source_instance_id="other"), _consent(target_domain="other"),
            _consent(allow_memories="yes"),
        ]:
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                _export(consent=wrong)

    def test_detects_bit_flip(self):
        capsule = _export()
        tampered = capsule.replace(b"Investigate", b"Manipulate")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self._restore(tampered)

    def test_rejects_unsupported_schema_even_when_resealed(self):
        envelope = json.loads(_export())
        envelope["body"]["schema_version"] = "director.software-agent-state.v999"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self._restore(_reseal(envelope))

    def test_rejects_human_subject_even_when_resealed(self):
        envelope = json.loads(_export())
        envelope["body"]["subject_kind"] = "HUMAN"
        with self.assertRaisesRegex(ValueError, "software agent"):
            self._restore(_reseal(envelope))

    def test_rejects_fake_authority_or_executable_content(self):
        state = _state()
        state["credentials"] = {"token": "private"}
        with self.assertRaisesRegex(ValueError, "state must"):
            _export(state=state)
        state = _state()
        state["effects"] = ["shell"]
        with self.assertRaises(ValueError):
            _export(state=state)
        envelope = json.loads(_export())
        envelope["body"]["state"]["host_authority"] = "YES"
        with self.assertRaises(ValueError):
            self._restore(_reseal(envelope))

    def test_rejects_nonfinite_and_boolean_goal_values(self):
        for value in [True, float("nan"), float("inf"), 10**20, 10**309]:
            with self.subTest(value=value):
                state = _state()
                state["goal"]["target_value"] = value
                with self.assertRaises(ValueError):
                    _export(state=state)

    def test_rejects_duplicate_memory_id(self):
        state = _state()
        state["memories"].append(copy.deepcopy(state["memories"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate memory"):
            _export(state=state, consent=_consent(allow_memories=True))

    def test_rejects_noncanonical_json_and_duplicate_keys(self):
        good = _export()
        with self.assertRaisesRegex(ValueError, "canonical"):
            self._restore(good + b" ")
        with self.assertRaises(ValueError):
            self._restore(b'{"body":{},"body":{},"sha256":"broken"}')

    def test_rejects_oversize_capsule(self):
        with self.assertRaises(ValueError):
            self._restore(b"a" * 32769)
        state = _state()
        state["memories"] = [
            {"memory_id": f"m{i}", "summary": "x" * 512} for i in range(32)
        ]
        # This remains below the raw size cap, establishing bounded behavior.
        self.assertLessEqual(len(_export(state=state, consent=_consent(allow_memories=True))), 32768)

    def test_existing_ledger_file_remains_private_on_posix(self):
        with AgentStateLedger(self.ledger_path):
            pass
        if os.name == "posix":
            self.assertEqual(self.ledger_path.stat().st_mode & 0o777, 0o600)

    def test_cannot_use_in_memory_ledger_as_durable_replay_guard(self):
        with self.assertRaisesRegex(ValueError, "durable"):
            AgentStateLedger(":memory:")


if __name__ == "__main__":
    unittest.main()
