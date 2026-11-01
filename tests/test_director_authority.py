"""Offline authority reference: schema matching must never become a grant."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from runtime.director.authority import evaluate_reference_authority


NOW = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)


def request(**changes):
    fields = {
        "mission_id": "mission-1",
        "mission_revision": 3,
        "dossier_sha256": "c" * 64,
        "proposal_id": "proposal-1",
        "actor_id": "actor-1",
        "action_class": "write-record",
        "target_id": "local-record-1",
        "parameters_sha256": "a" * 64,
        "audience": "lappy-director",
    }
    fields.update(changes)
    return fields


def grant(**changes):
    fields = {
        "policy_version": "director.authority.v1",
        "grant_id": "grant-1",
        "issuer_id": "claimed-human",
        "subject_actor_id": "actor-1",
        "mission_id": "mission-1",
        "mission_revision": 3,
        "dossier_sha256": "c" * 64,
        "proposal_id": "proposal-1",
        "action_class": "write-record",
        "target_id": "local-record-1",
        "parameters_sha256": "a" * 64,
        "audience": "lappy-director",
        "not_before_utc": "2026-11-01T13:00:00Z",
        "expires_at_utc": "2026-11-01T15:00:00Z",
        "revocation_epoch": 7,
        "revoked_at_utc": None,
        "nonce": "0123456789abcdef0123456789abcdef",
    }
    fields.update(changes)
    return fields


def assess(action=..., envelope=..., *, now=NOW, epoch=7):
    return evaluate_reference_authority(
        request() if action is ... else action,
        grant() if envelope is ... else envelope,
        now_utc=now,
        declared_revocation_epoch=epoch,
    )


class DirectorAuthorityReferenceTests(unittest.TestCase):
    def assert_never_granted(self, receipt):
        self.assertIn(receipt["status"], ("DENIED", "REQUIRES_HOST_ATTESTATION"))
        self.assertEqual(receipt["authorization_status"], "NOT_GRANTED")
        self.assertEqual(receipt["effect_status"], "NOT_EXECUTED")
        self.assertFalse(receipt["verified_grant"])
        json.dumps(receipt, sort_keys=True)

    def test_matching_untrusted_envelope_requires_host_attestation(self):
        receipt = assess()
        self.assert_never_granted(receipt)
        self.assertEqual(receipt["status"], "REQUIRES_HOST_ATTESTATION")
        self.assertIn("ISSUER_NOT_AUTHENTICATED", receipt["reason_codes"])
        self.assertIn("NONCE_REPLAY_NOT_CHECKED", receipt["reason_codes"])
        self.assertIn("REVOCATION_LEDGER_NOT_CHECKED", receipt["reason_codes"])

    def assert_denied(self, receipt):
        self.assert_never_granted(receipt)
        self.assertEqual(receipt["status"], "DENIED")
        self.assertTrue(receipt["reason_codes"])

    def test_missing_grant_explicit(self):
        self.assert_denied(evaluate_reference_authority(request(), None, now_utc=NOW, declared_revocation_epoch=7))

    def test_action_subject_fields_are_all_exactly_bound(self):
        mutations = {
            "mission_id": "mission-2", "mission_revision": 4,
            "dossier_sha256": "d" * 64,
            "proposal_id": "proposal-2", "subject_actor_id": "actor-2",
            "action_class": "delete", "target_id": "other-record",
            "parameters_sha256": "b" * 64, "audience": "vn1-director",
        }
        for field, changed in mutations.items():
            with self.subTest(field=field):
                receipt = assess(envelope=grant(**{field: changed}))
                self.assert_denied(receipt)
                self.assertIn("ACTION_SUBJECT_MISMATCH", receipt["reason_codes"])

    def test_changed_parameters_target_audience_denied_even_with_admin_claim(self):
        for field, changed in (("parameters_sha256", "b" * 64),
                               ("target_id", "other-record"),
                               ("audience", "untrusted-host")):
            with self.subTest(field=field):
                receipt = assess(envelope=grant(issuer_id="admin", **{field: changed}))
                self.assert_denied(receipt)

    def test_changed_dossier_with_same_mission_revision_denied(self):
        changed_dossier = request(dossier_sha256="d" * 64)
        receipt = assess(action=changed_dossier, envelope=grant())
        self.assert_denied(receipt)
        self.assertIn("ACTION_SUBJECT_MISMATCH", receipt["reason_codes"])

    def test_administrator_or_approval_claims_are_not_authorization(self):
        for claim in ({"role": "ADMIN"}, {"approved": True}, {"issuer_role": "root"}):
            with self.subTest(claim=claim):
                receipt = assess(envelope=grant(**claim))
                self.assert_denied(receipt)
                self.assertIn("GRANT_SCHEMA_INVALID", receipt["reason_codes"])
        receipt = assess(envelope=grant(issuer_id="administrator"))
        self.assertEqual(receipt["status"], "REQUIRES_HOST_ATTESTATION")
        self.assert_never_granted(receipt)

    def test_malformed_requests_and_envelopes_are_denied(self):
        invalid = (None, [], "grant", {}, {"approved": True})
        for specimen in invalid:
            with self.subTest(specimen=specimen):
                self.assert_denied(assess(action=specimen, envelope=grant()))
                self.assert_denied(assess(action=request(), envelope=specimen))
        self.assert_denied(assess(action=request(role="admin")))
        self.assert_denied(assess(envelope=grant(grant_id="bad identifier with spaces")))
        self.assert_denied(assess(action=request(mission_revision=True)))
        self.assert_denied(assess(envelope=grant(mission_revision=True)))
        self.assert_denied(assess(action=request(parameters_sha256="A" * 64)))
        self.assert_denied(assess(envelope=grant(nonce="predictable")))

    def test_version_issued_times_and_expiry_fail_closed(self):
        for mutation, expected in (
            ({"policy_version": "director.authority.v0"}, "POLICY_VERSION_UNSUPPORTED"),
            ({"not_before_utc": "2026-11-01T14:00:01Z"}, "GRANT_NOT_YET_VALID"),
            ({"expires_at_utc": "2026-11-01T14:00:00Z"}, "GRANT_EXPIRED"),
            ({"expires_at_utc": "not-a-date"}, "GRANT_SCHEMA_INVALID"),
            ({"not_before_utc": "2026-11-01T12:00:00"}, "GRANT_SCHEMA_INVALID"),
            ({"not_before_utc": "2026-11-01T17:00:00Z", "expires_at_utc": "2026-11-01T16:00:00Z"}, "GRANT_TIME_WINDOW_INVALID"),
            ({"revoked_at_utc": "2026-11-01T13:30:00Z"}, "GRANT_MARKED_REVOKED"),
        ):
            with self.subTest(mutation=mutation):
                receipt = assess(envelope=grant(**mutation))
                self.assert_denied(receipt)
                self.assertIn(expected, receipt["reason_codes"])

    def test_declared_epoch_cannot_substitute_for_durable_revocation_ledger(self):
        self.assert_denied(assess(envelope=grant(revocation_epoch=6)))
        receipt = assess(envelope=grant(revocation_epoch=8))
        self.assert_denied(receipt)
        self.assertIn("REVOCATION_EPOCH_MISMATCH", receipt["reason_codes"])
        matching = assess(envelope=grant(revocation_epoch=7))
        self.assertEqual(matching["status"], "REQUIRES_HOST_ATTESTATION")
        self.assertIn("REVOCATION_LEDGER_NOT_CHECKED", matching["reason_codes"])

    def test_nonce_shape_is_not_replay_protection(self):
        # Determinism is intentional: this is not an in-memory nonce ledger.
        # A live host must consume a nonce atomically before any effect.
        first = assess()
        second = assess()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "REQUIRES_HOST_ATTESTATION")
        self.assert_never_granted(second)

    def test_time_boundaries_are_explicit_and_fail_closed(self):
        start = datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(assess(now=start)["status"], "REQUIRES_HOST_ATTESTATION")
        expired = assess(now=end)
        self.assert_denied(expired)
        self.assertIn("GRANT_EXPIRED", expired["reason_codes"])
        self.assert_denied(assess(envelope=grant(revoked_at_utc="invalid")))

    def test_bad_host_context_is_rejected_as_caller_programming_error(self):
        with self.assertRaises(ValueError):
            assess(now=NOW.replace(tzinfo=None))
        for bad_epoch in (-1, True, "7"):
            with self.assertRaises(ValueError):
                assess(epoch=bad_epoch)

    def test_reference_evaluation_has_no_io_or_effects(self):
        with patch("builtins.open", side_effect=AssertionError("filesystem access")), \
             patch("socket.socket", side_effect=AssertionError("socket access")), \
             patch("subprocess.run", side_effect=AssertionError("process execution")):
            self.assert_never_granted(assess())
            self.assert_denied(assess(envelope=grant(target_id="different")))


if __name__ == "__main__":
    unittest.main()
