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
GITHUB_APP_BOT_LOGIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\[bot\]$"
)
UNTRUSTED_ALLOWED_FILES = {
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "LICENSE",
}
REGISTRY_BOT_BRANCH_KINDS = ("intake", "sync", "review", "yank")
GENERATED_REGISTRY_FILES = {
    "plugin_details.json",
    "public/v1/index.json",
}


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
    return (
        len(parts) == 4
        and parts[0] == "plugins"
        and parts[2] == "releases"
        and parts[3].endswith(".json")
    )


def is_plugin_metadata_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return (
        len(parts) == 3
        and parts[0] == "plugins"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2] == "plugin.json"
    )


def is_review_path(path: str) -> bool:
    parts = normalize(path).split("/")
    return (
        len(parts) == 3
        and parts[0] == "reviews"
        and PLUGIN_ID.fullmatch(parts[1]) is not None
        and parts[2].endswith(".json")
        and len(parts[2]) > len(".json")
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
    trusted_reviewers: set[str],
) -> str | None:
    """Enforce fail-closed, mutation-kind-specific App PR path rules."""

    code = status[:1]
    if code in {"R", "C"}:
        return f"机器人 PR 不能重命名或复制文件：{path}"

    if kind in {"intake", "sync"}:
        if path == "plugins.json" or path in GENERATED_REGISTRY_FILES:
            return None if code in {"A", "M"} else f"{kind} PR 不能删除注册表视图：{path}"
        if is_plugin_metadata_path(path) or is_release_path(path):
            return None if code == "A" else f"{kind} PR 只能追加新的插件历史文件：{path}"
        return f"{kind} PR 不允许修改：{path}"

    if kind == "review":
        if path == "public/v1/index.json":
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
        if is_review_path(path) and code == "D":
            return None
        return f"yank PR 不允许修改：{path}"

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
    return None


def evaluate_policy(
    changes: list[tuple[str, list[str]]],
    actor: str,
    trusted_reviewers: set[str],
    is_yank_only: Callable[[str], bool],
    review_matches_actor: Callable[[str, str], bool],
    *,
    registry_bot_login: str | None = None,
    base_repository: str | None = None,
    head_repository: str | None = None,
    head_ref: str | None = None,
    event_sender: str | None = None,
    event_action: str | None = None,
) -> list[str]:
    errors: list[str] = []
    plugin_ids: set[str] = set()
    actor_is_trusted = actor.casefold() in trusted_reviewers
    actor_is_registry_bot = bool(
        registry_bot_login
        and actor.casefold() == registry_bot_login.casefold()
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
    )

    if actor_is_registry_bot and (
        bot_kind is None or not bot_is_same_repository or not bot_event_is_owned
    ):
        errors.append(
            "registry bot 只允许由该 App 自身更新同仓 "
            "registry-bot/intake|sync|review|yank 分支 PR"
        )

    for status, paths in changes:
        for path in paths:
            plugin_id = plugin_id_from_path(path)
            if plugin_id is not None:
                plugin_ids.add(plugin_id)

        if status.startswith(("R", "C")):
            errors.append(f"不能重命名已收录文件：{' -> '.join(paths)}")
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

    if len(plugin_ids) > 1 and not (
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


def load_repository_policy(base: str) -> tuple[set[str], str | None]:
    try:
        value = json.loads(git("show", f"{base}:repository.json"))
        reviewers = value["trustedReviewers"]
        registry_bot_login = value.get("registryBotLogin")
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("无法从可信 base 读取 trustedReviewers") from exc
    if not isinstance(reviewers, list) or any(not isinstance(item, str) for item in reviewers):
        raise ValueError("可信 base 的 trustedReviewers 无效")
    # The optional case is only a bootstrap path for the PR that first adds
    # the App configuration.  With no configured login, bot privileges remain
    # fail-closed and every App PR is treated like an ordinary contributor.
    if registry_bot_login is None:
        return {item.casefold() for item in reviewers}, None
    if (
        not isinstance(registry_bot_login, str)
        or GITHUB_APP_BOT_LOGIN.fullmatch(registry_bot_login) is None
    ):
        raise ValueError("可信 base 的 registryBotLogin 无效")
    return {item.casefold() for item in reviewers}, registry_bot_login


def pull_request_context_from_event(
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Load PR source metadata for callers such as validate.yml that omit flags."""

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None, None, None, None, None
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        pull_request = event["pull_request"]
        base_repository = event["repository"]["full_name"]
        head_repository = pull_request["head"]["repo"]["full_name"]
        head_ref = pull_request["head"]["ref"]
        event_sender = event["sender"]["login"]
        event_action = event["action"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return None, None, None, None, None
    values = (base_repository, head_repository, head_ref, event_sender, event_action)
    return tuple(value if isinstance(value, str) and value else None for value in values)


def yank_only_change(base: str, path: str, head: str = "HEAD") -> bool:
    try:
        previous = json.loads(git("show", f"{base}:{path}"))
        current = json.loads(git("show", f"{head}:{path}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False
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
        and current.get("yanked") is True
        and isinstance(current.get("yankReason"), str)
        and bool(current["yankReason"].strip())
    )


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
    parser.add_argument("--base-repository", help="canonical base owner/repository")
    parser.add_argument("--head-repository", help="canonical head owner/repository")
    parser.add_argument("--head-ref", help="pull request head branch")
    parser.add_argument("--event-sender", help="login that triggered the pull request event")
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
        trusted_reviewers, registry_bot_login = load_repository_policy(base)
        changes = parse_name_status(
            git("diff", "--name-status", "-z", "--find-renames", f"{base}...{head}")
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    (
        event_base_repository,
        event_head_repository,
        event_head_ref,
        event_sender,
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
        registry_bot_login=registry_bot_login,
        base_repository=args.base_repository or event_base_repository,
        head_repository=args.head_repository or event_head_repository,
        head_ref=args.head_ref or event_head_ref,
        event_sender=args.event_sender or event_sender,
        event_action=args.event_action or event_action,
    )
    if errors:
        for error in errors:
            print(f"PR 规则失败：{error}", file=sys.stderr)
        return 1
    print("PR 变更范围、可信审核与历史版本保护规则通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
