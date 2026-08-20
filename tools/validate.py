#!/usr/bin/env python3
"""Validate, refresh, and deterministically publish the NyaLauncher registry.

``plugins.json`` contains monitored publisher pointers and immutable GitHub
numeric identities. Each publisher owns
a root ``_manifest.json`` with stable metadata and a bounded complete
``releases[]`` history of fixed GitHub Release ZIPs.  Missing releases are
validated in bounded batches and appended to ``plugins/<id>/releases``, the
immutable central history.  ``plugin_details.json`` and ``public/v1/index.json``
are generated views; only the public view receives administrator reviews.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse


_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def ascii_fold(value: str) -> str:
    """Case-fold ASCII without Python's broader Unicode equivalences."""

    return value.translate(_ASCII_LOWER_TRANSLATION)


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+$")
SETTING_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")
GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
GITHUB_APP_BOT_LOGIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\[bot\]$"
)
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
API_VERSION = re.compile(r"^1(?:\.[0-9]+){1,2}$")
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
    "appearance",
    "automation",
    "gameplay",
    "integration",
    "launch",
    "management",
    "utilities",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
}
MAXIMUM_MANIFEST_BYTES = 1024 * 1024
MAXIMUM_PACKAGE_BYTES = 256 * 1024 * 1024
MAXIMUM_EXPANDED_BYTES = 1024 * 1024 * 1024
MAXIMUM_ENTRY_BYTES = 512 * 1024 * 1024
MAXIMUM_ENTRIES = 4096
MAXIMUM_PLUGIN_COUNT = 2048
MAXIMUM_RELEASE_COUNT = 128
MAXIMUM_PUBLISHER_HISTORY_BYTES = 4 * 1024 * 1024 * 1024
MAXIMUM_NEW_RELEASE_COUNT = 16
MAXIMUM_NEW_RELEASE_BYTES = 512 * 1024 * 1024
MAXIMUM_REFRESH_PUBLISHERS = 64
MAXIMUM_INDEX_BYTES = 4 * 1024 * 1024
MAXIMUM_CAPABILITY_COUNT = 64
MAXIMUM_SETTING_COUNT = 256
MAXIMUM_STORED_VALUE_CHARACTERS = 32768
MAXIMUM_SEMVER_NUMBER = 2**31 - 1
SETTING_KINDS = {
    ascii_fold(value): value
    for value in (
        "Boolean",
        "Integer",
        "Number",
        "Text",
        "MultilineText",
        "Secret",
        "Choice",
        "File",
        "Directory",
    )
}
SETTING_SCOPES = {
    ascii_fold(value): value for value in ("Global", "MinecraftInstance")
}
WINDOWS_INVALID_FILENAME_CHARACTERS = set('<>:"/\\|?*')
_MISSING = object()


class ValidationFailure(Exception):
    """A registry input violates the publication contract."""


class AvailabilityFailure(ValidationFailure):
    """A remote dependency was temporarily unavailable; retry is appropriate."""


