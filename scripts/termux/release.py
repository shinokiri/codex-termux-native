#!/usr/bin/env python3
"""Prepare official release source and publish only the matching validated package."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

if __package__:
    from .release_source import assemble, git, refresh_prepared_locks
else:
    from release_source import assemble, git, refresh_prepared_locks

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "shinokiri/codex-termux-native"
ARCHIVE = "codex-package-aarch64-linux-android.tar.gz"
ASSETS = (ARCHIVE, "codex-package_SHA256SUMS", "install.sh")


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, *, method="GET", data=None, upload=None):
        url = (
            f"https://uploads.github.com/repos/{REPOSITORY}/{path}"
            if upload
            else f"https://api.github.com/{path}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-termux-release",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if upload:
            body = upload.read_bytes()
            headers["Content-Type"] = "application/octet-stream"
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(
                Request(url, data=body, headers=headers, method=method),
                timeout=600 if upload else 30,
            ) as response:
                contents = response.read()
                return json.loads(contents) if contents else None
        except HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            raise

    def repo(self, path, **kwargs):
        return self.request(f"repos/{REPOSITORY}/{path}", **kwargs)


def write_outputs(**values):
    for name, value in values.items():
        print(f"{name}={value}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as target:
            for name, value in values.items():
                target.write(f"{name}={value}\n")


def prepare(args, api):
    release = api.request("repos/openai/codex/releases/latest")
    tag = release["tag_name"]
    if (
        release["draft"]
        or release["prerelease"]
        or not re.fullmatch(r"rust-v\d+\.\d+\.\d+", tag)
    ):
        raise RuntimeError(
            "The selected upstream release is not a published stable version"
        )
    version = f"{tag.removeprefix('rust-v')}+termux.{args.revision}"
    release_tag = f"termux-v{version}"
    published = api.repo(f"releases/tags/{quote(release_tag, safe='')}")
    if published and not published["draft"]:
        write_outputs(build="false", reason="already-published")
        return
    marker = f"termux-build-{tag}-r{args.revision}"
    previous = api.repo(f"git/ref/tags/{marker}")
    # A failed-job rerun keeps GITHUB_RUN_ID and the original source. Let a
    # retried prepare job resume even if its first attempt already wrote a tag.
    retry = args.retry or int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1
    if previous and not retry and not args.dry_run:
        write_outputs(
            build="false", reason="already-attempted-use-retry-for-a-failed-run"
        )
        return
    if args.check_only:
        write_outputs(build="true")
        return
    port_commit = git(ROOT, "rev-parse", args.port_ref)
    if not args.dry_run and not previous:
        api.repo(
            "git/refs",
            method="POST",
            data={"ref": f"refs/tags/{marker}", "sha": port_commit},
        )
    branch = f"termux/releases/{version}"
    existing = api.repo(f"git/ref/heads/{quote(branch, safe='/')}")
    if existing and not args.dry_run:
        commit = refresh_prepared_locks(ROOT, args.work_dir, existing["object"]["sha"])
    else:
        commit = assemble(ROOT, args.work_dir, port_commit, tag, args.revision)
    if not args.dry_run and (not existing or commit != existing["object"]["sha"]):
        # Pass the credential through Git's environment configuration, never
        # through a logged command or a persisted remote URL.
        authorization = base64.b64encode(
            f"x-access-token:{api.token}".encode()
        ).decode()
        env = os.environ.copy()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
            }
        )
        subprocess.run(
            ["git", "push", "origin", f"{commit}:refs/heads/{branch}"],
            cwd=ROOT,
            env=env,
            check=True,
        )
    write_outputs(
        build="true", source_ref=commit, version=version, release_tag=release_tag
    )


def verified_package(directory, version, commit):
    upstream_version = version.partition("+termux.")[0]
    checksum_lines = (directory / "codex-package_SHA256SUMS").read_text().splitlines()
    expected = next(
        line.split()[0] for line in checksum_lines if line.split()[1:] == [ARCHIVE]
    )
    archive_path = directory / ARCHIVE
    with archive_path.open("rb") as archive:
        actual = hashlib.file_digest(archive, "sha256").hexdigest()
    if expected != actual:
        raise RuntimeError("The release archive does not match its checksum")
    with tarfile.open(archive_path) as archive:
        info = json.load(archive.extractfile("BUILD-INFO.json"))
        manifest = json.load(archive.extractfile("codex-package.json"))
        if (
            info["codex_commit"] != commit
            or info["release_version"] != version
            or info["cli_version"] != upstream_version
            or manifest["version"] != upstream_version
            or info["target"] != "aarch64-linux-android"
            or info["source_dirty"]
        ):
            raise RuntimeError(
                "Release identity does not match the source that passed CI"
            )
        for binary in ("bin/codex", "bin/codex-code-mode-host"):
            if not archive.getmember(binary).isfile():
                raise RuntimeError(f"Missing release executable: {binary}")
    for name in ASSETS:
        if not (directory / name).is_file():
            raise RuntimeError(f"Missing release asset: {name}")
    return info


def release_key(tag):
    match = re.fullmatch(r"termux-v(\d+)\.(\d+)\.(\d+)\+termux\.([1-9]\d*)", tag)
    if not match:
        raise RuntimeError(f"Cannot compare an unexpected release tag: {tag}")
    return tuple(map(int, match.groups()))


def publish(args, api):
    info = verified_package(args.artifact_dir, args.version, args.source_ref)
    tag = f"termux-v{args.version}"
    release = api.repo(f"releases/tags/{quote(tag, safe='')}")
    if release and not release["draft"]:
        print(f"Already published: {release['html_url']}")
        return
    # target_commitish is ignored by GitHub when the tag already exists.
    # Never publish a new package under a tag pointing at a different source.
    tag_ref = api.repo(f"git/ref/tags/{quote(tag, safe='')}")
    if tag_ref:
        # Resolve existing annotated tags too. The commits endpoint returns 422
        # for an absent tag, while the refs endpoint returns 404.
        tagged_commit = api.repo(f"commits/{quote(tag, safe='')}")
        if tagged_commit["sha"] != args.source_ref:
            raise RuntimeError(
                "The release tag points to different source; use a new revision"
            )
    metadata = {
        "tag_name": tag,
        "target_commitish": args.source_ref,
        "name": f"Codex {info['cli_version']} for Termux",
        "draft": True,
        "prerelease": False,
        "body": (
            f"Native Android ARM64 build of official [{info['upstream_ref']}]"
            f"(https://github.com/openai/codex/commit/{info['upstream_commit']}).\n\n"
            f"Source: `{args.source_ref}`. Android API 29 or newer.\n\n"
            "The Android build, ELF/package checks and selected regressions passed before publication. "
            "This community port is not an official OpenAI binary. Phone login, TUN networking "
            "and interactive behavior still need device validation.\n\n"
            "Install or update with `codex update`, or run:\n\n```sh\n"
            f"curl -fsSL https://github.com/{REPOSITORY}/releases/latest/download/install.sh | sh\n```\n"
        ),
    }
    if not release:
        release = api.repo(
            "releases",
            method="POST",
            data=metadata,
        )
    else:
        release = api.repo(f"releases/{release['id']}", method="PATCH", data=metadata)
    for name in ASSETS:
        path = args.artifact_dir / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = next(
            (asset for asset in release["assets"] if asset["name"] == name), None
        )
        if existing and existing.get("digest") == f"sha256:{digest}":
            continue
        if existing:
            api.repo(f"releases/assets/{existing['id']}", method="DELETE")
        asset = api.request(
            f"releases/{release['id']}/assets?name={quote(name)}",
            method="POST",
            upload=path,
        )
        if asset.get("digest") != f"sha256:{digest}":
            raise RuntimeError(f"Uploaded asset checksum did not match: {name}")
    # Re-read the channel after uploading: an older run may finish after a
    # newer version or packaging revision was already published.
    latest = api.repo("releases/latest")
    make_latest = not latest or release_key(tag) > release_key(latest["tag_name"])
    published = api.repo(
        f"releases/{release['id']}",
        method="PATCH",
        data={"draft": False, "make_latest": "true" if make_latest else "false"},
    )
    print(f"Published: {published['html_url']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--work-dir", type=Path, required=True)
    prepare_parser.add_argument("--revision", type=int, default=1)
    prepare_parser.add_argument("--port-ref", default="HEAD")
    prepare_parser.add_argument("--retry", action="store_true")
    prepare_parser.add_argument("--dry-run", action="store_true")
    prepare_parser.add_argument("--check-only", action="store_true")
    publish_parser = commands.add_parser("publish")
    publish_parser.add_argument("--artifact-dir", type=Path, required=True)
    publish_parser.add_argument("--version", required=True)
    publish_parser.add_argument("--source-ref", required=True)
    args = parser.parse_args()
    if args.command == "prepare" and args.revision < 1:
        parser.error("The Termux revision must be positive")
    token = os.environ.get("GH_TOKEN")
    if not token and not (
        args.command == "prepare" and (args.dry_run or args.check_only)
    ):
        parser.error("GH_TOKEN is required for release writes")
    api = GitHub(token)
    if args.command == "prepare":
        prepare(args, api)
    else:
        publish(args, api)


if __name__ == "__main__":
    main()
