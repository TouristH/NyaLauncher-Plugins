import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.issue_submission as submission


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class IssueSubmissionTests(unittest.TestCase):
    def test_parse_issue_form_sections(self):
        body = """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/example/test
"""
        self.assertEqual(
            submission.parse_sections(body),
            {
                "插件 ID / Plugin ID": "dev.example.test",
                "仓库地址 / Repository URL": "https://github.com/example/test",
            },
        )

    def test_request_metadata_is_safe_for_github_outputs(self):
        event = {
            "issue": {
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test
""",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output.txt"
            with patch.object(
                submission, "request_is_centrally_applied", return_value=False
            ):
                submission.write_request_metadata(event, output)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "request_kind=add\nplugin_id=dev.example.test\nneeds_refresh=true\n",
            )

    def test_applied_add_metadata_skips_an_unnecessary_remote_refresh(self):
        event = {
            "issue": {
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test
""",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output.txt"
            with patch.object(
                submission, "request_is_centrally_applied", return_value=True
            ):
                submission.write_request_metadata(event, output)
            self.assertIn("needs_refresh=false\n", output.read_text(encoding="utf-8"))

    def test_heavy_validation_requires_exact_command_from_trusted_reviewer(self):
        event = {
            "issue": {"user": {"login": "author"}},
            "comment": {"body": "/validate", "user": {"login": "stranger"}},
        }
        with patch.object(submission, "trusted_reviewers", return_value={"touristh"}):
            with self.assertRaises(submission.SubmissionFailure):
                submission.check_validation_permission(event)
            event["comment"]["user"]["login"] = "author"
            with self.assertRaises(submission.SubmissionFailure):
                submission.check_validation_permission(event)
            event["comment"]["user"]["login"] = "TouristH"
            submission.check_validation_permission(event)

    def test_approve_preflight_requires_exact_command_from_trusted_reviewer(self):
        event = {
            "issue": {"title": "[Plugin] dev.example.test"},
            "comment": {"body": "/approve", "user": {"login": "stranger"}},
        }
        with patch.object(submission, "trusted_reviewers", return_value={"touristh"}):
            with self.assertRaises(submission.PermissionFailure):
                submission.check_trusted_command_permission(event, "approve")
            event["comment"]["user"]["login"] = "TouristH"
            submission.check_trusted_command_permission(event, "approve")

    def test_reject_preflight_accepts_reason_but_not_command_prefix(self):
        event = {
            "issue": {"title": "[Plugin] dev.example.test"},
            "comment": {
                "body": "/reject 缺少必要的兼容性说明",
                "user": {"login": "TouristH"},
            },
        }
        with patch.object(submission, "trusted_reviewers", return_value={"touristh"}):
            submission.check_trusted_command_permission(event, "reject")
            event["comment"]["body"] = "/rejectevil"
            with self.assertRaises(submission.PermissionFailure):
                submission.check_trusted_command_permission(event, "reject")

    def test_unauthorized_validate_never_publishes_failure_state(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            write_json(
                event_path,
                {
                    "issue": {
                        "title": "[Plugin] dev.example.test",
                        "body": "",
                        "user": {"login": "author"},
                    },
                    "comment": {
                        "body": "/validate",
                        "user": {"login": "stranger"},
                    },
                },
            )
            with (
                patch.object(submission, "trusted_reviewers", return_value={"touristh"}),
                patch.object(submission, "publish_validation") as publish,
                patch.object(
                    submission.sys,
                    "argv",
                    [
                        "issue_submission.py",
                        "validate",
                        "--event",
                        str(event_path),
                        "--publish",
                    ],
                ),
            ):
                self.assertEqual(submission.main(), 1)
            publish.assert_not_called()

    def test_initialize_creates_labels_without_fetching_or_downloading(self):
        event = {"issue": {"number": 9, "title": "[Plugin] dev.example.test"}}
        calls = []

        def api(_event, method, path, body=None):
            calls.append((method, path, body))
            return {}

        with (
            patch.object(
                submission,
                "github_context",
                return_value=("TouristH/NyaLauncher-Plugins", 9, "token"),
            ),
            patch.object(submission, "github_api", side_effect=api),
            patch.object(submission.validator, "fetch_publisher_manifest") as fetch,
            patch.object(submission.validator, "download_release_asset") as download,
        ):
            submission.initialize_issue(event)

        fetch.assert_not_called()
        download.assert_not_called()
        created = {
            call[2]["name"]
            for call in calls
            if call[0] == "POST" and call[1] == "labels"
        }
        self.assertEqual(
            created,
            {
                "plugin-submission",
                "plugin-yank",
                "review-request",
                "queued-for-intake",
                "pending-review",
            },
        )
        self.assertIn(
            (
                "POST",
                "issues/9/labels",
                {"labels": ["plugin-submission", "queued-for-intake"]},
            ),
            calls,
        )

    def test_review_initialization_resolves_canonical_hash_for_prompt(self):
        sha256 = "a" * 64
        event = {
            "issue": {
                "number": 10,
                "title": "[Review] io.github.example.test 1.2.3",
                "body": (
                    "### 插件 ID / Plugin ID\n\nio.github.example.test\n\n"
                    "### 版本 / Version\n\n1.2.3\n"
                ),
            }
        }
        catalog = [
            {
                "id": "io.github.example.test",
                "releases": [
                    {
                        "version": "1.2.3",
                        "yanked": False,
                        "download": {"sha256": sha256},
                    }
                ],
            }
        ]
        calls = []

        def api(_event, method, path, body=None):
            calls.append((method, path, body))
            return [] if method == "GET" else {}

        with (
            patch.object(
                submission,
                "github_context",
                return_value=("TouristH/NyaLauncher-Plugins", 10, "token"),
            ),
            patch.object(submission, "github_api", side_effect=api),
            patch.object(submission.validator, "load_catalog", return_value=catalog),
        ):
            submission.initialize_issue(event)

        prompt = next(
            body["body"]
            for method, path, body in calls
            if method == "POST" and path == "issues/10/comments"
        )
        self.assertIn(
            f"/verify io.github.example.test@1.2.3 sha256:{sha256}",
            prompt,
        )

    def test_yank_keeps_history_and_revokes_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "repository.json", {"trustedReviewers": ["TouristH"]})
            write_json(
                root / "plugins.json",
                [
                    {
                        "id": "dev.example.test",
                        "repositoryUrl": "https://github.com/example/test",
                    }
                ],
            )
            release_path = root / "plugins/dev.example.test/releases/1.0.0.json"
            write_json(release_path, {"version": "1.0.0", "yanked": False})
            review_path = root / "reviews/dev.example.test/1.0.0.json"
            write_json(review_path, {"reviewer": "TouristH"})
            event = {
                "issue": {
                    "title": "[Yank] dev.example.test",
                    "body": """
### 插件 ID / Plugin ID

dev.example.test

### 版本 / Versions

1.0.0

### 撤回原因 / Reason

Known unsafe behavior.
""",
                },
                "comment": {"body": "/approve", "user": {"login": "TouristH"}},
            }
            with patch.object(submission, "ROOT", root):
                submission.apply_request(event, "TouristH")
                # A successful registry push followed by a failed GitHub API
                # completion can safely run the same yank approval again.
                submission.apply_request(event, "TouristH")
            release = json.loads(release_path.read_text(encoding="utf-8"))
            self.assertTrue(release["yanked"])
            self.assertEqual(release["yankReason"], "Known unsafe behavior.")
            self.assertFalse(review_path.exists())
            self.assertEqual(
                json.loads((root / "plugins.json").read_text(encoding="utf-8")),
                [
                    {
                        "id": "dev.example.test",
                        "repositoryUrl": "https://github.com/example/test",
                    }
                ],
            )

    def test_add_approval_is_idempotent_when_exact_request_is_already_central(self):
        event = {
            "issue": {
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/example/test/
""",
            },
            "comment": {"body": "/approve", "user": {"login": "TouristH"}},
        }
        listing = {
            "id": "dev.example.test",
            "repositoryUrl": "https://github.com/example/test",
        }
        central = {
            **listing,
            "name": "Test",
            "releases": [
                {
                    "version": "1.2.0",
                    "channel": "stable",
                    "download": {"size": 123, "sha256": "a" * 64},
                    "yanked": False,
                }
            ],
        }
        with (
            patch.object(submission, "trusted_reviewers", return_value={"touristh"}),
            patch.object(
                submission.validator, "load_plugin_list", return_value=[listing]
            ),
            patch.object(
                submission.validator, "load_catalog", return_value=[central]
            ),
            patch.object(submission, "validate_add") as validate_add,
        ):
            summary = submission.apply_request(event, "TouristH")

        self.assertIn("幂等重试", summary)
        self.assertIn("`1.2.0`", summary)
        validate_add.assert_not_called()

    def test_add_apply_defers_the_only_asset_download_to_target_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "plugins.json", [])
            event = {
                "issue": {
                    "title": "[Plugin] dev.example.test",
                    "body": """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/example/test
""",
                },
                "comment": {"body": "/approve", "user": {"login": "TouristH"}},
            }
            with (
                patch.object(submission, "ROOT", root),
                patch.object(submission, "trusted_reviewers", return_value={"touristh"}),
                patch.object(submission.validator, "load_plugin_list", return_value=[]),
                patch.object(submission.validator, "load_catalog", return_value=[]),
                patch.object(submission.validator, "fetch_publisher_manifest") as fetch,
                patch.object(submission.validator, "download_release_asset") as download,
            ):
                summary = submission.apply_request(event, "TouristH")

            fetch.assert_not_called()
            download.assert_not_called()
            self.assertIn("定向 Release ZIP 校验", summary)
            self.assertEqual(
                json.loads((root / "plugins.json").read_text(encoding="utf-8")),
                [
                    {
                        "id": "dev.example.test",
                        "repositoryUrl": "https://github.com/example/test",
                    }
                ],
            )

    def test_completion_comment_and_close_are_safe_to_retry_after_partial_failure(self):
        event = {"issue": {"number": 7}}
        comments = [
            {
                "user": {"login": "github-actions[bot]"},
                "body": (
                    "## ✅ 验证通过\n\n发布者元数据反射了 "
                    + submission.APPROVAL_COMMENT_MARKER
                ),
            }
        ]
        calls = []
        close_attempts = 0

        def api(_event, method, path, body=None):
            nonlocal close_attempts
            calls.append((method, path, body))
            if method == "GET" and path.startswith("issues/7/comments?"):
                return list(comments)
            if method == "POST" and path == "issues/7/comments":
                comments.append(
                    {
                        "user": {"login": "github-actions[bot]"},
                        "body": body["body"],
                    }
                )
                return {}
            if method == "PATCH" and path == "issues/7":
                close_attempts += 1
                if close_attempts == 1:
                    raise submission.SubmissionFailure("temporary close failure")
            return {}

        with (
            patch.object(
                submission,
                "github_context",
                return_value=("TouristH/NyaLauncher-Plugins", 7, "token"),
            ),
            patch.object(submission, "github_api", side_effect=api),
        ):
            with self.assertRaises(submission.SubmissionFailure):
                submission.publish_completion(event, "Approved fixture.")
            submission.publish_completion(event, "Approved fixture.")

        comment_posts = [
            call
            for call in calls
            if call[0] == "POST" and call[1] == "issues/7/comments"
        ]
        self.assertEqual(len(comment_posts), 1)
        self.assertEqual(close_attempts, 2)
        self.assertEqual(len(comments), 2)
        self.assertTrue(comments[1]["body"].startswith(submission.APPROVAL_COMMENT_HEADING))
        self.assertIn(submission.APPROVAL_COMMENT_MARKER, comments[1]["body"])

    def test_comment_flood_cannot_permanently_block_terminal_state(self):
        event = {"issue": {"number": 7}}
        flooded_page = [
            {"user": {"login": "someone"}, "body": "noise"}
            for _ in range(100)
        ]
        with patch.object(submission, "github_api", return_value=flooded_page) as api:
            self.assertFalse(
                submission.has_heading_comment(event, 7, submission.APPROVAL_COMMENT_HEADING)
            )
        self.assertEqual(api.call_count, 10)

    def test_terminal_preflight_refuses_the_opposite_decision(self):
        event = {"issue": {"number": 7}}
        with patch.object(submission, "live_issue_labels", return_value={"rejected"}):
            with self.assertRaises(submission.SubmissionFailure):
                submission.check_terminal_state(event, "approve")
        with patch.object(submission, "live_issue_labels", return_value={"approved"}):
            with self.assertRaises(submission.SubmissionFailure):
                submission.check_terminal_state(event, "reject")

    def test_approved_issue_cannot_be_edited_into_a_second_add_request(self):
        event = {
            "issue": {
                "number": 7,
                "title": "[Plugin] dev.example.second",
                "body": """
### 插件 ID / Plugin ID

dev.example.second

### 仓库地址 / Repository URL

https://github.com/example/second
""",
            }
        }
        first = {
            "id": "dev.example.first",
            "repositoryUrl": "https://github.com/example/first",
            "releases": [{"version": "1.0.0", "yanked": False}],
        }
        with (
            patch.object(submission, "live_issue_labels", return_value={"approved"}),
            patch.object(submission.validator, "load_plugin_list", return_value=[first]),
            patch.object(submission.validator, "load_catalog", return_value=[first]),
        ):
            with self.assertRaises(submission.SubmissionFailure):
                submission.check_terminal_state(event, "approve")

    def test_approved_issue_cannot_be_edited_into_a_second_yank_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "plugins/dev.example.test/releases/1.0.0.json",
                {
                    "version": "1.0.0",
                    "yanked": True,
                    "yankReason": "Original reason.",
                },
            )
            write_json(
                root / "plugins/dev.example.test/releases/2.0.0.json",
                {"version": "2.0.0", "yanked": False},
            )
            event = {
                "issue": {
                    "number": 7,
                    "title": "[Yank] dev.example.test",
                    "body": """
### 插件 ID / Plugin ID

dev.example.test

### 版本 / Versions

2.0.0

### 撤回原因 / Reason

Second request.
""",
                }
            }
            with (
                patch.object(submission, "ROOT", root),
                patch.object(submission, "live_issue_labels", return_value={"approved"}),
            ):
                with self.assertRaises(submission.SubmissionFailure):
                    submission.check_terminal_state(event, "approve")

    def test_late_validation_cannot_relabel_a_terminal_issue(self):
        event = {"issue": {"number": 7}}
        for terminal in ({"approved"}, {"rejected"}):
            with self.subTest(terminal=terminal), patch.object(
                submission, "live_issue_labels", return_value=terminal
            ), patch.object(submission, "github_api") as api:
                submission.publish_validation(event, True, "Late result")
                api.assert_not_called()

    def test_validation_cleans_up_when_approval_wins_during_api_writes(self):
        event = {"issue": {"number": 7}}
        calls = []

        def api(_event, method, path, body=None):
            calls.append((method, path, body))
            if method == "POST" and path == "issues/7/comments":
                return {"id": 99}
            return {}

        with (
            patch.object(
                submission,
                "github_context",
                return_value=("TouristH/NyaLauncher-Plugins", 7, "token"),
            ),
            patch.object(
                submission,
                "live_issue_labels",
                side_effect=[set(), {"approved"}],
            ),
            patch.object(submission, "github_api", side_effect=api),
        ):
            submission.publish_validation(event, True, "Late result")

        self.assertIn(("DELETE", "issues/comments/99", None), calls)
        for label in ("pending-validation", "validated", "validation-failed"):
            self.assertIn(("DELETE", f"issues/7/labels/{label}", None), calls)

    def test_reject_refuses_a_pushed_add_when_completion_failed(self):
        event = {
            "issue": {
                "number": 7,
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/example/test
""",
            }
        }
        listing = {
            "id": "dev.example.test",
            "repositoryUrl": "https://github.com/example/test",
        }
        central = {
            **listing,
            "releases": [{"version": "1.0.0", "yanked": False}],
        }
        with (
            patch.object(submission, "live_issue_labels", return_value=set()),
            patch.object(submission.validator, "load_plugin_list", return_value=[listing]),
            patch.object(submission.validator, "load_catalog", return_value=[central]),
        ):
            with self.assertRaises(submission.SubmissionFailure):
                submission.check_terminal_state(event, "reject")

    def test_reject_refuses_a_pushed_yank_when_completion_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "plugins/dev.example.test/releases/1.0.0.json",
                {
                    "version": "1.0.0",
                    "yanked": True,
                    "yankReason": "Known unsafe release.",
                },
            )
            event = {
                "issue": {
                    "number": 7,
                    "title": "[Yank] dev.example.test",
                    "body": """
### 插件 ID / Plugin ID

dev.example.test

### 版本 / Versions

1.0.0

### 撤回原因 / Reason

Known unsafe release.
""",
                }
            }
            with (
                patch.object(submission, "ROOT", root),
                patch.object(submission, "live_issue_labels", return_value=set()),
            ):
                with self.assertRaises(submission.SubmissionFailure):
                    submission.check_terminal_state(event, "reject")

    def test_reject_cannot_override_an_approved_issue(self):
        event = {
            "issue": {"number": 7},
            "comment": {
                "body": "/reject 已经不能再拒绝",
                "user": {"login": "TouristH"},
            },
        }
        with (
            patch.object(submission, "trusted_reviewers", return_value={"touristh"}),
            patch.object(
                submission,
                "github_context",
                return_value=("TouristH/NyaLauncher-Plugins", 7, "token"),
            ),
            patch.object(submission, "live_issue_labels", return_value={"approved"}),
            patch.object(submission, "github_api") as api,
        ):
            with self.assertRaises(submission.SubmissionFailure):
                submission.reject_request(event, "TouristH")
        api.assert_not_called()

    def test_partial_yank_keeps_active_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "repository.json", {"trustedReviewers": ["TouristH"]})
            listing = {
                "id": "dev.example.test",
                "repositoryUrl": "https://github.com/example/test",
            }
            write_json(root / "plugins.json", [listing])
            write_json(
                root / "plugins/dev.example.test/releases/1.0.0.json",
                {"version": "1.0.0", "yanked": False},
            )
            write_json(
                root / "plugins/dev.example.test/releases/2.0.0.json",
                {"version": "2.0.0", "yanked": False},
            )
            event = {
                "issue": {
                    "title": "[Yank] dev.example.test",
                    "body": """
### 插件 ID / Plugin ID

dev.example.test

### 版本 / Versions

1.0.0

### 撤回原因 / Reason

Broken legacy release.
""",
                },
                "comment": {"body": "/approve", "user": {"login": "TouristH"}},
            }
            with patch.object(submission, "ROOT", root):
                submission.apply_request(event, "TouristH")

            self.assertEqual(
                json.loads((root / "plugins.json").read_text(encoding="utf-8")),
                [listing],
            )

    def test_yank_rejects_plugin_id_path_traversal_before_filesystem_access(self):
        event = {
            "issue": {
                "title": "[Yank] traversal",
                "body": """
### 插件 ID / Plugin ID

../../outside

### 版本 / Versions

all

### 撤回原因 / Reason

Invalid target.
""",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(submission, "ROOT", Path(directory)):
                with self.assertRaises(submission.SubmissionFailure):
                    submission.parse_yank_request(event)

    def test_add_canonicalizes_repository_before_duplicate_check(self):
        event = {
            "issue": {
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/example/test/
""",
            }
        }
        existing = [
            {
                "id": "dev.example.other",
                "repositoryUrl": "https://github.com/example/test",
            }
        ]
        with (
            patch.object(submission.validator, "load_plugin_list", return_value=existing),
            patch.object(submission.validator, "fetch_publisher_manifest") as fetch,
        ):
            with self.assertRaises(submission.SubmissionFailure):
                submission.validate_add(event)
            fetch.assert_not_called()

    def test_first_add_validates_latest_bounded_batch_and_reports_backfill(self):
        event = {
            "issue": {
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/example/test
""",
            }
        }
        plugin = {
            "id": "dev.example.test",
            "name": "Test",
            "repositoryUrl": "https://github.com/example/test",
        }
        releases = [
            {
                "version": version,
                "channel": "stable",
                "download": {"size": 10, "sha256": character * 64},
            }
            for version, character in (("1.0.0", "a"), ("1.1.0", "b"), ("1.2.0", "c"))
        ]
        with (
            patch.object(submission.validator, "load_plugin_list", return_value=[]),
            patch.object(submission.validator, "load_catalog", return_value=[]),
            patch.object(
                submission.validator,
                "fetch_publisher_manifest",
                return_value={"publisher": "fixture"},
            ),
            patch.object(
                submission.validator,
                "validate_publisher_manifest_releases",
                return_value=(plugin, releases),
            ),
            patch.object(
                submission.validator,
                "publisher_missing_releases",
                return_value=releases,
            ),
            patch.object(
                submission.validator,
                "plan_publisher_candidates",
                return_value=releases[-2:],
            ),
            patch.object(
                submission.validator, "download_release_asset", return_value=b"zip"
            ) as download,
            patch.object(submission.validator, "validate_runtime_package") as runtime,
        ):
            summary, _, latest = submission.validate_add(event)

        self.assertEqual(download.call_count, 2)
        self.assertEqual(runtime.call_count, 2)
        self.assertEqual(latest["version"], "1.2.0")
        self.assertIn("仍有 `1` 个较早版本", summary)

    def test_archived_plugin_id_cannot_be_hijacked_or_reactivated_without_new_version(self):
        event_template = """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

{repository}
"""
        historical = {
            "id": "dev.example.test",
            "repositoryUrl": "https://github.com/original/test",
            "releases": [{"version": "2.0.0", "yanked": True}],
        }
        plugin = {
            "id": "dev.example.test",
            "name": "Test",
            "repositoryUrl": "https://github.com/original/test",
        }
        with patch.object(submission.validator, "load_plugin_list", return_value=[]), patch.object(
            submission.validator, "load_catalog", return_value=[historical]
        ):
            hijack = {
                "issue": {
                    "title": "[Plugin] dev.example.test",
                    "body": event_template.format(
                        repository="https://github.com/attacker/test"
                    ),
                }
            }
            with patch.object(submission.validator, "fetch_publisher_manifest") as fetch:
                with self.assertRaises(submission.SubmissionFailure):
                    submission.validate_add(hijack)
                fetch.assert_not_called()

            stale = {
                "issue": {
                    "title": "[Plugin] dev.example.test",
                    "body": event_template.format(
                        repository="https://github.com/original/test"
                    ),
                }
            }
            with (
                patch.object(
                    submission.validator,
                    "fetch_publisher_manifest",
                    return_value={"publisher": "fixture"},
                ),
                patch.object(
                    submission.validator,
                    "validate_publisher_manifest_releases",
                    return_value=(plugin, [{"version": "2.0.0"}]),
                ),
                patch.object(
                    submission.validator, "publisher_missing_releases", return_value=[]
                ),
                patch.object(
                    submission.validator, "plan_publisher_candidates", return_value=[]
                ),
                patch.object(submission.validator, "download_release_asset") as download,
            ):
                with self.assertRaises(submission.SubmissionFailure):
                    submission.validate_add(stale)
                download.assert_not_called()

    def test_archived_plugin_cannot_reactivate_with_only_a_lower_backfill(self):
        event = {
            "issue": {
                "title": "[Plugin] dev.example.test",
                "body": """
### 插件 ID / Plugin ID

dev.example.test

### 仓库地址 / Repository URL

https://github.com/original/test
""",
            }
        }
        historical = {
            "id": "dev.example.test",
            "repositoryUrl": "https://github.com/original/test",
            "releases": [{"version": "2.0.0", "yanked": True}],
        }
        plugin = {
            "id": "dev.example.test",
            "name": "Test",
            "repositoryUrl": "https://github.com/original/test",
        }
        lower = {"version": "1.5.0"}
        with (
            patch.object(submission.validator, "load_plugin_list", return_value=[]),
            patch.object(submission.validator, "load_catalog", return_value=[historical]),
            patch.object(
                submission.validator,
                "fetch_publisher_manifest",
                return_value={"publisher": "fixture"},
            ),
            patch.object(
                submission.validator,
                "validate_publisher_manifest_releases",
                return_value=(plugin, [lower, {"version": "2.0.0"}]),
            ),
            patch.object(
                submission.validator, "publisher_missing_releases", return_value=[lower]
            ),
            patch.object(
                submission.validator, "plan_publisher_candidates", return_value=[lower]
            ),
            patch.object(submission.validator, "download_release_asset") as download,
        ):
            with self.assertRaises(submission.SubmissionFailure):
                submission.validate_add(event)
        download.assert_not_called()

    def test_yank_limits_reason_and_version_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugins/dev.example.test/releases").mkdir(parents=True)
            base = """
### 插件 ID / Plugin ID

dev.example.test

### 版本 / Versions

{versions}

### 撤回原因 / Reason

{reason}
"""
            for versions, reason in (
                ("all", "x" * 1025),
                ("all", "😀" * 513),
                (",".join(f"1.0.{index}" for index in range(129)), "bounded"),
            ):
                with self.subTest(reason_length=len(reason), versions=versions[:20]):
                    event = {
                        "issue": {
                            "title": "[Yank] dev.example.test",
                            "body": base.format(versions=versions, reason=reason),
                        }
                    }
                    with patch.object(submission, "ROOT", root):
                        with self.assertRaises(submission.SubmissionFailure):
                            submission.parse_yank_request(event)

    def test_approve_is_exact_and_trusted(self):
        event = {
            "issue": {"title": "[Yank] test", "body": ""},
            "comment": {"body": "/approve now", "user": {"login": "TouristH"}},
        }
        with patch.object(submission, "trusted_reviewers", return_value={"touristh"}):
            with self.assertRaises(submission.SubmissionFailure):
                submission.apply_request(event, "TouristH")


if __name__ == "__main__":
    unittest.main()