class PublisherCandidateFailure(ValidationFailure):
    """A candidate batch failed after consuming bounded download resources."""

    def __init__(
        self,
        message: str,
        attempted_count: int,
        attempted_bytes: int,
        *,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.attempted_count = attempted_count
        self.attempted_bytes = attempted_bytes
        self.retryable = retryable


def is_retryable_http_error(exc: urllib.error.HTTPError) -> bool:
    """Classify bounded remote availability failures without hiding bad input."""

    if exc.code in {408, 425, 429} or 500 <= exc.code <= 599:
        return True
    if exc.code != 403:
        return False
    headers = exc.headers
    if headers is None:
        return False
    retry_after = headers.get("Retry-After")
    remaining = headers.get("X-RateLimit-Remaining")
    return bool(isinstance(retry_after, str) and retry_after.strip()) or remaining == "0"


def source_name(source: Path | str) -> str:
    if isinstance(source, Path):
        try:
            return source.relative_to(ROOT).as_posix()
        except ValueError:
            return str(source)
    return source


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValidationFailure(f"JSON 包含重复字段 {key!r}")
        value[key] = item
    return value


def reject_json_constant(value: str) -> None:
    raise ValidationFailure(f"JSON 包含非标准数值 {value}")


def parse_json_object(text: str, source: Path | str) -> dict:
    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_json_constant,
        )
    except ValidationFailure as exc:
        raise ValidationFailure(f"{source_name(source)}: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        raise ValidationFailure(f"{source_name(source)}: 无法解析 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValidationFailure(f"{source_name(source)}: JSON 根必须是对象")
    return value


def parse_json_array(text: str, source: Path | str) -> list:
    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_json_constant,
        )
    except ValidationFailure as exc:
        raise ValidationFailure(f"{source_name(source)}: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        raise ValidationFailure(f"{source_name(source)}: 无法解析 JSON：{exc}") from exc
    if not isinstance(value, list):
        raise ValidationFailure(f"{source_name(source)}: JSON 根必须是数组")
    return value


def validate_runtime_default_value_raw_lengths(
    text: str, source: Path | str
) -> dict[int, str]:
    r"""Mirror JsonElement.GetRawText().Length for settings default values.

    Python's parsed value loses escape spelling, while the launcher bounds the
    original JSON token.  Walk the already validated document without
    materializing it again so ``\uXXXX`` escapes cannot hide an oversized
    default value from the registry validator.
    """

    decoder = json.JSONDecoder()
    text_length = len(text)
    raw_defaults: dict[int, str] = {}

    def skip_whitespace(index: int) -> int:
        while index < text_length and text[index] in " \t\r\n":
            index += 1
        return index

    def validate_raw_value(path: tuple[object, ...], start: int, end: int) -> None:
        if not (
            len(path) == 3
            and isinstance(path[0], str)
            and ascii_fold(path[0]) == "settings"
            and isinstance(path[1], int)
            and isinstance(path[2], str)
            and ascii_fold(path[2]) == "defaultvalue"
        ):
            return
        raw = text[start:end]
        try:
            raw_length = len(raw.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise ValidationFailure(
                f"{source_name(source)}: settings defaultValue 包含无效 Unicode"
            ) from exc
        if raw_length > MAXIMUM_STORED_VALUE_CHARACTERS:
            raise ValidationFailure(
                f"{source_name(source)}: settings defaultValue 原始 JSON 超过 32768 字符"
            )
        raw_defaults[path[1]] = raw

    def scan_value(index: int, path: tuple[object, ...], depth: int) -> int:
        index = skip_whitespace(index)
        start = index
        if index >= text_length:
            raise ValueError("unexpected end of JSON")
        if text[index] == "{":
            depth += 1
            if depth > 64:
                raise ValidationFailure(
                    f"{source_name(source)}: JSON 嵌套深度超过启动器上限 64"
                )
            index = skip_whitespace(index + 1)
            if index < text_length and text[index] == "}":
                end = index + 1
                validate_raw_value(path, start, end)
                return end
            while True:
                key, index = decoder.raw_decode(text, index)
                if not isinstance(key, str):
                    raise ValueError("object key is not a string")
                index = skip_whitespace(index)
                if index >= text_length or text[index] != ":":
                    raise ValueError("missing object colon")
                child_start = skip_whitespace(index + 1)
                child_path = path + (key,)
                child_end = scan_value(child_start, child_path, depth)
                validate_raw_value(child_path, child_start, child_end)
                index = skip_whitespace(child_end)
                if index < text_length and text[index] == "}":
                    end = index + 1
                    validate_raw_value(path, start, end)
                    return end
                if index >= text_length or text[index] != ",":
                    raise ValueError("missing object separator")
                index = skip_whitespace(index + 1)
        if text[index] == "[":
            depth += 1
            if depth > 64:
                raise ValidationFailure(
                    f"{source_name(source)}: JSON 嵌套深度超过启动器上限 64"
                )
            index = skip_whitespace(index + 1)
            item_index = 0
            if index < text_length and text[index] == "]":
                end = index + 1
                validate_raw_value(path, start, end)
                return end
            while True:
                child_start = index
                child_path = path + (item_index,)
                child_end = scan_value(child_start, child_path, depth)
                validate_raw_value(child_path, child_start, child_end)
                item_index += 1
                index = skip_whitespace(child_end)
                if index < text_length and text[index] == "]":
                    end = index + 1
                    validate_raw_value(path, start, end)
                    return end
                if index >= text_length or text[index] != ",":
                    raise ValueError("missing array separator")
                index = skip_whitespace(index + 1)
        _, end = decoder.raw_decode(text, index)
        validate_raw_value(path, start, end)
        return end

    try:
        end = scan_value(0, (), 0)
        if skip_whitespace(end) != text_length:
            raise ValueError("trailing JSON data")
    except ValidationFailure:
        raise
    except (ValueError, RecursionError) as exc:
        raise ValidationFailure(
            f"{source_name(source)}: 无法定位 settings defaultValue 原始 JSON：{exc}"
        ) from exc
    return raw_defaults


def load_object(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationFailure(f"{source_name(path)}: 无法读取 JSON：{exc}") from exc
    return parse_json_object(text, path)


def load_array(path: Path, *, missing: list | None = None) -> list:
    if not path.exists() and missing is not None:
        return copy.deepcopy(missing)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationFailure(f"{source_name(path)}: 无法读取 JSON：{exc}") from exc
    return parse_json_array(text, path)


def require_exact_keys(
    source: Path | str,
    value: dict,
    required: set[str],
    optional: set[str],
) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing:
        raise ValidationFailure(f"{source_name(source)}: 缺少字段 {sorted(missing)}")
    if extra:
        raise ValidationFailure(f"{source_name(source)}: 未知字段 {sorted(extra)}")


def require_text(
    source: Path | str,
    field: str,
    value: object,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    try:
        text_length = (
            len(value.encode("utf-16-le")) // 2 if isinstance(value, str) else -1
        )
    except UnicodeEncodeError:
        text_length = -1
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or not 0 <= text_length <= maximum
    ):
        raise ValidationFailure(f"{source_name(source)}: {field} 不是有效文本")
    return value


def require_list_of_text(
    source: Path | str,
    field: str,
    value: object,
    maximum: int,
    item_maximum: int = 256,
    *,
    minimum: int = 0,
) -> list[str]:
    valid = isinstance(value, list) and minimum <= len(value) <= maximum
    if valid:
        for item in value:
            try:
                item_length = (
                    len(item.encode("utf-16-le")) // 2
                    if isinstance(item, str)
                    else -1
                )
            except UnicodeEncodeError:
                item_length = -1
            if (
                not isinstance(item, str)
                or not item.strip()
                or not 0 <= item_length <= item_maximum
            ):
                valid = False
                break
    if not valid:
        raise ValidationFailure(f"{source_name(source)}: {field} 不是有效文本数组")
    if len({item.casefold() for item in value}) != len(value):
        raise ValidationFailure(f"{source_name(source)}: {field} 包含重复值")
    return list(value)


def validate_schema_reference(source: Path | str, value: dict) -> None:
    if "$schema" in value:
        require_text(source, "$schema", value["$schema"], 2048)


def match_semver(value: str) -> re.Match[str] | None:
    match = SEMVER.fullmatch(value)
    if match is None:
        return None
    if any(int(match.group(index)) > MAXIMUM_SEMVER_NUMBER for index in range(1, 4)):
        return None
    prerelease = match.group(4)
    if prerelease and any(
        identifier.isdigit()
        and (
            (len(identifier) > 1 and identifier.startswith("0"))
            or int(identifier) > MAXIMUM_SEMVER_NUMBER
        )
        for identifier in prerelease.split(".")
    ):
        return None
    return match


def validate_semver(source: Path | str, field: str, value: object) -> str:
    text = require_text(source, field, value, 64)
    if match_semver(text) is None:
        raise ValidationFailure(f"{source_name(source)}: {field} 必须是严格 SemVer")
    return text


def semver_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    match = match_semver(version)
    if not match:
        raise ValueError(version)
    prerelease = match.group(4)
    if prerelease is None:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), 1, ()
    identifiers: list[tuple[int, object]] = []
    for item in prerelease.split("."):
        identifiers.append((0, int(item)) if item.isdigit() else (1, item))
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        0,
        tuple(identifiers),
    )


def require_https(source: Path | str, field: str, value: object) -> str:
    text = require_text(source, field, value, 2048)
    try:
        parsed = urlparse(text)
        port = parsed.port
        hostname = parsed.hostname
    except (UnicodeError, ValueError) as exc:
        raise ValidationFailure(
            f"{source_name(source)}: {field} 必须是使用 443 端口的无凭据 HTTPS URL"
        ) from exc
    if (
        any(
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in text
        )
        or "\\" in text
        or re.search(r"%(?![0-9A-Fa-f]{2})", text)
    ):
        raise ValidationFailure(
            f"{source_name(source)}: {field} 包含 URL 不允许的空白、控制字符或转义"
        )
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii") if hostname else ""
        labels = ascii_hostname.rstrip(".").split(".")
        if any(label.lower().startswith("xn--") for label in labels):
            for label in labels:
                if label.lower().startswith("xn--"):
                    label.encode("ascii").decode("idna")
    except (UnicodeError, ValueError) as exc:
        raise ValidationFailure(
            f"{source_name(source)}: {field} 包含无效 IDNA 主机名"
        ) from exc
    valid_hostname = (
        bool(ascii_hostname)
        and not ascii_hostname.endswith(".")
        and len(ascii_hostname) <= 253
        and all(
            1 <= len(label) <= 63
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            is not None
            for label in labels
        )
    )
    if (
        parsed.scheme != "https"
        or not valid_hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValidationFailure(
            f"{source_name(source)}: {field} 必须是使用 443 端口的无凭据 HTTPS URL"
        )
    return text


def validate_utc_timestamp(source: Path | str, field: str, value: object) -> str:
    text = require_text(source, field, value, 64)
    if UTC_TIMESTAMP.fullmatch(text) is None:
        raise ValidationFailure(f"{source_name(source)}: {field} 必须是 UTC ISO 8601 秒级时间")
    try:
        datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValidationFailure(f"{source_name(source)}: {field} 不是有效 UTC 时间") from exc
    return text


def github_repository_parts(
    source: Path | str, field: str, value: object
) -> tuple[str, str, str]:
    url = require_https(source, field, value)
    match = GITHUB_REPO.fullmatch(url)
    if match is None:
        raise ValidationFailure(f"{source_name(source)}: {field} 必须是 GitHub 仓库根地址")
    owner = match.group(1)
    repository = match.group(2).removesuffix(".git")
    if (
        GITHUB_LOGIN.fullmatch(owner) is None
        or not 1 <= len(repository) <= 100
        or repository in {".", ".."}
        or repository.endswith(".")
    ):
        raise ValidationFailure(f"{source_name(source)}: {field} 包含无效 GitHub 仓库名称")
    return owner, repository, f"https://github.com/{owner}/{repository}"


def same_github_repository(left: str, right: str) -> bool:
    left_match = GITHUB_REPO.fullmatch(left)
    right_match = GITHUB_REPO.fullmatch(right)
    if left_match is None or right_match is None:
        return False
    return (
        left_match.group(1).casefold(),
        left_match.group(2).removesuffix(".git").casefold(),
    ) == (
        right_match.group(1).casefold(),
        right_match.group(2).removesuffix(".git").casefold(),
    )


def validate_github_repository_identity(
    value: object,
    repository_url: str,
    source: Path | str,
) -> tuple[int, int, str]:
    """Bind a mutable owner/name path to GitHub's immutable numeric identities."""

    if not isinstance(value, dict):
        raise ValidationFailure(f"{source_name(source)}: GitHub 仓库信息必须是对象")
    repository_id = value.get("id")
    owner = value.get("owner")
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    html_url = value.get("html_url")
    if (
        type(repository_id) is not int
        or not 1 <= repository_id <= 2**63 - 1
        or type(owner_id) is not int
        or not 1 <= owner_id <= 2**63 - 1
        or not isinstance(html_url, str)
    ):
        raise ValidationFailure(f"{source_name(source)}: GitHub numeric repository identity 无效")
    _, _, canonical_url = github_repository_parts(source, "html_url", html_url)
    if not same_github_repository(canonical_url, repository_url):
        raise ValidationFailure(
            f"{source_name(source)}: GitHub 仓库路径已重命名、转移或被其他仓库接管，需人工迁移"
        )
    if (
        value.get("private") is not False
        or value.get("fork") is not False
        or value.get("archived") is not False
        or value.get("disabled") is True
    ):
        raise ValidationFailure(
            f"{source_name(source)}: 自动发布仓库必须公开、非 Fork、未归档且可用"
        )
    return repository_id, owner_id, canonical_url


def fetch_github_repository_identity(
    repository_url: str, source: Path | str
) -> tuple[int, int, str]:
    owner, repository, canonical_url = github_repository_parts(
        source, "repositoryUrl", repository_url
    )
    request = urllib.request.Request(
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "NyaLauncher-Plugins-Validator/2.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(MAXIMUM_MANIFEST_BYTES + 1)
    except urllib.error.HTTPError as exc:
        failure = AvailabilityFailure if is_retryable_http_error(exc) else ValidationFailure
        raise failure(
            f"{source_name(source)}: 无法核验 GitHub 仓库身份：HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise AvailabilityFailure(
            f"{source_name(source)}: 无法核验 GitHub 仓库身份：{exc}"
        ) from exc
    if len(payload) > MAXIMUM_MANIFEST_BYTES:
        raise ValidationFailure(f"{source_name(source)}: GitHub 仓库身份响应超过 1 MiB")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationFailure(
            f"{source_name(source)}: GitHub 仓库身份响应必须是 UTF-8"
        ) from exc
    value = parse_json_object(text, f"{source_name(source)}::GitHub API")
    return validate_github_repository_identity(value, canonical_url, source)


def validate_fixed_release_zip(
    source: Path | str, repository_url: str, value: object
) -> str:
    url = require_https(source, "release.download.url", value)
    parsed = urlparse(url)
    owner, repository, _ = github_repository_parts(source, "repository_url", repository_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationFailure(f"{source_name(source)}: Release ZIP URL 端口无效") from exc
    if (
        (parsed.hostname or "").casefold() != "github.com"
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)
    ):
        raise ValidationFailure(
            f"{source_name(source)}: download.url 必须是插件仓库的固定 GitHub Release ZIP"
        )
    decoded_path = unquote(parsed.path)
    prefix = f"/{owner}/{repository}/releases/download/"
    if not parsed.path.startswith(prefix):
        raise ValidationFailure(
            f"{source_name(source)}: download.url 的 owner/repo 必须与 repository_url 大小写精确一致"
        )
    suffix = decoded_path[len(prefix) :]
    parts = suffix.split("/")
    if (
        len(parts) != 2
        or any(not part or part in {".", ".."} for part in parts)
        or "\\" in suffix
        or not parts[1].endswith(".zip")
    ):
        raise ValidationFailure(
            f"{source_name(source)}: download.url 必须固定到 releases/download/<tag>/<asset>.zip"
        )
    return url


def validate_release_notes_url(
    source: Path | str, repository_url: str, value: object
) -> str:
    """Require a tag-specific GitHub Release page from the publisher repo."""

    url = require_https(source, "release_notes_url", value)
    parsed = urlparse(url)
    owner, repository, _ = github_repository_parts(
        source, "repository_url", repository_url
    )
    prefix = f"/{owner}/{repository}/releases/tag/"
    if (
        ascii_fold(parsed.hostname or "") != "github.com"
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or not parsed.path.startswith(prefix)
    ):
        raise ValidationFailure(
            f"{source_name(source)}: release_notes_url 必须属于发布仓库的 GitHub Release tag"
        )
    tag = unquote(parsed.path[len(prefix) :])
    tag_segments = tag.split("/")
    if any(
        not segment or segment in {".", ".."} or "\\" in segment
        for segment in tag_segments
    ):
        raise ValidationFailure(
            f"{source_name(source)}: release_notes_url 必须包含非空 Release tag"
        )
    return url


def validate_capabilities(source: Path | str, field: str, value: object) -> list[str]:
    items = require_list_of_text(source, field, value, 64, item_maximum=128)
    unknown = set(items) - KNOWN_CAPABILITIES
    if unknown:
        raise ValidationFailure(
            f"{source_name(source)}: {field} 包含未知能力 {sorted(unknown)}"
        )
    return items


def load_plugin_list() -> list[dict]:
    """Load and strictly validate active publisher pointers from plugins.json."""

    values = load_array(ROOT / "plugins.json")
    if len(values) > MAXIMUM_PLUGIN_COUNT:
        raise ValidationFailure(f"plugins.json: 插件总数不能超过 {MAXIMUM_PLUGIN_COUNT}")
    result: list[dict] = []
    seen_ids: set[str] = set()
    seen_repositories: set[str] = set()
    seen_repository_ids: set[int] = set()
    for index, value in enumerate(values):
        source = f"plugins.json[{index}]"
        if not isinstance(value, dict):
            raise ValidationFailure(f"{source}: 条目必须是对象")
        require_exact_keys(
            source,
            value,
            {"id", "repositoryUrl", "repositoryId", "ownerId"},
            set(),
        )
        plugin_id = require_text(source, "id", value["id"], 128)
        if PLUGIN_ID.fullmatch(plugin_id) is None:
            raise ValidationFailure(f"{source}: id 必须是小写反向域名")
        _, _, repository_url = github_repository_parts(
            source, "repositoryUrl", value["repositoryUrl"]
        )
        repository_id = value["repositoryId"]
        owner_id = value["ownerId"]
        if type(repository_id) is not int or not 1 <= repository_id <= 2**63 - 1:
            raise ValidationFailure(f"{source}: repositoryId 必须是正 Int64")
        if type(owner_id) is not int or not 1 <= owner_id <= 2**63 - 1:
            raise ValidationFailure(f"{source}: ownerId 必须是正 Int64")
        if plugin_id.casefold() in seen_ids:
            raise ValidationFailure(f"{source}: 插件 ID 重复")
        if repository_url.casefold() in seen_repositories:
            raise ValidationFailure(f"{source}: 同一发布仓库不能重复收录")
        if repository_id in seen_repository_ids:
            raise ValidationFailure(f"{source}: 同一 GitHub repositoryId 不能重复收录")
        seen_ids.add(plugin_id.casefold())
        seen_repositories.add(repository_url.casefold())
        seen_repository_ids.add(repository_id)
        result.append(
            {
                "id": plugin_id,
                "repositoryUrl": repository_url,
                "repositoryId": repository_id,
                "ownerId": owner_id,
            }
        )
    return sorted(result, key=lambda item: item["id"].casefold())


def is_allowed_manifest_url(url: str) -> bool:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (parsed.hostname or "").casefold() == "raw.githubusercontent.com"
    )


class ManifestRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urljoin(req.full_url, newurl)
        if not is_allowed_manifest_url(target):
            raise ValidationFailure(f"发布清单重定向到不允许的地址：{target}")
        redirects = getattr(req, "redirect_dict", {})
        if len(redirects) >= 5:
            raise ValidationFailure("发布清单重定向次数超过 5 次")
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_repository_manifest(repository_url: str, source: str) -> dict:
    """Fetch an unbound repository's root ``_manifest.json`` safely.

    Discovery needs to read the manifest before it knows the claimed plugin
    ID.  Keeping that fetch here makes it use the same host, redirect, size,
    timeout, UTF-8, and duplicate-key rules as normal publisher refreshes.
    """

    source = require_text("repository manifest", "source", source, 2048)
    owner, repository, _ = github_repository_parts(
        source, "repositoryUrl", repository_url
    )
    url = (
        "https://raw.githubusercontent.com/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/HEAD/_manifest.json"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NyaLauncher-Plugins-Validator/2.0",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(ManifestRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response:
            if not is_allowed_manifest_url(response.geturl()):
                raise ValidationFailure(f"{source}: 发布清单最终地址不受允许")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAXIMUM_MANIFEST_BYTES:
                        raise ValidationFailure(f"{source}: _manifest.json 超过 1 MiB")
                except ValueError as exc:
                    raise ValidationFailure(
                        f"{source}: _manifest.json Content-Length 无效"
                    ) from exc
            payload = response.read(MAXIMUM_MANIFEST_BYTES + 1)
    except ValidationFailure:
        raise
    except urllib.error.HTTPError as exc:
        failure = AvailabilityFailure if is_retryable_http_error(exc) else ValidationFailure
        raise failure(
            f"{source}: 无法读取发布仓库根目录 _manifest.json：HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise AvailabilityFailure(
            f"{source}: 无法读取发布仓库根目录 _manifest.json：{exc}"
        ) from exc
    if len(payload) > MAXIMUM_MANIFEST_BYTES:
        raise ValidationFailure(f"{source}: _manifest.json 超过 1 MiB")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationFailure(f"{source}: _manifest.json 必须是 UTF-8") from exc
    return parse_json_object(text, f"{source}::_manifest.json")


def fetch_publisher_manifest(listing: dict) -> dict:
    """Fetch one publisher's root _manifest.json from its default branch."""

    if not isinstance(listing, dict):
        raise ValidationFailure("发布条目必须是对象")
    require_exact_keys(
        "publisher listing",
        listing,
        {"id", "repositoryUrl"},
        {"repositoryId", "ownerId"},
    )
    plugin_id = require_text("publisher listing", "id", listing["id"], 128)
    if PLUGIN_ID.fullmatch(plugin_id) is None:
        raise ValidationFailure("publisher listing: id 必须是小写反向域名")
    has_repository_id = "repositoryId" in listing
    has_owner_id = "ownerId" in listing
    if has_repository_id != has_owner_id:
        raise ValidationFailure("publisher listing: repositoryId 与 ownerId 必须同时存在")
    if has_repository_id:
        repository_id, owner_id, _ = fetch_github_repository_identity(
            listing["repositoryUrl"], plugin_id
        )
        if repository_id != listing["repositoryId"] or owner_id != listing["ownerId"]:
            raise ValidationFailure(
                f"{plugin_id}: GitHub numeric repository identity 已变化，拒绝自动同步"
            )
    return fetch_repository_manifest(listing["repositoryUrl"], plugin_id)


def validate_publisher_release(
    source: str,
    repository_url: str,
    value: object,
    index: int,
) -> dict:
    release_source = f"{source}.releases[{index}]"
    if not isinstance(value, dict):
        raise ValidationFailure(f"{release_source}: 必须是对象")
    require_exact_keys(
        release_source,
        value,
        {
            "version",
            "channel",
            "published_at",
            "release_notes_url",
            "download",
            "api_version",
            "minimum_launcher_version",
            "required_capabilities",
            "optional_capabilities",
        },
        {"maximum_launcher_version_exclusive"},
    )
    version = validate_semver(release_source, "version", value["version"])
    channel = value["channel"]
    if not isinstance(channel, str) or channel not in {"stable", "preview"}:
        raise ValidationFailure(f"{release_source}: channel 只能是 stable 或 preview")
    published_at = validate_utc_timestamp(
        release_source, "published_at", value["published_at"]
    )
    release_notes_url = validate_release_notes_url(
        release_source, repository_url, value["release_notes_url"]
    )

    api_version = require_text(release_source, "api_version", value["api_version"], 32)
    if API_VERSION.fullmatch(api_version) is None:
        raise ValidationFailure(f"{release_source}: 当前仅支持 API v1")
    minimum_launcher = validate_semver(
        release_source, "minimum_launcher_version", value["minimum_launcher_version"]
    )
    maximum_launcher = None
    if "maximum_launcher_version_exclusive" in value:
        maximum_launcher = validate_semver(
            release_source,
            "maximum_launcher_version_exclusive",
            value["maximum_launcher_version_exclusive"],
        )
        if semver_key(maximum_launcher) <= semver_key(minimum_launcher):
            raise ValidationFailure(
                f"{release_source}: maximum_launcher_version_exclusive 必须高于最低版本"
            )
    required_capabilities = validate_capabilities(
        release_source, "required_capabilities", value["required_capabilities"]
    )
    optional_capabilities = validate_capabilities(
        release_source, "optional_capabilities", value["optional_capabilities"]
    )
    if len(required_capabilities) + len(optional_capabilities) > 64:
        raise ValidationFailure(f"{release_source}: 能力声明合计不能超过 64 项")
    if {item.casefold() for item in required_capabilities} & {
        item.casefold() for item in optional_capabilities
    }:
        raise ValidationFailure(f"{release_source}: 必要与可选能力不能重复")

    download = value["download"]
    if not isinstance(download, dict):
        raise ValidationFailure(f"{release_source}: download 必须是对象")
    require_exact_keys(release_source, download, {"url", "sha256", "size"}, set())
    download_url = validate_fixed_release_zip(
        release_source, repository_url, download["url"]
    )
    sha256 = require_text(release_source, "download.sha256", download["sha256"], 64)
    if SHA256.fullmatch(sha256) is None:
        raise ValidationFailure(f"{release_source}: download.sha256 必须为 64 位小写十六进制")
    size = download["size"]
    if type(size) is not int or not 1 <= size <= MAXIMUM_PACKAGE_BYTES:
        raise ValidationFailure(f"{release_source}: download.size 必须在 1 B 到 256 MiB 之间")

    compatibility = {
        "manifestVersion": 1,
        "apiVersion": api_version,
        "minimumLauncherVersion": minimum_launcher,
    }
    if maximum_launcher is not None:
        compatibility["maximumLauncherVersionExclusive"] = maximum_launcher
    return {
        "version": version,
        "channel": channel,
        "publishedAt": published_at,
        "releaseNotesUrl": release_notes_url,
        "download": {"url": download_url, "sha256": sha256, "size": size},
        "compatibility": compatibility,
        "requiredCapabilities": required_capabilities,
        "optionalCapabilities": optional_capabilities,
        "yanked": False,
    }


def validate_publisher_manifest_releases(
    value: dict, listing: dict
) -> tuple[dict, list[dict]]:
    """Convert a complete publisher history into catalog objects."""

    source = f"{listing.get('id', '<unknown>')}::_manifest.json"
    if not isinstance(value, dict):
        raise ValidationFailure(f"{source}: JSON 根必须是对象")
    require_exact_keys(
        source,
        value,
        {
            "manifest_version",
            "id",
            "name",
            "description",
            "authors",
            "license",
            "repository_url",
            "maintainers",
            "categories",
            "releases",
        },
        {"$schema"},
    )
    validate_schema_reference(source, value)
    if type(value["manifest_version"]) is not int or value["manifest_version"] != 1:
        raise ValidationFailure(f"{source}: manifest_version 必须是 1")
    plugin_id = require_text(source, "id", value["id"], 128)
    if PLUGIN_ID.fullmatch(plugin_id) is None or plugin_id != listing.get("id"):
        raise ValidationFailure(f"{source}: id 必须合法且与 plugins.json 条目完全一致")
    _, _, repository_url = github_repository_parts(
        source, "repository_url", value["repository_url"]
    )
    listing_repository = require_text(
        source, "plugins.json.repositoryUrl", listing.get("repositoryUrl"), 2048
    )
    if not same_github_repository(repository_url, listing_repository):
        raise ValidationFailure(f"{source}: repository_url 必须与 plugins.json 指向同一仓库")
    authors = require_list_of_text(
        source, "authors", value["authors"], 64, item_maximum=256, minimum=1
    )
    maintainers = require_list_of_text(
        source, "maintainers", value["maintainers"], 16, item_maximum=39, minimum=1
    )
    if any(GITHUB_LOGIN.fullmatch(item) is None for item in maintainers):
        raise ValidationFailure(f"{source}: maintainers 必须是有效 GitHub 用户名")
    categories = require_list_of_text(
        source, "categories", value["categories"], 8, minimum=1
    )
    if set(categories) - CATEGORIES:
        raise ValidationFailure(f"{source}: categories 包含未知分类")
    plugin = {
        "id": plugin_id,
        "name": require_text(source, "name", value["name"], 256),
        "description": require_text(source, "description", value["description"], 8192),
        "authors": authors,
        "repositoryUrl": repository_url,
        "maintainers": maintainers,
        "categories": categories,
        "license": require_text(source, "license", value["license"], 256),
    }

    publisher_releases = value["releases"]
    if (
        not isinstance(publisher_releases, list)
        or not 1 <= len(publisher_releases) <= MAXIMUM_RELEASE_COUNT
    ):
        raise ValidationFailure(
            f"{source}: releases 必须包含 1 到 {MAXIMUM_RELEASE_COUNT} 个完整历史版本"
        )
    releases = [
        validate_publisher_release(source, repository_url, release, index)
        for index, release in enumerate(publisher_releases)
    ]
    if sum(release["download"]["size"] for release in releases) > (
        MAXIMUM_PUBLISHER_HISTORY_BYTES
    ):
        raise ValidationFailure(f"{source}: releases 声明的历史资产大小合计不能超过 4 GiB")
    for previous, current in zip(releases, releases[1:]):
        if semver_key(previous["version"]) >= semver_key(current["version"]):
            raise ValidationFailure(
                f"{source}: releases 必须按严格 SemVer 升序排列，且不能有相同优先级版本"
            )
    return plugin, releases


def validate_publisher_manifest(value: dict, listing: dict) -> tuple[dict, dict]:
    """Compatibility API returning stable metadata and the latest release."""

    plugin, releases = validate_publisher_manifest_releases(value, listing)
    return plugin, releases[-1]


def validate_plugin(path: Path, directory_name: str) -> dict:
    value = load_object(path)
    require_exact_keys(
        path,
        value,
        {
            "schemaVersion",
            "id",
            "name",
            "description",
            "authors",
            "repositoryUrl",
            "maintainers",
            "categories",
            "license",
        },
        {"$schema"},
    )
    validate_schema_reference(path, value)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValidationFailure(f"{source_name(path)}: schemaVersion 必须是 1")
    plugin_id = require_text(path, "id", value["id"], 128)
    if PLUGIN_ID.fullmatch(plugin_id) is None or plugin_id != directory_name:
        raise ValidationFailure(f"{source_name(path)}: id 必须合法且与目录名一致")
    require_text(path, "name", value["name"], 256)
    require_text(path, "description", value["description"], 8192)
    require_list_of_text(
        path, "authors", value["authors"], 64, item_maximum=256, minimum=1
    )
    github_repository_parts(path, "repositoryUrl", value["repositoryUrl"])
    maintainers = require_list_of_text(
        path, "maintainers", value["maintainers"], 16, item_maximum=39, minimum=1
    )
    if any(GITHUB_LOGIN.fullmatch(item) is None for item in maintainers):
        raise ValidationFailure(f"{source_name(path)}: maintainers 必须是有效 GitHub 用户名")
    categories = require_list_of_text(path, "categories", value["categories"], 8, minimum=1)
    if set(categories) - CATEGORIES:
        raise ValidationFailure(f"{source_name(path)}: categories 包含未知分类")
    require_text(path, "license", value["license"], 256)
    result = copy.deepcopy(value)
    result.pop("$schema", None)
    result.pop("schemaVersion", None)
    return result


def validate_release(path: Path, file_version: str, plugin: dict) -> dict:
    value = load_object(path)
    require_exact_keys(
        path,
        value,
        {
            "schemaVersion",
            "version",
            "channel",
            "publishedAt",
            "releaseNotesUrl",
            "download",
            "compatibility",
            "requiredCapabilities",
            "optionalCapabilities",
            "yanked",
        },
        {"$schema", "yankReason"},
    )
    validate_schema_reference(path, value)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValidationFailure(f"{source_name(path)}: schemaVersion 必须是 1")
    version = validate_semver(path, "version", value["version"])
    if version != file_version:
        raise ValidationFailure(f"{source_name(path)}: version 必须与文件名一致")
    if not isinstance(value["channel"], str) or value["channel"] not in {"stable", "preview"}:
        raise ValidationFailure(f"{source_name(path)}: channel 只能是 stable 或 preview")
    validate_utc_timestamp(path, "publishedAt", value["publishedAt"])
    require_https(path, "releaseNotesUrl", value["releaseNotesUrl"])
    download = value["download"]
    if not isinstance(download, dict):
        raise ValidationFailure(f"{source_name(path)}: download 必须是对象")
    require_exact_keys(path, download, {"url", "sha256", "size"}, set())
    validate_fixed_release_zip(path, plugin["repositoryUrl"], download["url"])
    if not isinstance(download["sha256"], str) or SHA256.fullmatch(download["sha256"]) is None:
        raise ValidationFailure(f"{source_name(path)}: sha256 必须是 64 位小写十六进制")
    if type(download["size"]) is not int or not 1 <= download["size"] <= MAXIMUM_PACKAGE_BYTES:
        raise ValidationFailure(f"{source_name(path)}: 下载大小必须在 1 B 到 256 MiB 之间")
    compatibility = value["compatibility"]
    if not isinstance(compatibility, dict):
        raise ValidationFailure(f"{source_name(path)}: compatibility 必须是对象")
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
        or API_VERSION.fullmatch(compatibility["apiVersion"]) is None
    ):
        raise ValidationFailure(f"{source_name(path)}: 当前仅支持 manifest v1 / API v1")
    minimum = validate_semver(path, "minimumLauncherVersion", compatibility["minimumLauncherVersion"])
    if "maximumLauncherVersionExclusive" in compatibility:
        maximum = validate_semver(
            path,
            "maximumLauncherVersionExclusive",
            compatibility["maximumLauncherVersionExclusive"],
        )
        if semver_key(maximum) <= semver_key(minimum):
            raise ValidationFailure(
                f"{source_name(path)}: maximumLauncherVersionExclusive 必须高于最低版本"
            )
    required = validate_capabilities(path, "requiredCapabilities", value["requiredCapabilities"])
    optional = validate_capabilities(path, "optionalCapabilities", value["optionalCapabilities"])
    if len(required) + len(optional) > 64:
        raise ValidationFailure(f"{source_name(path)}: 能力声明合计不能超过 64 项")
    if {item.casefold() for item in required} & {item.casefold() for item in optional}:
        raise ValidationFailure(f"{source_name(path)}: 必要与可选能力不能重复")
    if not isinstance(value["yanked"], bool):
        raise ValidationFailure(f"{source_name(path)}: yanked 必须是布尔值")
    if value["yanked"]:
        require_text(path, "yankReason", value.get("yankReason"), 1024)
    elif "yankReason" in value:
        raise ValidationFailure(f"{source_name(path)}: 未撤回版本不能设置 yankReason")
    result = copy.deepcopy(value)
    result.pop("$schema", None)
    result.pop("schemaVersion", None)
    return result


def load_catalog() -> list[dict]:
    """Load the authoritative central plugin and release history."""

    plugins_root = ROOT / "plugins"
    if not plugins_root.is_dir() or plugins_root.is_symlink():
        raise ValidationFailure("plugins/: 中心历史目录不存在")
    plugin_directories = sorted(
        (item for item in plugins_root.iterdir() if item.is_dir()),
        key=lambda item: item.name.casefold(),
    )
    if len(plugin_directories) > MAXIMUM_PLUGIN_COUNT:
        raise ValidationFailure(f"插件总数不能超过 {MAXIMUM_PLUGIN_COUNT}")
    result: list[dict] = []
    seen_ids: set[str] = set()
    for directory in plugin_directories:
        if directory.is_symlink():
            raise ValidationFailure(f"plugins/{directory.name}: 插件目录不能是符号链接")
        unexpected = {
            item.name for item in directory.iterdir() if item.name not in {"plugin.json", "releases"}
        }
        if unexpected:
            raise ValidationFailure(
                f"plugins/{directory.name}: 包含未知中心历史条目 {sorted(unexpected)}"
            )
        plugin_path = directory / "plugin.json"
        if not plugin_path.is_file() or plugin_path.is_symlink():
            raise ValidationFailure(f"plugins/{directory.name}: 缺少 plugin.json")
        plugin = validate_plugin(plugin_path, directory.name)
        if plugin["id"].casefold() in seen_ids:
            raise ValidationFailure(f"plugins/{directory.name}: 插件 ID 重复")
        seen_ids.add(plugin["id"].casefold())
        releases_root = directory / "releases"
        if not releases_root.is_dir() or releases_root.is_symlink():
            raise ValidationFailure(f"plugins/{directory.name}: 缺少 releases 目录")
        for item in releases_root.iterdir():
            if not item.is_file() or item.is_symlink() or item.suffix != ".json":
                raise ValidationFailure(
                    f"{source_name(item)}: releases 目录只能包含普通 JSON 文件"
                )
        release_files = list(releases_root.glob("*.json"))
        if not release_files:
            raise ValidationFailure(f"plugins/{directory.name}: 至少需要一个历史版本")
        if len(release_files) > MAXIMUM_RELEASE_COUNT:
            raise ValidationFailure(
                f"plugins/{directory.name}: 版本数不能超过 {MAXIMUM_RELEASE_COUNT}"
            )
        for path in release_files:
            if match_semver(path.stem) is None:
                raise ValidationFailure(f"{source_name(path)}: 文件名必须是严格 SemVer")
        precedence = [semver_key(path.stem) for path in release_files]
        if len(set(precedence)) != len(precedence):
            raise ValidationFailure(
                f"plugins/{directory.name}: 不能收录仅构建元数据不同、优先级相同的版本"
            )
        release_files.sort(key=lambda path: (semver_key(path.stem), path.stem), reverse=True)
        plugin["releases"] = [
            validate_release(path, path.stem, plugin) for path in release_files
        ]
        result.append(plugin)
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
            "stateBy",
            "stateAt",
            "lastCommandAt",
            "lastCommentId",
        },
        {"$schema", "notes"},
    )
    validate_schema_reference(path, value)
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValidationFailure(f"{source_name(path)}: schemaVersion 必须是 1")
    plugin_id = require_text(path, "pluginId", value["pluginId"], 128)
    if plugin_id != expected_plugin_id or PLUGIN_ID.fullmatch(plugin_id) is None:
        raise ValidationFailure(f"{source_name(path)}: pluginId 与审核目录不一致")
    version = validate_semver(path, "version", value["version"])
    if version != expected_version:
        raise ValidationFailure(f"{source_name(path)}: version 与审核文件名不一致")
    sha256 = require_text(path, "sha256", value["sha256"], 64)
    if SHA256.fullmatch(sha256) is None or sha256 != expected_sha256:
        raise ValidationFailure(
            f"{source_name(path)}: sha256 必须与被审核 Release 的固定哈希完全一致"
        )
    status = value["status"]
    if not isinstance(status, str) or status not in {"verified", "revoked"}:
        raise ValidationFailure(f"{source_name(path)}: status 必须是 verified 或 revoked")
    state_by = require_text(path, "stateBy", value["stateBy"], 39)
    if GITHUB_LOGIN.fullmatch(state_by) is None or state_by.casefold() not in trusted_reviewers:
        raise ValidationFailure(f"{source_name(path)}: stateBy 不在 trustedReviewers 中")
    state_at = validate_utc_timestamp(path, "stateAt", value["stateAt"])
    last_command_at = validate_utc_timestamp(
        path, "lastCommandAt", value["lastCommandAt"]
    )
    last_comment_id = value["lastCommentId"]
    if type(last_comment_id) is not int or not (1 <= last_comment_id <= 2**63 - 1):
        raise ValidationFailure(f"{source_name(path)}: lastCommentId 必须是正 Int64")
    if last_command_at < state_at:
        raise ValidationFailure(f"{source_name(path)}: lastCommandAt 不能早于 stateAt")
    notes = None
    if "notes" in value:
        notes = require_text(path, "notes", value["notes"], 4096, allow_empty=True)
    result = {
        "status": status,
        "sha256": sha256,
        "stateBy": state_by,
        "stateAt": state_at,
        "lastCommandAt": last_command_at,
        "lastCommentId": last_comment_id,
    }
    if notes is not None:
        result["notes"] = notes
    return result


