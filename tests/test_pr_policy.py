import unittest
from pathlib import Path

from tools.check_pr import evaluate_policy, parse_name_status


TRUSTED = {"touristh"}
REGISTRY_BOT = "nyalauncher-registry-bot[bot]"
REPOSITORY = "TouristH/NyaLauncher-Plugins"


def evaluate(
    changes,
    actor="contributor",
    yank_only=False,
    review_matches=True,
    *,
    bot_login=None,
    base_repository=None,
    head_repository=None,
    head_ref=None,
    event_sender=None,
    event_action=None,
):
    return evaluate_policy(
        changes,
        actor,
        TRUSTED,
        lambda _path: yank_only,
        lambda _path, _actor: review_matches,
        registry_bot_login=bot_login,
        base_repository=base_repository,
        head_repository=head_repository,
        head_ref=head_ref,
        event_sender=event_sender,
        event_action=event_action,
    )


def evaluate_bot(changes, kind, *, yank_only=False, review_matches=True, **overrides):
    values = {
        "actor": REGISTRY_BOT,
        "bot_login": REGISTRY_BOT,
        "base_repository": REPOSITORY,
        "head_repository": REPOSITORY,
        "head_ref": f"registry-bot/{kind}/run-123",
        "event_sender": REGISTRY_BOT,
        "event_action": "synchronize",
    }
    values.update(overrides)
    return evaluate(
        changes,
        yank_only=yank_only,
        review_matches=review_matches,
        **values,
    )


