import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_pr
from tools.check_pr import (
    bootstrap_migration_policy,
    evaluate_policy,
    identity_history_is_append_only,
    load_bootstrap_configuration,
    parse_name_status,
    review_revocation_change_is_safe,
    sync_identity_rename_is_safe,
    yank_transition_is_safe,
)


TRUSTED = {"touristh"}
TRUSTED_IDS = {"touristh": 143396778}
REGISTRY_BOT = "nyalauncher-registry-bot[bot]"
REGISTRY_BOT_ID = 987654321
REPOSITORY = "TouristH/NyaLauncher-Plugins"
UNSET = object()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def git_at(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


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
    event_sender_id=None,
    event_sender_type=None,
    event_action=None,
    actor_id=UNSET,
    actor_type="User",
    identity_safe=False,
    reactivation_release_safe=True,
    lifecycle_identity_safe=False,
    metadata_rename_safe=False,
    review_revocation_safe=False,
    bootstrap_migration=False,
    bootstrap_safe=False,
    bootstrap_anchor_creation=False,
    schema1_anchor_frozen=False,
    base_schema_version=None,
    head_schema_version=None,
):
    if actor_id is UNSET:
        actor_id = TRUSTED_IDS.get(actor.casefold(), 42)
    if event_sender is None:
        event_sender = actor
    if event_sender_id is None:
        event_sender_id = actor_id
    if event_sender_type is None:
        event_sender_type = actor_type
    return evaluate_policy(
        changes,
        actor,
        TRUSTED,
        lambda _path: yank_only,
        lambda _path, _actor: review_matches,
        sync_identity_update_is_safe=lambda _path: identity_safe,
        sync_reactivation_is_safe=lambda _path: reactivation_release_safe,
        lifecycle_identity_update_is_safe=lambda _path: lifecycle_identity_safe,
        plugin_metadata_rename_is_safe=lambda _path: metadata_rename_safe,
        review_revocation_is_safe=lambda _path: review_revocation_safe,
        bootstrap_migration=bootstrap_migration,
        bootstrap_path_is_safe=lambda _status, _path: bootstrap_safe,
        bootstrap_anchor_creation=bootstrap_anchor_creation,
        schema1_anchor_frozen=schema1_anchor_frozen,
        trusted_reviewer_ids=TRUSTED_IDS,
        actor_id=actor_id,
        actor_type=actor_type,
        registry_bot_login=bot_login,
        base_repository=base_repository,
        head_repository=head_repository,
        head_ref=head_ref,
        event_sender=event_sender,
        event_sender_id=event_sender_id,
        event_sender_type=event_sender_type,
        event_action=event_action,
        base_schema_version=base_schema_version,
        head_schema_version=head_schema_version,
    )