def load_repository_configuration() -> tuple[dict, set[str]]:
    path = ROOT / "repository.json"
    value = load_object(path)
    require_exact_keys(
        path,
        value,
        {
            "schemaVersion",
            "name",
            "sourceUrl",
            "launcherUrl",
            "indexPath",
            "registryBotLogin",
            "trustedReviewers",
        },
        {"$schema"},
    )
    validate_schema_reference(path, value)
    if (
        type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or value["indexPath"] != "public/v1/index.json"
    ):
        raise ValidationFailure("repository.json: 不支持的仓库配置")
    require_text(path, "name", value["name"], 128)
    _, _, source_url = github_repository_parts(path, "sourceUrl", value["sourceUrl"])
    value["sourceUrl"] = source_url
    require_https(path, "launcherUrl", value["launcherUrl"])
    bot_login = require_text(path, "registryBotLogin", value["registryBotLogin"], 44)
    if GITHUB_APP_BOT_LOGIN.fullmatch(bot_login) is None:
        raise ValidationFailure("repository.json: registryBotLogin 必须是 GitHub App [bot] 登录名")
    reviewers = require_list_of_text(
        path,
        "trustedReviewers",
        value["trustedReviewers"],
        32,
        item_maximum=39,
        minimum=1,
    )
    if any(GITHUB_LOGIN.fullmatch(item) is None for item in reviewers):
        raise ValidationFailure("repository.json: trustedReviewers 必须包含有效 GitHub 用户名")
    return value, {item.casefold() for item in reviewers}


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
        if (
            directory.is_symlink()
            or PLUGIN_ID.fullmatch(plugin_id) is None
            or plugin_id not in plugin_ids
        ):
            raise ValidationFailure(f"reviews/{plugin_id}: 审核目录没有对应的历史插件")
        for item in directory.iterdir():
            if not item.is_file() or item.is_symlink() or item.suffix != ".json":
                raise ValidationFailure(f"{source_name(item)}: 审核目录只能包含版本 JSON 文件")
        for review_path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            version = review_path.stem
            if match_semver(version) is None:
                raise ValidationFailure(f"{source_name(review_path)}: 文件名必须是严格 SemVer")
            release = releases_by_key.get((plugin_id, version))
            if release is None:
                raise ValidationFailure(f"{source_name(review_path)}: 没有对应的中心历史版本")
            if "review" in release:
                raise ValidationFailure(f"{source_name(review_path)}: 版本审核重复")
            review_record = validate_review(
                review_path,
                plugin_id,
                version,
                release["download"]["sha256"],
                trusted_reviewers,
            )
            # Revocation records remain durable ordering tombstones but never
            # enter the launcher contract. A yanked release is likewise never
            # shown as verified.
            if review_record["status"] == "verified" and not release["yanked"]:
                public_review = {
                    "status": "verified",
                    "sha256": review_record["sha256"],
                    "reviewedBy": review_record["stateBy"],
                    "reviewedAt": review_record["stateAt"],
                }
                if "notes" in review_record:
                    public_review["notes"] = review_record["notes"]
                release["review"] = public_review


