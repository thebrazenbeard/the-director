"""Adversarial checks for a local-only, non-executing Director task ledger."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime.director.task_store import LeaseRejected, TaskConflict, TaskStore


T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
DIGEST = hashlib.sha256(b"bounded analytical dossier").hexdigest()
RESULT = hashlib.sha256(b"recommendation only").hexdigest()


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "director-tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.addCleanup(self.store.close)

    def submit(self, name="work_a", **kwargs):
        return self.store.submit(name, "mission_a", 1, DIGEST, **kwargs)

    def test_submit_idempotent_and_conflicting_payload_rejected(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaises(TaskConflict):
            self.store.submit("work_a", "mission_a", 1, RESULT)
        with self.assertRaises(TaskConflict):
            self.store.submit("work_a", "mission_a", 2, DIGEST)

    def test_rejects_invalid_payloads_and_non_utc_clock(self):
        with self.assertRaises(ValueError):
            self.store.submit("../bad", "mission_a", 1, DIGEST)
        with self.assertRaises(ValueError):
            self.store.submit("work_a", "mission_a", True, DIGEST)
        with self.assertRaises(ValueError):
            self.store.submit("work_a", "mission_a", 1, "faked")
        self.submit()
        with self.assertRaises(ValueError):
            self.store.claim("worker", T0.replace(tzinfo=None), 5)
        with self.assertRaises(ValueError):
            self.store.claim("worker", T0, 0)

    def test_claim_complete_and_reopen_persistent_result(self):
        self.submit()
        lease = self.store.claim("worker_a", T0, 5)
        self.assertEqual(lease.fencing_token, 1)
        self.assertEqual(self.store.complete(lease, RESULT, T0 + timedelta(seconds=2))["status"], "DONE")
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=3), 5))
        self.store.close()
        with TaskStore(self.path) as restarted:
            row = restarted.inspect("work_a")
            self.assertEqual(row["status"], "DONE")
            self.assertEqual(row["result_sha256"], RESULT)
            self.assertEqual(row["fencing_token"], 1)

    def test_lost_response_idempotent_completion_but_not_contradictory(self):
        self.submit()
        lease = self.store.claim("worker_a", T0, 5)
        done = self.store.complete(lease, RESULT, T0 + timedelta(seconds=1))
        self.assertEqual(done, self.store.complete(lease, RESULT, T0 + timedelta(seconds=2)))
        with self.assertRaises(TaskConflict):
            self.store.complete(lease, DIGEST, T0 + timedelta(seconds=2))

    def test_completed_replay_must_bind_entire_lease_identity(self):
        self.submit()
        lease = self.store.claim("worker_a", T0, 5)
        self.store.complete(lease, RESULT, T0 + timedelta(seconds=1))
        mutations = (
            replace(lease, mission_id="mission_b"),
            replace(lease, revision=2),
            replace(lease, payload_sha256=RESULT),
            replace(lease, expires_at_utc=lease.expires_at_utc + timedelta(seconds=1)),
            replace(lease, lease_seconds=9),
        )
        for forged in mutations:
            with self.subTest(forged=forged):
                with self.assertRaises(LeaseRejected):
                    self.store.complete(forged, RESULT, T0 + timedelta(seconds=2))

    def test_two_connections_claim_exactly_once_under_contention(self):
        self.submit()
        barrier = threading.Barrier(2)
        leases = []
        errors = []

        def run(worker):
            try:
                with TaskStore(self.path) as other:
                    barrier.wait(timeout=5)
                    leases.append(other.claim(worker, T0, 5))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(name,)) for name in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(errors)
        self.assertEqual(sum(value is not None for value in leases), 1)

    def test_expired_lease_reclaimed_old_owner_fenced(self):
        self.submit(max_attempts=2)
        old = self.store.claim("worker_a", T0, 5)
        new = self.store.claim("worker_b", T0 + timedelta(seconds=5), 5)
        self.assertEqual(new.fencing_token, old.fencing_token + 1)
        for operation in (
            lambda: self.store.renew(old, T0 + timedelta(seconds=6)),
            lambda: self.store.complete(old, RESULT, T0 + timedelta(seconds=6)),
            lambda: self.store.fail(old, "failure", True, T0 + timedelta(seconds=6)),
        ):
            with self.assertRaises(LeaseRejected):
                operation()
        self.assertEqual(self.store.complete(new, RESULT, T0 + timedelta(seconds=6))["status"], "DONE")

    def test_expired_lease_reclaim_survives_process_restart(self):
        self.submit(max_attempts=2)
        old = self.store.claim("worker_a", T0, 3)
        self.store.close()
        with TaskStore(self.path) as restarted:
            new = restarted.claim("worker_b", T0 + timedelta(seconds=3), 3)
            self.assertEqual(new.fencing_token, 2)
            with self.assertRaises(LeaseRejected):
                restarted.complete(old, RESULT, T0 + timedelta(seconds=4))

    def test_expired_final_attempt_is_unresolved_not_success(self):
        self.submit(max_attempts=1)
        self.store.claim("worker_a", T0, 2)
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=2), 2))
        row = self.store.inspect("work_a")
        self.assertEqual(row["status"], "UNRESOLVED")
        self.assertIsNone(row["result_sha256"])

    def test_renew_requires_current_lease_and_invalidates_old_handle(self):
        self.submit()
        old = self.store.claim("worker_a", T0, 3)
        new = self.store.renew(old, T0 + timedelta(seconds=1))
        self.assertGreater(new.expires_at_utc, old.expires_at_utc)
        with self.assertRaises(LeaseRejected):
            self.store.complete(old, RESULT, T0 + timedelta(seconds=2))
        self.store.complete(new, RESULT, T0 + timedelta(seconds=2))

    def test_pause_and_resume_fence_inflight_leases(self):
        self.submit()
        lease = self.store.claim("worker_a", T0, 5)
        paused = self.store.request_pause("work_a", "operator pause", T0 + timedelta(seconds=1))
        self.assertEqual(paused["status"], "PAUSED")
        self.assertFalse(paused["worker_stop_confirmed"])
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=2), 5))
        with self.assertRaises(LeaseRejected):
            self.store.complete(lease, RESULT, T0 + timedelta(seconds=2))
        self.store.resume("work_a", T0 + timedelta(seconds=3))
        new = self.store.claim("worker_b", T0 + timedelta(seconds=4), 5)
        self.assertGreater(new.fencing_token, lease.fencing_token)

    def test_pause_resume_does_not_reset_exhausted_attempt_budget(self):
        self.submit(max_attempts=1)
        old = self.store.claim("worker_a", T0, 5)
        self.store.request_pause("work_a", "pause", T0 + timedelta(seconds=1))
        resumed = self.store.resume("work_a", T0 + timedelta(seconds=2))
        self.assertEqual(resumed["status"], "UNRESOLVED")
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=3), 5))
        self.assertEqual(self.store.inspect("work_a")["attempts"], 1)
        with self.assertRaises(LeaseRejected):
            self.store.complete(old, RESULT, T0 + timedelta(seconds=4))

    def test_cancel_fences_inflight_without_claiming_worker_stopped(self):
        self.submit()
        old = self.store.claim("worker_a", T0, 5)
        cancelled = self.store.cancel("work_a", "operator cancel", T0 + timedelta(seconds=1))
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertFalse(cancelled["worker_stop_confirmed"])
        with self.assertRaises(LeaseRejected):
            self.store.complete(old, RESULT, T0 + timedelta(seconds=2))
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=5), 5))

    def test_dependencies_require_prior_registration_and_completion(self):
        with self.assertRaises(ValueError):
            self.submit("work_b", depends_on=("missing",))
        with self.assertRaises(ValueError):
            self.submit("work_b", depends_on=("work_b",))
        self.submit("work_a")
        self.submit("work_b", depends_on=("work_a",))
        self.assertEqual(self.store.claim("worker_a", T0, 5).task_id, "work_a")
        lease = self.store.inspect("work_a")
        self.assertEqual(lease["status"], "LEASED")
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=1), 5))

    def test_dependency_unblocks_on_completion(self):
        self.submit("work_a")
        self.submit("work_b", depends_on=("work_a",))
        first = self.store.claim("worker_a", T0, 5)
        self.store.complete(first, RESULT, T0 + timedelta(seconds=1))
        second = self.store.claim("worker_b", T0 + timedelta(seconds=2), 5)
        self.assertEqual(second.task_id, "work_b")

    def test_retry_backoff_is_bounded_and_dead_letters(self):
        self.submit(max_attempts=2)
        first = self.store.claim("worker_a", T0, 5)
        pending = self.store.fail(first, "transient", True, T0 + timedelta(seconds=1))
        self.assertEqual(pending["status"], "PENDING")
        self.assertIsNone(self.store.claim("worker_b", T0 + timedelta(seconds=1), 5))
        second = self.store.claim("worker_b", T0 + timedelta(seconds=2), 5)
        self.assertIsNotNone(second)
        final = self.store.fail(second, "still_failing", True, T0 + timedelta(seconds=3))
        self.assertEqual(final["status"], "DEAD")
        self.assertIsNone(self.store.claim("worker_c", T0 + timedelta(seconds=30), 5))

    def test_clock_rollback_is_rejected(self):
        self.submit()
        self.store.claim("worker_a", T0 + timedelta(seconds=10), 5)
        with self.assertRaises(ValueError):
            self.store.claim("worker_b", T0 + timedelta(seconds=9), 5)

    def test_far_future_injected_clock_cannot_poison_persistent_watermark(self):
        self.submit()
        with self.assertRaises(ValueError):
            self.store.request_pause(
                "work_a", "clock-test", datetime(9999, 1, 1, tzinfo=timezone.utc)
            )
        self.assertEqual(self.store.inspect("work_a")["status"], "PENDING")
        self.store.request_pause("work_a", "pause", T0)
        self.store.close()
        with TaskStore(self.path) as reopened:
            self.assertEqual(reopened.resume("work_a", T0 + timedelta(seconds=1))["status"], "PENDING")

    def test_close_disallows_operations_and_uses_context_manager(self):
        self.store.close()
        with self.assertRaises(RuntimeError):
            self.store.inspect("work_a")
        with TaskStore(self.path) as other:
            other.submit("work_a", "mission_a", 1, DIGEST)
        self.assertTrue(self.path.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_new_sqlite_file_private_even_with_permissive_umask(self):
        self.store.close()
        with tempfile.TemporaryDirectory() as isolated:
            path = Path(isolated) / "private.sqlite3"
            old_mask = os.umask(0o022)
            try:
                with TaskStore(path) as db:
                    db.submit("work_a", "mission_a", 1, DIGEST)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                os.umask(old_mask)


if __name__ == "__main__":
    unittest.main()
