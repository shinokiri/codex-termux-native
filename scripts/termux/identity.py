"""Identify an upstream release or development snapshot without renumbering Cargo."""

import json
from pathlib import Path
import subprocess
import tomllib


def build_identity(root: Path) -> dict:
    upstream = json.loads((root / "scripts/termux/upstream.json").read_text())
    manifest = tomllib.loads((root / "codex-rs/Cargo.toml").read_text())
    version = manifest["workspace"]["package"]["version"]
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            text=True,
        ).strip()
    )
    # Runtime version fields use the exact upstream Cargo version. Packaging
    # revisions and the actual modified source commit stay in BUILD-INFO.json.
    release_revision = upstream.get("release_revision")
    if release_revision is not None:
        if (
            not isinstance(release_revision, int)
            or release_revision < 1
            or upstream["ref"] != f"rust-v{version}"
            or version == "0.0.0"
            or dirty
        ):
            raise RuntimeError(
                "A Termux release requires clean, versioned upstream source"
            )
    return {
        "codex_commit": commit,
        "source_dirty": dirty,
        "upstream_ref": upstream["ref"],
        "upstream_commit": upstream["commit"],
        "upstream_version": version,
        "package_version": version,
        "cli_version": version,
        "release_version": (
            f"{version}+termux.{release_revision}"
            if release_revision is not None
            else None
        ),
        "port_commit": upstream.get("port_commit"),
    }