def build_details() -> list[dict]:
    """Build the review-free generated catalog view from central history."""

    return copy.deepcopy(load_catalog())


def validate_active_catalog(listings: list[dict], plugins: list[dict]) -> None:
    """Ensure active pointers and archived central history cannot drift apart."""

    active_by_id = {listing["id"]: listing for listing in listings}
    catalog_by_id = {plugin["id"]: plugin for plugin in plugins}
    missing = sorted(set(active_by_id) - set(catalog_by_id), key=str.casefold)
    if missing:
        raise ValidationFailure(
            f"plugins.json: active 插件缺少中心历史：{', '.join(missing)}；请先执行 --refresh --write"
        )
    for plugin_id, plugin in catalog_by_id.items():
        listing = active_by_id.get(plugin_id)
        if listing is None:
            if any(not release["yanked"] for release in plugin["releases"]):
                raise ValidationFailure(
                    f"{plugin_id}: 已从 plugins.json 移除的归档插件必须先撤回全部历史版本"
                )
            continue
        if not same_github_repository(plugin["repositoryUrl"], listing["repositoryUrl"]):
            raise ValidationFailure(
                f"{plugin_id}: plugins.json repositoryUrl 与中心历史仓库不一致"
            )


def build_index(plugins: list[dict] | None = None) -> dict:
    """Build the launcher contract and inject only trusted hash-bound reviews."""

    repository, trusted_reviewers = load_repository_configuration()
    result_plugins = copy.deepcopy(load_catalog() if plugins is None else plugins)
    validate_active_catalog(load_plugin_list(), result_plugins)
    attach_reviews(result_plugins, trusted_reviewers)
    return {
        "schemaVersion": 1,
        "name": repository["name"],
        "sourceUrl": repository["sourceUrl"],
        "plugins": result_plugins,
    }


