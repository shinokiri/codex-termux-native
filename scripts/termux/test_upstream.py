"""Keep a new release distinct from an updated main snapshot."""

import unittest

from scripts.termux.check_upstream import compare


class UpstreamTest(unittest.TestCase):
    def test_channels_are_compared_independently(self):
        upstream = {
            "ref": "main",
            "commit": "old-main",
            "latest_release_seen": "rust-v1.2.3",
        }
        for head, tag, changed_main, changed_release in (
            ("old-main", "rust-v1.2.3", False, False),
            ("new-main", "rust-v1.2.3", True, False),
            ("old-main", "rust-v1.2.4", False, True),
        ):
            with self.subTest(head=head, tag=tag):
                report = compare(
                    upstream,
                    {"sha": head, "html_url": "https://github.com/openai/codex"},
                    {
                        "tag_name": tag,
                        "html_url": "https://github.com/openai/codex/releases",
                        "published_at": "2026-09-06T00:00:00Z",
                    },
                )
                self.assertEqual(
                    (
                        report["main"]["differs_from_integrated_commit"],
                        report["stable_release"]["new_since_last_review"],
                    ),
                    (changed_main, changed_release),
                )
