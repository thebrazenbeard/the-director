"""Synthetic source tests, NOT distinct-principal host qualification."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from runtime.director.broker import Broker, BrokerError, MAX_BYTES
from runtime.director.broker_socket import read_request, require_linux, serve_connection, serve_foreground


class FakeStream:
    def __init__(self, data):
        self.data = data

    def settimeout(self, timeout):
        pass

    def recv(self, count):
        result, self.data = self.data[:count], self.data[count:]
        return result


@unittest.skipUnless(sys.platform == "linux", "Linux source tests only; Windows unsupported")
class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "broker.db"
        self.broker = Broker(self.path)
        self.uid = os.getuid() + 10001
        self.broker.register("alpha", self.uid)
        self.broker.register("beta", self.uid + 1)
        self.sequence = 0

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def request(self, op="POLL", **extra):
        self.sequence += 1
        if op in ("ACK_RECEIPT", "REPORT_CLAIM") and "delivery_token" not in extra:
            row = self.broker.db.execute("SELECT delivery_token FROM commands WHERE id=?", (extra.get("command_id"),)).fetchone()
            extra["delivery_token"] = row[0] if row and row[0] else "0" * 64
        return dict(version=1, op=op, request_id=f"r{self.sequence}", epoch=self.broker.epoch, **extra)

    def admit(self):
        return self.broker.admit("alpha", "c1", "n1", {"assertion":"data only"}, self.broker.epoch)

    def handle(self, op="POLL", **extra):
        return self.broker.handle(self.uid, self.request(op, **extra))

    def test_crash_restart_receipt_replay_and_claim(self):
        self.admit()
        poll = self.request()
        delivered = self.broker.handle(self.uid, poll)
        self.broker.close()
        self.broker = Broker(self.path)
        self.assertEqual(delivered, self.broker.handle(self.uid, poll))
        ack = self.handle("ACK_RECEIPT", command_id="c1")
        self.assertEqual("RECEIVED", ack["status"])
        self.assertFalse(ack["effect_verified"])
        self.broker.close()
        self.broker = Broker(self.path)
        claim = self.handle("REPORT_CLAIM", command_id="c1", claim="SUCCEEDED")
        self.assertEqual("CLAIM_REPORTED", claim["status"])
        self.assertFalse(claim["effect_verified"])
        self.assertEqual("SUCCEEDED", self.handle("GET_RECEIPT", command_id="c1")["claim"])

    def test_admission_replay_and_conflicts(self):
        digest = self.admit()
        self.assertEqual(digest, self.admit())
        for command, nonce, payload in (("c1","n2",{}),("c2","n1",{}),("c1","n1",{})):
            with self.assertRaises(BrokerError):
                self.broker.admit("alpha", command, nonce, payload, self.broker.epoch)

    def test_request_replay_conflict(self):
        self.admit()
        request = self.request()
        self.broker.handle(self.uid, request)
        request.update(op="GET_RECEIPT", command_id="c1")
        with self.assertRaises(BrokerError):
            self.broker.handle(self.uid, request)

    def test_revoke_before_admission_and_claim(self):
        self.admit()
        poll = self.request()
        self.broker.handle(self.uid, poll)
        self.handle("ACK_RECEIPT", command_id="c1")
        self.broker.revoke("alpha")
        for request in (poll, self.request("REPORT_CLAIM", command_id="c1", claim="SUCCEEDED")):
            with self.assertRaises(BrokerError):
                self.broker.handle(self.uid, request)
        with self.assertRaises(BrokerError):
            self.admit()

    def test_cancel_fences_cached_delivery_and_survives_restart(self):
        self.admit()
        poll = self.request()
        self.broker.handle(self.uid, poll)
        self.broker.cancel("c1")
        self.broker.close()
        self.broker = Broker(self.path)
        with self.assertRaises(BrokerError):
            self.broker.handle(self.uid, poll)
        self.assertEqual("REVOKED", self.handle("GET_RECEIPT", command_id="c1")["status"])

    def test_same_root_duplicate_and_wrong_identity(self):
        for uid in (0, os.getuid(), self.uid, True, -1):
            with self.assertRaises(BrokerError):
                self.broker.register("other", uid)
        for uid in (os.getuid(), self.uid+2, "10001"):
            with self.assertRaises(BrokerError):
                self.broker.handle(uid, self.request())

    def test_no_cross_worker_ack_or_receipt(self):
        self.admit()
        self.handle()
        for operation in ("ACK_RECEIPT", "GET_RECEIPT", "REPORT_CLAIM"):
            extra = {"claim":"SUCCEEDED"} if operation == "REPORT_CLAIM" else {}
            with self.assertRaises(BrokerError):
                self.broker.handle(self.uid+1, self.request(operation, command_id="c1", **extra))

    def test_schema_spoofs_and_no_admission_ipc(self):
        requests = [self.request(worker_id="beta"), self.request(uid=self.uid), self.request("ADMIT"), [], self.request()]
        requests[-1]["version"] = True
        for request in requests:
            with self.assertRaises(BrokerError):
                self.broker.handle(self.uid, request)

    def test_no_ack_before_delivery_or_claim_before_receipt(self):
        self.admit()
        with self.assertRaises(BrokerError):
            self.handle("ACK_RECEIPT", command_id="c1")
        self.handle()
        with self.assertRaises(BrokerError):
            self.handle("REPORT_CLAIM", command_id="c1", claim="SUCCEEDED")

    def test_lease_expiry_and_redelivery(self):
        self.admit()
        self.handle()
        self.broker.db.execute("UPDATE commands SET lease_until=0")
        with self.assertRaises(BrokerError):
            self.handle("ACK_RECEIPT", command_id="c1")
        self.assertEqual("c1", self.handle()["command"]["id"])

    def test_transaction_rollback_injected_failure(self):
        self.admit()
        self.broker.db.execute("CREATE TRIGGER fail_replay BEFORE INSERT ON replay BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.handle()
        self.assertEqual("QUEUED", self.broker.db.execute("SELECT status FROM commands").fetchone()[0])
        self.broker.db.execute("DROP TRIGGER fail_replay")
        self.assertEqual("c1", self.handle()["command"]["id"])

    def test_trigger_rollback_preserves_original_error(self):
        self.admit()
        self.broker.db.execute("CREATE TRIGGER abort_delivery BEFORE UPDATE ON commands BEGIN SELECT RAISE(ROLLBACK, 'original trigger failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "original trigger failure"):
            self.handle()
        self.assertFalse(self.broker.db.in_transaction)
        self.broker.db.execute("DROP TRIGGER abort_delivery")
        self.assertEqual("c1", self.handle()["command"]["id"])

    def test_legacy_schema_refusal_does_not_mutate_database(self):
        path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE commands (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO commands VALUES ('preserved')")
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaisesRegex(BrokerError, "incompatible broker schema"):
            Broker(path)
        self.assertEqual(before, path.read_bytes())
        with sqlite3.connect(path) as conn:
            self.assertEqual([("commands",)], conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            self.assertEqual([("preserved",)], conn.execute("SELECT * FROM commands").fetchall())

    def test_foreground_database_lock_denied_then_recovers(self):
        # Synthetic peer credentials; this is not host isolation evidence.
        self.admit()
        request = self.request()
        path = Path(self.temp.name) / "mock_socket"
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.bind.side_effect = lambda value: path.touch(mode=0o600)
        clients = []
        lock = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(lock.close)
        lock.execute("BEGIN IMMEDIATE")
        def accept():
            if clients:
                lock.execute("ROLLBACK")
            payload = json.dumps(request).encode()
            stream = FakeStream(struct.pack("!I", len(payload)) + payload)
            client = MagicMock()
            client.__enter__.return_value = client
            client.recv.side_effect = stream.recv
            client.getsockopt.return_value = struct.pack("3i", 123, self.uid, 123)
            clients.append(client)
            return client, None
        listener.accept.side_effect = accept
        with patch("runtime.director.broker_socket.socket.socket", return_value=listener):
            serve_foreground(self.broker, path, threading.Event(), max_connections=2)
        replies = [json.loads(client.sendall.call_args.args[0][4:]) for client in clients]
        self.assertEqual({"version": 1, "error": "DENIED"}, replies[0])
        self.assertEqual("c1", replies[1]["command"]["id"])
        self.assertFalse(path.exists())

    def test_storage_error_denied_but_cancellation_propagates(self):
        for error in (sqlite3.DatabaseError("private storage details"), KeyboardInterrupt()):
            client = MagicMock()
            client.getsockopt.return_value = struct.pack("3i", 123, self.uid, 123)
            with patch("runtime.director.broker_socket.read_request", return_value=self.request()), patch.object(self.broker, "handle", side_effect=error):
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        serve_connection(self.broker, client)
                    client.sendall.assert_not_called()
                else:
                    serve_connection(self.broker, client)
                    self.assertEqual({"version": 1, "error": "DENIED"}, json.loads(client.sendall.call_args.args[0][4:]))

    def test_payload_bounds_nonfinite_and_stale_epoch(self):
        for payload in ("x"*MAX_BYTES, float("nan")):
            with self.assertRaises(BrokerError):
                self.broker.admit("alpha", "c1", "n1", payload, self.broker.epoch)
        request = self.request()
        request["epoch"] = 0
        with self.assertRaises(BrokerError):
            self.broker.handle(self.uid, request)

    def test_database_permissions_alias_and_link(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(BrokerError):
            Broker(self.path)
        os.chmod(self.path, 0o600)
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.path)
        with self.assertRaises(BrokerError):
            Broker(alias)
        alias.unlink()
        os.link(self.path, alias)
        with self.assertRaises(BrokerError):
            Broker(alias)

    def test_unprotected_parent(self):
        os.chmod(self.temp.name, 0o755)
        with self.assertRaises(BrokerError):
            Broker(self.path)
        os.chmod(self.temp.name, 0o700)

    def test_framing(self):
        for raw in (b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'['*2000+b']'*2000):
            with self.assertRaises(BrokerError):
                read_request(FakeStream(struct.pack("!I",len(raw))+raw), time.monotonic()+1)
        for size in (0, MAX_BYTES+1):
            with self.assertRaises(BrokerError):
                read_request(FakeStream(struct.pack("!I",size)), time.monotonic()+1)

    def test_truncation_deadline_and_valid_frame(self):
        raw = json.dumps(self.request()).encode()
        self.assertEqual("POLL", read_request(FakeStream(struct.pack("!I",len(raw))+raw),time.monotonic()+1)["op"])
        with self.assertRaises(BrokerError):
            read_request(FakeStream(b""),time.monotonic()-1)
        with self.assertRaises(BrokerError):
            read_request(FakeStream(struct.pack("!I",100)+b"x"),time.monotonic()+1)

    def test_real_kernel_same_uid_denied_not_host_qualification(self):
        left, right = socket.socketpair()
        with left, right:
            raw = json.dumps(self.request()).encode()
            try:
                left.sendall(struct.pack("!I",len(raw))+raw)
            except PermissionError:
                self.skipTest("sandbox denies AF_UNIX send; no transport qualification")
            serve_connection(self.broker,right)
            self.assertEqual("DENIED",read_request(left,time.monotonic()+1)["error"])

    def test_socket_existing_path_never_unlinked(self):
        path = Path(self.temp.name)/"socket"
        path.write_text("preserve")
        with self.assertRaises(BrokerError):
            serve_foreground(self.broker,path,threading.Event())
        self.assertEqual("preserve",path.read_text())

    def test_explicit_stopped_server_cleans_only_own_socket(self):
        stop = threading.Event()
        stop.set()
        path = Path(self.temp.name)/"socket"
        try:
            serve_foreground(self.broker,path,stop)
        except PermissionError:
            self.skipTest("sandbox denies AF_UNIX bind; no listener qualification")
        self.assertFalse(path.exists())

    def test_sidecar_alias_refused(self):
        sidecar = Path(str(self.path)+"-journal")
        sidecar.symlink_to(self.path)
        with self.assertRaises(BrokerError):
            Broker(self.path)

    def test_expired_cached_poll_is_not_reissued(self):
        self.admit()
        request = self.request()
        self.broker.handle(self.uid,request)
        with patch("runtime.director.broker.time.time", return_value=time.time()+60):
            with self.assertRaises(BrokerError):
                self.broker.handle(self.uid,request)

    def test_durable_ack_allows_unverified_claim_after_restart_and_expiry(self):
        self.admit()
        token = self.handle()["command"]["delivery_token"]
        ack = self.request("ACK_RECEIPT", command_id="c1", delivery_token=token)
        receipt = self.broker.handle(self.uid, ack)
        self.broker.close()
        self.broker = Broker(self.path)
        with patch("runtime.director.broker.time.time", return_value=time.time()+60):
            self.assertEqual(receipt, self.broker.handle(self.uid, ack))
            self.assertIsNone(self.handle()["command"])
            request = self.request("REPORT_CLAIM", command_id="c1", delivery_token=token, claim="SUCCEEDED")
            claim = self.broker.handle(self.uid,request)
            self.assertEqual("CLAIM_REPORTED", claim["status"])
            self.assertFalse(claim["effect_verified"])
            self.assertEqual(claim, self.broker.handle(self.uid,request))

    def test_old_delivery_token_cannot_ack_or_report_new_generation(self):
        self.admit()
        first = self.handle()["command"]["delivery_token"]
        self.broker.db.execute("UPDATE commands SET lease_until=0")
        second = self.handle()["command"]["delivery_token"]
        self.assertNotEqual(first,second)
        for operation in ("ACK_RECEIPT", "REPORT_CLAIM"):
            extra = {"claim":"SUCCEEDED"} if operation == "REPORT_CLAIM" else {}
            with self.assertRaises(BrokerError):
                self.handle(operation, command_id="c1", delivery_token=first, **extra)
        self.handle("ACK_RECEIPT", command_id="c1", delivery_token=second)
        self.assertFalse(self.handle("REPORT_CLAIM", command_id="c1", delivery_token=second, claim="UNKNOWN")["effect_verified"])

    def test_admission_validates_full_response_depth_and_nodes(self):
        payload = 0
        for _ in range(14):
            payload = [payload]
        self.broker.admit("alpha", "boundary", "boundary", payload, self.broker.epoch)
        self.assertEqual(payload, self.handle()["command"]["payload"])
        for bad in ([payload], [[payload]], [0]*8188):
            with self.assertRaises(BrokerError):
                self.broker.admit("alpha", "bad", "bad", bad, self.broker.epoch)
        self.admit()
        self.assertEqual("c1", self.handle()["command"]["id"])

    def test_default_server_not_exhausted_by_1000_denied_clients(self):
        # Source simulation only: no real bind or distinct-principal evidence.
        path = Path(self.temp.name)/"mock_socket"
        stop = threading.Event()
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.bind.side_effect = lambda value: path.touch(mode=0o600)
        count = [0]
        def accept():
            count[0] += 1
            self.assertEqual(0o660, path.stat().st_mode & 0o777)
            if count[0] == 1001:
                stop.set()
            return MagicMock(), None
        listener.accept.side_effect = accept
        with patch("runtime.director.broker_socket.socket.socket", return_value=listener), patch("runtime.director.broker_socket.serve_connection", side_effect=OSError("denied")):
            serve_foreground(self.broker,path,stop)
        self.assertEqual(1001,count[0])
        self.assertFalse(path.exists())

    def test_import_does_not_bind(self):
        import importlib
        import runtime.director.broker_socket as transport
        with patch("socket.socket", side_effect=AssertionError("unexpected socket")):
            importlib.reload(transport)


class PlatformTests(unittest.TestCase):
    def test_windows_transport_fails_closed(self):
        with patch("runtime.director.broker_socket.sys.platform", "win32"):
            with self.assertRaises(BrokerError):
                require_linux()

    def test_windows_store_fails_closed(self):
        with patch("runtime.director.broker.os.name", "nt"):
            with self.assertRaises(BrokerError):
                Broker("unused")
