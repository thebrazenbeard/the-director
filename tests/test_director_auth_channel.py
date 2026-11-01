"""Adversarial contract tests for the isolated authenticated-envelope adapter."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from runtime.director.auth_channel import AuthenticatedChannel


NOW = 1_800_000_000
KEYS = {"director": b"director-secret-with-at-least-32-byte-entropy-0001",
        "analyst": b"analyst-secret-with-at-least-32-byte-entropy-0002"}


class AuthenticatedChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "authenticated.sqlite3"
        self.channel = AuthenticatedChannel(self.path, KEYS)
        self.addCleanup(self.channel.close)

    def envelope(self, **kwargs):
        params = dict(sender_id="director", recipient_id="analyst", kind="MISSION_ASSIGNMENT",
                      payload={"goal": "inspect", "count": 1}, issued_at=NOW,
                      expires_at=NOW + 300, message_id="mission.01", nonce="a" * 32)
        params.update(kwargs)
        return self.channel.seal(**params)

    def verify(self, raw, **kwargs):
        opts = dict(expected_recipient="analyst", now_utc=NOW + 1)
        opts.update(kwargs)
        return self.channel.verify(raw, **opts)

    def test_signed_message_is_canonical_and_bounded_no_effect_authority(self):
        raw = self.envelope(payload={"z": [1, {"b": "hello", "a": "world"}], "a": True})
        envelope = json.loads(raw)
        self.assertEqual(envelope["protocol"], "director.authenticated-envelope.v1")
        self.assertEqual(envelope["sender_id"], "director")
        unsigned = {k: v for k, v in envelope.items() if k != "mac"}
        manual = hmac.new(
            KEYS["director"],
            b"the-director/authenticated-envelope/v1\x00" + json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            ).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(envelope["mac"], manual)
        verified = self.verify(raw)
        self.assertTrue(verified["sender_authenticated"])
        self.assertEqual(verified["authenticated_sender_id"], "director")
        self.assertEqual(verified["recipient_id"], "analyst")
        self.assertEqual(verified["payload"]["z"][1]["a"], "world")
        self.assertEqual(verified["effect_authority"], "NOT_GRANTED")
        self.assertEqual(verified["effect_status"], "NOT_EXECUTED")

    def test_canonical_field_order_independent_but_message_fields_bound(self):
        raw1 = self.envelope(payload={"a": 2, "b": 3})
        raw2 = self.envelope(payload={"b": 3, "a": 2})
        self.assertEqual(raw1, raw2)
        envelope = json.loads(raw1)
        for field, mutated in (("recipient_id", "intruder"), ("sender_id", "analyst"),
                               ("message_id", "changed"), ("kind", "MISSION_REPORT"),
                               ("nonce", "b" * 32), ("expires_at", NOW + 200)):
            changed = dict(envelope)
            changed[field] = mutated
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(json.dumps(changed).encode("utf-8"))
        changed = dict(envelope)
        changed["payload"] = {"goal": "execute", "count": 1}
        with self.assertRaisesRegex(ValueError, "MAC|signature"):
            self.verify(json.dumps(changed).encode("utf-8"))

    def test_replay_is_rejected_after_restart(self):
        raw = self.envelope()
        self.verify(raw)
        with self.assertRaisesRegex(ValueError, "replay"):
            self.verify(raw)
        self.channel.close()
        with AuthenticatedChannel(self.path, KEYS) as reopened:
            with self.assertRaisesRegex(ValueError, "replay"):
                reopened.verify(raw, expected_recipient="analyst", now_utc=NOW + 2)

    def test_signature_inspection_is_pure_and_requires_atomic_caller_replay_admission(self):
        raw = self.envelope()
        first = self.channel.inspect_signature(raw, expected_recipient="analyst",
                                               now_utc=NOW + 1)
        second = self.channel.inspect_signature(raw, expected_recipient="analyst",
                                                now_utc=NOW + 1)
        self.assertEqual(first, second)
        self.assertTrue(first["sender_authenticated"])
        self.assertFalse(first["replay_admitted"])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM authenticated_replay").fetchone()[0], 0)
        consumed = self.verify(raw)
        self.assertTrue(consumed["replay_admitted"])
        with self.assertRaisesRegex(ValueError, "replay"):
            self.verify(raw)
        # Inspection itself still doesn't enforce replay, even after another
        # adapter has consumed it. The coordinator MUST bind that identity to
        # its own report row in a single transaction.
        self.assertFalse(self.channel.inspect_signature(
            raw, expected_recipient="analyst", now_utc=NOW + 1)["replay_admitted"])

    def test_inspection_enforces_key_route_expiry_revocation_without_consuming(self):
        raw = self.envelope()
        self.assertTrue(self.channel.has_sender_key("director"))
        self.assertFalse(self.channel.has_sender_key("unconfigured"))
        with self.assertRaises(ValueError):
            self.channel.has_sender_key("invalid id")
        for opts in (dict(expected_recipient="intruder", now_utc=NOW + 1),
                     dict(expected_recipient="analyst", now_utc=NOW + 300),
                     dict(expected_recipient="analyst", now_utc=NOW - 1)):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.channel.inspect_signature(raw, **opts)
        tampered = json.loads(raw)
        tampered["payload"] = {"goal": "tampered"}
        with self.assertRaisesRegex(ValueError, "MAC|signature"):
            self.channel.inspect_signature(json.dumps(tampered).encode("ascii"),
                                           expected_recipient="analyst", now_utc=NOW + 1)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM authenticated_replay").fetchone()[0], 0)
        self.channel.revoke_sender("director")
        self.assertFalse(self.channel.has_sender_key("director"))
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.channel.inspect_signature(raw, expected_recipient="analyst", now_utc=NOW + 1)

    def test_distinct_message_same_nonce_and_distinct_nonce_same_id_rejected(self):
        self.verify(self.envelope())
        for raw in (self.envelope(message_id="mission.02"),
                    self.envelope(nonce="b" * 32)):
            with self.assertRaisesRegex(ValueError, "replay"):
                self.verify(raw)

    def test_wrong_recipient_expires_or_future_not_consumed(self):
        raw = self.envelope()
        for opts in (dict(expected_recipient="director"), dict(now_utc=NOW + 300),
                     dict(now_utc=NOW - 1)):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.verify(raw, **opts)
        self.assertTrue(self.verify(raw)["sender_authenticated"])

    def test_invalid_ttl_and_untrusted_clock_values(self):
        for change in (dict(expires_at=NOW), dict(expires_at=NOW + 86401),
                       dict(issued_at=True), dict(expires_at="tomorrow"),
                       dict(issued_at=-1)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.envelope(**change)
        with self.assertRaises(ValueError):
            self.verify(self.envelope(), now_utc=True)

    def test_wrong_key_and_unknown_sender_rejected(self):
        raw = self.envelope()
        self.channel.close()
        with AuthenticatedChannel(self.path, {"director": b"completely-different-secret-at-least-32bytes"}) as different:
            with self.assertRaisesRegex(ValueError, "MAC|signature"):
                different.verify(raw, expected_recipient="analyst", now_utc=NOW + 1)
        with AuthenticatedChannel(self.path, {"analyst": KEYS["analyst"]}) as unknown:
            with self.assertRaisesRegex(ValueError, "no configured|not configured"):
                unknown.verify(raw, expected_recipient="analyst", now_utc=NOW + 1)

    def test_invalid_signature_does_not_poison_replay_state(self):
        legitimate = self.envelope()
        corrupted = json.loads(legitimate)
        corrupted["payload"] = {"goal": "pretend to be director"}
        with self.assertRaisesRegex(ValueError, "MAC|signature"):
            self.verify(json.dumps(corrupted).encode("utf-8"))
        self.assertTrue(self.verify(legitimate)["sender_authenticated"])

    def test_sender_specific_nonce_namespace_and_authenticated_identity(self):
        first = self.envelope()
        other = self.envelope(sender_id="analyst", recipient_id="director",
                              kind="MISSION_REPORT", payload={"result": "received"})
        self.verify(first)
        second = self.channel.verify(other, expected_recipient="director", now_utc=NOW + 1)
        self.assertEqual(second["authenticated_sender_id"], "analyst")

    def test_durable_revocation_and_rotation(self):
        raw = self.envelope()
        self.channel.revoke_sender("director")
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.verify(raw)
        self.channel.close()
        with AuthenticatedChannel(self.path, KEYS) as reopened:
            with self.assertRaisesRegex(ValueError, "revoked"):
                reopened.verify(raw, expected_recipient="analyst", now_utc=NOW + 1)
        with AuthenticatedChannel(Path(self.temp.name) / "rotation.sqlite3", KEYS) as rotating:
            old = rotating.seal(sender_id="director", recipient_id="analyst", kind="MISSION_REPORT",
                                payload={}, issued_at=NOW, expires_at=NOW + 300)
            rotating.rotate_key("director", b"rotated-secret-with-at-least-32-byte-entropy-0003")
            with self.assertRaisesRegex(ValueError, "MAC|signature"):
                rotating.verify(old, expected_recipient="analyst", now_utc=NOW + 1)
            new = rotating.seal(sender_id="director", recipient_id="analyst", kind="MISSION_REPORT",
                                payload={}, issued_at=NOW, expires_at=NOW + 300)
            self.assertEqual(rotating.verify(new, expected_recipient="analyst", now_utc=NOW + 1)["authenticated_sender_id"], "director")

    def test_malformed_unknown_duplicate_and_oversize_envelopes_rejected(self):
        raw = self.envelope()
        for invalid in (b"not json", b"{\"mac\":\"x\",\"mac\":\"y\"}",
                        json.dumps({**json.loads(raw), "grant": "ADMIN"}).encode(),
                        b" " * 60000):
            with self.subTest(raw=invalid[:30]), self.assertRaises(ValueError):
                self.verify(invalid)

    def test_invalid_nested_payload_and_keys(self):
        for payload in ({"x": float("nan")}, {"x": object()}, {"x": "a" * 30000},
                        {"x": [0] * 1000}, {"x": "\ud800"}, {"\ud800": "bad-key"}):
            with self.subTest(payload=str(payload)[:20]), self.assertRaises(ValueError):
                self.envelope(payload=payload)
        with self.assertRaises(ValueError):
            AuthenticatedChannel(Path(self.temp.name) / "invalid.sqlite", {"director": b"short"})
        with self.assertRaisesRegex(ValueError, "distinct"):
            AuthenticatedChannel(Path(self.temp.name) / "duplicate.sqlite",
                                 {"director": b"x" * 32, "analyst": b"x" * 32})
        with self.assertRaisesRegex(ValueError, "unique"):
            self.channel.rotate_key("director", KEYS["analyst"])

    def test_connection_close_and_posix_mode_and_no_secret_persistence(self):
        raw = self.envelope()
        self.verify(raw)
        self.channel.close()
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT sql FROM sqlite_master").fetchall()
        text = str(rows)
        self.assertNotIn(KEYS["director"].decode(), text)
        self.assertNotIn(KEYS["director"], self.path.read_bytes())
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(RuntimeError):
            self.channel.verify(raw, expected_recipient="analyst", now_utc=NOW + 1)

    def test_no_in_memory_ledger_or_symlink(self):
        with self.assertRaises(ValueError):
            AuthenticatedChannel(":memory:", KEYS)
        if hasattr(os, "symlink"):
            path = Path(self.temp.name) / "link.sqlite3"
            try:
                path.symlink_to(self.path)
            except OSError:
                return
            with self.assertRaises(ValueError):
                AuthenticatedChannel(path, KEYS)

    @unittest.skipUnless(os.name == "posix", "POSIX file-mode admission boundary")
    def test_preexisting_replay_ledger_must_be_owner_only(self):
        path = Path(self.temp.name) / "insecure-existing.sqlite3"
        path.touch()
        for mode in (0o644, 0o640, 0o666):
            with self.subTest(mode=oct(mode)):
                path.chmod(mode)
                with self.assertRaisesRegex(ValueError, "owner-only"):
                    AuthenticatedChannel(path, KEYS)
        path.chmod(0o600)
        with AuthenticatedChannel(path, KEYS) as admitted:
            raw = admitted.seal(sender_id="director", recipient_id="analyst",
                                kind="MISSION_ASSIGNMENT", payload={},
                                issued_at=NOW, expires_at=NOW + 300)
            self.assertTrue(admitted.verify(raw, expected_recipient="analyst",
                                            now_utc=NOW + 1)["sender_authenticated"])

    @unittest.skipUnless(os.name == "posix", "POSIX SQLite sidecar-mode admission boundary")
    def test_insecure_wal_or_shm_sidecar_is_rejected(self):
        path = Path(self.temp.name) / "sidecar.sqlite3"
        path.touch(mode=0o600)
        for suffix in ("-wal", "-shm"):
            member = path.with_name(path.name + suffix)
            member.touch()
            member.chmod(0o666)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "owner-only"):
                AuthenticatedChannel(path, KEYS)
            member.unlink()

    @unittest.skipUnless(os.name == "posix", "POSIX symlink rejection")
    def test_preexisting_wal_symlink_is_rejected(self):
        path = Path(self.temp.name) / "symlink-sidecar.sqlite3"
        path.touch(mode=0o600)
        member = path.with_name(path.name + "-wal")
        member.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            AuthenticatedChannel(path, KEYS)


if __name__ == "__main__":
    unittest.main()
