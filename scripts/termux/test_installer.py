"""Exercise Termux upgrades through the official shell installer with local packages."""

import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest

from scripts.install.test_install_sh import create_legacy_release
from scripts.install.test_install_sh import run_installer_in
from scripts.install.test_install_sh import write_executable


def release_fixture(root, revision, *, cli_version="0.153.4", fail_visible_check=False):
    version = f"0.153.4+termux.{revision}"
    package = root / f"fixture-{revision}"
    (package / "bin").mkdir(parents=True)
    (package / "lib").mkdir()
    (package / "codex-package.json").write_text(json.dumps({"version": cli_version}))
    (package / "BUILD-INFO.json").write_text(json.dumps({"release_version": version}))
    cli_script = "#!/bin/sh\n"
    if fail_visible_check:
        cli_script += 'case "$0" in */install-bin/codex) exit 23;; esac\n'
    cli_script += f"echo 'codex-cli {cli_version}'\n"
    write_executable(package / "bin/codex", cli_script)
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
    def test_update_and_prune_keeps_only_current_even_without_a_new_release(self):
        for next_revision in (2, 3):
            with (
                self.subTest(revision=next_revision),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                fixtures = {}
                for revision in (1, 2):
                    fixtures[revision] = release_fixture(root, revision)
                    result, _ = run_installer_in(
                        root, "latest", platform="android", **fixtures[revision]
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                codex_home = root / "codex-home"
                current = codex_home / "packages/standalone/current"
                releases = current.resolve().parent
                self.assertEqual(len(list(releases.iterdir())), 2)
                user_data = {}
                for name in ("sessions/keep.jsonl", "auth.json", "config.toml"):
                    path = codex_home / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(name)
                    user_data[path] = name
                if next_revision == 3:
                    fixtures[3] = release_fixture(root, 3)
                (root / "requests.log").unlink()

                result, requests = run_installer_in(
                    root,
                    "latest",
                    platform="android",
                    installer_args=("--prune-after-install",),
                    **fixtures[next_revision],
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(
                    current.resolve().name,
                    f"0.153.4+termux.{next_revision}-aarch64-linux-android",
                )
                self.assertEqual(list(releases.iterdir()), [current.resolve()])
                self.assertTrue((current / "bin/codex-code-mode-host").is_file())
                self.assertEqual(
                    {path: path.read_text() for path in user_data}, user_data
                )
                self.assertEqual(
                    sum(url.endswith(".tar.gz") for url in requests),
                    int(next_revision == 3),
                )

    def test_update_and_prune_waits_for_visible_command_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, _ = run_installer_in(
                root, "latest", platform="android", **release_fixture(root, 1)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            current = root / "codex-home/packages/standalone/current"
            previous = current.resolve()
            result, _ = run_installer_in(
                root,
                "latest",
                platform="android",
                installer_args=("--prune-after-install",),
                **release_fixture(root, 2, fail_visible_check=True),
            )
            self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
            self.assertNotEqual(current.resolve(), previous)
            self.assertTrue((previous / "bin/codex").is_file())
            self.assertTrue((previous / "bin/codex-code-mode-host").is_file())
            self.assertNotIn("Removed old package:", result.stdout)

    def test_prune_keeps_only_current_without_touching_user_data_or_network(self):
        with tempfile.TemporaryDirectory(prefix="termux prune ") as temporary:
            root = Path(temporary)
            for revision in (1, 2, 3):
                result, _ = run_installer_in(
                    root,
                    "latest",
                    platform="android",
                    **release_fixture(root, revision),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            codex_home = root / "codex-home"
            for name in ("sessions/keep.jsonl", "auth.json", "config.toml"):
                path = codex_home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            current = codex_home / "packages/standalone/current"
            selected = current.resolve()
            releases = selected.parent
            unrelated = releases / "unrelated"
            unrelated.mkdir()
            (unrelated / "keep").write_text("unrelated")
            external = root / "external"
            external.mkdir()
            (external / "codex-package.json").write_text("{}")
            (releases / "external-link").symlink_to(external, target_is_directory=True)
            (root / "requests.log").unlink()

            for _ in range(2):
                result, requests = run_installer_in(
                    root, "latest", platform="android", installer_args=("--prune",)
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(requests, [])
                self.assertEqual(current.resolve(), selected)
                self.assertEqual(
                    {path.name for path in releases.iterdir()},
                    {selected.name, "unrelated", "external-link"},
                )
                self.assertTrue((current / "bin/codex-code-mode-host").is_file())
                self.assertTrue((external / "codex-package.json").is_file())
                self.assertEqual((unrelated / "keep").read_text(), "unrelated")
                for name in ("sessions/keep.jsonl", "auth.json", "config.toml"):
                    self.assertEqual((codex_home / name).read_text(), name)

    def test_prune_removes_the_legacy_installer_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, metadata = create_legacy_release(root)
            result, _ = run_installer_in(
                root,
                "latest",
                force_macos=True,
                metadata_json=metadata,
                legacy_archive_path=archive,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            current = root / "codex-home/packages/standalone/current"
            legacy = current.resolve()
            self.assertTrue((legacy / "codex-resources/rg").is_file())
            self.assertFalse((legacy / "codex-package.json").exists())
            result, _ = run_installer_in(
                root, "latest", platform="android", **release_fixture(root, 1)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            selected = current.resolve()
            (root / "requests.log").unlink()

            result, requests = run_installer_in(
                root, "latest", platform="android", installer_args=("--prune",)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(requests, [])
            self.assertFalse(legacy.exists())
            self.assertEqual(current.resolve(), selected)
            self.assertTrue((current / "bin/codex-code-mode-host").is_file())

    def test_prune_cleans_staging_after_install_finalization_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, _ = run_installer_in(
                root, "latest", platform="android", **release_fixture(root, 1)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            current = root / "codex-home/packages/standalone/current"
            selected = current.resolve()
            write_executable(
                root / "bin/mv",
                '#!/bin/sh\ncase "$1" in */releases/.staging.*) exit 23;; esac\n'
                'exec /bin/mv "$@"\n',
            )
            result, _ = run_installer_in(
                root, "latest", platform="android", **release_fixture(root, 2)
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            staging = list(selected.parent.glob(".staging.*"))
            self.assertEqual(len(staging), 1)
            self.assertTrue((staging[0] / "bin/codex-code-mode-host").is_file())
            (root / "bin/mv").unlink()
            (root / "requests.log").unlink()

            result, requests = run_installer_in(
                root, "latest", platform="android", installer_args=("--prune",)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(requests, [])
            self.assertEqual(list(selected.parent.iterdir()), [selected])
            self.assertEqual(current.resolve(), selected)
            self.assertTrue((current / "bin/codex-code-mode-host").is_file())

    def test_prune_preserves_packages_when_current_is_invalid(self):
        for invalid in ("missing", "outside", "incomplete", "staging"):
            with (
                self.subTest(current=invalid),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                result, _ = run_installer_in(
                    root, "latest", platform="android", **release_fixture(root, 1)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                current = root / "codex-home/packages/standalone/current"
                installed = current.resolve()
                if invalid == "incomplete":
                    (installed / "bin/codex-code-mode-host").unlink()
                elif invalid == "staging":
                    staged = installed.with_name(".staging.manual")
                    installed.rename(staged)
                    installed = staged
                    current.unlink()
                    current.symlink_to(installed, target_is_directory=True)
                else:
                    current.unlink()
                    if invalid == "outside":
                        current.symlink_to(root / "fixture-1", target_is_directory=True)
                (root / "requests.log").unlink()
                result, requests = run_installer_in(
                    root, "latest", platform="android", installer_args=("--prune",)
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(requests, [])
                self.assertTrue((installed / "bin/codex").is_file())

    def test_upgrade_switches_the_complete_package_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_data = root / "codex-home/sessions/keep.jsonl"
            user_data.parent.mkdir(parents=True)
            user_data.write_text("existing session\n")
            for revision in (1, 2):
                cli_version = "0.153.4+termux.1" if revision == 1 else "0.153.4"
                fixture = release_fixture(root, revision, cli_version=cli_version)
                result, requests = run_installer_in(
                    root,
                    "latest",
                    platform="android",
                    **fixture,
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
                self.assertEqual(
                    subprocess.check_output(
                        [current / "bin/codex", "--version"], text=True
                    ),
                    f"codex-cli {cli_version}\n",
                )
            (root / "requests.log").unlink()
            result, requests = run_installer_in(
                root, "latest", platform="android", **fixture
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(any(url.endswith(".tar.gz") for url in requests))
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
            result, _ = run_installer_in(
                root,
                "latest",
                platform="android",
                installer_args=("--prune-after-install",),
                **broken,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(os.readlink(current), previous)
            self.assertTrue((current / "bin/codex").is_file())
