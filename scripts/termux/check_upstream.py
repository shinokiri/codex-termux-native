#!/usr/bin/env python3
"""Report new official main commits and stable releases; never modify the checkout."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]


def fetch(path):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-termux-native-upstream-check",
    }
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"https://api.github.com/repos/openai/codex/{path}", headers=headers
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def compare(upstream, main, release):
    return {
        "integrated_ref": upstream["ref"],
        "integrated_commit": upstream["commit"],
        "main": {
            "commit": main["sha"],
            "url": main["html_url"],
            "differs_from_integrated_commit": main["sha"] != upstream["commit"],
        },
        "stable_release": {
            "tag": release["tag_name"],
            "url": release["html_url"],
            "published_at": release["published_at"],
            "new_since_last_review": release["tag_name"]
            != upstream["latest_release_seen"],
        },
    }


def main():
    upstream = json.loads((ROOT / "scripts/termux/upstream.json").read_text())
    report = compare(upstream, fetch("commits/main"), fetch("releases/latest"))
    report["checked_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        head = report["main"]
        release = report["stable_release"]
        with Path(summary).open("a") as output:
            output.write(
                "## Official upstream check\n\n"
                f"Integrated source: `{upstream['ref']}` at `{upstream['commit']}`.\n\n"
                "| Channel | Latest | Change |\n| --- | --- | --- |\n"
                f"| main | [{head['commit'][:12]}]({head['url']}) | "
                f"{'Different commit' if head['differs_from_integrated_commit'] else 'Current'} |\n"
                f"| Stable | [{release['tag']}]({release['url']}) | "
                f"{'New release to review' if release['new_since_last_review'] else 'Already seen'} |\n\n"
                "This check reports upstream state. It does not merge code, open a PR "
                "or build a candidate. Seeing a release does not mean its changes "
                "are integrated into a main snapshot.\n"
            )


if __name__ == "__main__":
    main()
