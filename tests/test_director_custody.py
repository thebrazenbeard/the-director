"""Offline tests for local reference custody; no outside attestation is implied."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase

from runtime.director.custody import (
    CustodyClosedError,
    CustodyConflictError,
    CustodyIntegrityError,
    CustodyStore,
)


UTC = "2026-11-01T12:30:00Z"


class CustodyTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "custody.sqlite"
        self.store = CustodyStore(self.path)
        self.addCleanup(self.store.close)

    def test_opaque_bytes_retrieved_without_transformation(self) -> None:
        content = b"\x00\xff\x01\n{not parsed}"
        receipt = self.store.append("ev-1", "evidence", content, UTC)
        record = self.store.get("ev-1")
        self.assertEqual(record.content, content)
        self.assertEqual(record.receipt, receipt)
        self.assertEqual(receipt.seq, 1)
        self.assertEqual(len(receipt.entry_hash), 64)

    def test_missing_record_returns_none(self) -> None:
        self.assertIsNone(self.store.get("absent"))

    def test_chain_continuity_for_multiple_entries(self) -> None:
        first = self.store.append("a", "dossier", b"a", UTC)
        second = self.store.append("b", "evidence", b"b", UTC)
        third = self.store.append("c", "evidence", b"c", UTC)
        self.assertEqual((first.seq, second.seq, third.seq), (1, 2, 3))
        self.assertEqual(second.prev_hash, first.entry_hash)
        self.assertEqual(third.prev_hash, second.entry_hash)
        self.assertTrue(self.store.verify_chain())

    def test_restart_preserves_content_and_receipt(self) -> None:
        receipt = self.store.append("ev-1", "evidence", b"persistent", UTC)
        self.store.close()
        reopened = CustodyStore(self.path)
        try:
            self.assertEqual(reopened.get("ev-1").receipt, receipt)
            self.assertEqual(reopened.get("ev-1").content, b"persistent")
            self.assertTrue(reopened.verify_chain())
        finally:
            reopened.close()

    def test_identical_append_is_idempotent_even_when_observed_time_differs(self) -> None:
        original = self.store.append("ev-1", "evidence", b"same", UTC)
        repeated = self.store.append("ev-1", "evidence", b"same", "2026-11-02T00:00:00Z")
        self.assertEqual(original, repeated)
        next_receipt = self.store.append("ev-2", "evidence", b"other", UTC)
        self.assertEqual(next_receipt.seq, 2)

    def test_changed_bytes_conflict_instead_of_overwriting(self) -> None:
        receipt = self.store.append("ev-1", "evidence", b"original", UTC)
        with self.assertRaises(CustodyConflictError):
            self.store.append("ev-1", "evidence", b"modified", UTC)
        self.assertEqual(self.store.get("ev-1").receipt, receipt)
        self.assertEqual(self.store.get("ev-1").content, b"original")

    def test_changed_kind_conflicts(self) -> None:
        self.store.append("ev-1", "evidence", b"same", UTC)
        with self.assertRaises(CustodyConflictError):
            self.store.append("ev-1", "dossier", b"same", UTC)

    def test_invalid_id_kind_bytes_or_utc_rejected(self) -> None:
        invalid = [
            ("bad id", "evidence", b"data", UTC),
            ("valid", "", b"data", UTC),
            ("valid", "evidence", "not bytes", UTC),
            ("valid", "evidence", b"data", "2026-11-01T12:30:00"),
            ("valid", "evidence", b"data", "2026-11-01T13:30:00+01:00"),
            ("valid", "evidence", b"data", "not-a-date"),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.append(*args)
        self.assertTrue(self.store.verify_chain())

    def test_reject_oversize_bytes(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append("ev-1", "evidence", b"x" * (256 * 1024 + 1), UTC)
        self.assertTrue(self.store.verify_chain())

    def test_max_records_bounds_growth(self) -> None:
        bounded = CustodyStore(self.path, max_records=1)
        try:
            bounded.append("ev-1", "evidence", b"first", UTC)
            with self.assertRaises(ValueError):
                bounded.append("ev-2", "evidence", b"second", UTC)
            self.assertEqual(bounded.append("ev-1", "evidence", b"first", UTC).seq, 1)
        finally:
            bounded.close()

    def test_default_capacity_is_small_and_requires_explicit_opt_in(self) -> None:
        for idx in range(256):
            self.store.append(f"r{idx}", "evidence", b"x", UTC)
        with self.assertRaises(ValueError):
            self.store.append("r256", "evidence", b"x", UTC)
        self.assertTrue(self.store.verify_chain())

        expanded = CustodyStore(self.path, max_records=257)
        try:
            self.assertEqual(expanded.append("r256", "evidence", b"x", UTC).seq, 257)
        finally:
            expanded.close()

    def test_content_tampering_detected_and_new_append_blocked(self) -> None:
        self.store.append("ev-1", "evidence", b"first", UTC)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE entries SET content = ? WHERE record_id = ?", (b"altered", "ev-1"))
        with self.assertRaises(CustodyIntegrityError):
            self.store.verify_chain()
        with self.assertRaises(CustodyIntegrityError):
            self.store.append("ev-2", "evidence", b"second", UTC)

    def test_metadata_tampering_detected(self) -> None:
        self.store.append("ev-1", "evidence", b"first", UTC)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE entries SET observed_at_utc = ? WHERE record_id = ?", ("2026-11-03T00:00:00Z", "ev-1"))
        with self.assertRaises(CustodyIntegrityError):
            self.store.verify_chain()

    def test_gap_from_deleted_row_detected(self) -> None:
        self.store.append("ev-1", "evidence", b"first", UTC)
        self.store.append("ev-2", "evidence", b"second", UTC)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM entries WHERE record_id = ?", ("ev-1",))
        with self.assertRaises(CustodyIntegrityError):
            self.store.verify_chain()

    def test_tail_deletion_detected_against_local_counter(self) -> None:
        self.store.append("ev-1", "evidence", b"first", UTC)
        self.store.append("ev-2", "evidence", b"second", UTC)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM entries WHERE record_id = ?", ("ev-2",))
        with self.assertRaises(CustodyIntegrityError):
            self.store.verify_chain()

    def test_two_connection_writers_serialize_without_duplicate_sequences(self) -> None:
        barrier = Barrier(2)

        def append_other(identifier: str) -> int:
            store = CustodyStore(self.path)
            try:
                barrier.wait(timeout=5)
                return store.append(identifier, "evidence", identifier.encode(), UTC).seq
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(append_other, f"record-{idx}") for idx in range(2)]
            self.assertEqual(sorted(f.result(timeout=10) for f in futures), [1, 2])
        self.assertTrue(self.store.verify_chain())

    def test_two_writers_same_identifier_return_same_receipt(self) -> None:
        barrier = Barrier(2)

        def append_same():
            store = CustodyStore(self.path)
            try:
                barrier.wait(timeout=5)
                return store.append("shared", "evidence", b"identical", UTC)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(append_same) for _ in range(2)]
            self.assertEqual(futures[0].result(timeout=10), futures[1].result(timeout=10))
        self.assertTrue(self.store.verify_chain())

    def test_closed_store_rejects_operations(self) -> None:
        self.store.close()
        with self.assertRaises(CustodyClosedError):
            self.store.append("a", "evidence", b"bytes", UTC)
        with self.assertRaises(CustodyClosedError):
            self.store.get("a")
        with self.assertRaises(CustodyClosedError):
            self.store.verify_chain()

