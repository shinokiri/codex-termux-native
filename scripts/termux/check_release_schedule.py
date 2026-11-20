#!/usr/bin/env python3
"""Check actual scheduled release runs without starting a build or changing releases."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from statistics import median
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

if __package__:
    from .release import ASSETS, REPOSITORY, GitHub
else:
    from release import ASSETS, REPOSITORY, GitHub


WORKFLOW = "actions/workflows/termux-release.yml"


def assess(workflow, runs, now, max_age_minutes, *, inspect_failure=None):
    # A manual run or rerun must not reset the scheduler's clock. created_at
    # records the original event; updated_at and run_started_at can move on rerun.
    scheduled = sorted(
        (run for run in runs if run["event"] == "schedule"),
        key=lambda run: run["created_at"],
        reverse=True,
    )
    times = [
        datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        for run in scheduled
    ]
    gaps = [
        (newer - older).total_seconds() / 60 for newer, older in zip(times, times[1:])
    ]
    issues = []
    details = {}
    if workflow["state"] != "active":
        issues.append(f"Release workflow is {workflow['state']}.")
    latest = None
    if not scheduled:
        issues.append("No scheduled release runs were found.")
    else:
        latest = {
            key: scheduled[0][key]
            for key in (
                "id",
                "html_url",
                "event",
                "head_sha",
                "created_at",
                "run_started_at",
                "run_attempt",
                "status",
                "conclusion",
            )
        }
        age = (now - times[0]).total_seconds() / 60
        latest["age_minutes"] = round(age, 2)
        if age > max_age_minutes:
            issues.append(
                f"No new scheduled release run for {age:.1f} minutes "
                f"(limit: {max_age_minutes} minutes)."
            )
        if latest["status"] == "completed" and latest["conclusion"] != "success":
            if inspect_failure is not None:
                details = inspect_failure(scheduled[0])
            if not details.get("recovered_release"):
                issues.append(
                    f"Latest scheduled release run concluded {latest['conclusion']}."
                )
    return {
        "checked_at": now.isoformat(),
        "healthy": not issues,
        "workflow_state": workflow["state"],
        "max_age_minutes": max_age_minutes,
        "scheduled_runs_observed": len(scheduled),
        "gap_minutes": {
            "min": round(min(gaps), 2),
            "median": round(median(gaps), 2),
            "max": round(max(gaps), 2),
        }
        if gaps
        else None,
        "latest_scheduled_run": latest,
        "issues": issues,
        **details,
    }


def published_after_failure(api, prepare_id, failed_at):
    # release.py prints the selected tag even when source preparation later fails.
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{prepare_id}/logs"
    )
    if api.token:
        # GitHub redirects to a signed download on a different host.
        request.add_unredirected_header("Authorization", f"Bearer {api.token}")
    target_line = re.compile(
        rb"\d{4}-\d{2}-\d{2}T[\d:.]+Z "
        rb"termux_release=(termux-v\d+\.\d+\.\d+\+termux\.[1-9]\d*)"
    )
    try:
        with urlopen(request, timeout=30) as log:
            tags = {
                match[1].decode("ascii")
                for line in log
                if (match := target_line.fullmatch(line.rstrip()))
            }
    except HTTPError as error:
        if error.code in (403, 404, 410):
            error.close()
            return None
        raise
    if len(tags) != 1:
        return None
    tag = tags.pop()
    release = api.repo(f"releases/tags/{quote(tag, safe='')}")
    if (
        not release
        or release["tag_name"] != tag
        or release["draft"]
        or release["prerelease"]
    ):
        return None
    published_at = release.get("published_at")
    if not published_at or (
        datetime.fromisoformat(published_at) <= datetime.fromisoformat(failed_at)
    ):
        return None
    uploaded = {
        asset["name"]
        for asset in release["assets"]
        if asset["state"] == "uploaded" and asset["size"] > 0
    }
    if not uploaded.issuperset(ASSETS):
        return None
    return {key: release[key] for key in ("tag_name", "published_at", "html_url")}


def failure_details(api, run):
    details = {"failed_jobs": []}
    prepare_id = None
    page = 1
    while True:
        jobs = api.repo(f"actions/runs/{run['id']}/jobs?per_page=100&page={page}")
        if jobs is None:
            raise RuntimeError("The failed scheduled run's jobs are unavailable.")
        for job in jobs["jobs"]:
            if job.get("name") == "prepare":
                prepare_id = job["id"]
            if job["conclusion"] in (
                "failure",
                "timed_out",
                "cancelled",
                "action_required",
            ):
                details["failed_jobs"].append(
                    {
                        "name": job["name"],
                        "conclusion": job["conclusion"],
                        "url": job["html_url"],
                        "failed_steps": [
                            step["name"]
                            for step in job.get("steps", [])
                            if step["conclusion"]
                            in ("failure", "timed_out", "cancelled")
                        ],
                    }
                )
        if page * 100 >= jobs["total_count"]:
            break
        page += 1
    if prepare_id is not None and run["conclusion"] == "failure":
        recovered = published_after_failure(api, prepare_id, run["updated_at"])
        if recovered:
            details["recovered_release"] = recovered
    return details


def check(api, now, max_age_minutes):
    workflow = api.repo(WORKFLOW)
    runs = api.repo(f"{WORKFLOW}/runs?event=schedule&per_page=100")
    if workflow is None or runs is None:
        raise RuntimeError("Release workflow or its run history is unavailable.")
    report = assess(
        workflow,
        runs["workflow_runs"],
        now,
        max_age_minutes,
        inspect_failure=lambda run: failure_details(api, run),
    )
    report.setdefault("failed_jobs", [])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-minutes", type=int, default=480)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.max_age_minutes <= 0:
        parser.error("--max-age-minutes must be positive")
    now = datetime.now(timezone.utc)
    try:
        report = check(GitHub(os.environ.get("GH_TOKEN")), now, args.max_age_minutes)
    except Exception as error:
        # Always retain evidence when GitHub itself cannot be queried. An API
        # error is an unknown state, never proof that the scheduler is healthy.
        report = {
            "checked_at": now.isoformat(),
            "healthy": False,
            "issues": [
                f"Could not check release scheduling: {type(error).__name__}: {error}"
            ],
        }
    serialized = json.dumps(report, indent=2) + "\n"
    print(serialized, end="")
    if args.output:
        args.output.write_text(serialized)
    for issue in report["issues"]:
        # Escape workflow command data, including error messages from HTTP APIs.
        escaped = issue.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{escaped}")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write("## Release schedule health\n\n")
            output.write(f"Checked at `{report['checked_at']}`.\n\n")
            output.write(
                "Healthy.\n\n" if report["healthy"] else "Needs attention.\n\n"
            )
            for issue in report["issues"]:
                output.write(f"- {issue}\n")
            if latest := report.get("latest_scheduled_run"):
                output.write(
                    f"\n[Latest scheduled run]({latest['html_url']}): "
                    f"created `{latest['created_at']}`, commit `{latest['head_sha']}`, "
                    f"status `{latest['status']}`, conclusion `{latest['conclusion']}`.\n\n"
                )
            if gaps := report.get("gap_minutes"):
                output.write(
                    f"Observed intervals in minutes: min {gaps['min']}, "
                    f"median {gaps['median']}, max {gaps['max']}.\n\n"
                )
            if release := report.get("recovered_release"):
                output.write(
                    f"Release recovered. [{release['tag_name']}]({release['html_url']}) "
                    f"was published at `{release['published_at']}` with all required assets.\n\n"
                )
            for job in report.get("failed_jobs", []):
                output.write(
                    f"- [{job['name']}]({job['url']}): {job['conclusion']}; {', '.join(job['failed_steps'])}\n"
                )
            output.write(
                "\nOnly original schedule events count as scheduler activity. "
                "Manual runs, pushes and reruns do not demonstrate a new timer event. "
                "The configured age threshold is an alert policy, not a GitHub scheduling guarantee. "
                "This check also runs on GitHub Actions and cannot detect an outage until it runs.\n"
            )
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
