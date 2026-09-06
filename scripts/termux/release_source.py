"""Apply this repository's Android delta to an exact official release tree."""

import json
import re
import subprocess
import tomllib


def git(root, *args, **kwargs):
    return subprocess.check_output(
        ["git", *map(str, args)], cwd=root, text=True, **kwargs
    ).strip()


def align_workspace_versions(root):
    workspace_root = root / "codex-rs"
    workspace = tomllib.loads((workspace_root / "Cargo.toml").read_text())["workspace"]
    version = workspace["package"]["version"]
    inherited = set()
    for member in workspace["members"]:
        for path in workspace_root.glob(f"{member}/Cargo.toml"):
            package = tomllib.loads(path.read_text()).get("package", {})
            if package.get("version") == {"workspace": True}:
                inherited.add(package["name"])
    lock = workspace_root / "Cargo.lock"
    blocks = lock.read_text().split("[[package]]")
    for index, block in enumerate(blocks[1:], 1):
        package = tomllib.loads(block)
        if package["name"] in inherited and "source" not in package:
            blocks[index] = re.sub(
                r'^version = "[^"]+"$',
                f'version = "{version}"',
                block,
                count=1,
                flags=re.M,
            )
    lock.write_text("[[package]]".join(blocks))


def assemble(root, destination, port_commit, tag, revision):
    upstream = json.loads(
        git(root, "show", f"{port_commit}:scripts/termux/upstream.json")
    )
    remote = "https://github.com/openai/codex.git"
    git(root, "fetch", "--depth=1", remote, upstream["commit"])
    git(root, "fetch", "--depth=1", remote, f"refs/tags/{tag}")
    source_commit = git(root, "rev-parse", "FETCH_HEAD^{commit}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "--detach", destination, source_commit)
    patch = subprocess.check_output(
        [
            "git",
            "diff",
            "--binary",
            upstream["commit"],
            port_commit,
            "--",
            ".",
            ":(exclude).github/workflows",
        ],
        cwd=root,
    )
    # A conflict is a maintenance task, never a reason to label a main snapshot
    # as the official release. Keep the failed checkout for the job's diagnostics.
    subprocess.run(
        ["git", "apply", "--3way", "--index"], cwd=destination, input=patch, check=True
    )
    align_workspace_versions(destination)
    (destination / "scripts/termux/upstream.json").write_text(
        json.dumps(
            {
                "ref": tag,
                "commit": source_commit,
                "latest_release_seen": tag,
                "release_revision": revision,
                "port_commit": port_commit,
            },
            indent=2,
        )
        + "\n"
    )
    git(destination, "add", ".")
    git(
        destination,
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-m",
        f"build(termux): adapt official {tag} (revision {revision})",
    )
    return git(destination, "rev-parse", "HEAD")
