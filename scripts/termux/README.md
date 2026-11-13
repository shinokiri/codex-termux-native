# Release automation checks

## Android privacy and request metadata

Starting with package revision 17, only an **unset** `analytics.enabled` selects
Android's privacy defaults. In this mode inference request metadata retains
`analytics_enabled=true`, while analytics collection and uploads, built-in
Statsig, custom OTLP exports and feedback uploads remain disabled. No analytics
queue, capture file or upload worker is created. Local feedback ZIP saving still
works.

An explicit `analytics.enabled` value opts into the official interpretation of
that value, without an additional port-specific switch:

| User configuration | Request metadata | Analytics events | Built-in Statsig metrics | Custom OTLP and feedback |
| --- | --- | --- | --- | --- |
| Unset | `true` | Disabled | Disabled | Disabled by Android privacy defaults |
| `true` | `true` | Official enabled behavior | Official configured/default behavior | Follow their independent official settings |
| `false` | `false` | Disabled | Disabled | Follow their independent official settings |

To try official default reporting, put this in the user-level config and restart
Codex processes using that config:

```toml
[analytics]
enabled = true
```

Leave `otel` and `feedback` unconfigured for their official defaults. Explicit
OTLP collectors are independent of `analytics.enabled`; setting it to `false`
does not disable a configured collector. Feedback is also independently
controlled by `feedback.enabled`. To return to the Android privacy defaults,
remove the `analytics.enabled` entry and restart. CLI configuration overrides
follow the same rules; existing clients preserve their selected policy when
cloned or rebound to another authentication context.

Ordinary inference, authentication and feature configuration remain separate
from optional telemetry. Matching request metadata or enabling reporting does
not establish eligibility for a private model experiment. Normal inference
still sends task context, and account data-use settings remain separate.

## Release scheduling

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
threshold is 480 minutes (eight hours), configurable with `--max-age-minutes`.
This accommodates the accepted hourly delivery cadence: the September 28, 2026
audit of 100 scheduled runs found intervals of 111–403 minutes. The threshold
controls when a scheduling gap becomes an error; the release cron still requests
a check every five minutes. Failed runs, disabled workflows and API errors remain
errors regardless of this age threshold. A recent queued or running release is
reported as pending work, without treating it as a failed build.

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
