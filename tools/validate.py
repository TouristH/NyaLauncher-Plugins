#!/usr/bin/env python3
"""Validate registry sources and deterministically generate public/v1/index.json."""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import io
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+$")
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
KNOWN_CAPABILITIES = {
    "ui.components",
    "ui.native",
    "network.http",
    "system.info.read",
    "user-files.read",
    "user-files.write",
    "process.start",
    "minecraft.instance.read",
    "minecraft.instance.modify",
    "minecraft.launch.modify",
}
CATEGORIES = {
    "appearance", "automation", "gameplay", "integration",
    "launch", "management", "utilities",
}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
MAXIMUM_PACKAGE_BYTES = 256 * 1024 * 1024
MAXIMUM_EXPANDED_BYTES = 1024 * 1024 * 1024
MAXIMUM_ENTRY_BYTES = 512 * 1024 * 1024
MAXIMUM_ENTRIES = 4096
MAXIMUM_PLUGIN_COUNT = 2048
MAXIMUM_RELEASE_COUNT = 128
MAXIMUM_INDEX_BYTES = 4 * 1024 * 1024


class ValidationFailure(Exception):
    pass


def load_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 无法读取 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: JSON 根必须是对象")
    return value


def require_exact_keys(path: Path, value: dict, required: set[str], optional: set[str]) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 缺少字段 {sorted(missing)}")
    if extra:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 未知字段 {sorted(extra)}")


def require_text(path: Path, field: str, value: object, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > maximum:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 不是有效文本")
    return value


def require_list_of_text(
    path: Path,
    field: str,
    value: object,
    maximum: int,
    item_maximum: int = 256,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or any(
        not isinstance(item, str) or not item.strip() or len(item) > item_maximum
        for item in value
    ):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 不是有效文本数组")
    if len({item.casefold() for item in value}) != len(value):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 包含重复值")
    return value


def validate_schema_reference(path: Path, value: dict) -> None:
    if "$schema" in value:
        require_text(path, "$schema", value["$schema"], 2048)


def match_semver(value: str) -> re.Match[str] | None:
    match = SEMVER.fullmatch(value)
    if match is None:
        return None
    prerelease = match.group(4)
    if prerelease and any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in prerelease.split(".")
    ):
        return None
    return match


def validate_semver(path: Path, field: str, value: object) -> str:
    text = require_text(path, field, value, 64)
    if match_semver(text) is None:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 必须是 SemVer")
    return text


def require_https(path: Path, field: str, value: object) -> str:
    text = require_text(path, field, value, 2048)
    parsed = urlparse(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationFailure(
            f"{path.relative_to(ROOT)}: {field} 必须是使用 443 端口的无凭据 HTTPS URL"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise ValidationFailure(
            f"{path.relative_to(ROOT)}: {field} 必须是使用 443 端口的无凭据 HTTPS URL"
        )
    return text


def validate_utc_timestamp(path: Path, field: str, value: object) -> str:
    text = require_text(path, field, value, 64)
    if UTC_TIMESTAMP.fullmatch(text) is None:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 必须是 UTC ISO 8601 秒级时间")
    try:
        datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 不是有效 UTC 时间") from exc
    return text


def is_allowed_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (host == "github.com" or host.endswith(".githubusercontent.com"))
    )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urljoin(req.full_url, newurl)
        if not is_allowed_asset_url(target):
            raise ValidationFailure(f"下载重定向到不允许的地址：{target}")
        redirects = getattr(req, "redirect_dict", {})
        if len(redirects) >= 5:
            raise ValidationFailure("下载重定向次数超过 5 次")
        return super().redirect_request(req, fp, code, msg, headers, target)


def semver_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    match = match_semver(version)
    if not match:
        raise ValueError(version)
    pre = match.group(4)
    if pre is None:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), 1, ()
    identifiers: list[tuple[int, object]] = []
    for item in pre.split("."):
        identifiers.append((0, int(item)) if item.isdigit() else (1, item))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), 0, tuple(identifiers)


