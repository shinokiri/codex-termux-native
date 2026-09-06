"""Check that source snapshots and tagged releases keep distinct identities."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.termux.identity import build_identity


class IdentityTest(unittest.TestCase):
    def test_snapshot_release_and_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts/termux").mkdir(parents=True)
            (root / "codex-rs").mkdir()
            manifest = root / "codex-rs/Cargo.toml"
            manifest.write_text('[workspace.package]\nversion = "0.0.0"\n')
            (root / "scripts/termux/upstream.json").write_text(
                json.dumps({"ref": "main", "commit": "a" * 40})
            )
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=root,
                check=True,
            )
            snapshot = build_identity(root)
            commit = snapshot["codex_commit"]
            self.assertEqual(
                snapshot["cli_version"], f"main.aaaaaaaaaaaa+termux.g{commit[:12]}"
            )
            self.assertFalse(snapshot["source_dirty"])
            manifest.write_text('[workspace.package]\nversion = "0.153.4"\n')
            release = build_identity(root)
            self.assertEqual(
                release["package_version"], f"0.153.4+termux.g{commit[:12]}.dirty"
            )
            self.assertEqual(release["cli_version"], release["package_version"])
            self.assertTrue(release["source_dirty"])
            # Reading the identity must not rewrite Cargo or its lockfile.
            self.assertEqual(
                manifest.read_text(), '[workspace.package]\nversion = "0.153.4"\n'
            )
            (root / "scripts/termux/upstream.json").write_text(
                json.dumps(
                    {"ref": "rust-v0.153.4", "commit": "b" * 40, "release_revision": 2}
                )
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qam",
                    "release fixture",
                ],
                cwd=root,
                check=True,
            )
            published = build_identity(root)
            self.assertEqual(published["release_version"], "0.153.4+termux.2")
            self.assertEqual(published["cli_version"], published["release_version"])
