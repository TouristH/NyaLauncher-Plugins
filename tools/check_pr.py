#!/usr/bin/env python3
"""Protect the Issue-managed registry, immutable releases, and admin reviews."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+$")
GITHUB_LOGIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"
)
GITHUB_APP_BOT_LOGIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\[bot\]$"
)
GENERATION_DIRECTORY = re.compile(r"g(?:[2-9]|[1-9][0-9]+)")
UNTRUSTED_ALLOWED_FILES = {
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "LICENSE",
}
REGISTRY_BOT_BRANCH_KINDS = ("intake", "sync", "review", "yank", "lifecycle")
GENERATED_REGISTRY_FILES = {
    "plugin_details.json",
    "public/v1/index.json",
    "public/v2/index.json",
}
BOOTSTRAP_CONFIGURATION = "migrations/v2-bootstrap.json"
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
    )
    return result.stdout


def normalize(path: str) -> str:
    return path.replace("\\", "/")


def load_json_at(revision: str, path: str) -> object:
    return json.loads(git("show", f"{revision}:{path}"))


def identity_history_is_append_only(previous: object, current: object) -> bool:
    """Protect numeric identity and every already-published URL alias."""

    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    if any(previous.get(key) != current.get(key) for key in ("id", "lineageId")):
        return False
    old_bindings = previous.get("generations")
    new_bindings = current.get("generations")
    if (
        not isinstance(old_bindings, list)
        or not isinstance(new_bindings, list)
        or not old_bindings
        or len(new_bindings) < len(old_bindings)
    ):
        return False
    for index, old_binding in enumerate(old_bindings):
        new_binding = new_bindings[index]
        if not isinstance(old_binding, dict) or not isinstance(new_binding, dict):
            return False
        if any(
            old_binding.get(key) != new_binding.get(key)
            for key in ("generation", "repositoryId", "ownerId")
        ):
            return False
        old_history = old_binding.get(
            "repositoryUrlHistory", [old_binding.get("repositoryUrl")]
        )
        new_history = new_binding.get("repositoryUrlHistory")
        if (
            not isinstance(old_history, list)
            or not isinstance(new_history, list)
            or not old_history
            or new_history[: len(old_history)] != old_history
            or not new_history
            or new_binding.get("repositoryUrl") != new_history[-1]
        ):
            return False
        if len(new_history) == len(old_history) and (
            new_binding.get("repositoryUrl") != old_binding.get("repositoryUrl")
        ):
            return False
        # Once a generation is no longer current its alias ledger is frozen.
        if index < len(old_bindings) - 1 and new_history != old_history:
            return False
    for index, binding in enumerate(
        new_bindings[len(old_bindings) :], start=len(old_bindings) + 1
    ):
        if (
            not isinstance(binding, dict)
            or binding.get("generation") != index
            or not isinstance(binding.get("repositoryUrlHistory"), list)
            or binding["repositoryUrlHistory"] != [binding.get("repositoryUrl")]
        ):
            return False
    return True


def identity_update_is_safe(base: str, path: str, head: str = "HEAD") -> bool:
    try:
        return identity_history_is_append_only(
            load_json_at(base, path), load_json_at(head, path)
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def sync_identity_rename_is_safe(previous: object, current: object) -> bool:
    """Allow only same-generation rename or retired-lineage reactivation."""

    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    old_bindings = previous.get("generations")
    new_bindings = current.get("generations")
    if (
        not isinstance(old_bindings, list)
        or not isinstance(new_bindings, list)
        or not old_bindings
        or len(old_bindings) != len(new_bindings)
        or previous.get("generation") != current.get("generation")
        or previous.get("events", []) != current.get("events", [])
    ):
        return False
    old_status = previous.get("lifecycleStatus")
    new_status = current.get("lifecycleStatus")
    if (old_status, new_status) not in {
        ("active", "active"),
        ("retired", "active"),
        ("transferred", "active"),
    }:
        return False
    for key in set(previous) | set(current):
        if key in {"generations", "lifecycleStatus", "events"}:
            continue
        if previous.get(key) != current.get(key):
            return False
    if old_bindings[:-1] != new_bindings[:-1]:
        return False
    old_binding = old_bindings[-1]
    new_binding = new_bindings[-1]
    if not isinstance(old_binding, dict) or not isinstance(new_binding, dict):
        return False
    if any(
        old_binding.get(key) != new_binding.get(key)
        for key in ("generation", "repositoryId", "ownerId")
    ):
        return False
    expected_binding_status = "retired" if old_status == "retired" else "active"
    if (
        old_binding.get("status") != expected_binding_status
        or new_binding.get("status") != "active"
    ):
        return False
    old_history = old_binding.get("repositoryUrlHistory")
    new_history = new_binding.get("repositoryUrlHistory")
    if (
        not isinstance(old_history, list)
        or not isinstance(new_history, list)
        or not old_history
        or new_history[: len(old_history)] != old_history
        or len(new_history) not in {len(old_history), len(old_history) + 1}
        or old_binding.get("repositoryUrl") != old_history[-1]
        or new_binding.get("repositoryUrl") != new_history[-1]
    ):
        return False
    if len(new_history) == len(old_history) and new_binding.get(
        "repositoryUrl"
    ) != old_binding.get("repositoryUrl"):
        return False
    allowed_binding_keys = {
        "generation",
        "repositoryUrl",
        "repositoryUrlHistory",
        "repositoryId",
        "ownerId",
        "status",
    }
    return set(old_binding) == set(new_binding) == allowed_binding_keys


def lifecycle_identity_update_is_safe(previous: object, current: object) -> bool:
    """Allow exactly one audited retire or transfer state transition."""

    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    for key in set(previous) | set(current):
        if key in {"generation", "lifecycleStatus", "generations", "events"}:
            continue
        if previous.get(key) != current.get(key):
            return False
    old_events = previous.get("events", [])
    new_events = current.get("events", [])
    old_bindings = previous.get("generations")
    new_bindings = current.get("generations")
    if (
        not isinstance(old_events, list)
        or not isinstance(new_events, list)
        or new_events[: len(old_events)] != old_events
        or len(new_events) != len(old_events) + 1
        or not isinstance(old_bindings, list)
        or not isinstance(new_bindings, list)
        or not old_bindings
        or not isinstance(new_events[-1], dict)
    ):
        return False
    event = new_events[-1]
    old_generation = previous.get("generation")
    operation = event.get("operation")
    if (
        event.get("generation") != old_generation
        or event.get("sourceRepositoryId")
        != old_bindings[-1].get("repositoryId")
    ):
        return False
    if operation == "retire":
        if (
            previous.get("lifecycleStatus") not in {"active", "transferred"}
            or current.get("lifecycleStatus") != "retired"
            or current.get("generation") != old_generation
            or len(new_bindings) != len(old_bindings)
            or old_bindings[:-1] != new_bindings[:-1]
        ):
            return False
        expected = dict(old_bindings[-1])
        expected["status"] = "retired"
        return new_bindings[-1] == expected
    if operation == "transfer":
        next_generation = old_generation + 1 if type(old_generation) is int else None
        if (
            previous.get("lifecycleStatus") != "retired"
            or current.get("lifecycleStatus") != "transferred"
            or current.get("generation") != next_generation
            or len(new_bindings) != len(old_bindings) + 1
            or old_bindings[:-1] != new_bindings[:-2]
            or event.get("targetGeneration") != next_generation
            or event.get("targetRepositoryId")
            != new_bindings[-1].get("repositoryId")
        ):
            return False
        expected_old = dict(old_bindings[-1])
        expected_old["status"] = "transferred"
        target = new_bindings[-1]
        return bool(
            new_bindings[-2] == expected_old
            and isinstance(target, dict)
            and target.get("generation") == next_generation
            and target.get("status") == "active"
            and target.get("repositoryUrlHistory") == [target.get("repositoryUrl")]
            and target.get("repositoryId") != old_bindings[-1].get("repositoryId")
        )
    return False


def sync_identity_update_is_safe(
    base: str, path: str, head: str = "HEAD"
) -> bool:
    try:
        return sync_identity_rename_is_safe(
            load_json_at(base, path), load_json_at(head, path)
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def semver_precedence(value: str) -> tuple[int, int, int, int, tuple]:
    match = SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(value)
    prerelease = match.group(4)
    identifiers: tuple = ()
    if prerelease is not None:
        identifiers = tuple(
            (0, int(item)) if item.isdigit() else (1, item)
            for item in prerelease.split(".")
        )
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if prerelease is None else 0,
        identifiers,
    )


def sync_reactivation_has_new_release(
    base: str,
    head: str,
    identity_path: str,
    changes: list[tuple[str, list[str]]],
) -> bool:
    """Require a retired/transferred lineage to add a higher usable release."""

    try:
        previous = load_json_at(base, identity_path)
        current = load_json_at(head, identity_path)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    transition = (
        previous.get("lifecycleStatus"),
        current.get("lifecycleStatus"),
    )
    if transition == ("active", "active"):
        return True
    if transition not in {("retired", "active"), ("transferred", "active")}:
        return False
    generation = current.get("generation")
    plugin_id = plugin_id_from_path(identity_path)
    if type(generation) is not int or plugin_id is None:
        return False
    release_root = f"plugins/{plugin_id}"
    if generation > 1:
        release_root += f"/generations/g{generation}"
    release_root += "/releases"
    added_paths = {
        paths[0]
        for status, paths in changes
        if status == "A"
        and len(paths) == 1
        and paths[0].startswith(release_root + "/")
        and is_release_path(paths[0])
    }
    if not added_paths:
        return False
    try:
        base_paths = git(
            "ls-tree", "-r", "--name-only", base, "--", release_root
        ).splitlines()
        base_versions = [
            load_json_at(base, path).get("version")
            for path in base_paths
            if is_release_path(path)
        ]
        base_keys = [
            semver_precedence(version)
            for version in base_versions
            if isinstance(version, str)
        ]
        highest = max(base_keys) if base_keys else None
        for path in added_paths:
            release = load_json_at(head, path)
            if (
                not isinstance(release, dict)
                or release.get("generation") != generation
                or release.get("yanked") is not False
                or not isinstance(release.get("version"), str)
            ):
                continue
            if highest is None or semver_precedence(release["version"]) > highest:
                return True
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        ValueError,
        AttributeError,
    ):
        return False
    return False


def lifecycle_identity_path_is_safe(
    base: str, path: str, head: str = "HEAD"
) -> bool:
    try:
        return lifecycle_identity_update_is_safe(
            load_json_at(base, path), load_json_at(head, path)
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def plugin_metadata_rename_is_safe(
    base: str, path: str, head: str = "HEAD"
) -> bool:
    try:
        previous = load_json_at(base, path)
        current = load_json_at(head, path)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    old_url = previous.get("repositoryUrl")
    new_url = current.get("repositoryUrl")
    if not isinstance(old_url, str) or not isinstance(new_url, str) or old_url == new_url:
        return False
    previous = dict(previous)
    current = dict(current)
    previous.pop("repositoryUrl", None)
    current.pop("repositoryUrl", None)
    return previous == current


def review_revocation_change_is_safe(
    previous: object,
    current: object,
    trusted_reviewer_ids: dict[str, int],
) -> bool:
    """Allow exactly one current reviewer to append a revocation tombstone."""

    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    allowed_keys = {
        "$schema",
        "schemaVersion",
        "generation",
        "pluginId",
        "version",
        "sha256",
        "status",
        "stateBy",
        "stateById",
        "stateAt",
        "lastCommandAt",
        "lastCommentId",
        "notes",
    }
    immutable_keys = {
        "$schema",
        "schemaVersion",
        "generation",
        "pluginId",
        "version",
        "sha256",
    }
    actor = current.get("stateBy")
    previous_command_at = previous.get("lastCommandAt")
    command_at = current.get("lastCommandAt")
    if (
        not set(previous).issubset(allowed_keys)
        or not set(current).issubset(allowed_keys)
        or any(previous.get(key) != current.get(key) for key in immutable_keys)
        or previous.get("status") != "verified"
        or current.get("status") != "revoked"
        or not isinstance(actor, str)
        or current.get("stateById") != trusted_reviewer_ids.get(actor.casefold())
        or not isinstance(previous_command_at, str)
        or not isinstance(command_at, str)
        or current.get("stateAt") != command_at
        or type(previous.get("lastCommentId")) is not int
        or type(current.get("lastCommentId")) is not int
        or (command_at, current["lastCommentId"])
        <= (previous_command_at, previous["lastCommentId"])
        or not isinstance(current.get("notes"), str)
        or not current["notes"].strip()
    ):
        return False
    return True


def review_revocation_path_is_safe(
    base: str,
    path: str,
    trusted_reviewer_ids: dict[str, int],
    head: str = "HEAD",
) -> bool:
    try:
        return review_revocation_change_is_safe(
            load_json_at(base, path),
            load_json_at(head, path),
            trusted_reviewer_ids,
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def canonical_bootstrap_repository_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", value
    )
    if match is None:
        return False
    owner, repository = match.groups()
    return bool(
        GITHUB_LOGIN.fullmatch(owner) is not None
        and 1 <= len(repository) <= 100
        and repository not in {".", ".."}
        and not repository.endswith(".")
        and not repository.casefold().endswith(".git")
    )


def load_bootstrap_configuration(
    base: str, trusted_reviewers: set[str]
) -> tuple[dict[str, int], dict[str, int], dict[str, dict]] | None:
    try:
        value = load_json_at(base, BOOTSTRAP_CONFIGURATION)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "$schema",
        "schemaVersion",
        "targetRepositorySchemaVersion",
        "trustedReviewerIds",
        "targetTrustedReviewerIds",
        "publisherBindings",
    }:
        raise ValueError("base bootstrap trust anchor 结构无效")
    raw_ids = value.get("trustedReviewerIds")
    raw_target_ids = value.get("targetTrustedReviewerIds")
    raw_bindings = value.get("publisherBindings")
    if (
        value.get("schemaVersion") != 1
        or value.get("targetRepositorySchemaVersion") != 2
        or not isinstance(raw_ids, dict)
        or len({str(login).casefold() for login in raw_ids}) != len(raw_ids)
        or any(
            not isinstance(login, str)
            or GITHUB_LOGIN.fullmatch(login) is None
            or type(actor_id) is not int
            or not 1 <= actor_id <= 2**63 - 1
            for login, actor_id in raw_ids.items()
        )
        or not isinstance(raw_target_ids, dict)
        or len({str(login).casefold() for login in raw_target_ids})
        != len(raw_target_ids)
        or any(
            not isinstance(login, str)
            or GITHUB_LOGIN.fullmatch(login) is None
            or type(actor_id) is not int
            or not 1 <= actor_id <= 2**63 - 1
            for login, actor_id in raw_target_ids.items()
        )
        or not isinstance(raw_bindings, dict)
        or not raw_bindings
    ):
        raise ValueError("base bootstrap trust anchor 的 reviewer/publisher 映射无效")
    reviewer_ids = {
        login.casefold(): actor_id for login, actor_id in raw_ids.items()
    }
    target_reviewer_ids = {
        login.casefold(): actor_id for login, actor_id in raw_target_ids.items()
    }
    if (
        set(reviewer_ids) != trusted_reviewers
        or len(set(reviewer_ids.values())) != len(reviewer_ids)
        or len(set(target_reviewer_ids.values())) != len(target_reviewer_ids)
        or not set(reviewer_ids).issubset(target_reviewer_ids)
        or any(
            target_reviewer_ids.get(login) != actor_id
            for login, actor_id in reviewer_ids.items()
        )
    ):
        raise ValueError("base bootstrap trust anchor 的 reviewer 数字身份无效")
    bindings: dict[str, dict] = {}
    lineage_ids: set[str] = set()
    repository_ids: set[int] = set()
    for plugin_id, binding in raw_bindings.items():
        if (
            not isinstance(plugin_id, str)
            or len(plugin_id) > 128
            or PLUGIN_ID.fullmatch(plugin_id) is None
            or not isinstance(binding, dict)
            or set(binding)
            != {"lineageId", "repositoryUrl", "repositoryId", "ownerId"}
            or not isinstance(binding.get("lineageId"), str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                binding["lineageId"],
            )
            is None
            or not canonical_bootstrap_repository_url(binding.get("repositoryUrl"))
            or any(
                type(binding.get(key)) is not int
                or not 1 <= binding[key] <= 2**63 - 1
                for key in ("repositoryId", "ownerId")
            )
        ):
            raise ValueError(f"base bootstrap publisher binding 无效：{plugin_id}")
        if (
            binding["lineageId"] in lineage_ids
            or binding["repositoryId"] in repository_ids
        ):
            raise ValueError(
                "base bootstrap publisher lineageId/repositoryId 必须全局唯一"
            )
        lineage_ids.add(binding["lineageId"])
        repository_ids.add(binding["repositoryId"])
        bindings[plugin_id] = dict(binding)
    return (
        reviewer_ids,
        target_reviewer_ids,
        bindings,
    )


def release_bootstrap_change_is_safe(base: str, head: str, path: str) -> bool:
    try:
        previous = load_json_at(base, path)
        current = load_json_at(head, path)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    expected = dict(previous)
    expected["generation"] = 1
    return current == expected


def review_bootstrap_change_is_safe(
    base: str,
    head: str,
    path: str,
    bootstrap_reviewer_ids: dict[str, int],
) -> bool:
    try:
        previous = load_json_at(base, path)
        current = load_json_at(head, path)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    state_by = previous.get("stateBy")
    if not isinstance(state_by, str):
        return False
    expected = dict(previous)
    expected["generation"] = 1
    expected["stateById"] = bootstrap_reviewer_ids.get(state_by.casefold())
    return type(expected["stateById"]) is int and current == expected


def bootstrap_migration_policy(
    base: str,
    head: str,
    changes: list[tuple[str, list[str]]],
    trusted_reviewers: set[str],
) -> tuple[bool, Callable[[str, str], bool]]:
    """Recognize the single v1→v2 data migration and nothing broader."""

    try:
        base_repository = load_json_at(base, "repository.json")
        head_repository = load_json_at(head, "repository.json")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False, lambda _status, _path: False
    if (
        not isinstance(base_repository, dict)
        or not isinstance(head_repository, dict)
        or base_repository.get("schemaVersion") != 1
        or head_repository.get("schemaVersion") != 2
    ):
        return False, lambda _status, _path: False
    bootstrap = load_bootstrap_configuration(base, trusted_reviewers)
    if bootstrap is None:
        return False, lambda _status, _path: False
    bootstrap_reviewer_ids, target_reviewer_ids, publisher_bindings = bootstrap

    immutable_repository_fields = {
        "name",
        "sourceUrl",
        "launcherUrl",
        "indexPath",
        "registryBotLogin",
    }
    if any(
        base_repository.get(key) != head_repository.get(key)
        for key in immutable_repository_fields
    ) or (
        head_repository.get("indexV2Path") != "public/v2/index.json"
        or not isinstance(head_repository.get("v2MinimumLauncherVersion"), str)
        or {
            str(login).casefold(): actor_id
            for login, actor_id in head_repository.get("trustedReviewerIds", {}).items()
        }
        != target_reviewer_ids
        or {
            str(login).casefold()
            for login in head_repository.get("trustedReviewers", [])
        }
        != set(target_reviewer_ids)
    ):
        return False, lambda _status, _path: False

    try:
        base_plugin_paths = {
            path
            for path in git(
                "ls-tree", "-r", "--name-only", base, "--", "plugins"
            ).splitlines()
            if re.fullmatch(r"plugins/[^/]+/plugin\.json", path)
        }
        base_plugin_ids = {path.split("/")[1] for path in base_plugin_paths}
        base_listings = load_json_at(base, "plugins.json")
        head_listings = load_json_at(head, "plugins.json")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False, lambda _status, _path: False
    if (
        base_plugin_ids != set(publisher_bindings)
        or not isinstance(base_listings, list)
        or not isinstance(head_listings, list)
    ):
        return False, lambda _status, _path: False

    base_active = {
        item.get("id"): item for item in base_listings if isinstance(item, dict)
    }
    head_active = {
        item.get("id"): item for item in head_listings if isinstance(item, dict)
    }
    if set(base_active) != set(head_active) or None in base_active or None in head_active:
        return False, lambda _status, _path: False
    for plugin_id, old_listing in base_active.items():
        new_listing = head_active[plugin_id]
        binding = publisher_bindings.get(plugin_id)
        if binding is None or any(
            new_listing.get(key) != expected
            for key, expected in {
                "id": plugin_id,
                "lineageId": binding["lineageId"],
                "generation": 1,
                "repositoryUrl": binding["repositoryUrl"],
                "repositoryId": binding["repositoryId"],
                "ownerId": binding["ownerId"],
            }.items()
        ):
            return False, lambda _status, _path: False
        if any(
            old_listing.get(key) != binding[key]
            for key in ("repositoryUrl", "repositoryId", "ownerId")
        ):
            return False, lambda _status, _path: False

    for plugin_id, binding in publisher_bindings.items():
        try:
            plugin = load_json_at(base, f"plugins/{plugin_id}/plugin.json")
            identity = load_json_at(head, f"plugins/{plugin_id}/identity.json")
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return False, lambda _status, _path: False
        expected_status = "active" if plugin_id in head_active else "retired"
        if (
            not isinstance(plugin, dict)
            or plugin.get("repositoryUrl") != binding["repositoryUrl"]
            or not isinstance(identity, dict)
            or identity.get("id") != plugin_id
            or identity.get("lineageId") != binding["lineageId"]
            or identity.get("generation") != 1
            or identity.get("lifecycleStatus") != expected_status
            or identity.get("events", []) != []
            or identity.get("generations")
            != [
                {
                    "generation": 1,
                    "repositoryUrl": binding["repositoryUrl"],
                    "repositoryUrlHistory": [binding["repositoryUrl"]],
                    "repositoryId": binding["repositoryId"],
                    "ownerId": binding["ownerId"],
                    "status": expected_status,
                }
            ]
        ):
            return False, lambda _status, _path: False

    safe_changes: set[tuple[str, str]] = set()
    for status, paths in changes:
        if status.startswith(("R", "C")) or len(paths) != 1:
            return False, lambda _status, _path: False
        code = status[:1]
        path = paths[0]
        safe = False
        if path in {
            "repository.json",
            "plugins.json",
            "plugin_details.json",
            "public/v1/index.json",
        }:
            safe = code == "M"
        elif path == "public/v2/index.json":
            safe = code in {"A", "M"}
        elif is_identity_path(path):
            safe = code == "A"
        elif is_release_path(path):
            safe = code == "M" and release_bootstrap_change_is_safe(base, head, path)
        elif is_review_path(path):
            safe = code == "M" and review_bootstrap_change_is_safe(
                base, head, path, bootstrap_reviewer_ids
            )
        if not safe:
            return False, lambda _status, _path: False
        safe_changes.add((status, path))
    required = {
        ("M", "repository.json"),
        ("M", "plugins.json"),
        ("M", "plugin_details.json"),
    }
    if not required.issubset(safe_changes) or not any(
        path == "public/v2/index.json" for _, path in safe_changes
    ):
        return False, lambda _status, _path: False
    return True, lambda status, path: (status, path) in safe_changes


def bootstrap_anchor_creation_is_safe(
    base: str,
    head: str,
    changes: list[tuple[str, list[str]]],
    trusted_reviewers: set[str],
) -> bool:
    """Allow PR1 to add the base-owned anchor once, while schema stays v1."""

    try:
        load_json_at(base, BOOTSTRAP_CONFIGURATION)
        return False
    except subprocess.CalledProcessError:
        pass
    except json.JSONDecodeError:
        return False
    try:
        head_repository = load_json_at(head, "repository.json")
        bootstrap = load_bootstrap_configuration(head, trusted_reviewers)
        base_listings = load_json_at(base, "plugins.json")
        plugin_paths = {
            path
            for path in git(
                "ls-tree", "-r", "--name-only", base, "--", "plugins"
            ).splitlines()
            if re.fullmatch(r"plugins/[^/]+/plugin\.json", path)
        }
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError):
        return False
    if (
        not isinstance(head_repository, dict)
        or head_repository.get("schemaVersion") != 1
        or bootstrap is None
        or not isinstance(base_listings, list)
    ):
        return False
    _, _, bindings = bootstrap
    if {path.split("/")[1] for path in plugin_paths} != set(bindings):
        return False
    active = {
        item.get("id"): item for item in base_listings if isinstance(item, dict)
    }
    for plugin_id, binding in bindings.items():
        try:
            plugin = load_json_at(base, f"plugins/{plugin_id}/plugin.json")
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return False
        if not isinstance(plugin, dict) or plugin.get("repositoryUrl") != binding[
            "repositoryUrl"
        ]:
            return False
        if plugin_id in active and any(
            active[plugin_id].get(key) != binding[key]
            for key in ("repositoryUrl", "repositoryId", "ownerId")
        ):
            return False
    return any(
        status == "A" and paths == [BOOTSTRAP_CONFIGURATION]
        for status, paths in changes
    )


def parse_name_status(value: str) -> list[tuple[str, list[str]]]:
    if "\0" in value:
        fields = value.split("\0")
        if fields[-1] == "":
            fields.pop()
        result: list[tuple[str, list[str]]] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            path_count = 2 if status.startswith(("R", "C")) else 1
            if not status or index + path_count > len(fields):
                raise ValueError("git name-status -z 输出不完整")
            result.append(
                (status, [normalize(path) for path in fields[index:index + path_count]])
            )
            index += path_count
        return result

    result: list[tuple[str, list[str]]] = []
    for line in value.splitlines():
        columns = line.split("\t")
        if len(columns) >= 2:
            result.append((columns[0], [normalize(path) for path in columns[1:]]))
    return result


def is_release_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return bool(
        len(parts) == 4
        and parts[0] == "plugins"
        and parts[2] == "releases"
        and parts[3].endswith(".json")
        or len(parts) == 6
        and parts[0] == "plugins"
        and parts[2] == "generations"
        and GENERATION_DIRECTORY.fullmatch(parts[3]) is not None
        and parts[4] == "releases"
        and parts[5].endswith(".json")
    )


def is_plugin_metadata_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return bool(
        len(parts) == 3
        and parts[0] == "plugins"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2] == "plugin.json"
        or len(parts) == 5
        and parts[0] == "plugins"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2] == "generations"
        and GENERATION_DIRECTORY.fullmatch(parts[3]) is not None
        and parts[4] == "plugin.json"
    )


def is_identity_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return bool(
        len(parts) == 3
        and parts[0] == "plugins"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2] == "identity.json"
    )


def is_tombstone_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return bool(
        len(parts) == 3
        and parts[0] == "tombstones"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2].endswith(".json")
    )


def is_review_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return bool(
        (len(parts) == 3
        and parts[0] == "reviews"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2].endswith(".json")
        and len(parts[2]) > len(".json"))
        or (len(parts) == 4
        and parts[0] == "reviews"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and GENERATION_DIRECTORY.fullmatch(parts[2]) is not None
        and parts[3].endswith(".json"))
    )


def registry_bot_branch_kind(head_ref: str | None) -> str | None:
    """Return the narrowly recognized mutation kind for an App-owned branch."""

    if not isinstance(head_ref, str):
        return None
    for kind in REGISTRY_BOT_BRANCH_KINDS:
        prefix = f"registry-bot/{kind}"
        if head_ref == prefix:
            return kind
        if head_ref.startswith(prefix + "/"):
            suffix = head_ref[len(prefix) + 1:]
            if (
                suffix
                and not suffix.startswith("/")
                and not suffix.endswith("/")
                and "//" not in suffix
                and ".." not in suffix
                and re.fullmatch(r"[A-Za-z0-9._/-]+", suffix) is not None
            ):
                return kind
    return None


def registry_bot_change_error(
    kind: str,
    status: str,
    path: str,
    is_yank_only: Callable[[str], bool],
    review_matches_actor: Callable[[str, str], bool],
    sync_identity_update_is_safe: Callable[[str], bool],
    sync_reactivation_is_safe: Callable[[str], bool],
    lifecycle_identity_update_is_safe: Callable[[str], bool],
    plugin_metadata_rename_is_safe: Callable[[str], bool],
    review_revocation_is_safe: Callable[[str], bool],
    trusted_reviewers: set[str],
) -> str | None:
    """Enforce fail-closed, mutation-kind-specific App PR path rules."""

    code = status[:1]
    if code in {"R", "C"}:
        return f"机器人 PR 不能重命名或复制文件：{path}"

    if kind in {"intake", "sync"}:
        if path == "plugins.json" or path in GENERATED_REGISTRY_FILES:
            return None if code in {"A", "M"} else f"{kind} PR 不能删除注册表视图：{path}"
        if is_plugin_metadata_path(path):
            if code == "A":
                return None
            if kind == "sync" and code == "M" and plugin_metadata_rename_is_safe(path):
                return None
            return f"{kind} PR 只能新增元数据，或执行受约束的同代仓库 URL 重命名：{path}"
        if is_release_path(path):
            return None if code == "A" else f"{kind} PR 只能追加新的插件历史文件：{path}"
        if is_identity_path(path):
            if kind == "intake" and code == "A":
                return None
            if kind == "sync" and code == "A":
                return None
            if (
                kind == "sync"
                and code == "M"
                and sync_identity_update_is_safe(path)
                and sync_reactivation_is_safe(path)
            ):
                return None
            return f"{kind} PR 身份账本修改无效：{path}"
        return f"{kind} PR 不允许修改：{path}"

    if kind == "review":
        if path in GENERATED_REGISTRY_FILES:
            return None if code in {"A", "M"} else f"review PR 不能删除公开索引：{path}"
        if is_review_path(path) and code in {"A", "M"}:
            if any(review_matches_actor(path, reviewer) for reviewer in trusted_reviewers):
                return None
            return f"机器人审核记录的 stateBy 必须仍在 trustedReviewers 中：{path}"
        if is_review_path(path) and code == "D":
            return f"review PR 不能删除审核顺序墓碑：{path}"
        return f"review PR 只能更新审核记录并重建公开索引：{path}"

    if kind == "yank":
        if path == "plugins.json" or path in GENERATED_REGISTRY_FILES:
            return None if code in {"A", "M"} else f"yank PR 不能删除注册表视图：{path}"
        if is_release_path(path) and code == "M":
            return None if is_yank_only(path) else f"yank PR 只能执行 yanked-only 修改：{path}"
        if is_review_path(path) and code == "M":
            if review_revocation_is_safe(path):
                return None
            return f"yank 审核记录只能由当前可信审核者执行 verified→revoked 追加：{path}"
        return f"yank PR 不允许修改：{path}"

    if kind == "lifecycle":
        if path == "plugins.json" or path in GENERATED_REGISTRY_FILES:
            return None if code in {"A", "M"} else f"lifecycle PR 不能删除注册表视图：{path}"
        if is_identity_path(path):
            if code == "M" and lifecycle_identity_update_is_safe(path):
                return None
            return f"lifecycle PR 不得改写数字身份或旧 URL history：{path}"
        if is_release_path(path):
            if code == "M" and is_yank_only(path):
                return None
            return f"lifecycle PR 只能撤回现有版本，不得增删或改写发布事实：{path}"
        if is_review_path(path):
            if code == "M" and review_revocation_is_safe(path):
                return None
            return f"lifecycle PR 审核记录只能执行 verified→revoked 追加：{path}"
        if is_plugin_metadata_path(path):
            parts = normalize(path).split("/")
            if code == "A" and len(parts) == 5:
                return None
            return f"lifecycle PR 只能为新代际追加 plugin.json：{path}"
        if is_tombstone_path(path):
            return None if code == "A" else f"purge 墓碑只能追加：{path}"
        return f"lifecycle PR 不允许修改：{path}"

    return f"未知的机器人 PR 类型：{kind}"


def plugin_id_from_path(path: str) -> str | None:
    parts = normalize(path).split("/")
    if len(parts) >= 2 and parts[0] == "plugins" and PLUGIN_ID.fullmatch(parts[1]):
        return parts[1]
    if (
        len(parts) >= 3
        and parts[0] == "reviews"
        and PLUGIN_ID.fullmatch(parts[1])
        and parts[-1].endswith(".json")
    ):
        return parts[1]
    if len(parts) == 3 and parts[0] == "tombstones" and PLUGIN_ID.fullmatch(parts[1]):
        return parts[1]
    return None


def evaluate_policy(
    changes: list[tuple[str, list[str]]],
    actor: str,
    trusted_reviewers: set[str],
    is_yank_only: Callable[[str], bool],
    review_matches_actor: Callable[[str, str], bool],
    *,
    sync_identity_update_is_safe: Callable[[str], bool] = lambda _path: False,
    sync_reactivation_is_safe: Callable[[str], bool] = lambda _path: False,
    lifecycle_identity_update_is_safe: Callable[[str], bool] = lambda _path: False,
    plugin_metadata_rename_is_safe: Callable[[str], bool] = lambda _path: False,
    review_revocation_is_safe: Callable[[str], bool] = lambda _path: False,
    bootstrap_migration: bool = False,
    bootstrap_path_is_safe: Callable[[str, str], bool] = lambda _status, _path: False,
    bootstrap_anchor_creation: bool = False,
    schema1_anchor_frozen: bool = False,
    trusted_reviewer_ids: dict[str, int] | None = None,
    actor_id: int | None = None,
    actor_type: str | None = None,
    registry_bot_login: str | None = None,
    base_repository: str | None = None,
    head_repository: str | None = None,
    head_ref: str | None = None,
    event_sender: str | None = None,
    event_sender_id: int | None = None,
    event_sender_type: str | None = None,
    event_action: str | None = None,
    base_schema_version: int | None = None,
    head_schema_version: int | None = None,
) -> list[str]:
    errors: list[str] = []
    plugin_ids: set[str] = set()
    actor_key = actor.casefold()
    configured_actor_id = (
        trusted_reviewer_ids.get(actor_key)
        if trusted_reviewer_ids is not None
        else None
    )
    actor_is_trusted = (
        configured_actor_id == actor_id and actor_type == "User"
        if trusted_reviewer_ids is not None
        else actor_key in trusted_reviewers
    )
    actor_is_registry_bot = bool(
        registry_bot_login
        and actor_key == registry_bot_login.casefold()
        and type(actor_id) is int
        and actor_id > 0
        and actor_type == "Bot"
    )
    bot_kind = registry_bot_branch_kind(head_ref) if actor_is_registry_bot else None
    bot_is_same_repository = bool(
        base_repository
        and head_repository
        and base_repository.casefold() == head_repository.casefold()
    )
    # Every event that re-evaluates an App-owned PR must still have been
    # emitted by the App. Checking only ``synchronize`` lets a collaborator
    # push an unauthorized head commit, then close/reopen or edit the PR to
    # obtain fresh successful checks for that same commit.
    bot_event_is_owned = bool(
        event_sender
        and registry_bot_login
        and event_sender.casefold() == registry_bot_login.casefold()
        and event_sender_id == actor_id
        and event_sender_type == "Bot"
    )
    human_event_is_owned = bool(
        event_sender
        and event_sender.casefold() == actor_key
        and event_sender_id == actor_id
        and event_sender_type == "User"
    )

    if base_schema_version == 2 and head_schema_version != 2:
        errors.append(
            "repository schemaVersion 已进入 v2，任何 actor 或机器人均不得降级或移除 v2"
        )

    if schema1_anchor_frozen and actor_is_registry_bot:
        errors.append(
            "v2 bootstrap trust anchor 已固定；schema v1 迁移窗口内禁止所有 registry bot 数据 PR"
        )

    if trusted_reviewer_ids is not None and actor_key in trusted_reviewer_ids and not actor_is_trusted:
        errors.append(
            f"可信账号 {actor} 的 PR numeric user ID/type 与 base trustedReviewerIds 不一致"
        )

    if actor_is_trusted and not human_event_is_owned:
        errors.append(
            "可信 human PR 的触发 sender 必须与 PR author 的 login、numeric ID 和 User type 完全一致"
        )

    bootstrap_is_authorized = bool(
        bootstrap_migration
        and actor_is_trusted
        and human_event_is_owned
        and base_repository
        and head_repository
        and base_repository.casefold() == head_repository.casefold()
    )

    if actor_is_registry_bot and (
        bot_kind is None or not bot_is_same_repository or not bot_event_is_owned
    ):
        errors.append(
            "registry bot 只允许由该 App 自身更新同仓 "
            "registry-bot/intake|sync|review|yank|lifecycle 分支 PR"
        )

    for status, paths in changes:
        for path in paths:
            plugin_id = plugin_id_from_path(path)
            if plugin_id is not None:
                plugin_ids.add(plugin_id)

        if status.startswith(("R", "C")):
            errors.append(f"不能重命名已收录文件：{' -> '.join(paths)}")
            continue

        if BOOTSTRAP_CONFIGURATION in paths and not (
            bootstrap_anchor_creation
            and status == "A"
            and actor_is_trusted
            and human_event_is_owned
            and base_repository
            and head_repository
            and base_repository.casefold() == head_repository.casefold()
        ):
            errors.append(
                f"一次性 v2 migration trust anchor 已由 base 固定，任何 PR 均不得修改或删除：{BOOTSTRAP_CONFIGURATION}"
            )
            continue
        if BOOTSTRAP_CONFIGURATION in paths:
            continue

        if (
            schema1_anchor_frozen
            and "repository.json" in paths
            and not bootstrap_is_authorized
        ):
            errors.append(
                "v2 bootstrap trust anchor 已固定；完成唯一 v1→v2 migration 前不得修改 repository.json"
            )
            continue

        if bootstrap_is_authorized:
            for path in paths:
                if not bootstrap_path_is_safe(status, path):
                    errors.append(f"v1→v2 bootstrap migration 包含越界或非确定性修改：{path}")
            continue

        if actor_is_registry_bot:
            if bot_kind is None or not bot_is_same_repository or not bot_event_is_owned:
                continue
            for path in paths:
                error = registry_bot_change_error(
                    bot_kind,
                    status,
                    path,
                    is_yank_only,
                    review_matches_actor,
                    sync_identity_update_is_safe,
                    sync_reactivation_is_safe,
                    lifecycle_identity_update_is_safe,
                    plugin_metadata_rename_is_safe,
                    review_revocation_is_safe,
                    trusted_reviewers,
                )
                if error is not None:
                    errors.append(error)
            continue

        if status.startswith("D"):
            if not actor_is_trusted:
                errors.append(f"不能删除已收录文件：{paths[0]}")
            elif any(
                path.startswith("plugins/") and path != "plugins/README.md"
                for path in paths
            ):
                errors.append(f"不能删除插件历史目录中的文件：{paths[0]}")

        for path in paths:
            if is_release_path(path):
                if status == "A":
                    errors.append(
                        f"新版本只能由 Add Plugin Issue 或受信同步工作流写入，PR 不得新增：{path}"
                    )
                elif not actor_is_trusted:
                    errors.append(f"历史版本文件不可修改：{path}")
                elif not status.startswith("D") and not is_yank_only(path):
                    errors.append(f"历史版本只能由可信审核者执行 yanked-only 修改：{path}")

            if actor_is_trusted and (
                is_identity_path(path)
                or "/generations/g" in normalize(path)
                or is_tombstone_path(path)
            ):
                errors.append(
                    f"身份账本、代际目录和 purge 墓碑只能由受保护 lifecycle 工作流修改：{path}"
                )

            if not actor_is_trusted and path not in UNTRUSTED_ALLOWED_FILES:
                errors.append(
                    f"非可信账号 {actor} 的 PR 只能修改明确允许的根目录文档，不能修改：{path}；"
                    "插件发布请使用 Add Plugin Issue"
                )
            if (
                actor_is_trusted
                and path.startswith("reviews/")
                and path.endswith(".json")
                and status.startswith("D")
            ):
                errors.append(
                    f"审核撤销必须保留 status=revoked 的顺序墓碑，不能删除：{path}"
                )
            elif (
                actor_is_trusted
                and path.startswith("reviews/")
                and path.endswith(".json")
                and not review_matches_actor(path, actor)
            ):
                errors.append(f"审核文件 stateBy 必须与 PR 作者 {actor} 一致：{path}")

    if len(plugin_ids) > 1 and not bootstrap_is_authorized and not (
        actor_is_registry_bot and bot_kind in {"intake", "sync"} and bot_is_same_repository
    ):
        errors.append(f"一次 PR 只能涉及一个插件，当前涉及：{', '.join(sorted(plugin_ids))}")
    return list(dict.fromkeys(errors))


def load_trusted_reviewers(base: str) -> set[str]:
    try:
        value = json.loads(git("show", f"{base}:repository.json"))
        reviewers = value["trustedReviewers"]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("无法从可信 base 读取 trustedReviewers") from exc
    if not isinstance(reviewers, list) or any(not isinstance(item, str) for item in reviewers):
        raise ValueError("可信 base 的 trustedReviewers 无效")
    return {item.casefold() for item in reviewers}


def load_repository_schema_version(revision: str) -> int:
    try:
        value = load_json_at(revision, "repository.json")
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法从 {revision} 读取 repository schemaVersion") from exc
    version = value.get("schemaVersion", 1) if isinstance(value, dict) else None
    if version not in {1, 2}:
        raise ValueError(f"{revision} repository schemaVersion 必须是 1 或 2")
    return version


def load_repository_policy(
    base: str,
) -> tuple[set[str], dict[str, int] | None, str | None]:
    try:
        value = json.loads(git("show", f"{base}:repository.json"))
        reviewers = value["trustedReviewers"]
        schema_version = value.get("schemaVersion", 1)
        raw_reviewer_ids = value.get("trustedReviewerIds")
        registry_bot_login = value.get("registryBotLogin")
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("无法从可信 base 读取 trustedReviewers") from exc
    if not isinstance(reviewers, list) or any(not isinstance(item, str) for item in reviewers):
        raise ValueError("可信 base 的 trustedReviewers 无效")
    reviewer_keys = {item.casefold() for item in reviewers}
    reviewer_ids: dict[str, int] | None = None
    if schema_version == 2:
        if (
            not isinstance(raw_reviewer_ids, dict)
            or len(raw_reviewer_ids) != len(reviewers)
            or {str(item).casefold() for item in raw_reviewer_ids} != reviewer_keys
            or any(
                not isinstance(login, str)
                or type(reviewer_id) is not int
                or not 1 <= reviewer_id <= 2**63 - 1
                for login, reviewer_id in raw_reviewer_ids.items()
            )
        ):
            raise ValueError(
                "可信 base 的 trustedReviewerIds 必须与 trustedReviewers 数字身份一一对应"
            )
        reviewer_ids = {
            login.casefold(): reviewer_id
            for login, reviewer_id in raw_reviewer_ids.items()
        }
    elif (bootstrap := load_bootstrap_configuration(base, reviewer_keys)) is not None:
        reviewer_ids = bootstrap[0]
    # The optional bot case is only a bootstrap path for the PR that first adds
    # the App configuration.  With no configured login, bot privileges remain
    # fail-closed and every App PR is treated like an ordinary contributor.
    if registry_bot_login is None:
        return reviewer_keys, reviewer_ids, None
    if (
        not isinstance(registry_bot_login, str)
        or GITHUB_APP_BOT_LOGIN.fullmatch(registry_bot_login) is None
    ):
        raise ValueError("可信 base 的 registryBotLogin 无效")
    return reviewer_keys, reviewer_ids, registry_bot_login


def pull_request_context_from_event(
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    int | None,
    str | None,
]:
    """Load PR source metadata for callers such as validate.yml that omit flags."""

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return (None,) * 9
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        pull_request = event["pull_request"]
        base_repository = event["repository"]["full_name"]
        head_repository = pull_request["head"]["repo"]["full_name"]
        head_ref = pull_request["head"]["ref"]
        event_sender = event["sender"]["login"]
        event_sender_id = event["sender"]["id"]
        event_sender_type = event["sender"]["type"]
        actor_id = pull_request["user"]["id"]
        actor_type = pull_request["user"]["type"]
        event_action = event["action"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return (None,) * 9
    return (
        base_repository if isinstance(base_repository, str) else None,
        head_repository if isinstance(head_repository, str) else None,
        head_ref if isinstance(head_ref, str) else None,
        event_sender if isinstance(event_sender, str) else None,
        event_sender_id if type(event_sender_id) is int else None,
        event_sender_type if isinstance(event_sender_type, str) else None,
        actor_type if isinstance(actor_type, str) else None,
        actor_id if type(actor_id) is int else None,
        event_action if isinstance(event_action, str) else None,
    )


def yank_transition_is_safe(previous: object, current: object) -> bool:
    """Allow only the first false→true yank; the tombstone is immutable."""

    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    previous_body = dict(previous)
    current_body = dict(current)
    previous_body.pop("yanked", None)
    previous_body.pop("yankReason", None)
    current_body.pop("yanked", None)
    current_body.pop("yankReason", None)
    return (
        previous_body == current_body
        and previous.get("yanked") is False
        and "yankReason" not in previous
        and current.get("yanked") is True
        and isinstance(current.get("yankReason"), str)
        and bool(current["yankReason"].strip())
    )


def yank_only_change(base: str, path: str, head: str = "HEAD") -> bool:
    try:
        previous = json.loads(git("show", f"{base}:{path}"))
        current = json.loads(git("show", f"{head}:{path}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    return yank_transition_is_safe(previous, current)


def review_actor_matches(path: str, actor: str, head: str = "HEAD") -> bool:
    try:
        value = json.loads(git("show", f"{head}:{path}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and isinstance(value.get("stateBy"), str)
        and value["stateBy"].casefold() == actor.casefold()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="trusted base commit SHA")
    parser.add_argument("--head", default="HEAD", help="candidate commit or fetched ref")
    parser.add_argument("--actor", required=True, help="pull request author login")
    parser.add_argument("--actor-id", type=int, help="pull request author numeric user ID")
    parser.add_argument("--actor-type", help="pull request author GitHub account type")
    parser.add_argument("--base-repository", help="canonical base owner/repository")
    parser.add_argument("--head-repository", help="canonical head owner/repository")
    parser.add_argument("--head-ref", help="pull request head branch")
    parser.add_argument("--event-sender", help="login that triggered the pull request event")
    parser.add_argument("--event-sender-id", type=int, help="numeric ID that triggered the event")
    parser.add_argument("--event-sender-type", help="GitHub account type that triggered the event")
    parser.add_argument("--event-action", help="pull request webhook action")
    args = parser.parse_args()
    base = args.base.strip()
    head = args.head.strip()
    actor = args.actor.strip()
    if (
        not base
        or any(character not in "0123456789abcdefABCDEF" for character in base)
        or not head
        or head.startswith("-")
        or ".." in head
        or re.fullmatch(r"[A-Za-z0-9_./-]+", head) is None
        or not actor
    ):
        print("无效的 PR base SHA、head ref 或 actor", file=sys.stderr)
        return 1

    try:
        trusted_reviewers, trusted_reviewer_ids, registry_bot_login = load_repository_policy(base)
        changes = parse_name_status(
            git("diff", "--name-status", "-z", "--find-renames", f"{base}...{head}")
        )
        bootstrap_migration, bootstrap_path_is_safe = bootstrap_migration_policy(
            base, head, changes, trusted_reviewers
        )
        bootstrap_anchor_creation = bootstrap_anchor_creation_is_safe(
            base, head, changes, trusted_reviewers
        )
        base_schema_version = load_repository_schema_version(base)
        head_schema_version = load_repository_schema_version(head)
        schema1_anchor_frozen = bool(
            base_schema_version == 1
            and load_bootstrap_configuration(base, trusted_reviewers) is not None
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    (
        event_base_repository,
        event_head_repository,
        event_head_ref,
        event_sender,
        event_sender_id,
        event_sender_type,
        event_actor_type,
        event_actor_id,
        event_action,
    ) = (
        pull_request_context_from_event()
    )
    errors = evaluate_policy(
        changes,
        actor,
        trusted_reviewers,
        lambda path: yank_only_change(base, path, head),
        lambda path, candidate_actor: review_actor_matches(path, candidate_actor, head),
        sync_identity_update_is_safe=lambda path: sync_identity_update_is_safe(
            base, path, head
        ),
        sync_reactivation_is_safe=lambda path: sync_reactivation_has_new_release(
            base, head, path, changes
        ),
        lifecycle_identity_update_is_safe=lambda path: lifecycle_identity_path_is_safe(
            base, path, head
        ),
        plugin_metadata_rename_is_safe=lambda path: plugin_metadata_rename_is_safe(
            base, path, head
        ),
        review_revocation_is_safe=lambda path: review_revocation_path_is_safe(
            base, path, trusted_reviewer_ids or {}, head
        ),
        bootstrap_migration=bootstrap_migration,
        bootstrap_path_is_safe=bootstrap_path_is_safe,
        bootstrap_anchor_creation=bootstrap_anchor_creation,
        schema1_anchor_frozen=schema1_anchor_frozen,
        trusted_reviewer_ids=trusted_reviewer_ids,
        actor_id=args.actor_id if args.actor_id is not None else event_actor_id,
        actor_type=args.actor_type or event_actor_type,
        registry_bot_login=registry_bot_login,
        base_repository=args.base_repository or event_base_repository,
        head_repository=args.head_repository or event_head_repository,
        head_ref=args.head_ref or event_head_ref,
        event_sender=args.event_sender or event_sender,
        event_sender_id=(
            args.event_sender_id
            if args.event_sender_id is not None
            else event_sender_id
        ),
        event_sender_type=args.event_sender_type or event_sender_type,
        event_action=args.event_action or event_action,
        base_schema_version=base_schema_version,
        head_schema_version=head_schema_version,
    )
    if errors:
        for error in errors:
            print(f"PR 规则失败：{error}", file=sys.stderr)
        return 1
    print("PR 变更范围、可信审核与历史版本保护规则通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
