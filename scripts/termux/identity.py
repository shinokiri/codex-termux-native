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
    revision = f"termux.g{commit[:12]}" + (".dirty" if dirty else "")
    # main's 0.0.0 is an upstream placeholder, not a released Codex version.
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
        package_version = f"{version}+termux.{release_revision}"
        cli_version = package_version
    elif version == "0.0.0":
        cli_version = f"{upstream['ref']}.{upstream['commit'][:12]}+{revision}"
        package_version = version
    else:
        separator = "." if "+" in version else "+"
        package_version = f"{version}{separator}{revision}"
        cli_version = package_version
    return {
        "codex_commit": commit,
        "source_dirty": dirty,
        "upstream_ref": upstream["ref"],
        "upstream_commit": upstream["commit"],
        "upstream_version": version,
        "package_version": package_version,
        "cli_version": cli_version,
        "release_version": package_version if release_revision is not None else None,
        "port_commit": upstream.get("port_commit"),
    }
