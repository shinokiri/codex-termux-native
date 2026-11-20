"""Distinguish real scheduler activity, reruns, release failures and API outages."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

from scripts.termux.check_release_schedule import (
    assess,
    check,
    main,
    published_after_failure,
)
from scripts.termux.release import ASSETS


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
TAG = "termux-v0.171.0+termux.17"
TARGET_LINE = f"2026-09-28T11:32:01.1234567Z termux_release={TAG}\r\n".encode()
FAILED_AT = "2026-09-28T11:35:00Z"


def publication(**changes):
    return {
        "tag_name": TAG,
        "published_at": "2026-09-28T11:45:00Z",
        "html_url": "https://github.com/example/repo/releases/tag/" + TAG,
        "draft": False,
        "prerelease": False,
        "assets": [{"name": name, "state": "uploaded", "size": 1} for name in ASSETS],
        **changes,
    }


def run(identifier, created_at, **changes):
    return {
        "id": identifier,
        "html_url": f"https://github.com/example/repo/actions/runs/{identifier}",
        "event": "schedule",
        "head_sha": "a" * 40,
        "created_at": created_at,
        "run_started_at": created_at,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


class ReleaseScheduleTest(unittest.TestCase):
    def test_manual_runs_and_recent_reruns_do_not_hide_a_stale_timer(self):
        scheduled = run(
            1,
            "2026-09-28T10:00:00Z",
            run_attempt=3,
            run_started_at="2026-09-28T11:59:00Z",
        )
        history = [
            run(2, "2026-09-28T11:59:00Z", event="workflow_dispatch"),
            run(3, "2026-09-28T11:58:00Z", event="push"),
            scheduled,
        ]
        report = assess({"state": "active"}, history, NOW, 60)
        self.assertEqual(
            report,
            {
                "checked_at": NOW.isoformat(),
                "healthy": False,
                "workflow_state": "active",
                "max_age_minutes": 60,
                "scheduled_runs_observed": 1,
                "gap_minutes": None,
                "latest_scheduled_run": {**scheduled, "age_minutes": 120.0},
                "issues": [
                    "No new scheduled release run for 120.0 minutes (limit: 60 minutes)."
                ],
            },
        )

    def test_a_new_successful_schedule_recovers_from_an_older_failure(self):
        history = [
            run(1, "2026-09-28T09:00:00Z"),
            run(3, "2026-09-28T11:40:00Z"),
            run(2, "2026-09-28T10:00:00Z", conclusion="failure"),
        ]
        report = assess({"state": "active"}, history, NOW, 60)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(
            report["gap_minutes"], {"min": 60.0, "median": 80.0, "max": 100.0}
        )
        self.assertEqual(
            report["latest_scheduled_run"], {**history[1], "age_minutes": 20.0}
        )

    def test_disabled_workflow_and_absent_schedule_are_reported(self):
        report = assess(
            {"state": "disabled_inactivity"},
            [run(1, "2026-09-28T11:59:00Z", event="workflow_dispatch")],
            NOW,
            60,
        )
        self.assertFalse(report["healthy"])
        self.assertIsNone(report["latest_scheduled_run"])
        self.assertEqual(
            report["issues"],
            [
                "Release workflow is disabled_inactivity.",
                "No scheduled release runs were found.",
            ],
        )

    def test_alert_threshold_does_not_fail_a_recent_running_build(self):
        history = [
            run(1, "2026-09-28T11:00:00Z", status="in_progress", conclusion=None)
        ]
        self.assertTrue(assess({"state": "active"}, history, NOW, 60)["healthy"])
        self.assertFalse(assess({"state": "active"}, history, NOW, 59)["healthy"])

    def test_failed_job_links_survive_pagination(self):
        api = Mock()
        api.repo.side_effect = [
            {"state": "active"},
            {"workflow_runs": [run(7, "2026-09-28T11:50:00Z", conclusion="failure")]},
            {"total_count": 101, "jobs": [{"conclusion": "success"}] * 100},
            {
                "total_count": 101,
                "jobs": [
                    {
                        "name": "release-blocked",
                        "conclusion": "failure",
                        "html_url": "https://github.com/example/repo/actions/runs/7/job/9",
                        "steps": [
                            {"name": "Setup", "conclusion": "success"},
                            {
                                "name": "Report the unpublished release",
                                "conclusion": "failure",
                            },
                        ],
                    }
                ],
            },
        ]
        report = check(api, NOW, 60)
        self.assertFalse(report["healthy"])
        self.assertEqual(
            report["issues"], ["Latest scheduled release run concluded failure."]
        )
        self.assertEqual(
            report["failed_jobs"],
            [
                {
                    "name": "release-blocked",
                    "conclusion": "failure",
                    "url": "https://github.com/example/repo/actions/runs/7/job/9",
                    "failed_steps": ["Report the unpublished release"],
                }
            ],
        )
        self.assertEqual(
            api.repo.call_args.args, ("actions/runs/7/jobs?per_page=100&page=2",)
        )

    def test_publication_recovers_the_failed_target_and_preserves_timer_health(self):
        failed = run(
            7, "2026-09-28T11:30:00Z", conclusion="failure", updated_at=FAILED_AT
        )
        manual = run(8, "2026-09-28T11:50:00Z", event="workflow_dispatch")
        prepare = {
            "id": 9,
            "name": "prepare",
            "conclusion": "failure",
            "html_url": "job/9",
        }
        for state, threshold, issues in (
            ("active", 60, []),
            (
                "active",
                20,
                ["No new scheduled release run for 30.0 minutes (limit: 20 minutes)."],
            ),
            ("disabled_manually", 60, ["Release workflow is disabled_manually."]),
        ):
            with self.subTest(state=state, threshold=threshold):
                api = Mock(token="test-token")
                api.repo.side_effect = [
                    {"state": state},
                    {"workflow_runs": [manual, failed]},
                    {"total_count": 1, "jobs": [prepare]},
                    publication(),
                ]
                with patch(
                    "scripts.termux.check_release_schedule.urlopen",
                    return_value=io.BytesIO(TARGET_LINE * 2),
                ) as open_log:
                    report = check(api, NOW, threshold)
                self.assertEqual(report["issues"], issues)
                self.assertEqual(report["healthy"], not issues)
                self.assertEqual(report["latest_scheduled_run"]["id"], 7)
                self.assertEqual(report["failed_jobs"][0]["url"], "job/9")
                self.assertEqual(report["recovered_release"]["tag_name"], TAG)
                api.repo.assert_called_with("releases/tags/termux-v0.171.0%2Btermux.17")
                request = open_log.call_args.args[0]
                self.assertEqual(
                    request.get_header("Authorization"), "Bearer test-token"
                )
                redirect = HTTPRedirectHandler().redirect_request(
                    request, None, 302, "", {}, "https://storage.example/job.log"
                )
                self.assertIsNone(redirect.get_header("Authorization"))

    def test_unpublished_wrong_old_or_incomplete_package_keeps_failure_open(self):
        missing_checksum = [
            a for a in publication()["assets"] if a["name"] != ASSETS[1]
        ]
        for release in (
            None,
            publication(tag_name="termux-v0.171.0+termux.18"),
            publication(draft=True),
            publication(prerelease=True),
            publication(published_at="2026-09-28T11:34:59Z"),
            publication(published_at=FAILED_AT),
            publication(assets=missing_checksum),
            publication(assets=[dict(a, size=0) for a in publication()["assets"]]),
            publication(assets=[dict(a, state="new") for a in publication()["assets"]]),
        ):
            with (
                self.subTest(release=release),
                patch(
                    "scripts.termux.check_release_schedule.urlopen",
                    return_value=io.BytesIO(TARGET_LINE),
                ),
            ):
                api = Mock(token=None)
                api.repo.return_value = release
                self.assertIsNone(published_after_failure(api, 9, FAILED_AT))

    def test_missing_ambiguous_or_expired_target_keeps_failure_open(self):
        api = Mock(token=None)
        for content in (
            b"no target here\n",
            b"echo " + TARGET_LINE,
            TARGET_LINE + TARGET_LINE.replace(b"termux.17", b"termux.18"),
        ):
            with (
                self.subTest(content=content),
                patch(
                    "scripts.termux.check_release_schedule.urlopen",
                    return_value=io.BytesIO(content),
                ),
            ):
                self.assertIsNone(published_after_failure(api, 9, FAILED_AT))
        with patch(
            "scripts.termux.check_release_schedule.urlopen",
            side_effect=HTTPError("https://api.github.com/", 410, "Gone", {}, None),
        ):
            self.assertIsNone(published_after_failure(api, 9, FAILED_AT))
        api.repo.assert_not_called()

    def test_api_failure_is_saved_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "health.json"
            summary = Path(temporary) / "summary.md"
            with (
                patch(
                    "sys.argv", ["check_release_schedule.py", "--output", str(output)]
                ),
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
                patch("scripts.termux.check_release_schedule.GitHub") as github,
                redirect_stdout(io.StringIO()),
            ):
                unavailable = HTTPError(
                    "https://api.github.com/",
                    503,
                    "Service Unavailable",
                    {},
                    io.BytesIO(),
                )
                self.addCleanup(unavailable.close)
                github.return_value.repo.side_effect = unavailable
                self.assertEqual(main(), 1)
            report = json.loads(output.read_text())
            self.assertFalse(report["healthy"])
            self.assertIn("HTTPError: HTTP Error 503", report["issues"][0])
            self.assertIn("Needs attention", summary.read_text())

    def test_missing_workflow_metadata_is_not_healthy(self):
        api = Mock()
        api.repo.return_value = None
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            check(api, NOW, 60)
