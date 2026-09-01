import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import lifecycle
from tools import validate as validator


PLUGIN_ID = "io.github.alice.tool"
LINEAGE_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_URL = "https://github.com/alice/tool"
TARGET_URL = "https://github.com/bob/tool"
SOURCE_REPOSITORY_ID = 1001
TARGET_REPOSITORY_ID = 2002
SOURCE_OWNER_ID = 101
TARGET_OWNER_ID = 202
ADMIN_ID = 900


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def release(*, generation: int = 1, repository_url: str = SOURCE_URL) -> dict:
    return {
        "schemaVersion": 1,
        "generation": generation,
        "version": "1.0.0",
        "channel": "stable",
        "publishedAt": "2026-08-20T00:00:00Z",
        "releaseNotesUrl": f"{repository_url}/releases/tag/v1.0.0",
        "download": {
            "url": f"{repository_url}/releases/download/v1.0.0/{PLUGIN_ID}-1.0.0.zip",
            "sha256": ("a" if generation == 1 else "b") * 64,
            "size": 123,
        },
        "compatibility": {
            "manifestVersion": 1,
            "apiVersion": "1.0",
            "minimumLauncherVersion": (
                "0.1.0" if generation == 1 else "1.0.0-preview1"
            ),
        },
        "requiredCapabilities": [],
        "optionalCapabilities": [],
        "yanked": False,
    }


def plugin(repository_url: str = SOURCE_URL, maintainer: str = "alice") -> dict:
    return {
        "schemaVersion": 1,
        "id": PLUGIN_ID,
        "name": "Lifecycle fixture",
        "description": "Lifecycle fixture plugin.",
        "authors": [maintainer.title()],
        "repositoryUrl": repository_url,
        "maintainers": [maintainer],
        "categories": ["utilities"],
        "license": "MIT",
    }


def event(
    operation: str,
    *,
    generation: int = 1,
    source_repository_id: int = SOURCE_REPOSITORY_ID,
    target_repository_id: int | None = None,
    actor_id: int = ADMIN_ID,
    staging_pull_request: int | None = None,
) -> dict:
    target = (
        f" target:{target_repository_id}"
        if target_repository_id is not None
        else ""
    )
    target_url = TARGET_URL if operation == "transfer" else "N/A"
    if operation == "purge" and staging_pull_request is None:
        staging_pull_request = 88
    staging_command = (
        f" staging-pr:{staging_pull_request}"
        if staging_pull_request is not None
        else ""
    )
    staging_body = str(staging_pull_request) if staging_pull_request else "N/A"
    return {
        "repository": {"full_name": "TouristH/NyaLauncher-Plugins"},
        "issue": {
            "number": 7,
            "title": f"[Lifecycle] {PLUGIN_ID}",
            "body": (
                f"### 操作 / Operation\n\n{operation}\n\n"
                f"### 插件 ID / Plugin ID\n\n{PLUGIN_ID}\n\n"
                f"### 当前代际 / Current generation\n\n{generation}\n\n"
                "### 源数字仓库 ID / Source repository ID\n\n"
                f"{source_repository_id}\n\n"
                f"### 目标仓库 / Target repository\n\n{target_url}\n\n"
                "### Staging PR 编号 / Staging PR number\n\n"
                f"{staging_body}\n\n"
                "### 原因 / Reason\n\nAuthor-requested lifecycle change.\n"
            ),
        },
        "comment": {
            "id": 50,
            "body": (
                f"/apply-lifecycle {operation} {PLUGIN_ID}@g{generation} "
                f"source:{source_repository_id}{target}{staging_command}"
            ),
            "created_at": "2026-08-23T00:00:00Z",
            "user": {"login": "RegistryAdmin", "id": actor_id, "type": "User"},
        },
    }


