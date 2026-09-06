"""Exercise Termux upgrades through the official shell installer with local packages."""

import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.install.test_install_sh import run_installer_in
from scripts.install.test_install_sh import write_executable


def release_fixture(root, revision):
    version = f"0.153.4+termux.{revision}"
    package = root / f"fixture-{revision}"
    (package / "bin").mkdir(parents=True)
    (package / "lib").mkdir()
    (package / "codex-package.json").write_text(json.dumps({"version": version}))
    write_executable(package / "bin/codex", f"#!/bin/sh\necho 'codex-cli {version}'\n")
    write_executable(package / "bin/codex-code-mode-host", "#!/bin/sh\nexit 0\n")
    archive_path = root / "codex-package-aarch64-linux-android.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in package.iterdir():
            archive.add(path, arcname=path.name)
    archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = root / "codex-package_SHA256SUMS"
    checksum_path.write_text(f"{archive_digest}  {archive_path.name}\n")
    checksum_digest = hashlib.sha256(checksum_path.read_bytes()).hexdigest()
    metadata = json.dumps(
        {
            "tag_name": f"termux-v{version}",
            "assets": [
                {"name": archive_path.name, "digest": f"sha256:{archive_digest}"},
                {"name": checksum_path.name, "digest": f"sha256:{checksum_digest}"},
            ],
        }
    )
    return dict(
        metadata_json=metadata, archive_path=archive_path, checksum_path=checksum_path
    )


class TermuxInstallerTest(unittest.TestCase):
    def test_upgrade_switches_the_complete_package_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_data = root / "codex-home/sessions/keep.jsonl"
            user_data.parent.mkdir(parents=True)
            user_data.write_text("existing session\n")
            for revision in (1, 2):
                result, requests = run_installer_in(
                    root,
                    "latest",
                    platform="android",
                    **release_fixture(root, revision),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(
                    all("shinokiri/codex-termux-native" in url for url in requests)
                )
                current = root / "codex-home/packages/standalone/current"
                self.assertTrue(
                    os.readlink(current).endswith(
                        f"0.153.4+termux.{revision}-aarch64-linux-android"
                    )
                )
                self.assertTrue((current / "bin/codex-code-mode-host").is_file())
            self.assertEqual(user_data.read_text(), "existing session\n")

    def test_corrupt_download_leaves_the_installed_version_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, _ = run_installer_in(
                root, "latest", platform="android", **release_fixture(root, 1)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            current = root / "codex-home/packages/standalone/current"
            previous = os.readlink(current)
            broken = release_fixture(root, 2)
            broken["archive_path"].write_bytes(b"incomplete download")
            result, _ = run_installer_in(root, "latest", platform="android", **broken)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(os.readlink(current), previous)
            self.assertTrue((current / "bin/codex").is_file())
