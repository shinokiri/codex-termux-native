"""Exercise publication order and source identity without creating a real release."""

import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scripts.termux.release import ASSETS, prepare, publish
from scripts.termux.release_source import align_workspace_versions
from scripts.termux.test_installer import release_fixture


class FakeGitHub:
    def __init__(self, fail_upload=False):
        self.release = None
        self.fail_upload = fail_upload
        self.published = False
        self.tagged_commit = None

    def repo(self, path, *, method="GET", data=None):
        if method == "GET":
            if path.startswith("git/ref/tags/"):
                return (
                    {"object": {"type": "commit", **self.tagged_commit}}
                    if self.tagged_commit
                    else None
                )
            if path.startswith("commits/"):
                if not self.tagged_commit:
                    raise HTTPError(path, 422, "No commit found for SHA", {}, None)
                return self.tagged_commit
            return self.release
        if path == "releases" and method == "POST":
            assert data["draft"]
            self.release = {**data, "id": 1, "assets": []}
            return self.release
        if path == "releases/1" and method == "PATCH":
            self.release.update(data)
            if data.get("draft"):
                return self.release
            assert {asset["name"] for asset in self.release["assets"]} == set(ASSETS)
            self.published = True
            return {"html_url": "https://github.com/example/release"}
        raise AssertionError((path, method))

    def request(self, path, *, method, upload):
        if self.fail_upload:
            raise RuntimeError("upload interrupted")
        assert self.release["draft"] and not self.published
        asset = {
            "name": upload.name,
            "digest": "sha256:" + hashlib.sha256(upload.read_bytes()).hexdigest(),
        }
        self.release["assets"].append(asset)
        return asset