class LifecycleFixture:
    def __init__(self, root: Path):
        self.root = root
        write_json(
            root / "repository.json",
            {
                "schemaVersion": 2,
                "name": "Test registry",
                "sourceUrl": "https://github.com/TouristH/NyaLauncher-Plugins",
                "launcherUrl": "https://github.com/redstore-noob/NyaLauncher",
                "indexPath": "public/v1/index.json",
                "indexV2Path": "public/v2/index.json",
                "v2MinimumLauncherVersion": "1.0.0-preview1",
                "registryBotLogin": "nyalauncher-registry-bot[bot]",
                "trustedReviewers": ["RegistryAdmin"],
                "trustedReviewerIds": {"RegistryAdmin": ADMIN_ID},
            },
        )
        write_json(
            root / "plugins.json",
            [
                {
                    "id": PLUGIN_ID,
                    "lineageId": LINEAGE_ID,
                    "generation": 1,
                    "repositoryUrl": SOURCE_URL,
                    "repositoryId": SOURCE_REPOSITORY_ID,
                    "ownerId": SOURCE_OWNER_ID,
                }
            ],
        )
        write_json(root / "plugins" / PLUGIN_ID / "plugin.json", plugin())
        write_json(
            root / "plugins" / PLUGIN_ID / "identity.json",
            {
                "schemaVersion": 1,
                "id": PLUGIN_ID,
                "lineageId": LINEAGE_ID,
                "generation": 1,
                "lifecycleStatus": "active",
                "generations": [
                    {
                        "generation": 1,
                        "repositoryUrl": SOURCE_URL,
                        "repositoryUrlHistory": [SOURCE_URL],
                        "repositoryId": SOURCE_REPOSITORY_ID,
                        "ownerId": SOURCE_OWNER_ID,
                        "status": "active",
                    }
                ],
            },
        )
        write_json(
            root / "plugins" / PLUGIN_ID / "releases" / "1.0.0.json",
            release(),
        )
        write_json(
            root / "reviews" / PLUGIN_ID / "1.0.0.json",
            {
                "schemaVersion": 1,
                "generation": 1,
                "pluginId": PLUGIN_ID,
                "version": "1.0.0",
                "sha256": "a" * 64,
                "status": "verified",
                "stateBy": "RegistryAdmin",
                "stateById": ADMIN_ID,
                "stateAt": "2026-08-21T00:00:00Z",
                "lastCommandAt": "2026-08-21T00:00:00Z",
                "lastCommentId": 40,
            },
        )

    def retire_without_event(self) -> None:
        write_json(self.root / "plugins.json", [])
        identity_path = self.root / "plugins" / PLUGIN_ID / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["lifecycleStatus"] = "retired"
        identity["generations"][-1]["status"] = "retired"
        write_json(identity_path, identity)
        release_path = (
            self.root / "plugins" / PLUGIN_ID / "releases" / "1.0.0.json"
        )
        value = json.loads(release_path.read_text(encoding="utf-8"))
        value["yanked"] = True
        value["yankReason"] = "Retired."
        write_json(release_path, value)
        review_path = self.root / "reviews" / PLUGIN_ID / "1.0.0.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["status"] = "revoked"
        review["stateAt"] = "2026-08-22T00:00:00Z"
        review["lastCommandAt"] = "2026-08-22T00:00:00Z"
        review["lastCommentId"] = 41
        review["notes"] = "Lifecycle retirement: Retired."
        write_json(review_path, review)


def source_repository() -> dict:
    return {
        "repositoryId": SOURCE_REPOSITORY_ID,
        "ownerId": SOURCE_OWNER_ID,
        "repositoryUrl": SOURCE_URL,
        "ownerType": "User",
        "ownerLogin": "alice",
    }


def target_repository() -> dict:
    return {
        "repositoryId": TARGET_REPOSITORY_ID,
        "ownerId": TARGET_OWNER_ID,
        "repositoryUrl": TARGET_URL,
        "ownerType": "User",
        "ownerLogin": "bob",
    }