def validate_plugin(path: Path, directory_name: str) -> dict:
    value = load_object(path)
    require_exact_keys(
        path,
        value,
        {
            "schemaVersion", "id", "name", "description", "authors",
            "repositoryUrl", "maintainers", "categories", "license",
        },
        {"$schema"},
    )
    validate_schema_reference(path, value)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: schemaVersion 必须是 1")
    plugin_id = require_text(path, "id", value["id"], 128)
    if not PLUGIN_ID.fullmatch(plugin_id) or plugin_id != directory_name:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: id 必须合法且与目录名一致")
    require_text(path, "name", value["name"], 256)
    require_text(path, "description", value["description"], 8192, allow_empty=True)
    require_list_of_text(path, "authors", value["authors"], 64, item_maximum=256)
    repository_url = require_https(path, "repositoryUrl", value["repositoryUrl"])
    if not GITHUB_REPO.fullmatch(repository_url):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: repositoryUrl 必须是 GitHub 仓库根地址")
    maintainers = require_list_of_text(
        path, "maintainers", value["maintainers"], 16, item_maximum=39
    )
    if not maintainers or any(GITHUB_LOGIN.fullmatch(item) is None for item in maintainers):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: maintainers 必须是有效 GitHub 用户名")
    categories = require_list_of_text(path, "categories", value["categories"], 8)
    if not categories or set(categories) - CATEGORIES:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: categories 包含未知分类")
    require_text(path, "license", value["license"], 256)
    result = copy.deepcopy(value)
    result.pop("$schema", None)
    result.pop("schemaVersion", None)
    return result


def validate_capabilities(path: Path, field: str, value: object) -> list[str]:
    items = require_list_of_text(path, field, value, 64, item_maximum=128)
    unknown = set(items) - KNOWN_CAPABILITIES
    if unknown:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: {field} 包含未知能力 {sorted(unknown)}")
    return items


def validate_release(path: Path, file_version: str, plugin: dict) -> dict:
    value = load_object(path)
    require_exact_keys(
        path,
        value,
        {
            "schemaVersion", "version", "channel", "publishedAt", "releaseNotesUrl",
            "download", "compatibility", "requiredCapabilities", "optionalCapabilities", "yanked",
        },
        {"$schema", "yankReason"},
    )
    validate_schema_reference(path, value)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: schemaVersion 必须是 1")
    version = validate_semver(path, "version", value["version"])
    if version != file_version:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: version 必须是 SemVer 且与文件名一致")
    if not isinstance(value["channel"], str) or value["channel"] not in {"stable", "preview"}:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: channel 只能是 stable 或 preview")
    validate_utc_timestamp(path, "publishedAt", value["publishedAt"])
    require_https(path, "releaseNotesUrl", value["releaseNotesUrl"])

    download = value["download"]
    if not isinstance(download, dict):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: download 必须是对象")
    require_exact_keys(path, download, {"url", "sha256", "size"}, set())
    download_url = require_https(path, "download.url", download["url"])
    repo_match = GITHUB_REPO.fullmatch(plugin["repositoryUrl"])
    assert repo_match
    owner, repo = repo_match.group(1), repo_match.group(2).removesuffix(".git")
    expected_prefix = f"https://github.com/{owner}/{repo}/releases/download/"
    if not download_url.startswith(expected_prefix):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 下载必须来自插件仓库的固定 GitHub Release")
    if not isinstance(download["sha256"], str) or not SHA256.fullmatch(download["sha256"]):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: sha256 必须是 64 位小写十六进制")
    if type(download["size"]) is not int or not 1 <= download["size"] <= 256 * 1024 * 1024:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 下载大小必须在 1 B 到 256 MiB 之间")

    compatibility = value["compatibility"]
    if not isinstance(compatibility, dict):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: compatibility 必须是对象")
    require_exact_keys(
        path,
        compatibility,
        {"manifestVersion", "apiVersion", "minimumLauncherVersion"},
        {"maximumLauncherVersionExclusive"},
    )
    if (
        type(compatibility["manifestVersion"]) is not int
        or compatibility["manifestVersion"] != 1
        or not isinstance(compatibility["apiVersion"], str)
        or not re.fullmatch(r"1(?:\.\d+){1,2}", compatibility["apiVersion"])
    ):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 当前仅支持 manifest v1 / API v1")
    for field in ("minimumLauncherVersion", "maximumLauncherVersionExclusive"):
        if field in compatibility:
            validate_semver(path, field, compatibility[field])

    required = validate_capabilities(path, "requiredCapabilities", value["requiredCapabilities"])
    optional = validate_capabilities(path, "optionalCapabilities", value["optionalCapabilities"])
    if len(required) + len(optional) > 64:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 能力声明合计不能超过 64 项")
    if {item.casefold() for item in required} & {item.casefold() for item in optional}:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 必要与可选能力不能重复")
    if not isinstance(value["yanked"], bool):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: yanked 必须是布尔值")
    if value["yanked"]:
        require_text(path, "yankReason", value.get("yankReason"), 1024)
    elif "yankReason" in value:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: 未撤回版本不能设置 yankReason")

    result = copy.deepcopy(value)
    result.pop("$schema", None)
    result.pop("schemaVersion", None)
    return result


