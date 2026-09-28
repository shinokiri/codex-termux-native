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

from scripts.termux.check_release_schedule import assess, check, main


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


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
                github.return_value.repo.side_effect = HTTPError(
                    "https://api.github.com/",
                    503,
                    "Service Unavailable",
                    {},
                    None,
                )
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
