"""Durable, consented local recovery of data records; no process activation."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from runtime.director.agent_state import (
    AgentStateLedger, export_agent_state, restore_agent_state,
)
from runtime.director.recovery import RecoveryIntegrityError, RecoveryStore
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox


def _coordinator(*, paused=True, revision=1):
    return {
        "goal": {"goal_id": "goal1", "objective": "Analyze local quality", "metric": "quality"},
        "world": {"revision": revision, "facts": {}, "events": []},
        "operator_control": {"paused": paused, "stop_requested": False,
                             "confirmed_success_evidence_ids": []},
        "status": "PAUSED" if paused else "RUNNING",
        "reason": "OPERATOR_PAUSE" if paused else "SUCCESS_NOT_YET_VERIFIED",
        "assignments": [], "reports": [], "effect_status": "NOT_EXECUTED",
    }


def _consent(*, owner="owner1", domain="local.domain", allow_agent=False, approved=True):
    return {"approved": approved, "owner_id": owner,
            "target_domain": domain, "allow_agent_state": allow_agent}


def _approval(*, owner="owner1", domain="local.domain", rollback=False, approved=True):
    return {"approved": approved, "owner_id": owner,
            "target_domain": domain, "allow_rollback": rollback}


def _agent_record(path: Path):
    goal = {"goal_id": "agentgoal", "objective": "Check quality",
            "metric": "quality", "target_value": 8, "direction": "INCREASE"}
    source_state = {"goal": goal, "memories": [{"memory_id": "m1", "summary": "private-owner-note"}]}
    capsule = export_agent_state(
        capsule_id="cp.agent", agent_id="agent1", source_instance_id="source1",
        target_domain="local.domain", state=source_state,
        consent={"approved": True, "agent_id": "agent1", "source_instance_id": "source1",
                 "target_domain": "local.domain", "allow_memories": True, "operator_ref": "owner1"},
    )
    with AgentStateLedger(path) as ledger:
        return restore_agent_state(
            capsule, target_domain="local.domain", new_instance_id="new1",
            operator_approval={"approved": True, "agent_id": "agent1",
                               "new_instance_id": "new1", "target_domain": "local.domain"},
            ledger=ledger,
        )


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "recovery.sqlite3"
        self.store = RecoveryStore(self.path)
        self.addCleanup(self.store.close)

    def _checkpoint(self, checkpoint_id="one", *, generation=1, agent=None,
                    consent=None, snapshot=None, owner="owner1", domain="local.domain"):
        return self.store.checkpoint(
            checkpoint_id, owner_id=owner, target_domain=domain, generation=generation,
            coordinator_snapshot=_coordinator() if snapshot is None else snapshot,
            agent_state=agent,
            consent=_consent(owner=owner, domain=domain, allow_agent=agent is not None)
            if consent is None else consent,
        )

    def test_checkpoint_is_explicit_idempotent_and_durable(self):
        first = self._checkpoint()
        self.assertEqual(first["generation"], 1)
        self.assertEqual(first["effect_status"], "NOT_EXECUTED")
        self.assertFalse(first["source_authenticated"])
        self.assertEqual(first, self._checkpoint())
        self.store.close()
        with RecoveryStore(self.path) as reopened:
            preview = reopened.preview_restore("one", approval=_approval())
            self.assertEqual(preview["source_generation"], 1)
            self.assertEqual(preview["status"], "ELIGIBLE_PREVIEW")

    def test_conflicting_checkpoint_id_never_overwrites(self):
        self._checkpoint()
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self._checkpoint(snapshot=_coordinator(paused=False))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self._checkpoint(generation=2)

    def test_checkpoint_generations_are_monotonic_per_owner_domain(self):
        self._checkpoint()
        with self.assertRaisesRegex(ValueError, "not newer"):
            self._checkpoint("two", generation=1)
        self._checkpoint("two", generation=2)
        self.assertEqual(self.store.preview_restore("two", approval=_approval())["source_generation"], 2)
        # An independent owner and domain may start at generation one.
        self._checkpoint("another", generation=1, owner="owner2", domain="elsewhere")

    def test_explicit_consent_and_target_binding(self):
        for consent in (
            _consent(approved=False), _consent(owner="other"),
            _consent(domain="wrong"), {**_consent(), "allow_agent_state": 1},
            {**_consent(), "dangerous_extra": True},
        ):
            with self.subTest(consent=consent):
                with self.assertRaises(ValueError):
                    self._checkpoint(consent=consent)
        self.assertFalse(self.path.stat().st_size == 0)
        with self.assertRaises(KeyError):
            self.store.preview_restore("one", approval=_approval())

    def test_agent_state_requires_explicit_scope_and_is_data_only(self):
        agent = _agent_record(Path(self.tmp.name) / "agent-ledger.sqlite3")
        with self.assertRaisesRegex(ValueError, "opt-in"):
            self._checkpoint(agent=agent, consent=_consent())
        original = self._checkpoint(agent=agent)
        self.assertTrue(original["agent_state_included"])
        restored = self.store.restore("one", restore_id="restore1", approval=_approval())
        self.assertEqual(restored["agent_state"], agent)
        self.assertEqual(restored["agent_state"]["state"]["memories"][0]["summary"], "private-owner-note")
        self.assertFalse(restored["runtime_activated"])
        self.assertEqual(restored["authority"], "NOT_GRANTED")
        self.assertEqual(restored["effect_status"], "NOT_EXECUTED")

    def test_private_agent_text_never_leaks_in_preview(self):
        agent = _agent_record(Path(self.tmp.name) / "agent-ledger.sqlite3")
        self._checkpoint(agent=agent)
        preview = self.store.preview_restore("one", approval=_approval())
        self.assertTrue(preview["agent_state_included"])
        self.assertFalse(preview["runtime_activated"])
        self.assertNotIn("private-owner-note", str(preview))

    def test_agent_authority_and_cross_domain_claims_rejected(self):
        agent = _agent_record(Path(self.tmp.name) / "agent-ledger.sqlite3")
        for changes in ({"target_domain": "wrong"}, {"authority": "GRANTED"},
                        {"effects": "EXECUTED"}, {"subject_kind": "HUMAN"},
                        {"continuity_claim": "SUBJECTIVE_CONSCIOUSNESS_MOVED"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self._checkpoint(agent={**agent, **changes})

    def test_rejects_forged_agent_identity_and_memory_manifest(self):
        agent = _agent_record(Path(self.tmp.name) / "agent-ledger.sqlite3")
        bad_records = [
            {**agent, "new_instance_id": agent["restored_from_instance_id"]},
            {**agent, "memory_manifest": {**agent["memory_manifest"], "transferred_count": 0}},
            {**agent, "state": {"goal": agent["state"]["goal"], "memories": "not-list"}},
        ]
        for bad in bad_records:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._checkpoint(agent=bad)

    def test_approval_cannot_be_wrong_domain_or_owner(self):
        self._checkpoint()
        for approval in (_approval(approved=False), _approval(owner="attacker"),
                         _approval(domain="wrong"), {**_approval(), "allow_rollback": "yes"}):
            with self.subTest(approval=approval):
                with self.assertRaises(ValueError):
                    self.store.preview_restore("one", approval=approval)
                with self.assertRaises(ValueError):
                    self.store.restore("one", restore_id="r1", approval=approval)

    def test_preview_is_read_only_and_restoration_repeats_all_checks(self):
        self._checkpoint()
        for _ in range(3):
            self.assertEqual(self.store.preview_restore("one", approval=_approval())["status"], "ELIGIBLE_PREVIEW")
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM director_recovery_restores").fetchone()[0], 0)
        self.assertEqual(self.store.restore("one", restore_id="r1", approval=_approval())["restore_id"], "r1")

    def test_restore_receipt_recovers_after_client_crash_or_lost_response(self):
        self._checkpoint()
        admitted = self.store.restore("one", restore_id="r1", approval=_approval())
        self.store.close()
        with RecoveryStore(self.path) as reopened:
            self.assertEqual(reopened.get_restore("r1"), admitted)
            with self.assertRaisesRegex(ValueError, "already consumed"):
                reopened.restore("one", restore_id="r1", approval=_approval())

    def test_checkpoint_reuse_requires_explicit_rollback(self):
        self._checkpoint()
        self.store.restore("one", restore_id="r1", approval=_approval())
        with self.assertRaisesRegex(ValueError, "rollback"):
            self.store.preview_restore("one", approval=_approval())
        self.assertTrue(self.store.preview_restore("one", approval=_approval(rollback=True))["requires_rollback"])
        with self.assertRaisesRegex(ValueError, "already restored"):
            self.store.restore("one", restore_id="r2", approval=_approval())
        rollback = self.store.restore("one", restore_id="r2", approval=_approval(rollback=True))
        self.assertTrue(rollback["rollback_requested"])
        self.assertGreater(rollback["recovery_generation"], 2)

    def test_older_checkpoint_requires_explicit_rollback_even_if_not_previously_restored(self):
        self._checkpoint("one", generation=1)
        self._checkpoint("two", generation=2)
        with self.assertRaisesRegex(ValueError, "rollback"):
            self.store.preview_restore("one", approval=_approval())
        with self.assertRaisesRegex(ValueError, "rollback"):
            self.store.restore("one", restore_id="r1", approval=_approval())
        preview = self.store.preview_restore("one", approval=_approval(rollback=True))
        self.assertTrue(preview["requires_rollback"])
        restored = self.store.restore("one", restore_id="r1", approval=_approval(rollback=True))
        self.assertEqual(restored["source_generation"], 1)
        self.assertTrue(restored["rollback_requested"])

    def test_pause_and_observation_class_preserved_exactly(self):
        snapshot = _coordinator(paused=True)
        snapshot["world"]["events"] = [{"event_id": "e1", "evidence_class": "HYPOTHESIS",
                                         "claim_id": "quality", "value": 42}]
        self._checkpoint(snapshot=snapshot)
        restored = self.store.restore("one", restore_id="r1", approval=_approval())
        self.assertEqual(restored["coordinator_snapshot"], snapshot)
        self.assertTrue(restored["coordinator_snapshot"]["operator_control"]["paused"])
        self.assertEqual(restored["coordinator_snapshot"]["world"]["events"][0]["evidence_class"], "HYPOTHESIS")

    def test_accepts_real_coordinator_inspection_snapshot(self):
        with DirectorMailbox(Path(self.tmp.name) / "messages.sqlite3") as mailbox:
            with DirectorCoordinator(Path(self.tmp.name) / "coordinator.sqlite3", mailbox) as coordinator:
                coordinator.configure(
                    {"goal_id": "quality_goal", "objective": "Observe quality", "metric": "quality",
                     "target_value": 8, "direction": "INCREASE"},
                    [{"capability_id": "observer", "agent_id": "analyst1", "metric": "quality",
                      "expected_delta": 1, "cost_units": 0}], max_missions=2,
                )
                coordinator.configure_graph([
                    {"mission_id": "observer", "kind": "ANALYZE", "depends_on": [],
                     "resources": {"sensor": 1}, "priority": 1, "cost_units": 0},
                ], {"sensor": 1})
                coordinator.set_unavailable_graph_resources(["sensor"])
                coordinator.set_paused(True)
                snapshot = coordinator.inspect()
        self._checkpoint(snapshot=snapshot)
        recovered = self.store.restore("one", restore_id="r1", approval=_approval())
        self.assertEqual(recovered["coordinator_snapshot"], snapshot)
        self.assertTrue(recovered["coordinator_snapshot"]["operator_control"]["paused"])
        self.assertEqual(recovered["coordinator_snapshot"]["graph"]["unavailable_resources"], ["sensor"])
        self.assertEqual(recovered["coordinator_snapshot"]["authentication_mode"], "UNAUTHENTICATED_LOCAL")

    def test_authentication_mode_is_recorded_but_cannot_mint_authority(self):
        self._checkpoint(snapshot={**_coordinator(), "authentication_mode": "ENFORCED_LOCAL_HMAC"})
        restored = self.store.restore("one", restore_id="r1", approval=_approval())
        self.assertEqual(restored["coordinator_snapshot"]["authentication_mode"], "ENFORCED_LOCAL_HMAC")
        self.assertEqual(restored["authority"], "NOT_GRANTED")
        self.assertFalse(restored["owner_authenticated"])
        with self.assertRaisesRegex(ValueError, "authentication mode"):
            self._checkpoint("other", generation=3, snapshot={**_coordinator(),
                "authentication_mode": "TRUST_ALL_SENDERS"})

    def test_effect_claims_rejected_and_untrusted_source_not_authenticated(self):
        with self.assertRaisesRegex(ValueError, "effect"):
            self._checkpoint(snapshot={**_coordinator(), "effect_status": "EXECUTED"})
        self._checkpoint()
        record = self.store.restore("one", restore_id="r1", approval=_approval())
        self.assertFalse(record["owner_authenticated"])
        self.assertFalse(record["runtime_activated"])

    def test_corrupted_checkpoint_detected_before_preview_or_restore(self):
        self._checkpoint()
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE director_recovery_checkpoints SET record_json='{}' WHERE checkpoint_id='one'")
            connection.commit()
        with RecoveryStore(self.path) as reopened:
            with self.assertRaises(RecoveryIntegrityError):
                reopened.preview_restore("one", approval=_approval())
            with self.assertRaises(RecoveryIntegrityError):
                reopened.restore("one", restore_id="r1", approval=_approval())

    def test_corrupted_recovered_record_detected_after_restart(self):
        self._checkpoint()
        self.store.restore("one", restore_id="r1", approval=_approval())
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE director_recovery_restores SET generation=99 WHERE restore_id='r1'")
            connection.commit()
        with RecoveryStore(self.path) as reopened:
            with self.assertRaises(RecoveryIntegrityError):
                reopened.get_restore("r1")

    def test_uncommitted_checkpoint_disappears_after_crash_like_close(self):
        # The public method always commits atomically; exercise SQLite's
        # uncommitted-write rollback as a controlled crash simulation.
        self.store._connection().execute("BEGIN IMMEDIATE")
        self.store._connection().execute("""INSERT INTO director_recovery_checkpoints
            (checkpoint_id,owner_id,target_domain,generation,record_json,record_digest)
            VALUES('partial','owner1','local.domain',1,'{}','fake')""")
        self.store.close()
        with RecoveryStore(self.path) as reopened:
            with self.assertRaises(KeyError):
                reopened.preview_restore("partial", approval=_approval())
            self.assertEqual(reopened.checkpoint("one", owner_id="owner1", target_domain="local.domain",
                generation=1, coordinator_snapshot=_coordinator(), consent=_consent())["generation"], 1)

    def test_invalid_input_types_depth_size_and_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, "nested"):
            nested = []
            for _ in range(11):
                nested = [nested]
            self._checkpoint(snapshot={**_coordinator(), "reason": nested})
        with self.assertRaisesRegex(ValueError, "JSON"):
            self._checkpoint(snapshot={**_coordinator(), "reason": object()})
        with self.assertRaisesRegex(ValueError, "unbounded"):
            self._checkpoint(snapshot={**_coordinator(), "reason": "x" * 17000})
        with self.assertRaises(ValueError):
            self._checkpoint(snapshot={**_coordinator(), "injected_authority": True})
        with self.assertRaises(ValueError):
            self._checkpoint(generation=True)

    def test_file_created_private_on_posix_and_closes(self):
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.store.close()
        self.store.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.store.preview_restore("none", approval=_approval())

    def test_in_memory_and_symlink_storage_rejected(self):
        with self.assertRaisesRegex(ValueError, "durable"):
            RecoveryStore(":memory:")
        if os.name != "nt":
            alias = Path(self.tmp.name) / "alias.sqlite3"
            alias.symlink_to(self.path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                RecoveryStore(alias)

    def test_existing_group_or_other_access_rejected_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX permission-bit assertion")
        self.store.close()
        for mode in (0o644, 0o620, 0o601, 0o666):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaisesRegex(ValueError, "owner-only"):
                    RecoveryStore(self.path)
        self.path.chmod(0o600)
        with RecoveryStore(self.path) as owner_only:
            receipt = owner_only.checkpoint(
                "permitted", owner_id="owner1", target_domain="local.domain",
                generation=1, coordinator_snapshot=_coordinator(), consent=_consent(),
            )
            self.assertEqual(receipt["checkpoint_id"], "permitted")

    def test_existing_wal_and_shm_sidecars_must_be_owner_only_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX permission-bit assertion")
        self.store.close()
        for suffix in ("-wal", "-shm"):
            for mode in (0o644, 0o620, 0o601, 0o666):
                with self.subTest(suffix=suffix, mode=oct(mode)):
                    sidecar = Path(f"{self.path}{suffix}")
                    self.assertFalse(sidecar.exists())
                    sidecar.write_bytes(b"private simulated recovery data")
                    try:
                        sidecar.chmod(mode)
                        with self.assertRaisesRegex(ValueError, "owner-only"):
                            RecoveryStore(self.path)
                    finally:
                        sidecar.unlink()

    def test_existing_wal_and_shm_sidecars_reject_symlinks_and_directories(self):
        if os.name != "posix":
            self.skipTest("POSIX sidecar symlink assertion")
        self.store.close()
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix, kind="symlink"):
                sidecar = Path(f"{self.path}{suffix}")
                self.assertFalse(sidecar.exists())
                sidecar.symlink_to(self.path)
                try:
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        RecoveryStore(self.path)
                finally:
                    sidecar.unlink()
            with self.subTest(suffix=suffix, kind="directory"):
                sidecar.mkdir()
                try:
                    with self.assertRaisesRegex(ValueError, "regular file"):
                        RecoveryStore(self.path)
                finally:
                    sidecar.rmdir()


if __name__ == "__main__":
    unittest.main()