class LifecycleTests(unittest.TestCase):
    def confirmation(self) -> dict:
        return {
            "kind": "owner-comment",
            "ownerId": SOURCE_OWNER_ID,
            "commentId": 41,
        }

    def test_numeric_administrator_identity_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                self.assertRaises(lifecycle.LifecycleFailure),
            ):
                lifecycle.actor_identity(event("retire", actor_id=ADMIN_ID + 1))

    def test_archived_public_source_is_allowed_but_archived_target_is_not(self):
        value = {
            "id": SOURCE_REPOSITORY_ID,
            "html_url": SOURCE_URL,
            "private": False,
            "fork": False,
            "archived": True,
            "disabled": False,
            "owner": {
                "id": SOURCE_OWNER_ID,
                "login": "alice",
                "type": "User",
            },
        }
        with patch.object(lifecycle, "github_get", return_value=value):
            source = lifecycle.repository_by_id(
                event("retire"), SOURCE_REPOSITORY_ID, allow_archived=True
            )
            self.assertEqual(source["repositoryId"], SOURCE_REPOSITORY_ID)
            with self.assertRaises(lifecycle.LifecycleFailure):
                lifecycle.repository_by_id(event("retire"), SOURCE_REPOSITORY_ID)

    def test_issue_fields_and_command_are_bound_exactly(self):
        request = event("retire")
        request["issue"]["body"] = request["issue"]["body"].replace(
            "### 当前代际 / Current generation\n\n1",
            "### 当前代际 / Current generation\n\n2",
        )
        with self.assertRaises(lifecycle.LifecycleFailure):
            lifecycle.parse_request(request)

    def test_stale_generation_is_rejected_before_repository_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            stale = event("retire", generation=2)
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(lifecycle, "repository_by_id") as repository_lookup,
                self.assertRaisesRegex(lifecycle.LifecycleFailure, "拒绝重放"),
            ):
                lifecycle.apply(stale)
            repository_lookup.assert_not_called()

    def test_purge_staging_pr_is_app_owned_sha_bound_and_single_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            request = lifecycle.parse_request(event("purge"))
            head_sha = "a" * 40
            pull = {
                "state": "open",
                "base": {"ref": "main"},
                "head": {
                    "ref": "registry-bot/sync",
                    "sha": head_sha,
                    "repo": {"full_name": "TouristH/NyaLauncher-Plugins"},
                },
                "user": {
                    "login": "nyalauncher-registry-bot[bot]",
                    "id": 987654321,
                    "type": "Bot",
                },
            }
            expected_paths = (
                "plugins.json\0"
                f"plugins/{PLUGIN_ID}/identity.json\0"
                f"plugins/{PLUGIN_ID}/plugin.json\0"
                f"plugins/{PLUGIN_ID}/releases/1.0.0.json\0"
                "plugin_details.json\0public/v1/index.json\0public/v2/index.json\0"
            )

            def git_output(*arguments):
                if arguments[:2] == ("rev-parse", "HEAD"):
                    return head_sha + "\n"
                if arguments[:2] == ("diff", "--name-only"):
                    return expected_paths
                return "[]\n"

            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(lifecycle, "github_get", return_value=pull),
                patch.object(lifecycle, "git_output", side_effect=git_output),
                patch.object(
                    lifecycle.subprocess,
                    "run",
                    return_value=Mock(returncode=1),
                ),
                patch.dict(
                    lifecycle.os.environ,
                    {"NYA_LIFECYCLE_PUBLIC_REF": "origin/main"},
                ),
            ):
                staging = lifecycle.validate_purge_staging(
                    event("purge"), request
                )

            self.assertEqual(staging["number"], 88)
            self.assertEqual(staging["headSha"], head_sha)

            pull["state"] = "closed"
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(lifecycle, "github_get", return_value=pull),
                patch.object(lifecycle, "git_output", side_effect=git_output),
                patch.object(
                    lifecycle.subprocess,
                    "run",
                    return_value=Mock(returncode=1),
                ),
                patch.dict(
                    lifecycle.os.environ,
                    {
                        "NYA_LIFECYCLE_PUBLIC_REF": "origin/main",
                        "NYA_PURGE_STAGING_CLOSED_PR": "88",
                        "NYA_PURGE_STAGING_CLOSED_SHA": head_sha,
                        "NYA_PURGE_STAGING_CLOSED_HEAD_REF": (
                            "registry-bot/sync"
                        ),
                        "NYA_PURGE_STAGING_AUTHOR_ID": "987654321",
                    },
                    clear=True,
                ),
            ):
                closed_staging = lifecycle.validate_purge_staging(
                    event("purge"), request
                )
            self.assertEqual(closed_staging["headSha"], head_sha)

            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(lifecycle, "github_get", return_value=pull),
                patch.dict(
                    lifecycle.os.environ,
                    {"NYA_LIFECYCLE_PUBLIC_REF": "origin/main"},
                    clear=True,
                ),
                self.assertRaisesRegex(lifecycle.LifecycleFailure, "预先关闭"),
            ):
                lifecycle.validate_purge_staging(event("purge"), request)

            pull["state"] = "open"

            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(lifecycle, "github_get", return_value=pull),
                patch.object(
                    lifecycle,
                    "git_output",
                    side_effect=[
                        head_sha + "\n",
                        expected_paths + "plugins/other.id/plugin.json\0",
                    ],
                ),
                patch.object(
                    lifecycle.subprocess,
                    "run",
                    return_value=Mock(returncode=1),
                ),
                patch.dict(
                    lifecycle.os.environ,
                    {"NYA_LIFECYCLE_PUBLIC_REF": "origin/main"},
                ),
                self.assertRaisesRegex(lifecycle.LifecycleFailure, "越界路径"),
            ):
                lifecycle.validate_purge_staging(event("purge"), request)

    def test_retire_hides_plugin_yanks_history_and_keeps_review_tombstone(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(
                    lifecycle, "repository_by_id", return_value=source_repository()
                ),
                patch.object(
                    lifecycle,
                    "require_author_confirmation",
                    return_value=self.confirmation(),
                ),
            ):
                summary = lifecycle.apply(event("retire"))
                index_v1 = validator.build_index()
                index_v2 = validator.build_index_v2()

            self.assertIn("retired", summary)
            self.assertEqual(
                json.loads((fixture.root / "plugins.json").read_text(encoding="utf-8")),
                [],
            )
            identity = json.loads(
                (fixture.root / "plugins" / PLUGIN_ID / "identity.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(identity["lifecycleStatus"], "retired")
            self.assertEqual(identity["events"][0]["actorId"], ADMIN_ID)
            self.assertEqual(
                identity["events"][0]["sourceRepositoryId"], SOURCE_REPOSITORY_ID
            )
            central_release = json.loads(
                (
                    fixture.root
                    / "plugins"
                    / PLUGIN_ID
                    / "releases"
                    / "1.0.0.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(central_release["yanked"])
            review = json.loads(
                (
                    fixture.root
                    / "reviews"
                    / PLUGIN_ID
                    / "1.0.0.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(review["status"], "revoked")
            self.assertEqual(review["stateById"], ADMIN_ID)
            self.assertEqual(index_v1["plugins"], [])
            self.assertEqual(index_v2["plugins"][0]["visibility"], "hidden")

    def test_retire_only_yanks_current_unyanked_generation_and_preserves_tombstones(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            old_release_path = (
                fixture.root
                / "plugins"
                / PLUGIN_ID
                / "releases"
                / "1.0.0.json"
            )
            old_review_path = fixture.root / "reviews" / PLUGIN_ID / "1.0.0.json"
            old_release = json.loads(old_release_path.read_text(encoding="utf-8"))
            old_release["yanked"] = True
            old_release["yankReason"] = "Original g1 retirement."
            write_json(old_release_path, old_release)
            old_review = json.loads(old_review_path.read_text(encoding="utf-8"))
            old_review.update(
                {
                    "status": "revoked",
                    "stateBy": "FormerReviewer",
                    "stateById": 999,
                    "stateAt": "2026-08-21T00:00:00Z",
                    "lastCommandAt": "2026-08-21T00:00:00Z",
                    "lastCommentId": 39,
                    "notes": "Original g1 tombstone.",
                }
            )
            write_json(old_review_path, old_review)
            generation_root = (
                fixture.root
                / "plugins"
                / PLUGIN_ID
                / "generations"
                / "g2"
            )
            current_release_path = generation_root / "releases" / "2.0.0.json"
            current_release = release(generation=2, repository_url=TARGET_URL)
            current_release["version"] = "2.0.0"
            write_json(current_release_path, current_release)
            current_review_path = (
                fixture.root / "reviews" / PLUGIN_ID / "g2" / "2.0.0.json"
            )
            current_review = json.loads(old_review_path.read_text(encoding="utf-8"))
            current_review.update(
                {
                    "generation": 2,
                    "version": "2.0.0",
                    "sha256": current_release["download"]["sha256"],
                    "status": "verified",
                    "stateBy": "RegistryAdmin",
                    "stateById": ADMIN_ID,
                    "stateAt": "2026-08-22T00:00:00Z",
                    "lastCommandAt": "2026-08-22T00:00:00Z",
                    "lastCommentId": 40,
                    "notes": "Verified g2.",
                }
            )
            write_json(current_review_path, current_review)
            plugin_value = {
                "id": PLUGIN_ID,
                "generation": 2,
                "releases": [
                    {**current_release, "generation": 2},
                    {**old_release, "generation": 1},
                ],
            }

            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
            ):
                lifecycle.yank_all_releases(
                    plugin_value,
                    "Current g2 retirement.",
                    "RegistryAdmin",
                    ADMIN_ID,
                    "2026-08-23T00:00:00Z",
                    41,
                )

            self.assertEqual(
                json.loads(old_release_path.read_text(encoding="utf-8")),
                old_release,
            )
            self.assertEqual(
                json.loads(old_review_path.read_text(encoding="utf-8")),
                old_review,
            )
            self.assertEqual(
                json.loads(current_release_path.read_text(encoding="utf-8"))[
                    "yankReason"
                ],
                "Current g2 retirement.",
            )
            self.assertEqual(
                json.loads(current_review_path.read_text(encoding="utf-8"))[
                    "status"
                ],
                "revoked",
            )

    def test_transfer_increments_generation_and_duplicate_semver_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            fixture.retire_without_event()
            target_plugin = plugin(TARGET_URL, "bob")
            target_release = release(generation=2, repository_url=TARGET_URL)

            def repository_lookup(_event, repository_id, **_options):
                return (
                    source_repository()
                    if repository_id == SOURCE_REPOSITORY_ID
                    else target_repository()
                )

            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(
                    lifecycle, "repository_by_id", side_effect=repository_lookup
                ),
                patch.object(
                    lifecycle,
                    "require_author_confirmation",
                    return_value=self.confirmation(),
                ),
                patch.object(validator, "fetch_repository_manifest", return_value={}),
                patch.object(
                    validator,
                    "validate_publisher_manifest_releases",
                    return_value=(target_plugin, [target_release]),
                ),
            ):
                summary = lifecycle.apply(
                    event(
                        "transfer",
                        target_repository_id=TARGET_REPOSITORY_ID,
                    )
                )

            self.assertIn("g1 -> g2", summary)
            identity_path = fixture.root / "plugins" / PLUGIN_ID / "identity.json"
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            self.assertEqual(identity["generation"], 2)
            self.assertEqual(identity["lifecycleStatus"], "transferred")
            self.assertEqual(identity["generations"][0]["status"], "transferred")
            self.assertEqual(
                identity["events"][-1]["targetRepositoryId"], TARGET_REPOSITORY_ID
            )
            listing = json.loads(
                (fixture.root / "plugins.json").read_text(encoding="utf-8")
            )[0]
            self.assertEqual(listing["lineageId"], LINEAGE_ID)
            self.assertEqual(listing["generation"], 2)
            self.assertEqual(listing["repositoryId"], TARGET_REPOSITORY_ID)

            identity["lifecycleStatus"] = "active"
            write_json(identity_path, identity)
            write_json(
                fixture.root
                / "plugins"
                / PLUGIN_ID
                / "generations"
                / "g2"
                / "releases"
                / "1.0.0.json",
                target_release,
            )
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
            ):
                index_v1 = validator.build_index()
                index_v2 = validator.build_index_v2()
            self.assertEqual(index_v1["plugins"], [])
            versions = [
                (item["generation"], item["version"])
                for item in index_v2["plugins"][0]["releases"]
            ]
            self.assertEqual(versions, [(2, "1.0.0"), (1, "1.0.0")])
            self.assertEqual(index_v2["plugins"][0]["visibility"], "listed")

    def test_purge_rejects_active_or_public_lineage_and_tombstones_staging_only(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(Path(directory))
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(
                    lifecycle,
                    "validate_purge_staging",
                    side_effect=lifecycle.LifecycleFailure(
                        "插件身份已经存在于 main"
                    ),
                ),
                self.assertRaisesRegex(lifecycle.LifecycleFailure, "存在于 main"),
            ):
                lifecycle.apply(event("purge"))

            fixture.retire_without_event()
            shutil.rmtree(fixture.root / "reviews" / PLUGIN_ID)
            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(
                    lifecycle, "repository_by_id", return_value=source_repository()
                ),
                patch.object(
                    lifecycle,
                    "validate_purge_staging",
                    return_value={"number": 88},
                ),
                patch.object(lifecycle, "lineage_was_public", return_value=True),
                self.assertRaisesRegex(lifecycle.LifecycleFailure, "曾进入公开索引"),
            ):
                lifecycle.apply(event("purge"))

            with (
                patch.object(lifecycle, "ROOT", fixture.root),
                patch.object(validator, "ROOT", fixture.root),
                patch.object(
                    lifecycle, "repository_by_id", return_value=source_repository()
                ),
                patch.object(
                    lifecycle,
                    "validate_purge_staging",
                    return_value={"number": 88},
                ),
                patch.object(lifecycle, "lineage_was_public", return_value=False),
            ):
                summary = lifecycle.apply(event("purge"))

            self.assertIn("永久 lineage tombstone", summary)
            self.assertFalse((fixture.root / "plugins" / PLUGIN_ID).exists())
            tombstone = json.loads(
                (
                    fixture.root
                    / "tombstones"
                    / PLUGIN_ID
                    / f"{LINEAGE_ID}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(tombstone["purgedById"], ADMIN_ID)
            self.assertEqual(tombstone["repositoryId"], SOURCE_REPOSITORY_ID)


if __name__ == "__main__":
    unittest.main()
