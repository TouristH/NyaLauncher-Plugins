import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools import registry_bot as bot
from tools import validate as validator

REAL_RESOLVE_REPOSITORY_IDENTITY = bot.resolve_repository_identity


def issue(number, plugin_id, repository_url):
    return {
        "number": number,
        "title": f"[Plugin] {plugin_id}",
        "state": "open",
        "labels": [{"name": "plugin-submission"}],
        "body": (
            "### 插件 ID / Plugin ID\n\n"
            f"{plugin_id}\n\n"
            "### 仓库地址 / Repository URL\n\n"
            f"{repository_url}\n"
        ),
    }


def topic_repository(
    owner="alice",
    name="plugin",
    *,
    private=False,
    fork=False,
    archived=False,
    disabled=False,
    topics=None,
):
    owner_id = 1000 + sum(ord(character) for character in owner)
    repository_id = owner_id * 1000 + sum(ord(character) for character in name)
    return {
        "id": repository_id,
        "html_url": f"https://github.com/{owner}/{name}",
        "owner": {"id": owner_id, "login": owner},
        "private": private,
        "fork": fork,
        "archived": archived,
        "disabled": disabled,
        "visibility": "private" if private else "public",
        "topics": [bot.DISCOVERY_TOPIC] if topics is None else topics,
    }


def plugin_snapshot(plugin_id, repository_url, version="1.0.0"):
    return {
        "id": plugin_id,
        "name": "Plugin",
        "description": "Plugin description",
        "authors": ["Alice"],
        "repositoryUrl": repository_url,
        "maintainers": ["alice"],
        "categories": ["utilities"],
        "license": "MIT",
        "releases": [
            {
                "version": version,
                "yanked": False,
                "download": {"size": 1, "sha256": "a" * 64},
            }
        ],
    }


class RegistryBotTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"GITHUB_REPOSITORY": "TouristH/NyaLauncher-Plugins"},
            clear=False,
        )
        self.environment.start()
        self.identity = patch.object(
            bot,
            "resolve_repository_identity",
            side_effect=lambda candidate, repository_url, _api_get: (
                candidate.repository_id or 1001,
                candidate.owner_id or 101,
                repository_url,
            ),
        )
        self.identity.start()

    def tearDown(self):
        self.identity.stop()
        self.environment.stop()

    def test_issue_repository_identity_is_resolved_from_github_numeric_ids(self):
        candidate = bot.Candidate(
            source="issue", repository_url="https://github.com/alice/plugin"
        )
        metadata = topic_repository(owner="alice", name="plugin")
        repository_id, owner_id, repository_url = REAL_RESOLVE_REPOSITORY_IDENTITY(
            candidate,
            candidate.repository_url,
            lambda path: metadata,
        )
        self.assertEqual(repository_id, metadata["id"])
        self.assertEqual(owner_id, metadata["owner"]["id"])
        self.assertEqual(repository_url, candidate.repository_url)

    def test_pending_merge_issue_is_reconciled_after_data_reaches_main(self):
        plugin_id = "io.github.alice.plugin"
        repository_url = "https://github.com/alice/plugin"
        pending = issue(77, plugin_id, repository_url)
        pending["state"] = "closed"
        pending["labels"] = [{"name": "pending-merge"}]
        listing = {
            "id": plugin_id,
            "repositoryUrl": repository_url,
            "repositoryId": 1001,
            "ownerId": 101,
        }
        snapshot = plugin_snapshot(plugin_id, repository_url)

        def api_get(path):
            if "labels=pending-merge" in path:
                return [pending]
            if "/issues?" in path:
                return []
            return {"total_count": 0, "incomplete_results": False, "items": []}

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[listing]),
            patch.object(bot.validator, "load_catalog", return_value=[snapshot]),
            patch.object(bot.validator, "build_details", return_value=[snapshot]),
            patch.object(bot.validator, "refresh_details", return_value=[snapshot]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(
            result["reconciled"], [{"issueNumber": 77, "id": plugin_id}]
        )

    def test_closed_intake_issue_without_pending_label_is_reconciled_once(self):
        plugin_id = "io.github.alice.plugin"
        repository_url = "https://github.com/alice/plugin"
        closed = issue(78, plugin_id, repository_url)
        closed["state"] = "closed"
        closed["labels"] = [
            {"name": "plugin-submission"},
            {"name": "queued-for-intake"},
        ]
        listing = {
            "id": plugin_id,
            "repositoryUrl": repository_url,
            "repositoryId": 1001,
            "ownerId": 101,
        }
        snapshot = plugin_snapshot(plugin_id, repository_url)
        paths = []

        def api_get(path):
            paths.append(path)
            return [closed]

        reconciled = bot.collect_reconciled_issues(
            [listing], [snapshot], api_get
        )

        self.assertEqual(reconciled, [{"issueNumber": 78, "id": plugin_id}])
        self.assertEqual(len(paths), 3)
        self.assertTrue(any("labels=pending-merge" in path for path in paths))
        self.assertTrue(any("labels=plugin-submission" in path for path in paths))
        self.assertTrue(any("labels=queued-for-intake" in path for path in paths))

    def test_full_latest_reconciliation_page_rotates_through_older_backlog(self):
        plugin_id = "io.github.alice.plugin"
        repository_url = "https://github.com/alice/plugin"
        target = issue(178, plugin_id, repository_url)
        target["state"] = "closed"
        listing = {
            "id": plugin_id,
            "repositoryUrl": repository_url,
            "repositoryId": 1001,
            "ownerId": 101,
        }
        snapshot = plugin_snapshot(plugin_id, repository_url)
        junk = [
            {
                "number": number,
                "title": "not a plugin request",
                "state": "closed",
                "labels": [{"name": "plugin-submission"}],
                "body": "",
            }
            for number in range(1, bot.MAXIMUM_SEARCH_RESULTS + 1)
        ]
        paths = []

        def api_get(path):
            paths.append(path)
            if "labels=plugin-submission" in path and "&page=1" in path:
                return junk
            if "labels=plugin-submission" in path and "&page=2" in path:
                return [target]
            return []

        with patch.dict(os.environ, {"NYA_REFRESH_OFFSET": "0"}, clear=False):
            reconciled = bot.collect_reconciled_issues(
                [listing], [snapshot], api_get
            )

        self.assertEqual(reconciled, [{"issueNumber": 178, "id": plugin_id}])
        self.assertTrue(
            any(
                "labels=plugin-submission" in path and "&page=2" in path
                for path in paths
            )
        )
        self.assertTrue(all("direction=desc" in path for path in paths))

    def test_reconciliation_requires_active_pointer_matching_history_and_usable_release(self):
        plugin_id = "io.github.alice.plugin"
        repository_url = "https://github.com/alice/plugin"
        closed = issue(79, plugin_id, repository_url)
        closed["state"] = "closed"
        listing = {
            "id": plugin_id,
            "repositoryUrl": repository_url,
            "repositoryId": 1001,
            "ownerId": 101,
        }
        snapshot = plugin_snapshot(plugin_id, repository_url)

        def api_get(_path):
            return [closed]

        yanked = plugin_snapshot(plugin_id, repository_url)
        yanked["releases"][0]["yanked"] = True
        cases = (
            ([], [snapshot]),
            ([{**listing, "repositoryUrl": "https://github.com/alice/other"}], [snapshot]),
            ([listing], [yanked]),
        )
        for active, catalog in cases:
            with self.subTest(active=active, catalog=catalog):
                self.assertEqual(
                    bot.collect_reconciled_issues(active, catalog, api_get), []
                )

        open_issue = issue(80, plugin_id, repository_url)
        self.assertEqual(
            bot.collect_reconciled_issues(
                [listing], [snapshot], lambda _path: [open_issue]
            ),
            [],
        )

    def test_github_availability_errors_are_retryable(self):
        service_unavailable = urllib.error.HTTPError(
            "https://api.github.com/test",
            503,
            "unavailable",
            {},
            io.BytesIO(b"temporarily unavailable"),
        )
        try:
            with patch.object(
                bot.urllib.request, "urlopen", side_effect=service_unavailable
            ):
                with self.assertRaises(bot.RegistryBotRetryableFailure):
                    bot.github_get("/test")
        finally:
            service_unavailable.close()

        with patch.object(
            bot.urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(bot.RegistryBotRetryableFailure):
                bot.github_get("/test")

    def test_candidate_api_availability_failure_is_deferred_not_rejected(self):
        plugin_id = "io.github.alice.plugin"
        repository_url = "https://github.com/alice/plugin"

        def api_get(path):
            if "state=open&labels=plugin-submission" in path:
                return [issue(81, plugin_id, repository_url)]
            if "/issues?" in path:
                return []
            return {"total_count": 0, "incomplete_results": False, "items": []}

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(
                bot,
                "resolve_repository_identity",
                side_effect=bot.RegistryBotRetryableFailure("GitHub API 503"),
            ),
            patch.object(bot.validator, "refresh_details", return_value=[]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(result["rejected"], [])
        self.assertEqual([item["issueNumber"] for item in result["deferred"]], [81])
        self.assertIn("503", result["deferred"][0]["reason"])

    def test_topic_search_filters_non_public_fork_archived_and_wrong_topic(self):
        items = [
            topic_repository(owner="alice", name="valid"),
            topic_repository(owner="alice", name="private", private=True),
            topic_repository(owner="alice", name="fork", fork=True),
            topic_repository(owner="alice", name="archived", archived=True),
            topic_repository(owner="alice", name="disabled", disabled=True),
            topic_repository(owner="alice", name="wrong", topics=["other"]),
        ]
        seen_paths = []

        def api_get(path):
            seen_paths.append(path)
            return {"total_count": len(items), "incomplete_results": False, "items": items}

        candidates, warnings = bot.collect_topic_candidates(api_get)

        self.assertEqual([item.repository_url for item in candidates], ["https://github.com/alice/valid"])
        self.assertEqual(warnings, [])
        self.assertEqual(len(seen_paths), 1)
        self.assertIn("per_page=100", seen_paths[0])
        self.assertIn("nyalauncher-plugin", urllib_unquote(seen_paths[0]))

    def test_incomplete_topic_search_fails_closed_for_topics(self):
        candidates, warnings = bot.collect_topic_candidates(
            lambda _: {
                "total_count": 1,
                "incomplete_results": True,
                "items": [topic_repository()],
            }
        )
        self.assertEqual(candidates, [])
        self.assertTrue(any("不完整" in warning for warning in warnings))

    def test_topic_search_is_capped_at_one_hundred_results(self):
        items = [
            topic_repository(owner=f"user{number}", name="plugin")
            for number in range(120)
        ]
        candidates, warnings = bot.collect_topic_candidates(
            lambda _: {
                "total_count": len(items),
                "incomplete_results": False,
                "items": items,
            }
        )
        self.assertEqual(len(candidates), bot.MAXIMUM_SEARCH_RESULTS)
        self.assertTrue(any("只检查" in warning for warning in warnings))

    def test_issue_candidates_are_loaded_before_topic_candidates(self):
        calls = []
        manifests = {
            "https://github.com/alice/issue": {"id": "io.github.alice.issue"},
            "https://github.com/bob/topic": {"id": "io.github.bob.topic"},
        }
        priority_ids = []

        def api_get(path):
            if "/issues?" in path:
                return [issue(7, "io.github.alice.issue", "https://github.com/alice/issue")]
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [topic_repository(owner="bob", name="topic")],
            }

        def fetch(repository_url, _source):
            calls.append(repository_url)
            return manifests[repository_url]

        def refresh(listings, _details, **kwargs):
            priority_ids.extend(kwargs["priority_ids"])
            return [
                plugin_snapshot(item["id"], item["repositoryUrl"])
                for item in listings
            ]

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(bot.validator, "fetch_repository_manifest", side_effect=fetch),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", side_effect=refresh),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(
            calls,
            ["https://github.com/alice/issue", "https://github.com/bob/topic"],
        )
        self.assertEqual([item["source"] for item in result["accepted"]], ["issue", "topic"])
        self.assertEqual(result["accepted"][0]["issueNumber"], 7)
        self.assertEqual(
            priority_ids,
            ["io.github.alice.issue", "io.github.bob.topic"],
        )

    def test_namespace_must_match_repository_owner(self):
        def api_get(path):
            if "/issues?" in path:
                return []
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [topic_repository(owner="alice", name="bad")],
            }

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(
                bot.validator,
                "fetch_repository_manifest",
                return_value={"id": "io.github.mallory.bad"},
            ),
            patch.object(bot.validator, "refresh_details", return_value=[]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["rejected"][0]["id"], "io.github.mallory.bad")
        self.assertIn("io.github.alice.", result["rejected"][0]["reason"])

    def test_historical_id_cannot_be_claimed_by_another_repository(self):
        plugin_id = "io.github.alice.tool"
        repository_url = "https://github.com/alice/original"
        historical = plugin_snapshot(plugin_id, repository_url)
        active = {
            "id": plugin_id,
            "repositoryUrl": repository_url,
            "repositoryId": 2001,
            "ownerId": 201,
        }

        def api_get(path):
            if "/issues?" in path:
                return [issue(8, plugin_id, "https://github.com/alice/impostor")]
            return {"total_count": 0, "incomplete_results": False, "items": []}

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[active]),
            patch.object(bot.validator, "load_catalog", return_value=[historical]),
            patch.object(bot.validator, "build_details", return_value=[historical]),
            patch.object(bot.validator, "fetch_repository_manifest", return_value={"id": plugin_id}),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", return_value=[historical]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(result["accepted"], [])
        self.assertIn("插件 ID 已由", result["rejected"][0]["reason"])

    def test_legacy_archive_without_numeric_identity_requires_manual_migration(self):
        plugin_id = "io.github.alice.tool"
        repository_url = "https://github.com/alice/original"
        historical = plugin_snapshot(plugin_id, repository_url)

        def api_get(path):
            if "/issues?" in path:
                return [issue(8, plugin_id, repository_url)]
            return {"total_count": 0, "incomplete_results": False, "items": []}

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[historical]),
            patch.object(bot.validator, "build_details", return_value=[historical]),
            patch.object(
                bot.validator,
                "fetch_repository_manifest",
                return_value={"id": plugin_id},
            ),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", return_value=[historical]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(result["accepted"], [])
        self.assertIn("numeric identity", result["rejected"][0]["reason"])

    def test_failed_candidates_consume_the_attempt_budget(self):
        issues = [
            issue(
                number,
                f"io.github.user{number}.plugin",
                f"https://github.com/user{number}/plugin",
            )
            for number in range(1, 41)
        ]
        fetch_count = 0

        def api_get(path):
            if "/issues?" in path:
                return issues
            return {"total_count": 0, "incomplete_results": False, "items": []}

        def fail_fetch(_repository_url, _source):
            nonlocal fetch_count
            fetch_count += 1
            raise validator.ValidationFailure("invalid manifest")

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(bot.validator, "fetch_repository_manifest", side_effect=fail_fetch),
            patch.object(bot.validator, "refresh_details", return_value=[]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(fetch_count, bot.MAXIMUM_DISCOVERY_ATTEMPTS)
        self.assertEqual(len(result["rejected"]), bot.MAXIMUM_DISCOVERY_ATTEMPTS)
        self.assertTrue(any("尝试达到" in warning for warning in result["warnings"]))

    def test_candidate_queue_rotates_between_scheduled_runs(self):
        candidates = [
            bot.Candidate(source="issue", issue_number=number)
            for number in range(40)
        ]
        with patch.dict(os.environ, {"NYA_REFRESH_OFFSET": "1"}, clear=False):
            rotated = bot._rotate_candidates(candidates)
        self.assertEqual(rotated[0].issue_number, 1)
        self.assertEqual({item.issue_number for item in rotated}, set(range(40)))

        leaders = set()
        for run_number in range(32):
            with patch.dict(
                os.environ, {"NYA_REFRESH_OFFSET": str(run_number)}, clear=False
            ):
                leaders.add(bot._rotate_candidates(candidates[:32])[0].issue_number)
        self.assertEqual(leaders, set(range(32)))

    def test_same_id_from_different_repositories_is_rejected_for_both(self):
        shared_id = "io.github.alice.shared"

        def api_get(path):
            if "/issues?" in path:
                return []
            return {
                "total_count": 2,
                "incomplete_results": False,
                "items": [
                    topic_repository(owner="alice", name="one"),
                    topic_repository(owner="alice", name="two"),
                ],
            }

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(
                bot.validator,
                "fetch_repository_manifest",
                return_value={"id": shared_id},
            ),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", return_value=[]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(result["accepted"], [])
        self.assertEqual(len(result["rejected"]), 2)
        self.assertTrue(
            all("多个不同仓库声明同一插件 ID" in item["reason"] for item in result["rejected"])
        )

    def test_issue_flood_cannot_starve_topic_discovery(self):
        issues = [
            issue(
                number,
                f"io.github.spammer{number}.plugin",
                f"https://github.com/spammer{number}/plugin",
            )
            for number in range(1, 41)
        ]
        topic_url = "https://github.com/alice/plugin"
        topic_id = "io.github.alice.plugin"

        def api_get(path):
            if "/issues?" in path:
                return issues
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [topic_repository()],
            }

        def fetch(repository_url, _source):
            if repository_url == topic_url:
                return {"id": topic_id}
            raise validator.ValidationFailure("invalid spam manifest")

        def refresh(listings, _details, **_kwargs):
            return [
                plugin_snapshot(item["id"], item["repositoryUrl"])
                for item in listings
            ]

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(bot.validator, "fetch_repository_manifest", side_effect=fetch),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", side_effect=refresh),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertIn(topic_id, [item["id"] for item in result["accepted"]])
        self.assertTrue(any("Issue 洪泛" in warning for warning in result["warnings"]))

    def test_new_plugin_cap_is_enforced_before_heavy_refresh(self):
        repositories = [
            topic_repository(owner=f"user{number}", name="plugin")
            for number in range(12)
        ]
        refreshed_listings = []

        def api_get(path):
            if "/issues?" in path:
                return []
            return {
                "total_count": len(repositories),
                "incomplete_results": False,
                "items": repositories,
            }

        def fetch(repository_url, _source):
            owner = repository_url.split("/")[-2]
            return {"id": f"io.github.{owner}.plugin"}

        def refresh(listings, _details, **_kwargs):
            refreshed_listings.extend(listings)
            return [
                plugin_snapshot(item["id"], item["repositoryUrl"])
                for item in listings
            ]

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(bot.validator, "fetch_repository_manifest", side_effect=fetch),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", side_effect=refresh),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(len(refreshed_listings), bot.MAXIMUM_NEW_PLUGINS)
        self.assertEqual(len(result["accepted"]), bot.MAXIMUM_NEW_PLUGINS)
        self.assertTrue(any("新插件达到" in warning for warning in result["warnings"]))

    def test_idempotent_topic_discovery_keeps_existing_pointer_without_refetch(self):
        listing = {
            "id": "io.github.alice.plugin",
            "repositoryUrl": "https://github.com/alice/plugin",
        }
        snapshot = plugin_snapshot(listing["id"], listing["repositoryUrl"])

        def api_get(path):
            if "/issues?" in path:
                return []
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [topic_repository()],
            }

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[listing]),
            patch.object(bot.validator, "load_catalog", return_value=[snapshot]),
            patch.object(bot.validator, "build_details", return_value=[snapshot]),
            patch.object(bot.validator, "fetch_repository_manifest") as fetch,
            patch.object(bot.validator, "refresh_details", return_value=[snapshot]) as refresh,
            patch.object(bot, "_write_generated_views") as write_views,
        ):
            result = bot.collect(write=True, api_get=api_get)

        fetch.assert_not_called()
        refresh.assert_called_once()
        write_views.assert_called_once_with([listing])
        self.assertEqual(result["accepted"], [])
        self.assertEqual(result["rejected"], [])

    def test_idempotent_issue_for_existing_pointer_can_be_closed(self):
        listing = {
            "id": "io.github.alice.plugin",
            "repositoryUrl": "https://github.com/alice/plugin",
        }
        snapshot = plugin_snapshot(listing["id"], listing["repositoryUrl"])

        def api_get(path):
            if "/issues?" in path:
                return [issue(17, listing["id"], listing["repositoryUrl"])]
            return {"total_count": 0, "incomplete_results": False, "items": []}

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[listing]),
            patch.object(bot.validator, "load_catalog", return_value=[snapshot]),
            patch.object(bot.validator, "build_details", return_value=[snapshot]),
            patch.object(bot.validator, "fetch_repository_manifest") as fetch,
            patch.object(bot.validator, "refresh_details", return_value=[snapshot]),
        ):
            result = bot.collect(write=False, api_get=api_get)

        fetch.assert_not_called()
        self.assertEqual(
            result["accepted"],
            [
                {
                    "source": "issue",
                    "issueNumber": 17,
                    "repositoryUrl": listing["repositoryUrl"],
                    "id": listing["id"],
                    "alreadyListed": True,
                }
            ],
        )

    def test_topic_removal_never_unlists_an_active_plugin(self):
        listing = {
            "id": "io.github.alice.plugin",
            "repositoryUrl": "https://github.com/alice/plugin",
        }
        snapshot = plugin_snapshot(listing["id"], listing["repositoryUrl"])

        def api_get(path):
            if "/issues?" in path:
                return []
            return {"total_count": 0, "incomplete_results": False, "items": []}

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[listing]),
            patch.object(bot.validator, "load_catalog", return_value=[snapshot]),
            patch.object(bot.validator, "build_details", return_value=[snapshot]),
            patch.object(bot.validator, "refresh_details", return_value=[snapshot]),
            patch.object(bot, "_write_generated_views") as write_views,
        ):
            bot.collect(write=True, api_get=api_get)

        write_views.assert_called_once_with([listing])

    def test_active_topic_repositories_do_not_starve_a_new_candidate(self):
        active = [
            {
                "id": f"io.github.user{number}.plugin",
                "repositoryUrl": f"https://github.com/user{number}/plugin",
            }
            for number in range(bot.MAXIMUM_DISCOVERY_ATTEMPTS + 3)
        ]
        catalog = [
            plugin_snapshot(item["id"], item["repositoryUrl"])
            for item in active
        ]
        repositories = [
            topic_repository(owner=f"user{number}", name="plugin")
            for number in range(len(active))
        ]
        repositories.append(topic_repository(owner="newowner", name="plugin"))

        def api_get(path):
            if "/issues?" in path:
                return []
            return {
                "total_count": len(repositories),
                "incomplete_results": False,
                "items": repositories,
            }

        new_id = "io.github.newowner.plugin"
        new_url = "https://github.com/newowner/plugin"

        def refresh(listings, _details, **_kwargs):
            return [*catalog, plugin_snapshot(new_id, new_url)]

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=active),
            patch.object(bot.validator, "load_catalog", return_value=catalog),
            patch.object(bot.validator, "build_details", return_value=catalog),
            patch.object(
                bot.validator,
                "fetch_repository_manifest",
                return_value={"id": new_id},
            ) as fetch,
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", side_effect=refresh),
        ):
            result = bot.collect(write=False, api_get=api_get)

        fetch.assert_called_once_with(new_url, new_url)
        self.assertEqual([item["id"] for item in result["accepted"]], [new_id])

    def test_only_candidate_with_new_verified_history_gets_active_pointer(self):
        valid_id = "io.github.alice.valid"
        failed_id = "io.github.bob.failed"
        valid_url = "https://github.com/alice/valid"
        failed_url = "https://github.com/bob/failed"

        def api_get(path):
            if "/issues?" in path:
                return [issue(9, valid_id, valid_url), issue(10, failed_id, failed_url)]
            return {"total_count": 0, "incomplete_results": False, "items": []}

        def fetch(repository_url, _source):
            return {"id": valid_id if repository_url == valid_url else failed_id}

        def refresh(_listings, _details, *, warnings, **_kwargs):
            warnings.append(f"{failed_id}：ZIP 校验失败；保留原中心历史")
            return [plugin_snapshot(valid_id, valid_url)]

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(bot.validator, "fetch_repository_manifest", side_effect=fetch),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", side_effect=refresh),
            patch.object(bot, "_write_generated_views") as write_views,
        ):
            result = bot.collect(write=True, api_get=api_get)

        write_views.assert_called_once_with(
            [
                {
                    "id": valid_id,
                    "repositoryUrl": valid_url,
                    "repositoryId": 1001,
                    "ownerId": 101,
                }
            ]
        )
        self.assertEqual([item["id"] for item in result["accepted"]], [valid_id])
        failed = next(item for item in result["rejected"] if item["id"] == failed_id)
        self.assertIn("ZIP 校验失败", failed["reason"])

    def test_retryable_refresh_failure_is_deferred_not_rejected(self):
        plugin_id = "io.github.alice.plugin"
        repository_url = "https://github.com/alice/plugin"

        def api_get(path):
            if "state=open&labels=plugin-submission" in path:
                return [issue(82, plugin_id, repository_url)]
            if "/issues?" in path:
                return []
            return {"total_count": 0, "incomplete_results": False, "items": []}

        def refresh(
            _listings,
            _details,
            *,
            warnings,
            retryable_failures,
            **_kwargs,
        ):
            warnings.append(
                f"{plugin_id}：Release ZIP download HTTP 503；保留原中心历史"
            )
            retryable_failures.add(plugin_id)
            return []

        with (
            patch.object(bot.validator, "load_plugin_list", return_value=[]),
            patch.object(bot.validator, "load_catalog", return_value=[]),
            patch.object(bot.validator, "build_details", return_value=[]),
            patch.object(
                bot.validator,
                "fetch_repository_manifest",
                return_value={"id": plugin_id},
            ),
            patch.object(bot.validator, "validate_publisher_manifest_releases"),
            patch.object(bot.validator, "refresh_details", side_effect=refresh),
        ):
            result = bot.collect(write=False, api_get=api_get)

        self.assertEqual(result["rejected"], [])
        self.assertEqual([item["issueNumber"] for item in result["deferred"]], [82])
        self.assertIn("HTTP 503", result["deferred"][0]["reason"])

    def test_refresh_workflow_clears_stale_validation_failed_labels(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "refresh.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "Clear stale failure state from reopened or retryable fallback Issues",
            workflow,
        )
        self.assertIn("[.accepted[], .deferred[]]", workflow)
        self.assertIn("--label plugin-submission --limit 100", workflow)
        self.assertIn(
            "pending-merge plugin-submission queued-for-intake validation-failed",
            workflow,
        )


def urllib_unquote(value):
    # Keep the test module dependency-free while making the encoded query easy
    # to inspect.
    from urllib.parse import unquote_plus

    return unquote_plus(value)


if __name__ == "__main__":
    unittest.main()
