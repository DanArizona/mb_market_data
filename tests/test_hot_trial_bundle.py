import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

path = Path(__file__).parents[1] / "experiments/hot_provider_trial_20261018/bundle_guard.py"
spec = importlib.util.spec_from_file_location("hot_trial_bundle_guard", path)
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)


class BundleGuardTests(unittest.TestCase):
    def fixture(self, root, name="payload.txt", sha=None):
        manifest = root/"experiments/hot_provider_trial_20261018/source_manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"files":[{"path":name,"sha256":sha or hashlib.sha256(b"original").hexdigest()}]}))

    def test_changed_file_is_refused_without_modification(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/"payload.txt").write_text("changed"); self.fixture(root)
            with self.assertRaises(ValueError): guard.verify(root)
            self.assertEqual((root/"payload.txt").read_text(), "changed")

    def test_manifest_path_escape_is_refused(self):
        for name in ("../outside", "/outside", "C:/outside", "..\\outside"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); self.fixture(root,name)
                with self.assertRaises(ValueError): guard.verify(root)

    def test_unchanged_payload_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/"payload.txt").write_text("original"); self.fixture(root)
            self.assertEqual(len(guard.verify(root)["files"]),1)