class ReleaseTest(unittest.TestCase):
    def test_failed_job_rerun_resumes_preparation_after_the_attempt_was_recorded(self):
        api = Mock(token="local-fixture")
        api.request.return_value = {
            "tag_name": "rust-v0.153.4",
            "draft": False,
            "prerelease": False,
        }
        refs = {}

        def repository(path, method="GET", data=None):
            if method == "POST":
                self.assertEqual(path, "git/refs")
                ref = data["ref"].removeprefix("refs/")
                refs[ref] = {"object": {"sha": data["sha"]}}
                return refs[ref]
            return refs.get(path.removeprefix("git/ref/"))

        api.repo.side_effect = repository
        args = SimpleNamespace(
            revision=3,
            retry=False,
            dry_run=False,
            check_only=False,
            port_ref="HEAD",
            work_dir=Path("unused"),
        )
        with (
            patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1"}),
            patch("scripts.termux.release.git", return_value="a" * 40),
            patch("scripts.termux.release.assemble") as assemble,
            patch("scripts.termux.release.subprocess.run") as push,
            patch("scripts.termux.release.write_outputs") as outputs,
        ):
            assemble.side_effect = HTTPError(
                "source", 503, "temporary failure", {}, None
            )
            with self.assertRaises(HTTPError):
                prepare(args, api)
            push.assert_not_called()
            # Ordinary scheduled checks still deduplicate a recorded attempt.
            args.check_only = True
            prepare(args, api)
            outputs.assert_called_with(
                build="false",
                reason="already-attempted-use-retry-for-a-failed-run",
            )
            assemble.assert_called_once()
            # GitHub reruns the failed prepare job with the original arguments.
            with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
                prepare(args, api)
                outputs.assert_called_with(build="true")
                args.check_only = False
                assemble.side_effect = None
                assemble.return_value = "b" * 40
                prepare(args, api)
            push.assert_called_once()
            outputs.assert_called_with(
                build="true",
                source_ref="b" * 40,
                version="0.153.4+termux.3",
                release_tag="termux-v0.153.4+termux.3",
            )
            self.assertEqual(len(refs), 1)
            # A published release is skipped even in a retried job.
            refs["releases/tags/termux-v0.153.4%2Btermux.3"] = {"draft": False}
            with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "3"}):
                prepare(args, api)
            outputs.assert_called_with(build="false", reason="already-published")

    def test_publish_waits_for_all_assets_and_checks_the_built_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = release_fixture(root, 1)
            info = {
                "codex_commit": "c" * 40,
                "upstream_commit": "a" * 40,
                "upstream_ref": "rust-v0.153.4",
                "release_version": "0.153.4+termux.1",
                "cli_version": "0.153.4",
                "target": "aarch64-linux-android",
                "source_dirty": False,
            }
            package = root / "fixture-1"
            (package / "BUILD-INFO.json").write_text(json.dumps(info))
            with tarfile.open(fixture["archive_path"], "w:gz") as archive:
                for path in package.iterdir():
                    archive.add(path, arcname=path.name)
            digest = hashlib.sha256(fixture["archive_path"].read_bytes()).hexdigest()
            fixture["checksum_path"].write_text(
                f"{digest}  {fixture['archive_path'].name}\n"
            )
            (root / "install.sh").write_text("#!/bin/sh\nexit 0\n")
            args = SimpleNamespace(
                artifact_dir=root,
                version=info["release_version"],
                source_ref=info["codex_commit"],
            )
            api = FakeGitHub()
            publish(args, api)
            self.assertTrue(api.published)
            interrupted = FakeGitHub(fail_upload=True)
            with self.assertRaisesRegex(RuntimeError, "upload interrupted"):
                publish(args, interrupted)
            self.assertFalse(interrupted.published)
            # Retry the real interrupted draft after the validated source changed.
            info["codex_commit"] = "b" * 40
            args.source_ref = info["codex_commit"]
            (package / "BUILD-INFO.json").write_text(json.dumps(info))
            with tarfile.open(fixture["archive_path"], "w:gz") as archive:
                for path in package.iterdir():
                    archive.add(path, arcname=path.name)
            digest = hashlib.sha256(fixture["archive_path"].read_bytes()).hexdigest()
            fixture["checksum_path"].write_text(
                f"{digest}  {fixture['archive_path'].name}\n"
            )
            interrupted.fail_upload = False
            publish(args, interrupted)
            self.assertTrue(interrupted.published)
            self.assertEqual(interrupted.release["target_commitish"], args.source_ref)
            self.assertIn(f"Source: `{args.source_ref}`", interrupted.release["body"])
            matching_tag = FakeGitHub()
            matching_tag.tagged_commit = {"sha": args.source_ref}
            publish(args, matching_tag)
            self.assertTrue(matching_tag.published)
            tagged = FakeGitHub()
            tagged.tagged_commit = {"sha": "a" * 40}
            with self.assertRaisesRegex(RuntimeError, "tag points to different source"):
                publish(args, tagged)
            self.assertIsNone(tagged.release)
            args.source_ref = "d" * 40
            wrong_source = FakeGitHub()
            with self.assertRaisesRegex(RuntimeError, "Release identity"):
                publish(args, wrong_source)
            self.assertIsNone(wrong_source.release)

    def test_workspace_lock_alignment_does_not_change_registry_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "codex-rs/extra").mkdir(parents=True)
            (root / "codex-rs/Cargo.toml").write_text(
                '[workspace]\nmembers = ["extra"]\n[workspace.package]\nversion = "0.153.4"\n'
            )
            (root / "codex-rs/extra/Cargo.toml").write_text(
                '[package]\nname = "extra"\nversion.workspace = true\n'
            )
            (root / "codex-rs/extra/tests/support").mkdir(parents=True)
            (root / "codex-rs/extra/tests/support/Cargo.toml").write_text(
                '[package]\nname = "support"\nversion.workspace = true\n'
            )
            lock = root / "codex-rs/Cargo.lock"
            registry = '\nname = "external"\nversion = "1.2.3"\nsource = "registry+https://example.com"\n'
            lock.write_text(
                'version = 4\n\n[[package]]\nname = "extra"\nversion = "0.0.0"\n\n[[package]]'
                + registry
                + '\n[[package]]\nname = "support"\nversion = "0.0.0"\n'
            )
            align_workspace_versions(root)
            self.assertIn('name = "extra"\nversion = "0.153.4"', lock.read_text())
            self.assertIn(registry, lock.read_text())
            self.assertIn('name = "support"\nversion = "0.153.4"', lock.read_text())
