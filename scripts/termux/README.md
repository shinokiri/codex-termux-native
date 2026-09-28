# Release automation checks

`termux-release.yml` checks the latest official stable release and builds only
when the corresponding Termux package has not been published. Its five-minute
cron is a requested schedule, not a delivery guarantee. GitHub can delay or drop
scheduled events; see [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

`termux-upstream.yml` also checks the release scheduler hourly, after a release
workflow finishes, on relevant pushes, or when dispatched manually. It retains
`termux-release-schedule.json` as an artifact for 14 days and adds a job summary
with the most recent scheduled run, observed intervals and links to failed jobs.
This monitor shares GitHub Actions with the release workflow. It reports gaps
when it runs; it cannot independently guarantee detection during a GitHub outage.

Run the same read-only diagnosis locally:

```sh
python3 scripts/termux/check_release_schedule.py --output release-schedule.json
```

An existing `GH_TOKEN` is used when provided. Exit status 1 means the workflow is
disabled, no original scheduled event arrived within the alert threshold, the
latest scheduled run did not succeed, or GitHub could not be queried. The default
threshold is 60 minutes (twelve requested intervals), configurable with
`--max-age-minutes`. A recent queued or running release is reported as pending
work, without treating it as a failed build.

Only `event=schedule` with its original `created_at` timestamp advances the
scheduler's clock. A manual check, push-triggered run or retry can confirm release
logic but does not demonstrate a new timer event. A historical failed run remains
visible until a newer scheduled run arrives, even if the package was subsequently
published manually. Use the linked job and release history to distinguish these
cases. This diagnosis never starts builds, retries failed jobs or publishes.

When a scheduled check reports `release-needs-maintenance`, inspect the original
failed release run and repair its prepared source before dispatching a retry.
An `already-published` result should skip all build and publish jobs. Verify these
results in the release run's prepare log, rather than inferring them from the
monitor's success alone.