def validate_review(
    path: Path,
    expected_plugin_id: str,
    expected_version: str,
    expected_sha256: str,
    trusted_reviewers: set[str],
) -> dict:
    value = load_object(path)
    require_exact_keys(
        path,
        value,
        {
            "schemaVersion",
            "pluginId",
            "version",
            "sha256",
            "status",
            "reviewer",
            "reviewedAt",
        },
        {"$schema", "notes"},
    )
    validate_schema_reference(path, value)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: schemaVersion 必须是 1")
    plugin_id = require_text(path, "pluginId", value["pluginId"], 128)
    if plugin_id != expected_plugin_id or PLUGIN_ID.fullmatch(plugin_id) is None:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: pluginId 与审核目录不一致")
    version = validate_semver(path, "version", value["version"])
    if version != expected_version:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: version 与审核文件名不一致")
    sha256 = require_text(path, "sha256", value["sha256"], 64)
    if SHA256.fullmatch(sha256) is None or sha256 != expected_sha256:
        raise ValidationFailure(
            f"{path.relative_to(ROOT)}: sha256 必须与被审核 Release 的固定哈希完全一致"
        )
    if value["status"] != "verified" or not isinstance(value["status"], str):
        raise ValidationFailure(f"{path.relative_to(ROOT)}: status 只能是 verified")
    reviewer = require_text(path, "reviewer", value["reviewer"], 39)
    if GITHUB_LOGIN.fullmatch(reviewer) is None or reviewer.casefold() not in trusted_reviewers:
        raise ValidationFailure(f"{path.relative_to(ROOT)}: reviewer 不在 trustedReviewers 中")
    reviewed_at = validate_utc_timestamp(path, "reviewedAt", value["reviewedAt"])
    notes = None
    if "notes" in value:
        notes = require_text(path, "notes", value["notes"], 4096, allow_empty=True)

    result = {
        "status": "verified",
        "sha256": sha256,
        "reviewedBy": reviewer,
        "reviewedAt": reviewed_at,
    }
    if notes is not None:
        result["notes"] = notes
    return result


def attach_reviews(result_plugins: list[dict], trusted_reviewers: set[str]) -> None:
    reviews_root = ROOT / "reviews"
    if not reviews_root.exists():
        return
    releases_by_key = {
        (plugin["id"], release["version"]): release
        for plugin in result_plugins
        for release in plugin["releases"]
    }
    plugin_ids = {plugin["id"] for plugin in result_plugins}
    for directory in sorted(
        (item for item in reviews_root.iterdir() if item.is_dir()),
        key=lambda item: item.name.casefold(),
    ):
        plugin_id = directory.name
        if PLUGIN_ID.fullmatch(plugin_id) is None or plugin_id not in plugin_ids:
            raise ValidationFailure(f"reviews/{plugin_id}: 审核目录没有对应的已收录插件")
        for item in directory.iterdir():
            if not item.is_file() or item.suffix != ".json":
                raise ValidationFailure(
                    f"{item.relative_to(ROOT)}: 审核目录只能包含版本 JSON 文件"
                )
        review_files = sorted(directory.glob("*.json"), key=lambda item: item.name)
        for review_path in review_files:
            version = review_path.stem
            if match_semver(version) is None:
                raise ValidationFailure(f"{review_path.relative_to(ROOT)}: 文件名必须是严格 SemVer")
            release = releases_by_key.get((plugin_id, version))
            if release is None:
                raise ValidationFailure(
                    f"{review_path.relative_to(ROOT)}: 没有对应的插件 Release 描述"
                )
            if "review" in release:
                raise ValidationFailure(f"{review_path.relative_to(ROOT)}: 版本审核重复")
            release["review"] = validate_review(
                review_path,
                plugin_id,
                version,
                release["download"]["sha256"],
                trusted_reviewers,
            )


