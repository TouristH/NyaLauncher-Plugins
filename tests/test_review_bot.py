import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import review_bot


PLUGIN_ID = "dev.example.test"
VERSION = "1.2.3"
SHA256 = "a" * 64


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


class ReviewBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(review_bot, "ROOT", self.root)
        self.validator_root_patch = patch.object(review_bot.validator, "ROOT", self.root)
        self.root_patch.start()
        self.validator_root_patch.start()
        self.comments_patch = patch.object(
            review_bot, "fetch_issue_comments_since", return_value=[]
        )
        self.comments = self.comments_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.validator_root_patch.stop)
        self.addCleanup(self.comments_patch.stop)
        self.addCleanup(self.temporary.cleanup)

        write_json(
            self.root / "repository.json",
            {"trustedReviewers": ["TouristH", "SecondAdmin"]},
        )
        write_json(
            self.root / "plugins" / PLUGIN_ID / "plugin.json",
            {
                "$schema": "../../../schemas/catalog-plugin-v1.schema.json",
                "schemaVersion": 1,
                "id": PLUGIN_ID,
                "name": "Test Plugin",
                "description": "Fixture plugin.",
                "authors": ["Example"],
                "repositoryUrl": "https://github.com/example/test",
                "maintainers": ["Example"],
                "categories": ["utilities"],
                "license": "MIT",
            },
        )
        self.release_path = (
            self.root / "plugins" / PLUGIN_ID / "releases" / f"{VERSION}.json"
        )
        write_json(
            self.release_path,
            {
                "$schema": "../../../../schemas/catalog-release-v1.schema.json",
                "schemaVersion": 1,
                "version": VERSION,
                "channel": "stable",
                "publishedAt": "2026-08-20T00:00:00Z",
                "releaseNotesUrl": (
                    "https://github.com/example/test/releases/tag/v1.2.3"
                ),
                "download": {
                    "url": (
                        "https://github.com/example/test/releases/download/v1.2.3/"
                        "dev.example.test-1.2.3.zip"
                    ),
                    "sha256": SHA256,
                    "size": 7,
                },
                "compatibility": {
                    "manifestVersion": 1,
                    "apiVersion": "1.0",
                    "minimumLauncherVersion": "0.1.0",
                },
                "requiredCapabilities": [],
                "optionalCapabilities": [],
                "yanked": False,
            },
        )

    @property
    def review_path(self) -> Path:
        return self.root / "reviews" / PLUGIN_ID / f"{VERSION}.json"

    def event(
        self,
        action: str = "verify",
        *,
        actor: str = "TouristH",
        sha256: str = SHA256,
        note: str | None = None,
        created_at: str = "2026-08-21T01:02:03Z",
        comment_id: int = 100,
        issue_body: str = "untrusted mutable body",
    ) -> dict:
        suffix = f" {note}" if note is not None else ""
        return {
            "issue": {
                "number": 42,
                "title": f"[Review] {PLUGIN_ID} {VERSION}",
                "body": issue_body,
            },
            "comment": {
                "id": comment_id,
                "body": (
                    f"/{action} {PLUGIN_ID}@{VERSION} sha256:{sha256}{suffix}"
                ),
                "created_at": created_at,
                "user": {"login": actor},
            },
            "sender": {"login": actor},
        }

    def apply_verified(self, event: dict | None = None) -> review_bot.ApplyResult:
        with (
            patch.object(
                review_bot.validator,
                "download_release_asset",
                return_value=b"payload",
            ),
            patch.object(review_bot.validator, "validate_runtime_package"),
        ):
            return review_bot.apply_review(event or self.event())

    def test_parser_accepts_exact_commands_and_optional_note(self) -> None:
        verify = review_bot.parse_command(
            f"/verify {PLUGIN_ID}@1.2.3-preview.1+build.5 sha256:{SHA256} 已审阅源码"
        )
        self.assertEqual(verify.action, "verify")
        self.assertEqual(verify.version, "1.2.3-preview.1+build.5")
        self.assertEqual(verify.note, "已审阅源码")

        revoke = review_bot.parse_command(
            f"/revoke-review {PLUGIN_ID}@{VERSION} sha256:{SHA256} 安全问题"
        )
        self.assertEqual(revoke.action, "revoke-review")
        self.assertEqual(revoke.note, "安全问题")

    def test_parser_rejects_non_exact_or_unsafe_commands(self) -> None:
        invalid = [
            f" /verify {PLUGIN_ID}@{VERSION} sha256:{SHA256}",
            f"/verify {PLUGIN_ID}@{VERSION} sha256:{SHA256} ",
            f"/verify {PLUGIN_ID}@{VERSION} sha256:{SHA256}\nsecond line",
            f"/verify DEV.example.test@{VERSION} sha256:{SHA256}",
            f"/verify {PLUGIN_ID}@01.2.3 sha256:{SHA256}",
            f"/verify {PLUGIN_ID}@{VERSION} sha256:{SHA256.upper()}",
            f"/verify {PLUGIN_ID}@{VERSION} {SHA256}",
            f"/verify {PLUGIN_ID}@{VERSION} sha256:{SHA256} hidden\u200btext",
        ]
        for body in invalid:
            with self.subTest(body=body[:100]):
                with self.assertRaises(review_bot.ReviewPermissionFailure):
                    review_bot.parse_command(body)

    def test_authorization_uses_comment_actor_not_sender_or_issue_author(self) -> None:
        event = self.event(actor="stranger")
        event["sender"]["login"] = "TouristH"
        event["issue"]["user"] = {"login": "TouristH"}
        with self.assertRaises(review_bot.ReviewPermissionFailure):
            review_bot.authorize_event(event)

        event = self.event(actor="TOURISTH")
        command, actor = review_bot.authorize_event(event)
        self.assertEqual(actor, "TOURISTH")
        self.assertEqual(command.plugin_id, PLUGIN_ID)

        event["issue"]["title"] = f"[Plugin] {PLUGIN_ID}"
        with self.assertRaises(review_bot.ReviewPermissionFailure):
            review_bot.authorize_event(event)

    def test_verify_binds_canonical_sha_redownloads_and_writes_review(self) -> None:
        event = self.event(
            note="人工检查通过",
            issue_body="edited to claim dev.example.other@9.9.9 and another hash",
        )
        with (
            patch.object(
                review_bot.validator,
                "download_release_asset",
                return_value=b"payload",
            ) as download,
            patch.object(
                review_bot.validator, "validate_runtime_package"
            ) as validate_package,
        ):
            result = review_bot.apply_review(event)

        self.assertTrue(result.changed)
        download.assert_called_once()
        validate_package.assert_called_once()
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertEqual(
            review,
            {
                "$schema": "../../schemas/review-v1.schema.json",
                "schemaVersion": 1,
                "pluginId": PLUGIN_ID,
                "version": VERSION,
                "sha256": SHA256,
                "status": "verified",
                "stateBy": "TouristH",
                "stateAt": "2026-08-21T01:02:03Z",
                "lastCommandAt": "2026-08-21T01:02:03Z",
                "lastCommentId": 100,
                "notes": "人工检查通过",
            },
        )
        self.assertNotIn("dev.example.other", self.review_path.read_text(encoding="utf-8"))

    def test_later_verify_preserves_first_review_but_advances_order_marker(self) -> None:
        self.apply_verified(self.event(note="首次审核"))
        retry = self.event(
            actor="SecondAdmin",
            note="不得覆盖",
            created_at="2026-08-21T02:03:04Z",
            comment_id=200,
        )
        with (
            patch.object(
                review_bot.validator,
                "download_release_asset",
                return_value=b"payload",
            ) as download,
            patch.object(review_bot.validator, "validate_runtime_package") as runtime,
        ):
            result = review_bot.apply_review(retry)

        self.assertTrue(result.changed)
        self.assertEqual(result.reviewed_by, "TouristH")
        self.assertEqual(result.reviewed_at, "2026-08-21T01:02:03Z")
        self.assertEqual(result.note, "首次审核")
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertEqual(review["stateBy"], "TouristH")
        self.assertEqual(review["stateAt"], "2026-08-21T01:02:03Z")
        self.assertEqual(review["notes"], "首次审核")
        self.assertEqual(review["lastCommentId"], 200)
        download.assert_called_once()
        runtime.assert_called_once()

        with (
            patch.object(review_bot.validator, "download_release_asset", return_value=b"payload"),
            patch.object(review_bot.validator, "validate_runtime_package"),
        ):
            exact_retry = review_bot.apply_review(retry)
        self.assertFalse(exact_retry.changed)

    def test_verify_fails_hard_when_runtime_package_validation_fails(self) -> None:
        with (
            patch.object(
                review_bot.validator,
                "download_release_asset",
                return_value=b"payload",
            ),
            patch.object(
                review_bot.validator,
                "validate_runtime_package",
                side_effect=review_bot.validator.ValidationFailure("bad package"),
            ),
        ):
            with self.assertRaises(review_bot.validator.ValidationFailure):
                review_bot.apply_review(self.event())
        self.assertFalse(self.review_path.exists())

    def test_wrong_canonical_sha_and_yanked_release_are_rejected(self) -> None:
        with patch.object(review_bot.validator, "download_release_asset") as download:
            with self.assertRaises(review_bot.ReviewFailure):
                review_bot.apply_review(self.event(sha256="b" * 64))
            download.assert_not_called()

        release = json.loads(self.release_path.read_text(encoding="utf-8"))
        release["yanked"] = True
        release["yankReason"] = "Known risk."
        write_json(self.release_path, release)
        with patch.object(review_bot.validator, "download_release_asset") as download:
            with self.assertRaises(review_bot.ReviewFailure):
                review_bot.apply_review(self.event())
            download.assert_not_called()

    def test_comment_timestamp_must_be_utc_second_precision(self) -> None:
        with (
            patch.object(
                review_bot.validator,
                "download_release_asset",
                return_value=b"payload",
            ),
            patch.object(review_bot.validator, "validate_runtime_package"),
        ):
            with self.assertRaises(review_bot.ReviewFailure):
                review_bot.apply_review(
                    self.event(created_at="2026-08-21T01:02:03.123Z")
                )
        self.assertFalse(self.review_path.exists())

    def test_revoke_is_canonical_and_idempotent_without_downloading(self) -> None:
        self.apply_verified()
        revoke = self.event(
            action="revoke-review",
            note="撤销原因",
            created_at="2026-08-21T02:00:00Z",
            comment_id=200,
        )
        with patch.object(review_bot.validator, "download_release_asset") as download:
            first = review_bot.apply_review(revoke)
            second = review_bot.apply_review(revoke)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertEqual(review["status"], "revoked")
        self.assertEqual(review["stateBy"], "TouristH")
        self.assertEqual(review["lastCommentId"], 200)
        download.assert_not_called()

    def test_newer_revoke_blocks_an_older_verify_even_if_jobs_run_backwards(self) -> None:
        newer_revoke = self.event(
            action="revoke-review",
            note="撤销",
            created_at="2026-08-21T03:00:00Z",
            comment_id=300,
        )
        older_verify = self.event(
            action="verify",
            created_at="2026-08-21T02:00:00Z",
            comment_id=200,
        )
        with patch.object(review_bot.validator, "download_release_asset") as download:
            review_bot.apply_review(newer_revoke)
            with self.assertRaises(review_bot.ReviewFailure):
                review_bot.apply_review(older_verify)
        download.assert_not_called()
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertEqual(review["status"], "revoked")
        self.assertEqual(review["lastCommentId"], 300)

    def test_unmerged_newer_trusted_command_in_another_issue_blocks_older_verify(self) -> None:
        older_verify = self.event(
            action="verify",
            created_at="2026-08-21T02:00:00Z",
            comment_id=200,
        )
        newer_revoke = self.event(
            action="revoke-review",
            created_at="2026-08-21T03:00:00Z",
            comment_id=300,
        )["comment"]
        newer_revoke["issue_url"] = (
            "https://api.github.com/repos/TouristH/NyaLauncher-Plugins/issues/99"
        )
        self.comments.return_value = [newer_revoke]

        with patch.object(review_bot.validator, "download_release_asset") as download:
            with self.assertRaisesRegex(review_bot.ReviewFailure, "更新的可信管理员命令"):
                review_bot.apply_review(older_verify)
        download.assert_not_called()
        self.assertFalse(self.review_path.exists())

    def test_untrusted_later_comment_does_not_block_verification(self) -> None:
        later = self.event(
            action="revoke-review",
            actor="stranger",
            created_at="2026-08-21T03:00:00Z",
            comment_id=300,
        )["comment"]
        self.comments.return_value = [later]
        result = self.apply_verified()
        self.assertTrue(result.changed)

    def test_revoke_refuses_to_delete_a_mismatched_review_record(self) -> None:
        self.apply_verified()
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        review["sha256"] = "b" * 64
        write_json(self.review_path, review)
        with self.assertRaises(
            (review_bot.ReviewFailure, review_bot.validator.ValidationFailure)
        ):
            review_bot.apply_review(
                self.event(
                    action="revoke-review",
                    created_at="2026-08-21T02:00:00Z",
                    comment_id=200,
                )
            )
        self.assertTrue(self.review_path.exists())

    def test_apply_cli_writes_summary_and_safe_github_outputs(self) -> None:
        event_path = self.root / "event.json"
        summary_path = self.root / "summary.md"
        output_path = self.root / "github-output.txt"
        write_json(event_path, self.event(note="CLI 审核"))
        with (
            patch.object(
                review_bot.validator,
                "download_release_asset",
                return_value=b"payload",
            ),
            patch.object(review_bot.validator, "validate_runtime_package"),
            patch.object(
                review_bot.sys,
                "argv",
                [
                    "review_bot.py",
                    "apply",
                    "--event",
                    str(event_path),
                    "--summary",
                    str(summary_path),
                    "--github-output",
                    str(output_path),
                ],
            ),
        ):
            self.assertEqual(review_bot.main(), 0)

        summary = summary_path.read_text(encoding="utf-8")
        self.assertIn("人工审核已确认", summary)
        self.assertIn("CLI 审核", summary)
        self.assertEqual(
            output_path.read_text(encoding="utf-8"),
            (
                "action=verify\n"
                f"plugin_id={PLUGIN_ID}\n"
                f"version={VERSION}\n"
                f"sha256={SHA256}\n"
                f"review_path=reviews/{PLUGIN_ID}/{VERSION}.json\n"
                "changed=true\n"
            ),
        )


if __name__ == "__main__":
    unittest.main()