def public_plugin_to_source(plugin: dict) -> dict:
    body = {key: copy.deepcopy(value) for key, value in plugin.items() if key != "releases"}
    return {
        "$schema": "../../schemas/catalog-plugin-v1.schema.json",
        "schemaVersion": 1,
        **body,
    }


def public_release_to_source(release: dict) -> dict:
    return {
        "$schema": "../../../schemas/catalog-release-v1.schema.json",
        "schemaVersion": 1,
        **copy.deepcopy(release),
    }


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_existing_details_history(existing_details: list, catalog: list[dict]) -> None:
    if not isinstance(existing_details, list):
        raise ValidationFailure("plugin_details.json: JSON 根必须是数组")
    catalog_versions = {
        (plugin["id"], release["version"])
        for plugin in catalog
        for release in plugin["releases"]
    }
    for plugin in existing_details:
        if not isinstance(plugin, dict):
            raise ValidationFailure("plugin_details.json: 插件条目必须是对象")
        plugin_id = plugin.get("id")
        releases = plugin.get("releases")
        if not isinstance(plugin_id, str) or not isinstance(releases, list):
            raise ValidationFailure("plugin_details.json: 插件历史结构无效")
        for release in releases:
            version = release.get("version") if isinstance(release, dict) else None
            if not isinstance(version, str) or (plugin_id, version) not in catalog_versions:
                raise ValidationFailure(
                    f"plugin_details.json: 历史 {plugin_id} {version!r} 在中心目录中丢失，拒绝静默删除"
                )


def immutable_release_identity(release: dict) -> dict:
    """Return publisher-owned fields, excluding administrator yank state."""

    return {
        key: copy.deepcopy(value)
        for key, value in release.items()
        if key not in {"yanked", "yankReason", "review"}
    }


def verify_new_release_candidate(plugin: dict, release: dict) -> None:
    """Verify a publisher candidate before it can become central history."""

    payload = download_release_asset(plugin, release)
    validate_runtime_package(plugin, release, payload)
    print(f"新增候选资产通过：{plugin['id']} {release['version']}")


def publisher_missing_releases(
    plugin: dict,
    releases: list[dict],
    existing: dict | None,
) -> list[dict]:
    """Validate complete history and return every release missing centrally."""

    if existing is None:
        missing_releases = list(releases)
    else:
        if not same_github_repository(
            existing["repositoryUrl"], plugin["repositoryUrl"]
        ):
            raise ValidationFailure(
                f"{plugin['id']}: plugins.json 不能把已收录 ID 转移到另一个仓库"
            )
        publisher_by_version = {release["version"]: release for release in releases}
        central_by_version = {
            release["version"]: release for release in existing["releases"]
        }
        missing = sorted(
            set(central_by_version) - set(publisher_by_version),
            key=semver_key,
        )
        if missing:
            raise ValidationFailure(
                f"{plugin['id']}: _manifest.json 遗漏已知历史版本：{', '.join(missing)}"
            )
        for version, historical in central_by_version.items():
            if immutable_release_identity(historical) != immutable_release_identity(
                publisher_by_version[version]
            ):
                raise ValidationFailure(
                    f"{plugin['id']} {version}: 已存在同版本快照不可修改"
                )
        missing_releases = [
            release
            for release in releases
            if release["version"] not in central_by_version
        ]
        current_plugin = {
            key: value for key, value in existing.items() if key != "releases"
        }
        if current_plugin != plugin:
            raise ValidationFailure(
                f"{plugin['id']}: 顶层稳定插件元数据在首次收录后不可修改"
            )
        if not any(not release["yanked"] for release in existing["releases"]):
            if not missing_releases:
                raise ValidationFailure(
                    f"{plugin['id']}: active 插件历史已全部撤回，应发布新版本或从 plugins.json 归档"
                )
            highest_historical = max(
                existing["releases"], key=lambda item: semver_key(item["version"])
            )
            if not any(
                semver_key(release["version"])
                > semver_key(highest_historical["version"])
                for release in missing_releases
            ):
                raise ValidationFailure(
                    f"{plugin['id']}: 全部历史已撤回，重新激活必须发布高于历史最高版本 "
                    f"{highest_historical['version']} 的新版本"
                )

    return missing_releases


def plan_publisher_candidates(
    plugin: dict,
    releases: list[dict],
    existing: dict | None,
    *,
    maximum_candidates: int | None = None,
    maximum_candidate_bytes: int | None = None,
) -> list[dict]:
    """Select one bounded newest-first catch-up batch, returned ascending."""

    candidate_limit = (
        MAXIMUM_NEW_RELEASE_COUNT
        if maximum_candidates is None
        else maximum_candidates
    )
    byte_limit = (
        MAXIMUM_NEW_RELEASE_BYTES
        if maximum_candidate_bytes is None
        else maximum_candidate_bytes
    )
    if (
        type(candidate_limit) is not int
        or type(byte_limit) is not int
        or candidate_limit < 0
        or byte_limit < 0
    ):
        raise ValidationFailure("发布候选批次预算无效")
    missing = publisher_missing_releases(plugin, releases, existing)
    selected_descending: list[dict] = []
    declared_bytes = 0
    for release in reversed(missing):
        size = release["download"]["size"]
        if len(selected_descending) >= candidate_limit:
            break
        if declared_bytes + size > byte_limit:
            break
        selected_descending.append(release)
        declared_bytes += size
    return list(reversed(selected_descending))


def merge_publisher_snapshot(
    by_id: dict[str, dict],
    listing: dict,
    *,
    maximum_candidates: int | None = None,
    maximum_candidate_bytes: int | None = None,
) -> tuple[dict | None, list[dict]]:
    """Fetch and atomically merge one publisher's complete release history."""

    remote = fetch_publisher_manifest(listing)
    plugin, releases = validate_publisher_manifest_releases(remote, listing)
    existing = by_id.get(plugin["id"])
    candidates = plan_publisher_candidates(
        plugin,
        releases,
        existing,
        maximum_candidates=maximum_candidates,
        maximum_candidate_bytes=maximum_candidate_bytes,
    )
    if not candidates:
        return None, []

    # Verify every missing ZIP before mutating even the in-memory central view.
    # refresh_details writes only after all publishers have been planned.
    attempted_count = 0
    attempted_bytes = 0
    for release in candidates:
        attempted_count += 1
        attempted_bytes += release["download"]["size"]
        try:
            verify_new_release_candidate(plugin, release)
        except ValidationFailure as exc:
            raise PublisherCandidateFailure(
                str(exc),
                attempted_count,
                attempted_bytes,
                retryable=isinstance(exc, AvailabilityFailure),
            ) from exc

    if existing is None:
        merged = copy.deepcopy(plugin)
        merged["releases"] = copy.deepcopy(candidates)
        by_id[plugin["id"]] = merged
    else:
        existing.update(copy.deepcopy(plugin))
        existing["releases"].extend(copy.deepcopy(candidates))
        existing["releases"].sort(
            key=lambda item: (semver_key(item["version"]), item["version"]),
            reverse=True,
        )
    return plugin, candidates


def emit_refresh_warning(message: str) -> None:
    print(f"刷新警告：{message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning title=Publisher refresh skipped::{escaped}")


def order_refresh_listings(
    listings: list[dict],
    catalog: list[dict],
    priority_ids: list[str] | None = None,
) -> list[dict]:
    """Prioritize an approval target/new pointers, then rotate publishers."""

    target = os.environ.get("NYA_REFRESH_TARGET")
    if target:
        matches = [listing for listing in listings if listing["id"] == target]
        if len(matches) != 1:
            raise ValidationFailure(
                "NYA_REFRESH_TARGET 必须精确指向一个 active 插件"
            )
        # Approval is a targeted write transaction.  Do not let unrelated
        # publisher latency prevent its already-validated request from landing.
        return matches

    priority_rank = {
        plugin_id.casefold(): index
        for index, plugin_id in enumerate(priority_ids or [])
    }
    prioritized = sorted(
        (
            listing
            for listing in listings
            if listing["id"].casefold() in priority_rank
        ),
        key=lambda item: priority_rank[item["id"].casefold()],
    )
    prioritized_ids = {listing["id"].casefold() for listing in prioritized}
    catalog_ids = {plugin["id"].casefold() for plugin in catalog}
    new_listings = sorted(
        (
            listing
            for listing in listings
            if listing["id"].casefold() not in catalog_ids
            and listing["id"].casefold() not in prioritized_ids
        ),
        key=lambda item: item["id"].casefold(),
    )
    known_listings = sorted(
        (
            listing
            for listing in listings
            if listing["id"].casefold() in catalog_ids
            and listing["id"].casefold() not in prioritized_ids
        ),
        key=lambda item: item["id"].casefold(),
    )
    raw_offset = os.environ.get("NYA_REFRESH_OFFSET", "0")
    try:
        offset = int(raw_offset, 10)
    except ValueError as exc:
        raise ValidationFailure("NYA_REFRESH_OFFSET 必须是整数") from exc
    if known_listings:
        offset %= len(known_listings)
        known_listings = known_listings[offset:] + known_listings[:offset]
    return prioritized + new_listings + known_listings


