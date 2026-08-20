#!/usr/bin/env python3
"""Validate and apply Issue-based plugin listing and yank requests."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate as validator  # noqa: E402


SECTION = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
APPROVAL_COMMENT_MARKER = "<!-- nyalauncher-plugin-registry:approved -->"
APPROVAL_COMMENT_HEADING = "## 🎉 已批准并写入插件中心"
REJECTION_COMMENT_MARKER = "<!-- nyalauncher-plugin-registry:rejected -->"
REJECTION_COMMENT_HEADING = "## 🚫 维护者已拒绝"


class SubmissionFailure(Exception):
    pass


class PermissionFailure(SubmissionFailure):
    """The caller is not authorized; never mutate Issue labels or comments."""


def load_event(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SubmissionFailure(f"无法读取 GitHub 事件：{exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("issue"), dict):
        raise SubmissionFailure("事件中没有 Issue")
    return value


def parse_sections(body: str) -> dict[str, str]:
    matches = list(SECTION.finditer(body or ""))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end():end].strip()
        if value in {"_No response_", "No response"}:
            value = ""
        result[match.group(1).strip()] = value
    return result


def field(sections: dict[str, str], *labels: str) -> str:
    for label in labels:
        if label in sections and sections[label].strip():
            return sections[label].strip()
    raise SubmissionFailure(f"Issue 缺少字段：{labels[0]}")


def request_kind(event: dict) -> str:
    title = str(event["issue"].get("title") or "")
    if title.startswith("[Plugin]"):
        return "add"
    if title.startswith("[Yank]"):
        return "yank"
    raise SubmissionFailure("该 Issue 不是受支持的插件收录或撤回表单")


def request_plugin_id(event: dict) -> str:
    sections = parse_sections(str(event["issue"].get("body") or ""))
    plugin_id = field(sections, "插件 ID / Plugin ID")
    if len(plugin_id) > 128 or validator.PLUGIN_ID.fullmatch(plugin_id) is None:
        raise SubmissionFailure("插件 ID 必须是最长 128 字符的小写反向域名")
    return plugin_id


def write_request_metadata(event: dict, output_path: Path) -> None:
    kind = request_kind(event)
    plugin_id = request_plugin_id(event)
    needs_refresh = kind == "add" and not request_is_centrally_applied(event)
    with output_path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"request_kind={kind}\n")
        output.write(f"plugin_id={plugin_id}\n")
        output.write(f"needs_refresh={'true' if needs_refresh else 'false'}\n")


def trusted_reviewers() -> set[str]:
    path = ROOT / "repository.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        reviewers = value["trustedReviewers"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SubmissionFailure("无法读取 trustedReviewers") from exc
    if not isinstance(reviewers, list) or any(not isinstance(item, str) for item in reviewers):
        raise SubmissionFailure("trustedReviewers 配置无效")
    return {item.casefold() for item in reviewers}


def event_actor(event: dict) -> str:
    source = event.get("comment") or event.get("sender") or {}
    actor = source.get("user", source).get("login") if isinstance(source, dict) else None
    if not isinstance(actor, str) or not actor.strip():
        raise SubmissionFailure("事件中没有有效操作账号")
    return actor.strip()


def check_trusted_command_permission(event: dict, command_name: str) -> None:
    if "comment" not in event:
        raise PermissionFailure(f"该操作只能由可信维护者输入 /{command_name} 触发")
    command = str(event["comment"].get("body") or "").strip()
    command_matches = (
        re.fullmatch(r"/reject(?:\s+[\s\S]+)?", command) is not None
        if command_name == "reject"
        else command == f"/{command_name}"
    )
    if not command_matches:
        raise PermissionFailure(f"只接受精确的 /{command_name} 命令")
    try:
        actor = event_actor(event)
        reviewers = trusted_reviewers()
    except SubmissionFailure as exc:
        raise PermissionFailure(str(exc)) from exc
    if actor.casefold() not in reviewers:
        raise PermissionFailure(f"只有可信维护者可以执行 /{command_name}")


def check_validation_permission(event: dict) -> None:
    check_trusted_command_permission(event, "validate")


def parse_add_listing(event: dict) -> dict:
    sections = parse_sections(str(event["issue"].get("body") or ""))
    plugin_id = request_plugin_id(event)
    raw_repository_url = field(sections, "仓库地址 / Repository URL")
    try:
        _, _, repository_url = validator.github_repository_parts(
            "Issue repository URL", "repositoryUrl", raw_repository_url
        )
    except validator.ValidationFailure as exc:
        raise SubmissionFailure(str(exc)) from exc
    return {"id": plugin_id, "repositoryUrl": repository_url}


def add_summary(plugin: dict, release: dict, *, already_applied: bool = False) -> str:
    suffix = (
        "\n\n该请求已由中心目录完整体现；本次批准作为幂等重试安全完成。"
        if already_applied
        else ""
    )
    return (
        f"| 字段 | 值 |\n| --- | --- |\n"
        f"| 插件 | `{plugin['id']}` — {plugin['name']} |\n"
        f"| 版本 | `{release['version']}` ({release['channel']}) |\n"
        f"| 仓库 | {plugin['repositoryUrl']} |\n"
        f"| ZIP | `{release['download']['size']}` bytes |\n"
        f"| SHA-256 | `{release['download']['sha256']}` |\n\n"
        "固定 Release ZIP、包内清单和入口程序集均已通过自动验证。"
        f"{suffix}"
    )


def already_applied_add_summary(event: dict) -> str | None:
    """Return a summary only when this exact Add request is already central."""

    listing = parse_add_listing(event)
    active = next(
        (
            item
            for item in validator.load_plugin_list()
            if item["id"].casefold() == listing["id"].casefold()
        ),
        None,
    )
    if active is None:
        return None
    if not validator.same_github_repository(
        active["repositoryUrl"], listing["repositoryUrl"]
    ):
        raise SubmissionFailure(
            f"插件 ID 已由其他仓库收录：{active['repositoryUrl']}"
        )

    central = next(
        (
            item
            for item in validator.load_catalog()
            if item["id"].casefold() == listing["id"].casefold()
        ),
        None,
    )
    if central is None or not validator.same_github_repository(
        central["repositoryUrl"], listing["repositoryUrl"]
    ):
        raise SubmissionFailure(
            f"{listing['id']}: active 指针已存在，但中心历史未完整体现同一收录请求"
        )
    available = [release for release in central["releases"] if not release["yanked"]]
    if not available:
        raise SubmissionFailure(
            f"{listing['id']}: active 指针已存在，但中心没有未撤回版本"
        )
    current = max(available, key=lambda item: validator.semver_key(item["version"]))
    return add_summary(central, current, already_applied=True)


def prepare_add_listing(event: dict) -> dict:
    """Validate central ownership before the targeted refresh does one hard fetch."""

    listing = parse_add_listing(event)
    listings = validator.load_plugin_list()
    if any(item["id"].casefold() == listing["id"].casefold() for item in listings):
        raise SubmissionFailure(f"插件 ID 已收录：{listing['id']}")
    if any(
        validator.same_github_repository(item["repositoryUrl"], listing["repositoryUrl"])
        for item in listings
    ):
        raise SubmissionFailure(f"仓库地址已收录：{listing['repositoryUrl']}")
    historical = next(
        (
            item
            for item in validator.load_catalog()
            if item["id"].casefold() == listing["id"].casefold()
        ),
        None,
    )
    if historical is not None and not validator.same_github_repository(
        historical["repositoryUrl"], listing["repositoryUrl"]
    ):
        raise SubmissionFailure(
            f"插件 ID {listing['id']} 已有归档历史，不能转移到另一个发布仓库"
        )
    return listing


def summarize_applied_add(event: dict) -> str:
    summary = already_applied_add_summary(event)
    if summary is None:
        raise SubmissionFailure("目标插件尚未通过定向刷新写入中心历史")
    return summary


def validate_add(event: dict) -> tuple[str, dict, dict]:
    listing = parse_add_listing(event)
    plugin_id = listing["id"]
    repository_url = listing["repositoryUrl"]

    listings = validator.load_plugin_list()
    if any(item["id"].casefold() == plugin_id.casefold() for item in listings):
        raise SubmissionFailure(f"插件 ID 已收录：{plugin_id}")
    if any(item["repositoryUrl"].casefold() == repository_url.casefold() for item in listings):
        raise SubmissionFailure(f"仓库地址已收录：{repository_url}")

    catalog = validator.load_catalog()
    historical = next(
        (item for item in catalog if item["id"].casefold() == plugin_id.casefold()),
        None,
    )
    if historical is not None and not validator.same_github_repository(
        historical["repositoryUrl"], repository_url
    ):
        raise SubmissionFailure(
            f"插件 ID {plugin_id} 已有归档历史，不能转移到另一个发布仓库"
        )

    publisher = validator.fetch_publisher_manifest(listing)
    plugin, releases = validator.validate_publisher_manifest_releases(publisher, listing)
    missing = validator.publisher_missing_releases(plugin, releases, historical)
    candidates = validator.plan_publisher_candidates(plugin, releases, historical)
    if historical is not None and not candidates:
        raise SubmissionFailure("归档插件重新激活必须至少声明一个中心尚未收录的版本")
    if historical is not None:
        highest_historical = max(
            historical["releases"],
            key=lambda item: validator.semver_key(item["version"]),
        )
        if not any(
            validator.semver_key(release["version"])
            > validator.semver_key(highest_historical["version"])
            for release in candidates
        ):
            raise SubmissionFailure(
                "归档插件重新激活必须包含一个高于历史最高版本 "
                f"{highest_historical['version']} 的新候选；可同时补录更低版本"
            )
    # The newest bounded catch-up batch is verified fail-hard.  Known immutable
    # history is compared but never downloaded again on routine synchronization.
    for release in candidates:
        payload = validator.download_release_asset(plugin, release)
        validator.validate_runtime_package(plugin, release, payload)
    latest = candidates[-1]
    summary = add_summary(plugin, latest)
    summary += f"\n\n本次已完整验证 `{len(candidates)}` 个待写入历史版本。"
    remaining = len(missing) - len(candidates)
    if remaining:
        summary += f"\n\n中心仍有 `{remaining}` 个较早版本待后续有界批次回填。"
    return summary, listing, latest


def parse_yank_request(event: dict) -> tuple[str, list[str], str]:
    sections = parse_sections(str(event["issue"].get("body") or ""))
    plugin_id = field(sections, "插件 ID / Plugin ID")
    if len(plugin_id) > 128 or validator.PLUGIN_ID.fullmatch(plugin_id) is None:
        raise SubmissionFailure("插件 ID 必须是最长 128 字符的小写反向域名")
    versions_text = field(sections, "版本 / Versions")
    reason = field(sections, "撤回原因 / Reason")
    try:
        reason_length = len(reason.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise SubmissionFailure("撤回原因包含无效 Unicode") from exc
    if reason_length > 1024:
        raise SubmissionFailure("撤回原因不能超过 1024 个字符")
    directory = ROOT / "plugins" / plugin_id / "releases"
    if not directory.is_dir():
        raise SubmissionFailure(f"插件没有历史目录：{plugin_id}")
    available = {path.stem for path in directory.glob("*.json")}
    if versions_text.casefold() == "all":
        versions = sorted(available, key=validator.semver_key, reverse=True)
    else:
        versions = [item.strip() for item in versions_text.split(",") if item.strip()]
    if not versions or any(validator.match_semver(item) is None for item in versions):
        raise SubmissionFailure("版本必须是逗号分隔的严格 SemVer，或填写 all")
    versions = list(dict.fromkeys(versions))
    if len(versions) > 128:
        raise SubmissionFailure("一次撤回不能超过 128 个版本")
    missing = sorted(set(versions) - available)
    if missing:
        raise SubmissionFailure(f"版本未收录：{', '.join(missing)}")
    return plugin_id, versions, reason


def validate_yank(event: dict) -> str:
    plugin_id, versions, reason = parse_yank_request(event)
    return (
        f"将撤回 `{plugin_id}` 的版本：{', '.join(f'`{item}`' for item in versions)}。\n\n"
        f"原因：{reason}\n\n历史文件将保留，但启动器不再允许选择这些版本。"
    )


def validate_request(event: dict) -> str:
    check_validation_permission(event)
    kind = request_kind(event)
    if kind == "add":
        return validate_add(event)[0]
    return validate_yank(event)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def apply_request(event: dict, actor: str) -> str:
    if actor.casefold() not in trusted_reviewers():
        raise SubmissionFailure(f"{actor} 不在 trustedReviewers 中")
    comment = str((event.get("comment") or {}).get("body") or "").strip()
    if comment != "/approve":
        raise SubmissionFailure("只接受精确的 /approve 命令")

    kind = request_kind(event)
    if kind == "add":
        already_applied = already_applied_add_summary(event)
        if already_applied is not None:
            return already_applied
        listing = prepare_add_listing(event)
        listings = validator.load_plugin_list()
        listings.append(listing)
        listings.sort(key=lambda item: item["id"].casefold())
        write_json(ROOT / "plugins.json", listings)
        return (
            f"已登记 `{listing['id']}` 的 active 指针；批准工作流将对该插件执行一次"
            "失败即中止的定向 Release ZIP 校验并据结果生成最终摘要。"
        )

    plugin_id, versions, reason = parse_yank_request(event)
    releases: dict[str, tuple[Path, dict]] = {}
    for version in versions:
        release_path = ROOT / "plugins" / plugin_id / "releases" / f"{version}.json"
        try:
            release = json.loads(release_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SubmissionFailure(f"无法读取中心版本：{plugin_id} {version}") from exc
        if not isinstance(release, dict):
            raise SubmissionFailure(f"中心版本结构无效：{plugin_id} {version}")
        if release.get("yanked") is True and release.get("yankReason") != reason:
            raise SubmissionFailure(
                f"{plugin_id} {version} 已因其他原因撤回，拒绝覆盖原 yankReason"
            )
        releases[version] = (release_path, release)

    for version in versions:
        release_path, release = releases[version]
        release["yanked"] = True
        release["yankReason"] = reason
        write_json(release_path, release)
        review_path = ROOT / "reviews" / plugin_id / f"{version}.json"
        if review_path.exists():
            review_path.unlink()

    release_directory = ROOT / "plugins" / plugin_id / "releases"
    all_yanked = True
    for release_path in release_directory.glob("*.json"):
        try:
            release = json.loads(release_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SubmissionFailure(f"无法确认插件撤回状态：{release_path.name}") from exc
        if not isinstance(release, dict) or release.get("yanked") is not True:
            all_yanked = False
            break
    if all_yanked:
        listings_path = ROOT / "plugins.json"
        try:
            listings = json.loads(listings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SubmissionFailure("无法读取 active 插件列表") from exc
        if not isinstance(listings, list) or any(not isinstance(item, dict) for item in listings):
            raise SubmissionFailure("plugins.json active 插件列表无效")
        remaining = [item for item in listings if item.get("id") != plugin_id]
        if len(remaining) != len(listings):
            write_json(listings_path, remaining)
    return validate_yank(event)


def github_context(event: dict) -> tuple[str, int, str]:
    repository = event.get("repository") or {}
    full_name = repository.get("full_name")
    number = event["issue"].get("number")
    token = os.environ.get("GITHUB_TOKEN")
    if not isinstance(full_name, str) or type(number) is not int or not token:
        raise SubmissionFailure("缺少 GitHub API 上下文或 GITHUB_TOKEN")
    return full_name, number, token


def github_api(event: dict, method: str, path: str, body: object | None = None) -> object:
    full_name, _, token = github_context(event)
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{full_name}/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "NyaLauncher-Registry-Issue-Workflow/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise SubmissionFailure(f"GitHub API {method} {path} 失败：{exc.code} {detail}") from exc
    return json.loads(payload) if payload else None


def live_issue_labels(event: dict) -> set[str]:
    """Read terminal labels after queued jobs start, avoiding stale event state."""

    _, number, _ = github_context(event)
    issue = github_api(event, "GET", f"issues/{number}")
    if not isinstance(issue, dict):
        raise SubmissionFailure("GitHub API 返回了无效的 Issue 状态")
    labels = issue.get("labels", [])
    if not isinstance(labels, list):
        raise SubmissionFailure("GitHub API 返回了无效的 Issue 标签")
    result: set[str] = set()
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else None
        if isinstance(name, str):
            result.add(name.casefold())
    return result


def request_is_centrally_applied(event: dict) -> bool:
    """Detect a pushed approval even when its final Issue update failed."""

    try:
        kind = request_kind(event)
    except SubmissionFailure:
        return False
    if kind == "add":
        try:
            listing = parse_add_listing(event)
        except SubmissionFailure:
            return False
        active = next(
            (
                item
                for item in validator.load_plugin_list()
                if item["id"].casefold() == listing["id"].casefold()
            ),
            None,
        )
        if active is None or not validator.same_github_repository(
            active["repositoryUrl"], listing["repositoryUrl"]
        ):
            return False
        central = next(
            (
                item
                for item in validator.load_catalog()
                if item["id"].casefold() == listing["id"].casefold()
            ),
            None,
        )
        return bool(
            central is not None
            and validator.same_github_repository(
                central["repositoryUrl"], listing["repositoryUrl"]
            )
            and any(not release["yanked"] for release in central["releases"])
        )

    try:
        plugin_id, versions, reason = parse_yank_request(event)
    except SubmissionFailure:
        return False
    for version in versions:
        path = ROOT / "plugins" / plugin_id / "releases" / f"{version}.json"
        try:
            release = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SubmissionFailure(
                f"无法确认中心是否已执行撤回：{plugin_id} {version}"
            ) from exc
        if (
            not isinstance(release, dict)
            or release.get("yanked") is not True
            or release.get("yankReason") != reason
        ):
            return False
    return True


def check_terminal_state(event: dict, operation: str) -> None:
    labels = live_issue_labels(event)
    if operation == "approve" and "rejected" in labels:
        raise SubmissionFailure("该 Issue 已被拒绝，不能再批准")
    if (
        operation == "approve"
        and "approved" in labels
        and not request_is_centrally_applied(event)
    ):
        raise SubmissionFailure("已批准 Issue 的请求内容已变化，拒绝写入第二个请求")
    if operation == "reject" and (
        "approved" in labels or request_is_centrally_applied(event)
    ):
        raise SubmissionFailure("该 Issue 已批准并写入中心；如需下架请创建 Yank Issue")


def ensure_label(event: dict, name: str, color: str) -> None:
    try:
        github_api(event, "POST", "labels", {"name": name, "color": color})
    except SubmissionFailure as exc:
        if "422" not in str(exc):
            raise


def remove_label(event: dict, name: str) -> None:
    _, number, _ = github_context(event)
    try:
        github_api(event, "DELETE", f"issues/{number}/labels/{name}")
    except SubmissionFailure as exc:
        if "404" not in str(exc):
            raise


def remove_issue_comment(event: dict, comment_id: int) -> None:
    try:
        github_api(event, "DELETE", f"issues/comments/{comment_id}")
    except SubmissionFailure as exc:
        if "404" not in str(exc):
            print(f"清理过期验证评论失败：{exc}", file=sys.stderr)


def publish_validation(event: dict, success: bool, message: str) -> None:
    if live_issue_labels(event) & {"approved", "rejected"}:
        # A late validation run must not re-open or relabel a terminal Issue.
        return
    _, number, _ = github_context(event)
    labels = {
        "pending-validation": "d4c5f9",
        "validated": "2da44e",
        "validation-failed": "d73a4a",
    }
    for name, color in labels.items():
        ensure_label(event, name, color)
    for name in labels:
        remove_label(event, name)
    selected = "validated" if success else "validation-failed"
    github_api(event, "POST", f"issues/{number}/labels", {"labels": [selected]})
    heading = "✅ 验证通过" if success else "❌ 验证失败"
    comment = github_api(
        event,
        "POST",
        f"issues/{number}/comments",
        {"body": f"## {heading}\n\n{message}"},
    )
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if live_issue_labels(event) & {"approved", "rejected"}:
        # Approval may have raced the API writes above.  Restore its terminal
        # labels and remove only the comment created by this late validation.
        for name in labels:
            remove_label(event, name)
        if type(comment_id) is int and comment_id > 0:
            remove_issue_comment(event, comment_id)


def initialize_issue(event: dict) -> None:
    """Create missing repository labels and mark a new Issue without downloads."""

    title = str(event["issue"].get("title") or "")
    if title.startswith("[Plugin]"):
        selected = ["plugin-submission", "pending-validation"]
    elif title.startswith("[Yank]"):
        selected = ["plugin-yank", "pending-validation"]
    elif title.startswith("[Review]"):
        selected = ["review-request"]
    else:
        raise SubmissionFailure("该 Issue 不是受支持的插件工作流表单")
    definitions = {
        "plugin-submission": "5319e7",
        "plugin-yank": "d73a4a",
        "review-request": "0e8a16",
        "pending-validation": "d4c5f9",
    }
    for name, color in definitions.items():
        ensure_label(event, name, color)
    _, number, _ = github_context(event)
    github_api(event, "POST", f"issues/{number}/labels", {"labels": selected})


def has_heading_comment(event: dict, number: int, heading: str) -> bool:
    """Find a bot terminal comment without risking reflected marker spoofing."""

    for page in range(1, 11):
        comments = github_api(
            event,
            "GET",
            f"issues/{number}/comments?per_page=100&page={page}",
        )
        if not isinstance(comments, list):
            raise SubmissionFailure("GitHub API 返回了无效的 Issue 评论列表")
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            user = comment.get("user")
            author = user.get("login") if isinstance(user, dict) else None
            body = comment.get("body")
            if (
                isinstance(author, str)
                and author.casefold() == "github-actions[bot]"
                and isinstance(body, str)
                and body.startswith(heading)
            ):
                return True
        if len(comments) < 100:
            return False
    # Public users can flood comments.  Prefer one duplicate trusted terminal
    # comment over making approval/rejection permanently impossible.
    return False


def has_completion_comment(event: dict, number: int) -> bool:
    return has_heading_comment(event, number, APPROVAL_COMMENT_HEADING)


def publish_completion(event: dict, summary: str) -> None:
    _, number, _ = github_context(event)
    if "rejected" in live_issue_labels(event):
        raise SubmissionFailure("该 Issue 已被拒绝，不能再标记为批准")
    ensure_label(event, "approved", "0e8a16")
    github_api(event, "POST", f"issues/{number}/labels", {"labels": ["approved"]})
    if not has_completion_comment(event, number):
        github_api(
            event,
            "POST",
            f"issues/{number}/comments",
            {
                "body": (
                    f"{APPROVAL_COMMENT_HEADING}\n\n{summary.rstrip()}\n\n"
                    f"{APPROVAL_COMMENT_MARKER}"
                )
            },
        )
    github_api(event, "PATCH", f"issues/{number}", {"state": "closed"})
    # Remove transient labels last.  Until the durable comment and close have
    # succeeded, the Issue remains discoverable for a safe workflow retry.
    for name in ("pending-validation", "validated", "validation-failed"):
        remove_label(event, name)


def reject_request(event: dict, actor: str) -> None:
    if actor.casefold() not in trusted_reviewers():
        raise SubmissionFailure(f"{actor} 不在 trustedReviewers 中")
    command = str((event.get("comment") or {}).get("body") or "").strip()
    match = re.fullmatch(r"/reject(?:\s+([\s\S]+))?", command)
    if match is None:
        raise SubmissionFailure("拒绝命令格式为 /reject 原因")
    reason = (match.group(1) or "未提供原因").strip()
    _, number, _ = github_context(event)
    if "approved" in live_issue_labels(event) or request_is_centrally_applied(event):
        raise SubmissionFailure("该 Issue 已批准并写入中心；如需下架请创建 Yank Issue")
    ensure_label(event, "rejected", "b60205")
    github_api(event, "POST", f"issues/{number}/labels", {"labels": ["rejected"]})
    if not has_heading_comment(event, number, REJECTION_COMMENT_HEADING):
        github_api(
            event,
            "POST",
            f"issues/{number}/comments",
            {
                "body": (
                    f"{REJECTION_COMMENT_HEADING}\n\n审核者：@{actor}\n\n原因：{reason}\n\n"
                    f"{REJECTION_COMMENT_MARKER}"
                )
            },
        )
    github_api(event, "PATCH", f"issues/{number}", {"state": "closed"})
    for name in ("pending-validation", "validated", "validation-failed"):
        remove_label(event, name)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--event", required=True, type=Path)
    validate_parser.add_argument("--publish", action="store_true")
    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument("--event", required=True, type=Path)
    authorize_parser.add_argument(
        "--command",
        dest="trusted_command",
        choices=("validate", "approve", "reject"),
        default="validate",
    )
    initialize_parser = subparsers.add_parser("initialize")
    initialize_parser.add_argument("--event", required=True, type=Path)
    terminal_parser = subparsers.add_parser("check-terminal")
    terminal_parser.add_argument("--event", required=True, type=Path)
    terminal_parser.add_argument("--operation", choices=("approve", "reject"), required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--event", required=True, type=Path)
    apply_parser.add_argument("--actor", required=True)
    apply_parser.add_argument("--summary", type=Path)
    apply_parser.add_argument("--github-output", type=Path)
    summarize_parser = subparsers.add_parser("summarize")
    summarize_parser.add_argument("--event", required=True, type=Path)
    summarize_parser.add_argument("--summary", required=True, type=Path)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--event", required=True, type=Path)
    complete_parser.add_argument("--summary", required=True, type=Path)
    reject_parser = subparsers.add_parser("reject")
    reject_parser.add_argument("--event", required=True, type=Path)
    reject_parser.add_argument("--actor", required=True)
    args = parser.parse_args()

    try:
        event = load_event(args.event)
        if args.command == "initialize":
            initialize_issue(event)
            print("Issue 标签初始化完成")
        elif args.command == "authorize":
            check_trusted_command_permission(event, args.trusted_command)
            print("可信维护者验证授权通过")
        elif args.command == "check-terminal":
            check_terminal_state(event, args.operation)
            print("Issue 终态检查通过")
        elif args.command == "validate":
            try:
                message = validate_request(event)
            except PermissionFailure:
                # Authorization failures must never alter an Issue controlled
                # by a different user or cancel a trusted validation state.
                raise
            except (SubmissionFailure, validator.ValidationFailure) as exc:
                if args.publish:
                    publish_validation(event, False, str(exc))
                raise
            if args.publish:
                publish_validation(event, True, message)
            print(message)
        elif args.command == "apply":
            summary = apply_request(event, args.actor)
            if args.summary:
                args.summary.write_text(summary + "\n", encoding="utf-8", newline="\n")
            if args.github_output:
                write_request_metadata(event, args.github_output)
            print(summary)
        elif args.command == "summarize":
            summary = summarize_applied_add(event)
            args.summary.write_text(summary + "\n", encoding="utf-8", newline="\n")
            print(summary)
        elif args.command == "complete":
            summary = args.summary.read_text(encoding="utf-8")
            publish_completion(event, summary)
        else:
            reject_request(event, args.actor)
        return 0
    except (SubmissionFailure, validator.ValidationFailure, OSError, UnicodeError) as exc:
        print(f"Issue 处理失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