class PullRequestPolicyTests(unittest.TestCase):
    def test_bot_workflows_only_reuse_same_repository_app_prs(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("refresh.yml", "approve-issue.yml", "review-issue.yml"):
            with self.subTest(workflow=name):
                text = (root / ".github" / "workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertIn('head=${GITHUB_REPOSITORY_OWNER}:${BRANCH}', text)
                self.assertIn(".head.repo.full_name", text)
                self.assertIn(".user.login", text)

    def test_refresh_reconciles_closed_manual_issue_queue_labels(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".github" / "workflows" / "refresh.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Clear stale pending labels from closed review and yank Issues", text)
        self.assertIn("for LABEL in review-request pending-review", text)
        self.assertIn("-f state=closed", text)

    def test_name_status_parser_preserves_rename_paths(self):
        self.assertEqual(
            parse_name_status("R100\0plugins/a/old.json\0plugins/a/new.json\0"),
            [("R100", ["plugins/a/old.json", "plugins/a/new.json"])],
        )

    def test_name_status_parser_handles_newlines_in_paths(self):
        self.assertEqual(
            parse_name_status("A\0plugins/dev.example.test/new\nname.json\0"),
            [("A", ["plugins/dev.example.test/new\nname.json"])],
        )

    def test_untrusted_actor_cannot_add_review(self):
        errors = evaluate([("A", ["reviews/dev.example.test/1.0.0.json"])])
        self.assertTrue(any("不能修改" in error for error in errors))

    def test_trusted_actor_can_add_or_update_review_tombstone(self):
        for status in ("A", "M"):
            with self.subTest(status=status):
                self.assertEqual(
                    evaluate(
                        [(status, ["reviews/dev.example.test/1.0.0.json"])],
                        actor="TouristH",
                    ),
                    [],
                )

    def test_trusted_actor_cannot_delete_review_tombstone(self):
        errors = evaluate(
            [("D", ["reviews/dev.example.test/1.0.0.json"])],
            actor="TouristH",
        )
        self.assertTrue(any("顺序墓碑" in error for error in errors))

    def test_trust_is_case_insensitive(self):
        self.assertEqual(
            evaluate(
                [("A", ["reviews/dev.example.test/1.0.0.json"])],
                actor="TOURISTH",
            ),
            [],
        )

    def test_trusted_actor_cannot_claim_another_reviewer_identity(self):
        errors = evaluate(
            [("A", ["reviews/dev.example.test/1.0.0.json"])],
            actor="TouristH",
            review_matches=False,
        )
        self.assertTrue(any("stateBy 必须与 PR 作者" in error for error in errors))

    def test_untrusted_actor_cannot_change_protected_configuration(self):
        errors = evaluate([("M", ["repository.json"])])
        self.assertTrue(errors)

    def test_untrusted_actor_is_limited_to_a_strict_document_allowlist(self):
        self.assertEqual(evaluate([("M", ["README.md"])]), [])
        for path in ("sitecustomize.py", "unittest.py", "pyproject.toml", ".gitmodules"):
            with self.subTest(path=path):
                self.assertTrue(evaluate([("A", [path])]))

    def test_untrusted_actor_cannot_edit_generated_indexes(self):
        changes = [
            ("M", ["plugins.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
        ]
        self.assertTrue(evaluate(changes))

    def test_new_release_is_issue_managed_for_contributor(self):
        errors = evaluate([("A", ["plugins/dev.example.test/releases/1.1.0.json"])])
        self.assertTrue(any("Add Plugin Issue" in error for error in errors))

    def test_new_release_cannot_bypass_issue_sync_in_trusted_pr(self):
        errors = evaluate(
            [("A", ["plugins/dev.example.test/releases/1.1.0.json"])],
            actor="TouristH",
        )
        self.assertTrue(any("PR 不得新增" in error for error in errors))

    def test_historical_release_is_immutable_for_contributor(self):
        errors = evaluate([("M", ["plugins/dev.example.test/releases/1.0.0.json"])])
        self.assertTrue(any("历史版本" in error for error in errors))

    def test_trusted_actor_may_only_make_yank_only_release_change(self):
        change = [("M", ["plugins/dev.example.test/releases/1.0.0.json"])]
        self.assertEqual(evaluate(change, actor="TouristH", yank_only=True), [])
        self.assertTrue(evaluate(change, actor="TouristH", yank_only=False))

    def test_one_pull_request_cannot_span_plugins_and_reviews(self):
        errors = evaluate(
            [
                ("A", ["plugins/dev.example.one/releases/1.0.0.json"]),
                ("A", ["reviews/dev.example.two/1.0.0.json"]),
            ],
            actor="TouristH",
        )
        self.assertTrue(any("只能涉及一个插件" in error for error in errors))

    def test_registry_readme_is_not_mistaken_for_a_plugin_id(self):
        changes = [
            ("M", ["plugins/README.md"]),
            ("M", ["plugins/dev.example.test/plugin.json"]),
        ]
        self.assertEqual(evaluate(changes, actor="TouristH"), [])

    def test_trusted_actor_can_remove_obsolete_tooling_but_not_plugin_history(self):
        self.assertEqual(
            evaluate(
                [("D", [".github/ISSUE_TEMPLATE/obsolete.yml"])],
                actor="TouristH",
            ),
            [],
        )
        self.assertTrue(
            evaluate(
                [("D", ["plugins/dev.example.test/releases/1.0.0.json"])],
                actor="TouristH",
            )
        )

    def test_renames_are_rejected(self):
        errors = evaluate(
            [
                (
                    "R100",
                    [
                        "plugins/dev.example.test/releases/1.0.0.json",
                        "plugins/dev.example.test/releases/1.0.1.json",
                    ],
                )
            ]
        )
        self.assertTrue(any("不能重命名" in error for error in errors))

    def test_registry_bot_sync_may_append_multiple_plugins_and_generated_views(self):
        changes = [
            ("M", ["plugins.json"]),
            ("A", ["plugins/dev.example.one/plugin.json"]),
            ("A", ["plugins/dev.example.one/releases/1.0.0.json"]),
            ("A", ["plugins/dev.example.two/plugin.json"]),
            ("A", ["plugins/dev.example.two/releases/1.0.0.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
        ]
        self.assertEqual(evaluate_bot(changes, "sync"), [])

    def test_registry_bot_identity_branch_and_repository_must_all_match(self):
        change = [("A", ["plugins/dev.example.test/releases/1.0.0.json"])]
        cases = (
            {"actor": "lookalike[bot]"},
            {"head_repository": "attacker/NyaLauncher-Plugins"},
            {"head_ref": "registry-bot/sync-evil"},
            {"head_ref": "registry-bot/unknown/run-1"},
            {"event_sender": "collaborator"},
            {
                "event_action": "reopened",
                "event_sender": "collaborator",
            },
            {
                "event_action": "edited",
                "event_sender": "collaborator",
            },
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertTrue(evaluate_bot(change, "sync", **overrides))

        self.assertEqual(
            evaluate_bot(change, "sync", event_action="edited"),
            [],
        )

    def test_human_does_not_gain_bot_permissions_from_branch_name(self):
        errors = evaluate(
            [("A", ["plugins/dev.example.test/releases/1.0.0.json"])],
            actor="contributor",
            bot_login=REGISTRY_BOT,
            base_repository=REPOSITORY,
            head_repository=REPOSITORY,
            head_ref="registry-bot/sync/run-1",
        )
        self.assertTrue(any("PR 不得新增" in error for error in errors))

    def test_registry_bot_sync_cannot_modify_code_reviews_or_existing_history(self):
        for status, path in (
            ("M", "tools/validate.py"),
            ("A", "reviews/dev.example.test/1.0.0.json"),
            ("M", "plugins/dev.example.test/releases/1.0.0.json"),
            ("D", "plugins/dev.example.test/plugin.json"),
        ):
            with self.subTest(status=status, path=path):
                self.assertTrue(evaluate_bot([(status, [path])], "sync"))

    def test_registry_bot_review_is_narrow_and_reviewer_stays_trusted(self):
        changes = [
            ("A", ["reviews/dev.example.test/1.0.0.json"]),
            ("M", ["public/v1/index.json"]),
        ]
        self.assertEqual(evaluate_bot(changes, "review"), [])
        self.assertEqual(
            evaluate_bot(
                [
                    ("M", ["reviews/dev.example.test/1.0.0.json"]),
                    ("M", ["public/v1/index.json"]),
                ],
                "review",
            ),
            [],
        )
        self.assertTrue(
            evaluate_bot(changes, "review", review_matches=False)
        )
        self.assertTrue(
            evaluate_bot([("M", ["plugins.json"])], "review")
        )
        self.assertTrue(
            evaluate_bot(
                [
                    ("D", ["reviews/dev.example.test/1.0.0.json"]),
                    ("M", ["public/v1/index.json"]),
                ],
                "review",
            )
        )

    def test_registry_bot_review_and_yank_remain_single_plugin_operations(self):
        review_changes = [
            ("A", ["reviews/dev.example.one/1.0.0.json"]),
            ("A", ["reviews/dev.example.two/1.0.0.json"]),
            ("M", ["public/v1/index.json"]),
        ]
        self.assertTrue(evaluate_bot(review_changes, "review"))

        yank_changes = [
            ("M", ["plugins/dev.example.one/releases/1.0.0.json"]),
            ("D", ["reviews/dev.example.two/1.0.0.json"]),
            ("M", ["public/v1/index.json"]),
        ]
        self.assertTrue(evaluate_bot(yank_changes, "yank", yank_only=True))

    def test_registry_bot_yank_only_allows_yank_data_changes(self):
        changes = [
            ("M", ["plugins.json"]),
            ("M", ["plugins/dev.example.test/releases/1.0.0.json"]),
            ("D", ["reviews/dev.example.test/1.0.0.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
        ]
        self.assertEqual(evaluate_bot(changes, "yank", yank_only=True), [])
        self.assertTrue(evaluate_bot(changes, "yank", yank_only=False))
        self.assertTrue(
            evaluate_bot(
                [("A", ["plugins/dev.example.test/releases/2.0.0.json"])],
                "yank",
                yank_only=True,
            )
        )

    def test_registry_bot_copy_is_rejected(self):
        errors = evaluate_bot(
            [
                (
                    "C100",
                    [
                        "plugins/dev.example.test/releases/1.0.0.json",
                        "plugins/dev.example.test/releases/1.0.1.json",
                    ],
                )
            ],
            "sync",
        )
        self.assertTrue(any("不能重命名或复制" in error or "不能重命名" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
