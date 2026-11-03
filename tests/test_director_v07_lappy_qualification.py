"""Synthetic source tests, not Windows principal/ACL/process qualification."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from runtime.director.unattended_qualification import (
    HOST_INVENTORY_MAX_BYTES, HOST_QUALIFICATION_GAPS,
    assess_host_inventory, render_host_readiness,
)


def snapshot(**changes):
    data = dict(schema="director.host-inventory.v1", provenance="REAL_HOST_PROBE",
                platform="Windows", os_build=10000, logical_processors=4,
                powershell_major=5, process_64bit=True)
    data.update(changes)
    return json.dumps(data).encode()


class HostInventoryTests(unittest.TestCase):
    def assert_unqualified(self, result):
        self.assertEqual(result["status"], "UNQUALIFIED_HOST")
        self.assertIs(result["qualified"], False)
        self.assertEqual(result["release_decision"], "NO_GO")
        self.assertEqual(result["effect_authority"], "NOT_GRANTED")
        self.assertEqual(result["effect_status"], "NOT_EXECUTED")
        self.assertEqual({c["code"] for c in result["checks"]}, set(HOST_QUALIFICATION_GAPS))
        self.assertTrue(all(c["status"] == "NOT_TESTED" for c in result["checks"]))

    def test_inventory_can_never_attest_provenance_or_qualify(self):
        for raw in (None, snapshot(), snapshot(os_build=20000)):
            result = assess_host_inventory(raw)
            self.assert_unqualified(result)
            if raw:
                self.assertEqual(result["inventory_disposition"], "UNVERIFIED_INVENTORY_ONLY")
                self.assertEqual(result["evidence_provenance"]["REAL_HOST_PROBE"],
                                 "UNVERIFIED_SNAPSHOT")

    def test_all_pass_and_forged_signature_are_rejected(self):
        for changes in ({"qualified": True}, {"signature": "signed-by-host"},
                        {"checks": {c: "PASS" for c in HOST_QUALIFICATION_GAPS}},
                        {"host": "wrong-host"}, {"boot": "wrong-boot"},
                        {"commit": "wrong-commit"}, {"timestamp": 1}):
            with self.subTest(changes=changes):
                result = assess_host_inventory(snapshot(**changes))
                self.assert_unqualified(result)
                self.assertEqual(result["inventory_disposition"], "REJECTED_INPUT")

    def test_malformed_oversized_unsupported_inputs(self):
        invalid = [b"", b"{", b"\xff", b"[]", b"true", b"null", True, {}, "{}",
                   b" " * (HOST_INVENTORY_MAX_BYTES + 1),
                   b"[" * 1500 + b"]" * 1500,
                   snapshot(platform="Linux"), snapshot(schema="future"),
                   snapshot(provenance="TEST"), snapshot(os_build=True),
                   snapshot(logical_processors=0), snapshot(process_64bit="true"),
                   snapshot(powershell_major=101), snapshot(os_build=float("nan")),
                   snapshot().replace(b'"Windows"', b'"Windows", "platform": "Windows"')]
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:60]):
                result = assess_host_inventory(raw)
                self.assert_unqualified(result)
                self.assertEqual(result["inventory_disposition"], "REJECTED_INPUT")
                self.assertIsNone(result["inventory"])

    def test_snapshots_are_replayable_but_never_authorization(self):
        # No trusted timestamp/host/boot/commit exists; even identical copied
        # inventory cannot become a proof on its first or later presentation.
        raw = snapshot()
        self.assertEqual(assess_host_inventory(raw), assess_host_inventory(raw))
        self.assert_unqualified(assess_host_inventory(raw))

    def test_renderer_redacts_invalid_input_and_is_bounded(self):
        raw = snapshot(secret="PRIVATE-PATH-AND-CREDENTIAL")
        rendered = render_host_readiness(raw)
        self.assertNotIn("PRIVATE", rendered)
        self.assertLess(len(rendered.encode()), 8192)
        self.assert_unqualified(json.loads(rendered))
        self.assertEqual(render_host_readiness(snapshot()), render_host_readiness(snapshot()))

    @unittest.skipUnless(os.name == "nt", "Windows inventory only; no host proof")
    def test_windows_foreground_inventory_without_profile_or_mutations(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "director-v07-qualify-lappy.ps1"
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)],
            # Windows PowerShell process startup on a shared CI runner is
            # separate from the script's own two-second in-process probe bound.
            # Preserve a finite deadline and fail on timeout; never skip.
            capture_output=True, timeout=30, check=False,
        )
        # Execution-policy denial is a failure to investigate, not a reason to
        # change host policy or silently skip. Never print potentially private errors.
        self.assertEqual(result.returncode, 0, "read-only inventory unavailable")
        self.assertLessEqual(len(result.stdout), HOST_INVENTORY_MAX_BYTES)
        report = assess_host_inventory(result.stdout.strip())
        self.assertEqual(report["inventory_disposition"], "UNVERIFIED_INVENTORY_ONLY")
        self.assert_unqualified(report)


if __name__ == "__main__":
    unittest.main()