def evaluate_bot(changes, kind, *, yank_only=False, review_matches=True, **overrides):
    values = {
        "actor": REGISTRY_BOT,
        "actor_id": REGISTRY_BOT_ID,
        "actor_type": "Bot",
        "bot_login": REGISTRY_BOT,
        "base_repository": REPOSITORY,
        "head_repository": REPOSITORY,
        "head_ref": f"registry-bot/{kind}/run-123",
        "event_sender": REGISTRY_BOT,
        "event_sender_id": REGISTRY_BOT_ID,
        "event_sender_type": "Bot",
        "event_action": "synchronize",
        "lifecycle_identity_safe": kind == "lifecycle",
        "review_revocation_safe": kind in {"yank", "lifecycle"},
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
        for name in (
            "refresh.yml",
            "approve-issue.yml",
            "review-issue.yml",
            "lifecycle-issue.yml",
        ):
            with self.subTest(workflow=name):
                text = (root / ".github" / "workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertIn('head=${GITHUB_REPOSITORY_OWNER}:${BRANCH}', text)
                self.assertIn(".head.repo.full_name", text)
                self.assertIn(".user.login", text)

    def test_purge_staging_is_closed_early_but_branch_survives_until_merge(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / ".github" / "workflows" / "lifecycle-issue.yml"
        ).read_text(encoding="utf-8")

        freeze_job = text.index("  freeze_purge:")
        lifecycle_job = text.index("\n  lifecycle:")
        close_staging = text.index('gh pr close "$STAGING_PR"')
        apply_transaction = text.index(
            "- name: Verify author confirmation and apply the bound transaction"
        )
        merged_case = text.index("MERGED\\ *)")
        delete_staging_branch = text.index(
            "git/refs/heads/${PURGE_STAGING_HEAD_REF}"
        )

        self.assertLess(freeze_job, close_staging)
        self.assertLess(close_staging, lifecycle_job)
        self.assertNotIn("concurrency:", text[freeze_job:lifecycle_job])
        self.assertIn("needs: freeze_purge", text[lifecycle_job:])
        self.assertLess(close_staging, apply_transaction)
        self.assertLess(apply_transaction, merged_case)
        self.assertLess(merged_case, delete_staging_branch)
        self.assertEqual(
            text.count("git/refs/heads/${PURGE_STAGING_HEAD_REF}"), 1
        )
        self.assertNotIn(
            'push origin --delete "$PURGE_STAGING_HEAD_REF"', text
        )
        self.assertIn("NYA_PURGE_STAGING_CLOSED_SHA", text)
        self.assertIn('(.state == "open" or .state == "closed")', text)
        self.assertIn('if [ "$STAGING_STATE" = "open" ]', text)
        self.assertIn("git/ref/heads/${STAGING_HEAD_REF}", text)
        self.assertIn('"OPEN BEHIND"', text)
        self.assertIn("Timed out waiting for lifecycle PR", text)

    def test_refresh_reconciles_closed_manual_issue_queue_labels(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".github" / "workflows" / "refresh.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Clear stale pending labels from closed review and yank Issues", text)
        self.assertIn("for LABEL in review-request pending-review", text)
        self.assertIn("-f state=closed", text)
        self.assertLess(
            text.index("Require completed schema v2 migration"),
            text.index("python tools/registry_bot.py collect"),
        )
        self.assertIn(".schemaVersion == 2", text)

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

    def test_trusted_login_with_reused_numeric_identity_is_rejected(self):
        errors = evaluate(
            [("M", ["tools/validate.py"])],
            actor="TouristH",
            actor_id=143396779,
        )
        self.assertTrue(any("numeric user ID" in error for error in errors))

    def test_trusted_login_must_be_a_user_account(self):
        errors = evaluate(
            [("M", ["tools/validate.py"])],
            actor="TouristH",
            actor_type="Bot",
        )
        self.assertTrue(any("numeric user ID" in error for error in errors))

    def test_trusted_human_pr_sender_must_be_the_numeric_author(self):
        change = [("M", ["tools/validate.py"])]
        for overrides in (
            {"event_sender": "collaborator"},
            {"event_sender_id": TRUSTED_IDS["touristh"] + 1},
            {"event_sender_type": "Bot"},
        ):
            with self.subTest(overrides=overrides):
                errors = evaluate(change, actor="TouristH", **overrides)
                self.assertTrue(any("sender" in error for error in errors))

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

    def test_yank_tombstone_can_only_be_created_once(self):
        previous = {
            "version": "1.0.0",
            "download": {"sha256": "a" * 64, "size": 123},
            "yanked": False,
        }
        current = {
            **previous,
            "yanked": True,
            "yankReason": "Security incident.",
        }
        self.assertTrue(yank_transition_is_safe(previous, current))

        rewritten = {**current, "yankReason": "Different reason."}
        self.assertFalse(yank_transition_is_safe(current, rewritten))
        changed_hash = json.loads(json.dumps(current))
        changed_hash["download"]["sha256"] = "b" * 64
        self.assertFalse(yank_transition_is_safe(previous, changed_hash))
        changed_size = json.loads(json.dumps(current))
        changed_size["download"]["size"] = 124
        self.assertFalse(yank_transition_is_safe(previous, changed_size))

        release_path = "plugins/dev.example.test/releases/1.0.0.json"
        for kind in ("yank", "lifecycle"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    evaluate_bot(
                        [("M", [release_path])], kind, yank_only=True
                    ),
                    [],
                )
                self.assertTrue(
                    evaluate_bot(
                        [("M", [release_path])], kind, yank_only=False
                    )
                )

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
            ("A", ["plugins/dev.example.one/identity.json"]),
            ("A", ["plugins/dev.example.one/releases/1.0.0.json"]),
            ("A", ["plugins/dev.example.two/plugin.json"]),
            ("A", ["plugins/dev.example.two/identity.json"]),
            ("A", ["plugins/dev.example.two/releases/1.0.0.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
            ("M", ["public/v2/index.json"]),
        ]
        self.assertEqual(evaluate_bot(changes, "sync"), [])

    def test_only_owned_lifecycle_app_branch_can_mutate_identity_ledger(self):
        changes = [
            ("M", ["plugins/dev.example.test/identity.json"]),
            ("A", ["plugins/dev.example.test/generations/g2/plugin.json"]),
            ("M", ["plugins.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
            ("M", ["public/v2/index.json"]),
        ]
        self.assertEqual(evaluate_bot(changes, "lifecycle"), [])
        self.assertTrue(evaluate(changes, actor="TouristH"))
        self.assertTrue(
            evaluate_bot(changes, "lifecycle", event_sender="collaborator")
        )

    def test_generation_ten_paths_are_recognized(self):
        changes = [
            ("A", ["plugins/dev.example.test/generations/g10/plugin.json"]),
            ("A", ["plugins/dev.example.test/generations/g10/releases/1.0.0.json"]),
            ("A", ["reviews/dev.example.test/g10/1.0.0.json"]),
        ]
        self.assertEqual(evaluate_bot([changes[0]], "lifecycle"), [])
        self.assertEqual(evaluate_bot([changes[1]], "sync"), [])
        self.assertEqual(evaluate_bot([changes[2]], "review"), [])

    def test_sync_repository_rename_requires_safe_metadata_and_history_updates(self):
        changes = [
            ("M", ["plugins/dev.example.test/plugin.json"]),
            ("M", ["plugins/dev.example.test/identity.json"]),
            ("M", ["plugins.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
            ("M", ["public/v2/index.json"]),
        ]
        self.assertEqual(
            evaluate_bot(
                changes,
                "sync",
                identity_safe=True,
                metadata_rename_safe=True,
            ),
            [],
        )
        self.assertTrue(
            evaluate_bot(changes, "sync", metadata_rename_safe=True)
        )
        self.assertTrue(evaluate_bot(changes, "sync", identity_safe=True))

    def test_sync_status_reactivation_requires_a_new_higher_usable_release(self):
        identity = "plugins/dev.example.test/identity.json"
        release = "plugins/dev.example.test/releases/1.1.0.json"
        base_changes = [
            ("M", [identity]),
            ("M", ["plugins.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
            ("M", ["public/v2/index.json"]),
        ]
        self.assertTrue(
            evaluate_bot(
                base_changes,
                "sync",
                identity_safe=True,
                reactivation_release_safe=False,
            )
        )
        self.assertEqual(
            evaluate_bot(
                [*base_changes, ("A", [release])],
                "sync",
                identity_safe=True,
                reactivation_release_safe=True,
            ),
            [],
        )

    def test_sync_reactivation_release_gate_checks_generation_semver_and_yank(self):
        identity_path = "plugins/dev.example.test/identity.json"
        release_path = "plugins/dev.example.test/releases/1.1.0.json"
        previous = {
            "generation": 1,
            "lifecycleStatus": "retired",
        }
        current = {
            "generation": 1,
            "lifecycleStatus": "active",
        }
        candidate = {
            "generation": 1,
            "version": "1.1.0",
            "yanked": False,
        }
        base_tree = {
            "value": "plugins/dev.example.test/releases/1.0.0.json\n"
        }

        def load_at(revision, path):
            if path == identity_path:
                return previous if revision == "base" else current
            if revision == "base":
                return {"version": "1.0.0"}
            return candidate

        with (
            patch.object(check_pr, "load_json_at", side_effect=load_at),
            patch.object(
                check_pr,
                "git",
                side_effect=lambda *_arguments: base_tree["value"],
            ),
        ):
            self.assertFalse(
                check_pr.sync_reactivation_has_new_release(
                    "base", "head", identity_path, [("M", [identity_path])]
                )
            )
            self.assertTrue(
                check_pr.sync_reactivation_has_new_release(
                    "base", "head", identity_path, [("A", [release_path])]
                )
            )
            candidate["version"] = "0.9.0"
            self.assertFalse(
                check_pr.sync_reactivation_has_new_release(
                    "base", "head", identity_path, [("A", [release_path])]
                )
            )
            candidate["version"] = "1.0.0+rebuilt"
            self.assertFalse(
                check_pr.sync_reactivation_has_new_release(
                    "base", "head", identity_path, [("A", [release_path])]
                )
            )
            candidate.update({"generation": 2, "version": "1.1.0"})
            self.assertFalse(
                check_pr.sync_reactivation_has_new_release(
                    "base", "head", identity_path, [("A", [release_path])]
                )
            )
            candidate.update({"version": "1.1.0", "yanked": True})
            self.assertFalse(
                check_pr.sync_reactivation_has_new_release(
                    "base", "head", identity_path, [("A", [release_path])]
                )
            )
            current["generation"] = 2
            candidate.update({"generation": 2, "version": "1.0.0", "yanked": False})
            base_tree["value"] = ""
            generation_two_release = (
                "plugins/dev.example.test/generations/g2/releases/1.0.0.json"
            )
            self.assertTrue(
                check_pr.sync_reactivation_has_new_release(
                    "base",
                    "head",
                    identity_path,
                    [("A", [generation_two_release])],
                )
            )

    def test_url_history_old_entries_cannot_be_deleted_reordered_or_inserted(self):
        old = {
            "id": "dev.example.test",
            "lineageId": "11111111-1111-4111-8111-111111111111",
            "generation": 1,
            "lifecycleStatus": "active",
            "generations": [
                {
                    "generation": 1,
                    "repositoryUrl": "https://github.com/example/current",
                    "repositoryUrlHistory": [
                        "https://github.com/example/old",
                        "https://github.com/example/current",
                    ],
                    "repositoryId": 10,
                    "ownerId": 20,
                    "status": "active",
                }
            ],
        }
        appended = json.loads(json.dumps(old))
        appended["generations"][0]["repositoryUrl"] = "https://github.com/example/new"
        appended["generations"][0]["repositoryUrlHistory"].append(
            "https://github.com/example/new"
        )
        self.assertTrue(identity_history_is_append_only(old, appended))
        for history in (
            ["https://github.com/example/current"],
            [
                "https://github.com/example/current",
                "https://github.com/example/old",
            ],
            [
                "https://github.com/example/old",
                "https://github.com/attacker/alias",
                "https://github.com/example/current",
            ],
        ):
            with self.subTest(history=history):
                malicious = json.loads(json.dumps(old))
                malicious["generations"][0]["repositoryUrlHistory"] = history
                malicious["generations"][0]["repositoryUrl"] = history[-1]
                self.assertFalse(identity_history_is_append_only(old, malicious))

    def test_transfer_target_sync_can_activate_without_rewriting_identity(self):
        previous = {
            "schemaVersion": 1,
            "id": "dev.example.test",
            "lineageId": "11111111-1111-4111-8111-111111111111",
            "generation": 2,
            "lifecycleStatus": "transferred",
            "generations": [
                {
                    "generation": 1,
                    "repositoryUrl": "https://github.com/example/old",
                    "repositoryUrlHistory": ["https://github.com/example/old"],
                    "repositoryId": 10,
                    "ownerId": 20,
                    "status": "transferred",
                },
                {
                    "generation": 2,
                    "repositoryUrl": "https://github.com/example/new",
                    "repositoryUrlHistory": ["https://github.com/example/new"],
                    "repositoryId": 11,
                    "ownerId": 21,
                    "status": "active",
                },
            ],
            "events": [{"operation": "transfer", "generation": 1}],
        }
        current = json.loads(json.dumps(previous))
        current["lifecycleStatus"] = "active"
        self.assertTrue(sync_identity_rename_is_safe(previous, current))
        current["events"].append({"operation": "attacker"})
        self.assertFalse(sync_identity_rename_is_safe(previous, current))

    def test_revocation_diff_is_exact_and_requires_current_numeric_reviewer(self):
        previous = {
            "schemaVersion": 1,
            "generation": 1,
            "pluginId": "dev.example.test",
            "version": "1.0.0",
            "sha256": "a" * 64,
            "status": "verified",
            "stateBy": "TouristH",
            "stateById": TRUSTED_IDS["touristh"],
            "stateAt": "2026-08-20T00:00:00Z",
            "lastCommandAt": "2026-08-20T00:00:00Z",
            "lastCommentId": 10,
        }
        current = dict(previous)
        current.update(
            {
                "status": "revoked",
                "stateAt": "2026-08-20T00:00:00Z",
                "lastCommandAt": "2026-08-20T00:00:00Z",
                "lastCommentId": 11,
                "notes": "Lifecycle retirement.",
            }
        )
        self.assertTrue(
            review_revocation_change_is_safe(previous, current, TRUSTED_IDS)
        )
        for field, value in (
            ("sha256", "b" * 64),
            ("stateById", TRUSTED_IDS["touristh"] + 1),
            ("lastCommentId", 9),
        ):
            with self.subTest(field=field):
                malicious = dict(current)
                malicious[field] = value
                self.assertFalse(
                    review_revocation_change_is_safe(
                        previous, malicious, TRUSTED_IDS
                    )
                )

    def test_lifecycle_branch_allows_only_observable_transition_diffs(self):
        valid = [
            ("M", ["plugins/dev.example.test/identity.json"]),
            ("M", ["plugins/dev.example.test/releases/1.0.0.json"]),
            ("M", ["reviews/dev.example.test/1.0.0.json"]),
            ("M", ["plugins.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
            ("M", ["public/v2/index.json"]),
        ]
        self.assertEqual(
            evaluate_bot(valid, "lifecycle", yank_only=True), []
        )
        malicious = (
            ("M", "plugins/dev.example.test/releases/1.0.0.json", {"yank_only": False}),
            ("D", "plugins/dev.example.test/releases/1.0.0.json", {}),
            ("A", "plugins/dev.example.test/releases/2.0.0.json", {}),
            ("D", "reviews/dev.example.test/1.0.0.json", {}),
            ("M", "reviews/dev.example.test/1.0.0.json", {"review_revocation_safe": False}),
            ("M", "plugins/dev.example.test/plugin.json", {}),
            ("D", "plugins/dev.example.test/generations/g2/plugin.json", {}),
            ("D", "plugins/dev.example.test/identity.json", {}),
        )
        for status, path, overrides in malicious:
            with self.subTest(status=status, path=path):
                self.assertTrue(
                    evaluate_bot([(status, [path])], "lifecycle", **overrides)
                )

    def test_schema_v2_is_permanent_and_schema1_anchor_freezes_writers(self):
        repository_change = [("M", ["repository.json"])]
        self.assertTrue(
            evaluate(
                repository_change,
                actor="TouristH",
                base_schema_version=2,
                head_schema_version=1,
            )
        )
        self.assertTrue(
            evaluate_bot(
                repository_change,
                "sync",
                base_schema_version=2,
                head_schema_version=1,
            )
        )
        self.assertTrue(
            evaluate_bot(
                [("M", ["plugins.json"])],
                "sync",
                schema1_anchor_frozen=True,
                base_schema_version=1,
                head_schema_version=1,
            )
        )
        self.assertTrue(
            evaluate(
                repository_change,
                actor="TouristH",
                schema1_anchor_frozen=True,
                base_schema_version=1,
                head_schema_version=1,
            )
        )
        self.assertEqual(
            evaluate(
                repository_change,
                actor="TouristH",
                schema1_anchor_frozen=True,
                bootstrap_migration=True,
                bootstrap_safe=True,
                base_repository=REPOSITORY,
                head_repository=REPOSITORY,
                base_schema_version=1,
                head_schema_version=2,
            ),
            [],
        )

    def test_bootstrap_anchor_rejects_ambiguous_numeric_or_url_facts(self):
        anchor = {
            "$schema": "../schemas/registry-bootstrap-v1.schema.json",
            "schemaVersion": 1,
            "targetRepositorySchemaVersion": 2,
            "trustedReviewerIds": {"TouristH": 143396778},
            "targetTrustedReviewerIds": {
                "TouristH": 143396778,
                "redstore-noob": 206107690,
            },
            "publisherBindings": {
                "dev.example.one": {
                    "lineageId": "11111111-1111-4111-8111-111111111111",
                    "repositoryUrl": "https://github.com/example/one",
                    "repositoryId": 10,
                    "ownerId": 20,
                }
            },
        }
        with patch.object(check_pr, "load_json_at", return_value=anchor):
            loaded = load_bootstrap_configuration("base", {"touristh"})
        self.assertEqual(loaded[0], {"touristh": 143396778})

        invalid_values = []
        duplicate_reviewer = json.loads(json.dumps(anchor))
        duplicate_reviewer["trustedReviewerIds"]["touristh"] = 143396778
        invalid_values.append(duplicate_reviewer)
        git_suffix = json.loads(json.dumps(anchor))
        git_suffix["publisherBindings"]["dev.example.one"][
            "repositoryUrl"
        ] = "https://github.com/example/one.git"
        invalid_values.append(git_suffix)
        duplicate_repository = json.loads(json.dumps(anchor))
        duplicate_repository["publisherBindings"]["dev.example.two"] = {
            "lineageId": "22222222-2222-4222-8222-222222222222",
            "repositoryUrl": "https://github.com/example/two",
            "repositoryId": 10,
            "ownerId": 20,
        }
        invalid_values.append(duplicate_repository)
        duplicate_lineage = json.loads(json.dumps(anchor))
        duplicate_lineage["publisherBindings"]["dev.example.two"] = {
            "lineageId": "11111111-1111-4111-8111-111111111111",
            "repositoryUrl": "https://github.com/example/two",
            "repositoryId": 11,
            "ownerId": 20,
        }
        invalid_values.append(duplicate_lineage)
        for value in invalid_values:
            with self.subTest(value=value), patch.object(
                check_pr, "load_json_at", return_value=value
            ), self.assertRaises(ValueError):
                load_bootstrap_configuration("base", {"touristh"})

    def test_bootstrap_anchor_can_only_be_created_once_and_never_changed(self):
        path = check_pr.BOOTSTRAP_CONFIGURATION
        self.assertEqual(
            evaluate(
                [("A", [path])],
                actor="TouristH",
                bootstrap_anchor_creation=True,
                base_repository=REPOSITORY,
                head_repository=REPOSITORY,
            ),
            [],
        )
        for status in ("A", "M", "D"):
            with self.subTest(status=status):
                self.assertTrue(
                    evaluate(
                        [(status, [path])],
                        actor="TouristH",
                        bootstrap_anchor_creation=False,
                        base_repository=REPOSITORY,
                        head_repository=REPOSITORY,
                    )
                )

    def test_one_time_bootstrap_accepts_deterministic_migration_without_v1_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git_at(root, "init", "-q")
            git_at(root, "config", "user.name", "Test")
            git_at(root, "config", "user.email", "test@example.invalid")
            plugin_id = "dev.example.test"
            repository_url = "https://github.com/example/test"
            lineage_id = "11111111-1111-4111-8111-111111111111"
            base_repository = {
                "schemaVersion": 1,
                "name": "Test registry",
                "sourceUrl": "https://github.com/TouristH/NyaLauncher-Plugins",
                "launcherUrl": "https://github.com/redstore-noob/NyaLauncher",
                "indexPath": "public/v1/index.json",
                "registryBotLogin": REGISTRY_BOT,
                "trustedReviewers": ["TouristH"],
            }
            base_listing = {
                "id": plugin_id,
                "repositoryUrl": repository_url,
                "repositoryId": 10,
                "ownerId": 20,
            }
            anchor = {
                "$schema": "../schemas/registry-bootstrap-v1.schema.json",
                "schemaVersion": 1,
                "targetRepositorySchemaVersion": 2,
                "trustedReviewerIds": {"TouristH": 143396778},
                "targetTrustedReviewerIds": {"TouristH": 143396778},
                "publisherBindings": {
                    plugin_id: {
                        "lineageId": lineage_id,
                        "repositoryUrl": repository_url,
                        "repositoryId": 10,
                        "ownerId": 20,
                    }
                },
            }
            release = {
                "schemaVersion": 1,
                "version": "1.0.0",
                "download": {
                    "url": repository_url + "/releases/download/v1.0.0/test.zip",
                    "sha256": "a" * 64,
                    "size": 123,
                },
                "yanked": False,
            }
            review = {
                "schemaVersion": 1,
                "pluginId": plugin_id,
                "version": "1.0.0",
                "sha256": "a" * 64,
                "status": "verified",
                "stateBy": "TouristH",
            }
            write_json(root / "repository.json", base_repository)
            write_json(root / "plugins.json", [base_listing])
            write_json(root / "migrations" / "v2-bootstrap.json", anchor)
            write_json(
                root / "plugins" / plugin_id / "plugin.json",
                {"id": plugin_id, "repositoryUrl": repository_url},
            )
            release_path = root / "plugins" / plugin_id / "releases" / "1.0.0.json"
            review_path = root / "reviews" / plugin_id / "1.0.0.json"
            write_json(release_path, release)
            write_json(review_path, review)
            write_json(root / "plugin_details.json", [{"id": plugin_id}])
            write_json(root / "public" / "v1" / "index.json", {"plugins": []})
            git_at(root, "add", ".")
            git_at(root, "commit", "-qm", "schema1 base")
            base = git_at(root, "rev-parse", "HEAD").strip()

            head_repository = {
                **base_repository,
                "schemaVersion": 2,
                "indexV2Path": "public/v2/index.json",
                "v2MinimumLauncherVersion": "0.1.2-testplug.1",
                "trustedReviewerIds": {"TouristH": 143396778},
            }
            head_listing = {
                **base_listing,
                "lineageId": lineage_id,
                "generation": 1,
            }
            write_json(root / "repository.json", head_repository)
            write_json(root / "plugins.json", [head_listing])
            write_json(
                root / "plugins" / plugin_id / "identity.json",
                {
                    "schemaVersion": 1,
                    "id": plugin_id,
                    "lineageId": lineage_id,
                    "generation": 1,
                    "lifecycleStatus": "active",
                    "generations": [
                        {
                            "generation": 1,
                            "repositoryUrl": repository_url,
                            "repositoryUrlHistory": [repository_url],
                            "repositoryId": 10,
                            "ownerId": 20,
                            "status": "active",
                        }
                    ],
                },
            )
            write_json(release_path, {**release, "generation": 1})
            write_json(
                review_path,
                {**review, "generation": 1, "stateById": 143396778},
            )
            write_json(
                root / "plugin_details.json",
                [{"id": plugin_id, "lineageId": lineage_id}],
            )
            write_json(root / "public" / "v2" / "index.json", {"plugins": []})
            git_at(root, "add", ".")
            git_at(root, "commit", "-qm", "schema2 migration")
            head = git_at(root, "rev-parse", "HEAD").strip()

            with patch.object(check_pr, "ROOT", root):
                changes = parse_name_status(
                    check_pr.git(
                        "diff", "--name-status", "-z", "--find-renames", f"{base}...{head}"
                    )
                )
                recognized, safe = bootstrap_migration_policy(
                    base, head, changes, {"touristh"}
                )
            self.assertTrue(recognized)
            self.assertNotIn("public/v1/index.json", {p[0] for _, p in changes})
            self.assertTrue(all(safe(status, paths[0]) for status, paths in changes))

            malicious = dict(json.loads(release_path.read_text(encoding="utf-8")))
            malicious["download"] = {**malicious["download"], "sha256": "b" * 64}
            write_json(release_path, malicious)
            git_at(root, "add", ".")
            git_at(root, "commit", "-qm", "malicious migration fact")
            malicious_head = git_at(root, "rev-parse", "HEAD").strip()
            with patch.object(check_pr, "ROOT", root):
                malicious_changes = parse_name_status(
                    check_pr.git(
                        "diff",
                        "--name-status",
                        "-z",
                        "--find-renames",
                        f"{base}...{malicious_head}",
                    )
                )
                recognized, _ = bootstrap_migration_policy(
                    base, malicious_head, malicious_changes, {"touristh"}
                )
            self.assertFalse(recognized)

    def test_purge_tombstone_is_append_only_on_lifecycle_branch(self):
        path = "tombstones/dev.example.test/11111111-1111-4111-8111-111111111111.json"
        self.assertEqual(evaluate_bot([("A", [path])], "lifecycle"), [])
        self.assertTrue(evaluate_bot([("M", [path])], "lifecycle"))
        self.assertTrue(evaluate_bot([("D", [path])], "lifecycle"))

    def test_registry_bot_identity_branch_and_repository_must_all_match(self):
        change = [("A", ["plugins/dev.example.test/releases/1.0.0.json"])]
        cases = (
            {"actor": "lookalike[bot]"},
            {"head_repository": "attacker/NyaLauncher-Plugins"},
            {"head_ref": "registry-bot/sync-evil"},
            {"head_ref": "registry-bot/unknown/run-1"},
            {"event_sender": "collaborator"},
            {"event_sender_id": REGISTRY_BOT_ID + 1},
            {"event_sender_type": "User"},
            {"actor_id": REGISTRY_BOT_ID + 1},
            {"actor_type": "User"},
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
            ("M", ["reviews/dev.example.test/1.0.0.json"]),
            ("M", ["plugin_details.json"]),
            ("M", ["public/v1/index.json"]),
            ("M", ["public/v2/index.json"]),
        ]
        self.assertEqual(evaluate_bot(changes, "yank", yank_only=True), [])
        self.assertTrue(
            evaluate_bot(
                [("D", ["reviews/dev.example.test/1.0.0.json"])],
                "yank",
                yank_only=True,
            )
        )
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
