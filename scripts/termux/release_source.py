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
    # Cargo also includes path dependencies that are not listed in members,
    # including the nested test-support crates. Their lock entries must agree.
    for path in workspace_root.rglob("Cargo.toml"):
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
    # GITHUB_TOKEN cannot write workflows. Keep those files exactly as already
    # reviewed on the controller commit, including removal of upstream-only files.
    git(
        destination,
        "restore",
        f"--source={port_commit}",
        "--staged",
        "--worktree",
        ".github/workflows",
    )
    align_workspace_versions(destination)
    subprocess.run(["just", "bazel-lock-update"], cwd=destination, check=True)
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
    tree = git(destination, "write-tree")
    commit = git(
        destination,
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit-tree",
        tree,
        "-p",
        port_commit,
        "-m",
        f"build(termux): adapt official {tag} (revision {revision})",
    )
    # The source tree came from the exact tag; parenting it to the controller
    # avoids introducing that tag's unrelated workflow history on the new branch.
    git(destination, "reset", "--soft", commit)
    return commit


def refresh_prepared_locks(root, destination, commit):
    """Keep a prepared branch's fixes while canonicalizing its internal locks."""
    git(root, "fetch", "--depth=1", "origin", commit)
    git(root, "worktree", "add", "--detach", destination, commit)
    align_workspace_versions(destination)
    subprocess.run(["just", "bazel-lock-update"], cwd=destination, check=True)
    if not git(destination, "diff", "--", "codex-rs/Cargo.lock", "MODULE.bazel.lock"):
        return commit
    git(destination, "add", "codex-rs/Cargo.lock", "MODULE.bazel.lock")
    git(
        destination,
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-m",
        "fix(termux): align prepared release lockfiles",
    )
    return git(destination, "rev-parse", "HEAD")