def refresh_details(
    listings: list[dict],
    existing_details: list,
    *,
    write: bool = False,
    best_effort: bool = False,
    warnings: list[str] | None = None,
    priority_ids: list[str] | None = None,
    retryable_failures: set[str] | None = None,
) -> list[dict]:
    """Fetch publishers and append immutable central snapshots.

    Every central version must remain in the publisher history with identical
    publisher-owned fields.  Missing versions may be backfilled at any SemVer
    position.  Issue approval uses fail-hard candidate verification; scheduled
    refresh may opt into per-publisher best-effort isolation.
    """

    catalog = load_catalog()
    ensure_existing_details_history(existing_details, catalog)
    by_id = {plugin["id"]: copy.deepcopy(plugin) for plugin in catalog}
    pending_plugins: dict[str, dict] = {}
    pending_releases: dict[tuple[str, str], dict] = {}
    remaining_candidates = MAXIMUM_NEW_RELEASE_COUNT
    remaining_candidate_bytes = MAXIMUM_NEW_RELEASE_BYTES
    catalog_ids = {plugin["id"].casefold() for plugin in catalog}
    has_active_publishers = any(
        listing["id"].casefold() in catalog_ids for listing in listings
    )
    has_discovery_publishers = any(
        listing["id"].casefold() not in catalog_ids for listing in listings
    )
    reserve_for_active = (
        has_active_publishers
        and has_discovery_publishers
        and not os.environ.get("NYA_REFRESH_TARGET")
    )
    if reserve_for_active:
        active_candidate_reserve = max(1, MAXIMUM_NEW_RELEASE_COUNT // 2)
        active_byte_reserve = max(1, MAXIMUM_NEW_RELEASE_BYTES // 2)
        discovery_candidates = max(
            0, MAXIMUM_NEW_RELEASE_COUNT - active_candidate_reserve
        )
        discovery_candidate_bytes = max(
            0, MAXIMUM_NEW_RELEASE_BYTES - active_byte_reserve
        )
    else:
        discovery_candidates = remaining_candidates
        discovery_candidate_bytes = remaining_candidate_bytes
    processed_publishers = 0
    for listing in order_refresh_listings(listings, catalog, priority_ids):
        if (
            remaining_candidates <= 0
            or remaining_candidate_bytes <= 0
            or processed_publishers >= MAXIMUM_REFRESH_PUBLISHERS
        ):
            break
        listing_id = listing["id"].casefold()
        is_discovery = listing_id not in catalog_ids
        candidate_budget = remaining_candidates
        candidate_byte_budget = remaining_candidate_bytes
        if reserve_for_active and is_discovery:
            candidate_budget = min(candidate_budget, discovery_candidates)
            candidate_byte_budget = min(
                candidate_byte_budget, discovery_candidate_bytes
            )
            if candidate_budget <= 0 or candidate_byte_budget <= 0:
                continue
        # Every attempted publisher consumes the network-attempt budget,
        # including unreachable, invalid, or already-current manifests.
        processed_publishers += 1
        try:
            plugin, releases = merge_publisher_snapshot(
                by_id,
                listing,
                maximum_candidates=candidate_budget,
                maximum_candidate_bytes=candidate_byte_budget,
            )
        except PublisherCandidateFailure as exc:
            if (
                exc.attempted_count > candidate_budget
                or exc.attempted_bytes > candidate_byte_budget
            ):
                raise ValidationFailure("发布候选失败批次超过本轮剩余全局预算") from exc
            remaining_candidates -= exc.attempted_count
            remaining_candidate_bytes -= exc.attempted_bytes
            if reserve_for_active and is_discovery:
                discovery_candidates -= exc.attempted_count
                discovery_candidate_bytes -= exc.attempted_bytes
            if exc.retryable and retryable_failures is not None:
                retryable_failures.add(listing_id)
            if not best_effort:
                raise
            message = f"{listing.get('id', '<unknown>')}：{exc}；保留原中心历史"
            if warnings is not None:
                warnings.append(message)
            emit_refresh_warning(message)
            continue
        except AvailabilityFailure as exc:
            if retryable_failures is not None:
                retryable_failures.add(listing_id)
            if not best_effort:
                raise
            message = f"{listing.get('id', '<unknown>')}：{exc}；保留原中心历史"
            if warnings is not None:
                warnings.append(message)
            emit_refresh_warning(message)
            continue
        except ValidationFailure as exc:
            if not best_effort:
                raise
            message = f"{listing.get('id', '<unknown>')}：{exc}；保留原中心历史"
            if warnings is not None:
                warnings.append(message)
            emit_refresh_warning(message)
            continue
        if plugin is not None and releases:
            used_candidates = len(releases)
            used_bytes = sum(release["download"]["size"] for release in releases)
            if (
                used_candidates > candidate_budget
                or used_bytes > candidate_byte_budget
            ):
                raise ValidationFailure(
                    f"{plugin['id']}: 发布候选超过本轮剩余全局预算"
                )
            remaining_candidates -= used_candidates
            remaining_candidate_bytes -= used_bytes
            if reserve_for_active and is_discovery:
                discovery_candidates -= used_candidates
                discovery_candidate_bytes -= used_bytes
            pending_plugins[plugin["id"]] = plugin
            for release in releases:
                pending_releases[(plugin["id"], release["version"])] = release
    result = sorted(by_id.values(), key=lambda item: item["id"].casefold())
    if len(result) > MAXIMUM_PLUGIN_COUNT:
        raise ValidationFailure(
            f"中心历史与新收录插件合计不能超过 {MAXIMUM_PLUGIN_COUNT}"
        )
    if write:
        for plugin_id in sorted(pending_plugins, key=str.casefold):
            path = ROOT / "plugins" / plugin_id / "plugin.json"
            write_text_atomic(
                path, canonical_json(public_plugin_to_source(pending_plugins[plugin_id]))
            )
        for plugin_id, version in sorted(
            pending_releases,
            key=lambda item: (item[0].casefold(), semver_key(item[1])),
        ):
            path = ROOT / "plugins" / plugin_id / "releases" / f"{version}.json"
            if path.exists():
                raise ValidationFailure(f"{source_name(path)}: 历史文件已存在，拒绝覆盖")
            write_text_atomic(
                path,
                canonical_json(public_release_to_source(pending_releases[(plugin_id, version)])),
            )
    return result


def is_allowed_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
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


def download_release_asset(plugin: dict, release: dict) -> bytes:
    """Download a fixed GitHub Release ZIP and verify size and SHA-256."""

    url = release["download"]["url"]
    if not is_allowed_asset_url(url) or (urlparse(url).hostname or "").casefold() != "github.com":
        raise ValidationFailure(f"{plugin['id']} {release['version']}: 下载地址不受允许")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NyaLauncher-Plugins-Validator/2.0",
            "Accept": "application/octet-stream",
        },
    )
    opener = urllib.request.build_opener(SafeRedirectHandler())
    expected_size = release["download"]["size"]
    try:
        with opener.open(request, timeout=60) as response:
            if not is_allowed_asset_url(response.geturl()):
                raise ValidationFailure(
                    f"{plugin['id']} {release['version']}: 最终下载地址不受允许"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise ValidationFailure(
                        f"{plugin['id']} {release['version']}: Content-Length 无效"
                    ) from exc
                if declared_length != expected_size:
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
    except ValidationFailure:
        raise
    except urllib.error.HTTPError as exc:
        failure = AvailabilityFailure if is_retryable_http_error(exc) else ValidationFailure
        raise failure(
            f"{plugin['id']} {release['version']}: 无法下载 Release 资产：HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise AvailabilityFailure(
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
    if not isinstance(name, str):
        raise ValidationFailure(f"{plugin_id} {version}: ZIP 路径必须是字符串")
    try:
        name_utf16_length = len(name.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValidationFailure(
            f"{plugin_id} {version}: ZIP 路径包含无效 Unicode"
        ) from exc
    if (
        not name
        or name_utf16_length > 512
        or unicodedata.normalize("NFC", name) != name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or is_directory != name.endswith("/")
    ):
        raise ValidationFailure(f"{plugin_id} {version}: ZIP 不安全路径 {name!r}")
    path = name[:-1] if is_directory else name
    segments = path.split("/")
    if not segments or any(
        not segment
        or segment in {".", ".."}
        or len(segment.encode("utf-16-le")) // 2 > 255
        or ":" in segment
        or any(ord(character) < 32 for character in segment)
        or any(character in '<>"|?*' for character in segment)
        or segment.endswith((" ", "."))
        or segment.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        for segment in segments
    ):
        raise ValidationFailure(f"{plugin_id} {version}: ZIP 不安全路径 {name!r}")
    return "/".join(segments)


def runtime_object(source: str, value: object) -> dict[str, object]:
    """Model System.Text.Json's case-insensitive object binding safely."""

    if not isinstance(value, dict):
        raise ValidationFailure(f"{source}: 必须是对象")
    folded: dict[str, object] = {}
    for key, item in value.items():
        normalized = ascii_fold(key)
        if normalized in folded:
            raise ValidationFailure(f"{source}: 包含大小写重复字段 {key!r}")
        folded[normalized] = item
    return folded


def runtime_member(
    source: str,
    value: dict[str, object],
    field: str,
    default: object = _MISSING,
) -> object:
    folded = ascii_fold(field)
    if folded in value:
        return value[folded]
    if default is _MISSING:
        raise ValidationFailure(f"{source}: 缺少必填字段 {field}")
    return copy.deepcopy(default)


def runtime_text(
    source: str,
    value: dict[str, object],
    field: str,
    maximum: int,
    *,
    default: object = _MISSING,
    nullable: bool = False,
    nonblank: bool = False,
) -> str | None:
    item = runtime_member(source, value, field, default)
    if item is None and nullable:
        return None
    try:
        text_length = (
            len(item.encode("utf-16-le")) // 2 if isinstance(item, str) else -1
        )
    except UnicodeEncodeError:
        text_length = -1
    if not isinstance(item, str) or not 0 <= text_length <= maximum or (
        nonblank and not item.strip()
    ):
        raise ValidationFailure(f"{source}: {field} 不是有效文本或超过 {maximum} 字符")
    return item


def runtime_text_list(
    source: str,
    value: dict[str, object],
    field: str,
    maximum: int,
    item_maximum: int,
    *,
    default: object = _MISSING,
    nonblank: bool = True,
) -> list[str]:
    items = runtime_member(source, value, field, default)
    valid = isinstance(items, list) and len(items) <= maximum
    if valid:
        for item in items:
            try:
                item_length = (
                    len(item.encode("utf-16-le")) // 2
                    if isinstance(item, str)
                    else -1
                )
            except UnicodeEncodeError:
                item_length = -1
            if (
                not isinstance(item, str)
                or not 0 <= item_length <= item_maximum
                or (nonblank and not item.strip())
            ):
                valid = False
                break
    if not valid:
        raise ValidationFailure(f"{source}: {field} 不是有效文本数组")
    return list(items)


def runtime_optional_number(
    source: str, value: dict[str, object], field: str
) -> int | float | None:
    item = runtime_member(source, value, field, None)
    if item is None:
        return None
    try:
        finite = (
            not isinstance(item, bool)
            and isinstance(item, (int, float))
            and math.isfinite(float(item))
        )
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ValidationFailure(f"{source}: {field} 必须是有限数字或 null")
    return item


def validate_runtime_default_value(
    source: str,
    setting: dict[str, object],
    default_value: object,
    raw_token: str | None = None,
) -> str | None:
    kind = setting["kind"]
    title = setting["title"]
    try:
        raw = raw_token if raw_token is not None else json.dumps(
            default_value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationFailure(f"{source}: defaultValue 不是有效 JSON") from exc
    try:
        raw_length = len(raw.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValidationFailure(f"{source}: defaultValue 包含无效 Unicode") from exc
    if raw_length > MAXIMUM_STORED_VALUE_CHARACTERS:
        raise ValidationFailure(f"{source}: defaultValue 超过 32768 字符")
    if default_value is None:
        # JsonElement? binds an explicit JSON null as no default in the host.
        return None
    if kind in {"File", "Directory"}:
        raise ValidationFailure(f"{source}: 路径设置不能声明非空 defaultValue")

    if kind == "Boolean":
        valid_type = isinstance(default_value, bool)
        display = "true" if default_value is True else "false"
    elif kind == "Integer":
        valid_type = (
            isinstance(default_value, int)
            and not isinstance(default_value, bool)
            and -(2**63) <= default_value <= 2**63 - 1
        )
        display = raw
    elif kind == "Number":
        try:
            valid_type = (
                isinstance(default_value, (int, float))
                and not isinstance(default_value, bool)
                and math.isfinite(float(default_value))
            )
        except (OverflowError, ValueError):
            valid_type = False
        # JsonNode.Parse(...).ToJsonString() preserves the original number
        # token (for example 1.00 or 1E+02), and the launcher applies its text
        # length and regex rules to that exact spelling.
        display = raw
    else:
        valid_type = isinstance(default_value, str)
        display = default_value if valid_type else ""
    if not valid_type:
        raise ValidationFailure(f"{source}: {title} 的 defaultValue JSON 类型与 kind 不一致")
    try:
        display_length = len(display.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValidationFailure(f"{source}: defaultValue 包含无效 Unicode") from exc
    if display_length > MAXIMUM_STORED_VALUE_CHARACTERS:
        raise ValidationFailure(f"{source}: {title} 的 defaultValue 展示值过长")
    if setting["required"] and not display.strip():
        raise ValidationFailure(f"{source}: 必填设置 {title} 的 defaultValue 不能为空")
    maximum_length = setting["maximumLength"]
    if maximum_length is not None and display_length > maximum_length:
        raise ValidationFailure(f"{source}: {title} 的 defaultValue 超过 maximumLength")
    if kind == "Choice" and not any(
        option["value"] == display for option in setting["options"]
    ):
        raise ValidationFailure(f"{source}: {title} 的 defaultValue 不在 options 中")
    if kind in {"Integer", "Number"}:
        number = float(default_value)
        minimum = setting["minimum"]
        maximum = setting["maximum"]
        if (minimum is not None and number < minimum) or (
            maximum is not None and number > maximum
        ):
            raise ValidationFailure(f"{source}: {title} 的 defaultValue 超出范围")
        step = setting["step"]
        if step is not None:
            origin = minimum if minimum is not None else 0
            try:
                steps = (number - origin) / step
                valid_step = math.isfinite(steps) and (
                    abs(steps - round(steps)) <= 0.0000001
                )
            except (OverflowError, ValueError, ZeroDivisionError):
                valid_step = False
            if not valid_step:
                raise ValidationFailure(f"{source}: {title} 的 defaultValue 不符合 step")
    return display


def validate_dotnet_regex_patterns(source: str, settings: list[dict]) -> None:
    """Compile patterns and match defaults with the launcher's .NET engine."""

    patterns = [
        {
            "pattern": setting["pattern"],
            "hasDefault": "_defaultDisplay" in setting,
            "display": setting.get("_defaultDisplay", ""),
        }
        for setting in settings
        if isinstance(setting.get("pattern"), str) and setting["pattern"].strip()
    ]
    if not patterns:
        return
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        raise ValidationFailure(
            f"{source}: 验证 settings.pattern 需要 PowerShell/.NET Regex 运行时"
        )
    script = r"""
$ErrorActionPreference = 'Stop'
$items = @([Console]::In.ReadToEnd() | ConvertFrom-Json)
for ($index = 0; $index -lt $items.Count; $index++) {
    try {
        $regex = [regex]::new(
            [string]$items[$index].pattern,
            [Text.RegularExpressions.RegexOptions]::CultureInvariant,
            [TimeSpan]::FromMilliseconds(250))
        if ([bool]$items[$index].hasDefault -and
            -not $regex.IsMatch([string]$items[$index].display)) {
            [Console]::Error.WriteLine("$index`ndefaultValue does not match pattern")
            exit 3
        }
    }
    catch [Text.RegularExpressions.RegexMatchTimeoutException] {
        [Console]::Error.WriteLine("$index`n$($_.Exception.Message)")
        exit 4
    }
    catch [ArgumentException] {
        [Console]::Error.WriteLine("$index`n$($_.Exception.Message)")
        exit 2
    }
}
"""
    try:
        completed = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            # Keep the pipe ASCII-only so Windows locales such as GBK cannot
            # fail before PowerShell's JSON decoder restores Unicode patterns.
            input=json.dumps(patterns, ensure_ascii=True),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationFailure(
            f"{source}: 无法安全编译 settings.pattern：{exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[:1000]
        raise ValidationFailure(
            f"{source}: settings.pattern 无效、超时或与 defaultValue 不匹配"
            + (f"：{detail}" if detail else "")
        )


def validate_runtime_setting(
    source: str,
    value: object,
    raw_default_token: str | None = None,
) -> dict[str, object]:
    fields = runtime_object(source, value)
    key = runtime_text(source, fields, "key", 128, nonblank=True)
    title = runtime_text(source, fields, "title", 256, nonblank=True)
    assert key is not None and title is not None
    if not SETTING_KEY.fullmatch(key):
        raise ValidationFailure(f"{source}: 设置 key 无效")

    raw_kind = runtime_member(source, fields, "kind", "Text")
    raw_scope = runtime_member(source, fields, "scope", "Global")
    if not isinstance(raw_kind, str) or ascii_fold(raw_kind) not in SETTING_KINDS:
        raise ValidationFailure(f"{source}: kind 不是受支持的字符串枚举")
    if not isinstance(raw_scope, str) or ascii_fold(raw_scope) not in SETTING_SCOPES:
        raise ValidationFailure(f"{source}: scope 不是受支持的字符串枚举")
    kind = SETTING_KINDS[ascii_fold(raw_kind)]
    scope = SETTING_SCOPES[ascii_fold(raw_scope)]

    required = runtime_member(source, fields, "required", False)
    if not isinstance(required, bool):
        raise ValidationFailure(f"{source}: required 必须是布尔值")
    minimum = runtime_optional_number(source, fields, "minimum")
    maximum = runtime_optional_number(source, fields, "maximum")
    step = runtime_optional_number(source, fields, "step")
    maximum_length = runtime_member(source, fields, "maximumLength", None)
    if maximum_length is not None and (
        isinstance(maximum_length, bool)
        or not isinstance(maximum_length, int)
        or not -(2**31) <= maximum_length <= 2**31 - 1
    ):
        raise ValidationFailure(f"{source}: maximumLength 必须是 32 位整数或 null")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValidationFailure(f"{source}: minimum 不能大于 maximum")
    if step is not None and step <= 0:
        raise ValidationFailure(f"{source}: step 必须大于 0")
    if maximum_length is not None and maximum_length <= 0:
        raise ValidationFailure(f"{source}: maximumLength 必须大于 0")

    options_value = runtime_member(source, fields, "options", [])
    if not isinstance(options_value, list) or len(options_value) > 256:
        raise ValidationFailure(f"{source}: options 必须是至多 256 项的数组")
    options: list[dict[str, str]] = []
    for index, option_value in enumerate(options_value):
        option_source = f"{source}.options[{index}]"
        option_fields = runtime_object(option_source, option_value)
        option_value_text = runtime_text(
            option_source, option_fields, "value", 1024
        )
        option_label = runtime_text(option_source, option_fields, "label", 256)
        option_description = runtime_text(
            option_source, option_fields, "description", 2048, default=""
        )
        assert option_value_text is not None
        assert option_label is not None
        assert option_description is not None
        options.append(
            {
                "value": option_value_text,
                "label": option_label,
                "description": option_description,
            }
        )

    file_extensions = runtime_text_list(
        source,
        fields,
        "fileExtensions",
        64,
        32,
        default=[],
        nonblank=False,
    )
    if kind == "Choice":
        if not options:
            raise ValidationFailure(f"{source}: Choice 设置必须声明 options")
        if any(
            not option["value"].strip() or not option["label"].strip()
            for option in options
        ):
            raise ValidationFailure(f"{source}: Choice option 的 value/label 不能为空")
        if len({option["value"] for option in options}) != len(options):
            raise ValidationFailure(f"{source}: Choice option 包含重复 value")
    if kind == "File" and any(
        not extension.strip()
        or not extension.startswith(".")
        or any(
            character in WINDOWS_INVALID_FILENAME_CHARACTERS or ord(character) < 32
            for character in extension
        )
        for extension in file_extensions
    ):
        raise ValidationFailure(f"{source}: fileExtensions 包含无效文件后缀")

    pattern = runtime_text(
        source, fields, "pattern", 2048, default=None, nullable=True
    )
    # Python only enforces the JSON type and length here.  The manifest-level
    # helper below compiles and matches with the same .NET Regex engine used by
    # the launcher, including syntax such as \p{L} and (?<name>...).

    setting: dict[str, object] = {
        "key": key,
        "title": title,
        "description": runtime_text(
            source, fields, "description", 4096, default=""
        ),
        "kind": kind,
        "scope": scope,
        "required": required,
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
        "maximumLength": maximum_length,
        "pattern": pattern,
        "placeholder": runtime_text(
            source, fields, "placeholder", 1024, default=None, nullable=True
        ),
        "options": options,
        "fileExtensions": file_extensions,
    }
    if "defaultvalue" in fields:
        default_display = validate_runtime_default_value(
            source, setting, fields["defaultvalue"], raw_default_token
        )
        if default_display is not None:
            setting["_defaultDisplay"] = default_display
    return setting


def validate_runtime_manifest_contract(
    plugin_id: str,
    version: str,
    manifest: object,
    raw_default_tokens: dict[int, str] | None = None,
) -> dict[str, object]:
    """Validate launcher-facing ``plugin.json`` strong types and host limits."""

    source = f"{plugin_id} {version}::plugin.json"
    fields = runtime_object(source, manifest)
    manifest_version = runtime_member(source, fields, "manifestVersion", 1)
    if isinstance(manifest_version, bool) or not isinstance(manifest_version, int):
        raise ValidationFailure(f"{source}: manifestVersion 必须是整数")

    canonical: dict[str, object] = {
        "manifestVersion": manifest_version,
        "id": runtime_text(source, fields, "id", 128, nonblank=True),
        "name": runtime_text(source, fields, "name", 256, nonblank=True),
        "version": runtime_text(source, fields, "version", 64, nonblank=True),
        "apiVersion": runtime_text(source, fields, "apiVersion", 32, default="1.0"),
        "minimumLauncherVersion": runtime_text(
            source,
            fields,
            "minimumLauncherVersion",
            64,
            default=None,
            nullable=True,
        ),
        "description": runtime_text(
            source, fields, "description", 8192, default=""
        ),
        "authors": runtime_text_list(
            source, fields, "authors", 64, 256, default=[]
        ),
        "homepage": runtime_text(
            source, fields, "homepage", 2048, default=None, nullable=True
        ),
        "license": runtime_text(
            source, fields, "license", 256, default=None, nullable=True
        ),
        "icon": runtime_text(
            source, fields, "icon", 4096, default=None, nullable=True
        ),
        "entryAssembly": runtime_text(
            source, fields, "entryAssembly", 4096, nonblank=True
        ),
        "entryType": runtime_text(source, fields, "entryType", 1024, nonblank=True),
        "requiredCapabilities": runtime_text_list(
            source,
            fields,
            "requiredCapabilities",
            MAXIMUM_CAPABILITY_COUNT,
            128,
            default=[],
        ),
        "optionalCapabilities": runtime_text_list(
            source,
            fields,
            "optionalCapabilities",
            MAXIMUM_CAPABILITY_COUNT,
            128,
            default=[],
        ),
    }
    assert isinstance(canonical["id"], str)
    assert isinstance(canonical["version"], str)
    assert isinstance(canonical["apiVersion"], str)
    if not PLUGIN_ID.fullmatch(canonical["id"]):
        raise ValidationFailure(f"{source}: id 不是小写反向域名")
    if not match_semver(canonical["version"]):
        raise ValidationFailure(f"{source}: version 不是语义版本")
    if not API_VERSION.fullmatch(canonical["apiVersion"]):
        raise ValidationFailure(f"{source}: apiVersion 与宿主主版本 1 不兼容")
    minimum_launcher = canonical["minimumLauncherVersion"]
    if isinstance(minimum_launcher, str) and minimum_launcher.strip() and not match_semver(
        minimum_launcher
    ):
        raise ValidationFailure(f"{source}: minimumLauncherVersion 不是语义版本")

    capabilities = canonical["requiredCapabilities"] + canonical["optionalCapabilities"]
    if len(capabilities) > MAXIMUM_CAPABILITY_COUNT:
        raise ValidationFailure(f"{source}: 能力声明合计不能超过 64 项")
    if len({ascii_fold(item) for item in capabilities}) != len(capabilities):
        raise ValidationFailure(f"{source}: 能力声明包含大小写重复项")
    unsupported = next(
        (
            item
            for item in canonical["requiredCapabilities"]
            if ascii_fold(item) not in {ascii_fold(known) for known in KNOWN_CAPABILITIES}
        ),
        None,
    )
    if unsupported is not None:
        raise ValidationFailure(f"{source}: 不支持必要能力 {unsupported}")

    settings_value = runtime_member(source, fields, "settings", [])
    if not isinstance(settings_value, list) or len(settings_value) > MAXIMUM_SETTING_COUNT:
        raise ValidationFailure(f"{source}: settings 必须是至多 256 项的数组")
    settings = [
        validate_runtime_setting(
            f"{source}.settings[{index}]",
            setting,
            None if raw_default_tokens is None else raw_default_tokens.get(index),
        )
        for index, setting in enumerate(settings_value)
    ]
    keys = [ascii_fold(setting["key"]) for setting in settings]
    if len(set(keys)) != len(keys):
        raise ValidationFailure(f"{source}: settings 包含大小写重复 key")
    validate_dotnet_regex_patterns(source, settings)
    for setting in settings:
        setting.pop("_defaultDisplay", None)
    canonical["settings"] = settings
    return canonical


def validate_runtime_package(plugin: dict, release: dict, payload: bytes) -> None:
    """Validate a ZIP against the launcher's runtime package contract."""

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
        names_by_folded: dict[str, zipfile.ZipInfo] = {}
        names_exact: dict[str, zipfile.ZipInfo] = {}
        expanded = 0
        for entry in entries:
            is_directory = entry.is_dir()
            normalized = validate_zip_path(plugin_id, version, entry.filename, is_directory)
            folded = normalized.casefold()
            if folded in names_by_folded:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 包含重复路径 {normalized}")
            names_by_folded[folded] = entry
            names_exact[normalized] = entry
            unix_type = (entry.external_attr >> 16) & 0xF000
            if entry.flag_bits & 0x1:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 不能包含加密条目 {normalized}")
            if unix_type not in (0, 0x4000, 0x8000) or entry.external_attr & 0x400:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 包含链接或特殊文件 {normalized}")
            if (is_directory and unix_type == 0x8000) or (
                not is_directory and unix_type == 0x4000
            ):
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 文件类型标记冲突 {normalized}")
            if is_directory and entry.file_size != 0:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 目录条目不能包含文件内容 {normalized}")
            if entry.file_size > MAXIMUM_ENTRY_BYTES:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 单条目超过 512 MiB")
            if entry.file_size > 1024 and (
                entry.compress_size == 0 or entry.file_size > entry.compress_size * 200
            ):
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 压缩比异常 {normalized}")
            expanded += entry.file_size
            if expanded > MAXIMUM_EXPANDED_BYTES:
                raise ValidationFailure(f"{plugin_id} {version}: ZIP 解压后超过 1 GiB")
        for folded, entry in names_by_folded.items():
            parts = folded.split("/")
            for index in range(1, len(parts)):
                ancestor = names_by_folded.get("/".join(parts[:index]))
                if ancestor is not None and not ancestor.is_dir():
                    raise ValidationFailure(
                        f"{plugin_id} {version}: ZIP 文件路径同时被用作目录 {entry.filename}"
                    )
        for folded, entry in names_by_folded.items():
            if entry.is_dir():
                continue
            received = 0
            try:
                with archive.open(entry) as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > entry.file_size:
                            raise ValidationFailure(
                                f"{plugin_id} {version}: ZIP 条目实际内容超过声明大小 {folded}"
                            )
            except ValidationFailure:
                raise
            except (
                EOFError,
                RuntimeError,
                NotImplementedError,
                OSError,
                zipfile.BadZipFile,
                zlib.error,
            ) as exc:
                raise ValidationFailure(
                    f"{plugin_id} {version}: ZIP 条目无法完整解压或校验 {folded}：{exc}"
                ) from exc
            if received != entry.file_size:
                raise ValidationFailure(
                    f"{plugin_id} {version}: ZIP 条目实际大小与声明不一致 {folded}"
                )
        manifest_entry = names_exact.get("plugin.json")
        if manifest_entry is None or manifest_entry.is_dir():
            raise ValidationFailure(f"{plugin_id} {version}: ZIP 根目录缺少 plugin.json")
        if manifest_entry.file_size > MAXIMUM_MANIFEST_BYTES:
            raise ValidationFailure(f"{plugin_id} {version}: plugin.json 超过 1 MiB")
        try:
            manifest_text = archive.read(manifest_entry).decode("utf-8")
            manifest_source = f"{plugin_id} {version}::plugin.json"
            manifest_value = parse_json_object(manifest_text, manifest_source)
            raw_default_tokens = validate_runtime_default_value_raw_lengths(
                manifest_text, manifest_source
            )
            manifest = validate_runtime_manifest_contract(
                plugin_id,
                version,
                manifest_value,
                raw_default_tokens,
            )
        except (KeyError, UnicodeError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ValidationFailure(f"{plugin_id} {version}: 无法读取包内 plugin.json：{exc}") from exc
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
                    or len({ascii_fold(item) for item in actual}) != len(actual)
                    or {ascii_fold(item) for item in actual}
                    != {ascii_fold(item) for item in expected_value}
                ):
                    raise ValidationFailure(f"{plugin_id} {version}: 包内 {field} 与版本索引不一致")
            elif type(actual) is not type(expected_value) or actual != expected_value:
                raise ValidationFailure(f"{plugin_id} {version}: 包内 {field} 与版本索引不一致")
        entry_assembly = manifest["entryAssembly"]
        for field in ("name", "description", "authors", "license"):
            if field in plugin and manifest.get(field) != plugin[field]:
                raise ValidationFailure(
                    f"{plugin_id} {version}: 包内 {field} 与发布清单元数据不一致"
                )
        assembly_path = validate_zip_path(plugin_id, version, entry_assembly, False)
        assembly_entry = names_exact.get(assembly_path)
        if (
            assembly_entry is None
            or assembly_entry.is_dir()
            or not ascii_fold(assembly_path).endswith(".dll")
        ):
            raise ValidationFailure(f"{plugin_id} {version}: entryAssembly 不存在或不是 DLL")
        try:
            if archive.open(assembly_entry).read(2) != b"MZ":
                raise ValidationFailure(f"{plugin_id} {version}: entryAssembly 不是 PE 程序集")
        except (RuntimeError, zipfile.BadZipFile) as exc:
            raise ValidationFailure(f"{plugin_id} {version}: 无法读取 entryAssembly：{exc}") from exc
        if any(
            name.rsplit("/", 1)[-1] == "nyalauncher.plugin.abstractions.dll"
            for name in names_by_folded
        ):
            raise ValidationFailure(
                f"{plugin_id} {version}: 插件包不能携带 NyaLauncher.Plugin.Abstractions.dll"
            )
        icon = manifest["icon"]
        if isinstance(icon, str) and icon.strip():
            icon_path = validate_zip_path(plugin_id, version, icon, False)
            if icon_path not in names_exact or names_exact[icon_path].is_dir():
                raise ValidationFailure(f"{plugin_id} {version}: 声明的 icon 不存在")


def verify_assets(
    index: dict,
    *,
    best_effort: bool = False,
    warnings: list[str] | None = None,
) -> None:
    checked = 0
    for plugin in index["plugins"]:
        for release in plugin["releases"]:
            if release["yanked"]:
                continue
            try:
                payload = download_release_asset(plugin, release)
                validate_runtime_package(plugin, release, payload)
            except ValidationFailure as exc:
                if not best_effort:
                    raise
                message = (
                    f"{plugin['id']} {release['version']} 历史资产监测失败：{exc}；"
                    "保留固定哈希和中心历史"
                )
                if warnings is not None:
                    warnings.append(message)
                emit_refresh_warning(message)
                continue
            checked += 1
            print(f"资产通过：{plugin['id']} {release['version']}")
    print(f"资产验证完成：{checked} 个未撤回版本")


def render(value: object) -> str:
    rendered = canonical_json(value)
    if len(rendered.encode("utf-8")) > MAXIMUM_INDEX_BYTES:
        raise ValidationFailure(f"生成 JSON 不能超过 {MAXIMUM_INDEX_BYTES} 字节")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="fetch publisher _manifest.json files and append new snapshots",
    )
    parser.add_argument("--write", action="store_true", help="rewrite generated views")
    parser.add_argument("--check", action="store_true", help="fail when generated views are stale")
    parser.add_argument(
        "--verify-assets",
        action="store_true",
        help="download every non-yanked fixed Release ZIP and validate its package",
    )
    parser.add_argument(
        "--best-effort",
        action="store_true",
        help="isolate publisher/history availability failures and emit warnings",
    )
    args = parser.parse_args()
    try:
        refresh_warnings: list[str] = []
        listings = load_plugin_list()
        # The version directories, not plugin_details.json, are authoritative.
        # Passing their generated view also lets --write repair a missing or
        # malformed plugin_details.json instead of trusting it as history.
        existing_details = build_details()
        if args.refresh:
            details = refresh_details(
                listings,
                existing_details,
                write=args.write,
                best_effort=args.best_effort,
                warnings=refresh_warnings,
            )
            if args.write:
                details = build_details()
        else:
            details = existing_details
        index = build_index(details)
        rendered_details = render(details)
        rendered_index = render(index)
        details_path = ROOT / "plugin_details.json"
        index_path = ROOT / "public/v1/index.json"
        if args.write:
            write_text_atomic(details_path, rendered_details)
            write_text_atomic(index_path, rendered_index)
        if args.check:
            current_details = (
                details_path.read_text(encoding="utf-8") if details_path.exists() else ""
            )
            current_index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
            if current_details != rendered_details:
                raise ValidationFailure(
                    "plugin_details.json 不是由中心历史确定性生成的，请运行 python tools/validate.py --write"
                )
            if current_index != rendered_index:
                raise ValidationFailure(
                    "public/v1/index.json 不是由中心历史与审核确定性生成的，请运行 python tools/validate.py --write"
                )
        if args.verify_assets:
            verify_assets(
                index,
                best_effort=args.best_effort,
                warnings=refresh_warnings,
            )
        print(
            f"验证通过：{len(details)} 个插件，"
            f"{sum(len(plugin['releases']) for plugin in details)} 个历史版本，"
            f"{len(rendered_index.encode('utf-8'))} 字节索引，"
            f"{len(refresh_warnings)} 个隔离警告"
        )
        return 0
    except ValidationFailure as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        print(f"验证失败：无法读写生成文件：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