def download_release_asset(plugin: dict, release: dict) -> bytes:
    url = release["download"]["url"]
    if not is_allowed_asset_url(url) or urlparse(url).hostname.lower() != "github.com":
        raise ValidationFailure(f"{plugin['id']} {release['version']}: 下载地址不受允许")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NyaLauncher-Plugins-Validator/1.0",
            "Accept": "application/octet-stream",
        },
    )
    opener = urllib.request.build_opener(SafeRedirectHandler())
    expected_size = release["download"]["size"]
    try:
        with opener.open(request, timeout=60) as response:
            final_url = response.geturl()
            if not is_allowed_asset_url(final_url):
                raise ValidationFailure(
                    f"{plugin['id']} {release['version']}: 最终下载地址不受允许"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) != expected_size:
                raise ValidationFailure(
                    f"{plugin['id']} {release['version']}: Content-Length 与 size 不一致"
                )
            chunks: list[bytes] = []
            received = 0
            digest = hashlib.sha256()
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size or received > MAXIMUM_PACKAGE_BYTES:
                    raise ValidationFailure(
                        f"{plugin['id']} {release['version']}: 下载超过声明大小"
                    )
                digest.update(chunk)
                chunks.append(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValidationFailure(
            f"{plugin['id']} {release['version']}: 无法下载 Release 资产：{exc}"
        ) from exc

    if received != expected_size:
        raise ValidationFailure(
            f"{plugin['id']} {release['version']}: 实际大小 {received} 与 size {expected_size} 不一致"
        )
    actual_hash = digest.hexdigest()
    if actual_hash != release["download"]["sha256"]:
        raise ValidationFailure(
            f"{plugin['id']} {release['version']}: SHA-256 不一致（实际 {actual_hash}）"
        )
    return b"".join(chunks)


def validate_zip_path(plugin_id: str, version: str, name: str, is_directory: bool) -> str:
    if (
        not name
        or len(name) > 512
        or unicodedata.normalize("NFC", name) != name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or (is_directory != name.endswith("/"))
    ):
        raise ValidationFailure(f"{plugin_id} {version}: ZIP 不安全路径 {name!r}")
    path = name[:-1] if is_directory else name
    segments = path.split("/")
    if not segments or any(
        not segment
        or segment in {".", ".."}
        or ":" in segment
        or any(character in '<>"|?*' for character in segment)
        or segment.endswith((" ", "."))
        or segment.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        for segment in segments
    ):
        raise ValidationFailure(f"{plugin_id} {version}: ZIP 不安全路径 {name!r}")
    return "/".join(segments)


def validate_runtime_package(plugin: dict, release: dict, payload: bytes) -> None:
    plugin_id = plugin["id"]
    version = release["version"]
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValidationFailure(f"{plugin_id} {version}: 下载资产不是有效 ZIP：{exc}") from exc

    with archive:
        entries = archive.infolist()
        if not 1 <= len(entries) <= MAXIMUM_ENTRIES:
            raise ValidationFailure(f"{plugin_id} {version}: ZIP 条目数量超限")
        names: dict[str, zipfile.ZipInfo] = {}
        expanded = 0
        for entry in entries:
            is_directory = entry.is_dir()
            normalized = validate_zip_path(plugin_id, version, entry.filename, is_directory)
            folded = normalized.casefold()
            if folded in names:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 包含重复路径 {normalized}")
            names[folded] = entry
            unix_type = (entry.external_attr >> 16) & 0xF000
            if unix_type not in (0, 0x4000, 0x8000) or entry.external_attr & 0x400:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 包含链接或特殊文件 {normalized}")
            if (is_directory and unix_type == 0x8000) or (not is_directory and unix_type == 0x4000):
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 文件类型标记冲突 {normalized}")
            if is_directory and entry.file_size != 0:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 目录条目不能包含文件内容 {normalized}")
            if entry.file_size > MAXIMUM_ENTRY_BYTES:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 单条目超过 512 MiB")
            if entry.file_size > 1024 and (
                entry.compress_size == 0 or entry.file_size // entry.compress_size > 200
            ):
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 压缩比异常 {normalized}")
            expanded += entry.file_size
            if expanded > MAXIMUM_EXPANDED_BYTES:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 解压后超过 1 GiB")

        manifest_entry = names.get("plugin.json")
        if manifest_entry is None or manifest_entry.is_dir():
            raise ValidationFailure(f"{plugin_id} {version}: ZIP 根目录缺少 plugin.json")
        if manifest_entry.file_size > 1024 * 1024:
            raise ValidationFailure(f"{plugin_id} {version}: plugin.json 超过 1 MiB")
        try:
            manifest = json.loads(archive.read(manifest_entry).decode("utf-8"))
        except (KeyError, UnicodeError, json.JSONDecodeError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ValidationFailure(f"{plugin_id} {version}: 无法读取包内 plugin.json：{exc}") from exc
        if not isinstance(manifest, dict):
            raise ValidationFailure(f"{plugin_id} {version}: 包内 plugin.json 根必须是对象")

        expected = release["compatibility"]
        checks = {
            "manifestVersion": expected["manifestVersion"],
            "id": plugin_id,
            "version": version,
            "apiVersion": expected["apiVersion"],
            "minimumLauncherVersion": expected["minimumLauncherVersion"],
            "requiredCapabilities": release["requiredCapabilities"],
            "optionalCapabilities": release["optionalCapabilities"],
        }
        for field, expected_value in checks.items():
            actual = manifest.get(field, [] if field.endswith("Capabilities") else None)
            if field.endswith("Capabilities"):
                if (
                    not isinstance(actual, list)
                    or any(not isinstance(item, str) for item in actual)
                    or len({item.casefold() for item in actual}) != len(actual)
                    or {item.casefold() for item in actual}
                    != {item.casefold() for item in expected_value}
                ):
                    raise ValidationFailure(
                        f"{plugin_id} {version}: 包内 {field} 与版本索引不一致"
                    )
            elif type(actual) is not type(expected_value) or actual != expected_value:
                raise ValidationFailure(f"{plugin_id} {version}: 包内 {field} 与版本索引不一致")

        entry_assembly = manifest.get("entryAssembly")
        if not isinstance(entry_assembly, str) or not entry_assembly.strip():
            raise ValidationFailure(f"{plugin_id} {version}: 包内 entryAssembly 缺失")
        entry_type = manifest.get("entryType")
        if not isinstance(entry_type, str) or not entry_type.strip() or len(entry_type) > 1024:
            raise ValidationFailure(f"{plugin_id} {version}: 包内 entryType 缺失或无效")
        authors = manifest.get("authors", [])
        if (
            not isinstance(authors, list)
            or len(authors) > 64
            or any(
                not isinstance(author, str)
                or not author.strip()
                or len(author) > 256
                for author in authors
            )
        ):
            raise ValidationFailure(f"{plugin_id} {version}: 包内 authors 无效")
        assembly_path = validate_zip_path(plugin_id, version, entry_assembly, False).casefold()
        assembly_entry = names.get(assembly_path)
        if assembly_entry is None or assembly_entry.is_dir() or not assembly_path.endswith(".dll"):
            raise ValidationFailure(f"{plugin_id} {version}: entryAssembly 不存在或不是 DLL")
        try:
            if archive.open(assembly_entry).read(2) != b"MZ":
                raise ValidationFailure(f"{plugin_id} {version}: entryAssembly 不是 PE 程序集")
        except (RuntimeError, zipfile.BadZipFile) as exc:
            raise ValidationFailure(f"{plugin_id} {version}: 无法读取 entryAssembly：{exc}") from exc

        if any(name.rsplit("/", 1)[-1] == "nyalauncher.plugin.abstractions.dll" for name in names):
            raise ValidationFailure(
                f"{plugin_id} {version}: 插件包不能携带 NyaLauncher.Plugin.Abstractions.dll"
            )
        icon = manifest.get("icon")
        if icon is not None:
            if not isinstance(icon, str):
                raise ValidationFailure(f"{plugin_id} {version}: icon 必须是字符串")
            icon_path = validate_zip_path(plugin_id, version, icon, False).casefold()
            if icon_path not in names or names[icon_path].is_dir():
                raise ValidationFailure(f"{plugin_id} {version}: 声明的 icon 不存在")


def verify_assets(index: dict) -> None:
    checked = 0
    for plugin in index["plugins"]:
        for release in plugin["releases"]:
            if release["yanked"]:
                continue
            payload = download_release_asset(plugin, release)
            validate_runtime_package(plugin, release, payload)
            checked += 1
            print(f"资产通过：{plugin['id']} {release['version']}")
    print(f"资产验证完成：{checked} 个未撤回版本")


def build_index() -> dict:
    repository_path = ROOT / "repository.json"
    repository = load_object(repository_path)
    require_exact_keys(
        repository_path,
        repository,
        {
            "schemaVersion",
            "name",
            "sourceUrl",
            "launcherUrl",
            "indexPath",
            "trustedReviewers",
        },
        {"$schema"},
    )
    validate_schema_reference(repository_path, repository)
    if (
        type(repository["schemaVersion"]) is not int
        or repository["schemaVersion"] != 1
        or not isinstance(repository["indexPath"], str)
        or repository["indexPath"] != "public/v1/index.json"
    ):
        raise ValidationFailure("repository.json: 不支持的仓库配置")
    require_text(repository_path, "name", repository["name"], 128)
    require_https(repository_path, "sourceUrl", repository["sourceUrl"])
    require_https(repository_path, "launcherUrl", repository["launcherUrl"])
    reviewers = require_list_of_text(
        repository_path,
        "trustedReviewers",
        repository["trustedReviewers"],
        32,
        item_maximum=39,
    )
    if not reviewers or any(GITHUB_LOGIN.fullmatch(item) is None for item in reviewers):
        raise ValidationFailure("repository.json: trustedReviewers 必须包含有效 GitHub 用户名")
    trusted_reviewers = {item.casefold() for item in reviewers}

    plugins_root = ROOT / "plugins"
    result_plugins: list[dict] = []
    seen_ids: set[str] = set()
    plugin_directories = sorted(
        (item for item in plugins_root.iterdir() if item.is_dir()),
        key=lambda p: p.name.casefold(),
    )
    if len(plugin_directories) > MAXIMUM_PLUGIN_COUNT:
        raise ValidationFailure(f"插件总数不能超过 {MAXIMUM_PLUGIN_COUNT}")
    for directory in plugin_directories:
        plugin_path = directory / "plugin.json"
        if not plugin_path.is_file():
            raise ValidationFailure(f"plugins/{directory.name}: 缺少 plugin.json")
        plugin = validate_plugin(plugin_path, directory.name)
        folded = plugin["id"].casefold()
        if folded in seen_ids:
            raise ValidationFailure(f"plugins/{directory.name}: 插件 ID 重复")
        seen_ids.add(folded)

        releases_root = directory / "releases"
        if not releases_root.is_dir():
            raise ValidationFailure(f"plugins/{directory.name}: 缺少 releases 目录")
        release_files = list(releases_root.glob("*.json"))
        if not release_files:
            raise ValidationFailure(f"plugins/{directory.name}: 至少需要一个版本")
        if len(release_files) > MAXIMUM_RELEASE_COUNT:
            raise ValidationFailure(
                f"plugins/{directory.name}: 版本数不能超过 {MAXIMUM_RELEASE_COUNT}"
            )
        for path in release_files:
            if match_semver(path.stem) is None:
                raise ValidationFailure(f"{path.relative_to(ROOT)}: 文件名必须是严格 SemVer")
        release_files.sort(
            key=lambda path: (semver_key(path.stem), path.stem),
            reverse=True,
        )
        plugin["releases"] = [validate_release(path, path.stem, plugin) for path in release_files]
        result_plugins.append(plugin)

    attach_reviews(result_plugins, trusted_reviewers)

    return {
        "schemaVersion": 1,
        "name": repository["name"],
        "sourceUrl": repository["sourceUrl"],
        "plugins": result_plugins,
    }


def render(value: dict) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if len(rendered.encode("utf-8")) > MAXIMUM_INDEX_BYTES:
        raise ValidationFailure(f"生成索引不能超过 {MAXIMUM_INDEX_BYTES} 字节")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="rewrite the generated index")
    parser.add_argument("--check", action="store_true", help="fail if the generated index is stale")
    parser.add_argument(
        "--verify-assets",
        action="store_true",
        help="download every non-yanked Release and validate its hash and package structure",
    )
    args = parser.parse_args()
    try:
        generated = render(build_index())
        index_path = ROOT / "public/v1/index.json"
        current = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        if args.write:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(generated, encoding="utf-8", newline="\n")
        if args.check and current != generated:
            raise ValidationFailure("public/v1/index.json 不是由当前条目确定性生成的，请运行 python tools/validate.py --write")
        if args.verify_assets:
            verify_assets(json.loads(generated))
        print(f"验证通过：{len(generated.encode('utf-8'))} 字节索引")
        return 0
    except ValidationFailure as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
