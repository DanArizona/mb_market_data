"""Regression checks for strict accepted-source verification on Windows."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from integration_source_integrity import verify_integrated_sources


class SourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "probes" / "example.py"
        self.path.parent.mkdir()
        self.original = b"alpha\nbeta\n"
        (self.root / "INTEGRATION_SOURCE_INVENTORY.json").write_text(
            json.dumps({"files": {"probes/example.py": hashlib.sha256(self.original).hexdigest()}}),
            encoding="utf-8",
        )

    def verify(self, *, windows_checkout):
        return verify_integrated_sources(self.root, windows_checkout=windows_checkout)

    def test_exact_bytes_are_accepted(self):
        self.path.write_bytes(self.original)
        self.assertEqual(self.verify(windows_checkout=False)["crlf_normalized"], 0)

    def test_windows_checkout_crlf_is_accepted(self):
        self.path.write_bytes(self.original.replace(b"\n", b"\r\n"))
        self.assertEqual(self.verify(windows_checkout=True)["crlf_normalized"], 1)

    def test_crlf_is_not_accepted_outside_windows(self):
        self.path.write_bytes(self.original.replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(ValueError, "Integrated source differs"):
            self.verify(windows_checkout=False)

    def test_changed_content_is_rejected_even_with_crlf(self):
        self.path.write_bytes(b"alpha\r\nchanged\r\n")
        with self.assertRaisesRegex(ValueError, "Integrated source differs"):
            self.verify(windows_checkout=True)

    def test_missing_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Integrated source differs"):
            self.verify(windows_checkout=True)


if __name__ == "__main__":
    unittest.main()
