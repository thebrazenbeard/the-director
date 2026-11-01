"""Owner-only persisted coordinator and SQLite sidecar access checks."""

import os
import tempfile
import unittest
from pathlib import Path

from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox


@unittest.skipUnless(os.name == "posix", "POSIX path permissions differ from Windows ACLs")
class CoordinatorPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "director.sqlite3"
        self.mailbox = DirectorMailbox(Path(self.temp.name) / "mailbox.sqlite3")
        self.addCleanup(self.mailbox.close)

    def test_reopening_world_readable_main_db_fails_closed(self):
        with DirectorCoordinator(self.path, self.mailbox):
            pass
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            DirectorCoordinator(self.path, self.mailbox)
        self.path.chmod(0o600)
        with DirectorCoordinator(self.path, self.mailbox):
            pass

    def test_preexisting_insecure_wal_and_symlink_shm_are_rejected(self):
        wal = self.path.with_name(self.path.name + "-wal")
        shm = self.path.with_name(self.path.name + "-shm")
        with DirectorCoordinator(self.path, self.mailbox):
            pass
        wal.touch(mode=0o600)
        wal.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            DirectorCoordinator(self.path, self.mailbox)
        wal.unlink()
        shm.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            DirectorCoordinator(self.path, self.mailbox)
        shm.unlink()
        with DirectorCoordinator(self.path, self.mailbox):
            pass


if __name__ == "__main__":
    unittest.main()
