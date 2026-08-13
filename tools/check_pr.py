#!/usr/bin/env python3
"""Enforce scoped plugin PRs, immutable releases, and trusted review changes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PREFIXES = (
    ".github/",
    "reviews/",
    "schemas/",
    "templates/",
    "tests/",
    "tools/",
)
PROTECTED_FILES = {
    ".gitattributes",
    "repository.json",
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


def plugin_id_from_path(path: str) -> str | None:
    parts = normalize(path).split("/")
    if len(parts) >= 2 and parts[0] == "plugins":
        return parts[1]
    if len(parts) >= 3 and parts[0] == "reviews" and parts[-1].endswith(".json"):
        return parts[1]
    return None


def evaluate_policy(
    changes: list[tuple[str, list[str]]],
    actor: str,
    trusted_reviewers: set[str],
    is_yank_only: Callable[[str], bool],
    review_matches_actor: Callable[[str, str], bool],
) -> list[str]:
    errors: list[str] = []
    plugin_ids: set[str] = set()
    actor_is_trusted = actor.casefold() in trusted_reviewers

    for status, paths in changes:
        for path in paths:
            plugin_id = plugin_id_from_path(path)
            if plugin_id is not None:
                plugin_ids.add(plugin_id)

        if status.startswith("R"):
            errors.append(f"不能重命名已收录文件：{' -> '.join(paths)}")
            continue
        if status.startswith("D"):
            if not actor_is_trusted or not all(path.startswith("reviews/") for path in paths):
                errors.append(f"不能删除已收录文件：{paths[0]}")

        for path in paths:
            if is_release_path(path) and status != "A":
                if not actor_is_trusted:
                    errors.append(f"历史版本文件不可修改，只能新增版本：{path}")
                elif not status.startswith("D") and not is_yank_only(path):
                    errors.append(f"历史版本只能由可信审核者执行 yanked-only 修改：{path}")

            is_protected = path.startswith(PROTECTED_PREFIXES) or path in PROTECTED_FILES
            if is_protected and not actor_is_trusted:
                errors.append(f"非可信账号 {actor} 不能修改维护者保护文件：{path}")
            if (
                actor_is_trusted
                and path.startswith("reviews/")
                and path.endswith(".json")
                and not status.startswith("D")
                and not review_matches_actor(path, actor)
            ):
                errors.append(f"审核文件 reviewer 必须与 PR 作者 {actor} 一致：{path}")

    if len(plugin_ids) > 1:
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


def yank_only_change(base: str, path: str) -> bool:
    try:
        previous = json.loads(git("show", f"{base}:{path}"))
        current = json.loads((ROOT / path).read_text(encoding="utf-8"))
    except (subprocess.CalledProcessError, OSError, UnicodeError, json.JSONDecodeError):
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


def review_actor_matches(path: str, actor: str) -> bool:
    try:
        value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and isinstance(value.get("reviewer"), str)
        and value["reviewer"].casefold() == actor.casefold()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="trusted base commit SHA")
    parser.add_argument("--actor", required=True, help="pull request author login")
    args = parser.parse_args()
    base = args.base.strip()
    actor = args.actor.strip()
    if (
        not base
        or any(character not in "0123456789abcdefABCDEF" for character in base)
        or not actor
    ):
        print("无效的 PR base SHA 或 actor", file=sys.stderr)
        return 1

    try:
        trusted_reviewers = load_trusted_reviewers(base)
        changes = parse_name_status(
            git("diff", "--name-status", "-z", "--find-renames", f"{base}...HEAD")
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    errors = evaluate_policy(
        changes,
        actor,
        trusted_reviewers,
        lambda path: yank_only_change(base, path),
        review_actor_matches,
    )
    if errors:
        for error in errors:
            print(f"PR 规则失败：{error}", file=sys.stderr)
        return 1
    print("PR 变更范围、可信审核与历史版本保护规则通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
