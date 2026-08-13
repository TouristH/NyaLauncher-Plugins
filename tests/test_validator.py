import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import tools.validate as validator
from tools.validate import ValidationFailure


PLUGIN = {
    "id": "dev.example.test",
}
RELEASE = {
    "version": "1.0.0",
    "compatibility": {
        "manifestVersion": 1,
        "apiVersion": "1.0",
        "minimumLauncherVersion": "0.1.0",
    },
    "requiredCapabilities": [],
    "optionalCapabilities": [],
}
MANIFEST = {
    "manifestVersion": 1,
    "id": "dev.example.test",
    "name": "Test",
    "version": "1.0.0",
    "apiVersion": "1.0",
    "minimumLauncherVersion": "0.1.0",
    "entryAssembly": "Test.dll",
    "entryType": "Test.Plugin",
    "authors": ["Example"],
    "requiredCapabilities": [],
    "optionalCapabilities": [],
}


def create_zip(extra_name=None, manifest=None, directory_payload=False):
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "plugin.json",
            json.dumps(manifest or MANIFEST, ensure_ascii=False).encode("utf-8"),
        )
        archive.writestr("Test.dll", b"MZtest assembly metadata")
        if extra_name is not None:
            archive.writestr(extra_name, b"unsafe")
        if directory_payload:
            directory = zipfile.ZipInfo("nonempty/")
            archive.writestr(directory, b"not a directory payload")
    return memory.getvalue()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


class RegistryFixture:
    plugin_id = "dev.example.test"
    version = "1.0.0"
    sha256 = "a" * 64

    def __init__(self, root: Path):
        self.root = root
        self.plugin_path = root / "plugins" / self.plugin_id / "plugin.json"
        self.release_path = (
            root / "plugins" / self.plugin_id / "releases" / f"{self.version}.json"
        )
        self.review_path = root / "reviews" / self.plugin_id / f"{self.version}.json"

        write_json(
            root / "repository.json",
            {
                "schemaVersion": 1,
                "name": "Test registry",
                "sourceUrl": "https://github.com/TouristH/NyaLauncher-Plugins",
                "launcherUrl": "https://github.com/redstore-noob/NyaLauncher",
                "indexPath": "public/v1/index.json",
                "trustedReviewers": ["TouristH"],
            },
        )
        write_json(
            self.plugin_path,
            {
                "schemaVersion": 1,
                "id": self.plugin_id,
                "name": "Test",
                "description": "Fixture",
                "authors": ["Example"],
                "repositoryUrl": "https://github.com/example/test",
                "maintainers": ["example"],
                "categories": ["utilities"],
                "license": "MIT",
            },
        )
        write_json(
            self.release_path,
            {
                "schemaVersion": 1,
                "version": self.version,
                "channel": "stable",
                "publishedAt": "2026-08-13T00:00:00Z",
                "releaseNotesUrl": "https://github.com/example/test/releases/tag/v1.0.0",
                "download": {
                    "url": "https://github.com/example/test/releases/download/v1.0.0/test.zip",
                    "sha256": self.sha256,
                    "size": 123,
                },
                "compatibility": {
                    "manifestVersion": 1,
                    "apiVersion": "1.0",
                    "minimumLauncherVersion": "0.1.0",
                },
                "requiredCapabilities": [],
                "optionalCapabilities": [],
                "yanked": False,
            },
        )
        write_json(
            self.review_path,
            {
                "schemaVersion": 1,
                "pluginId": self.plugin_id,
                "version": self.version,
                "sha256": self.sha256,
                "status": "verified",
                "reviewer": "TouristH",
                "reviewedAt": "2026-08-13T00:00:00Z",
                "notes": "Exact artifact reviewed.",
            },
        )


class RuntimePackageTests(unittest.TestCase):
    def test_valid_package(self):
        validator.validate_runtime_package(PLUGIN, RELEASE, create_zip())

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(ValidationFailure):
            validator.validate_runtime_package(PLUGIN, RELEASE, create_zip("../escape.txt"))

    def test_backslash_path_is_rejected(self):
        with self.assertRaises(ValidationFailure):
            validator.validate_zip_path(
                PLUGIN["id"], RELEASE["version"], "folder\\escape.txt", False
            )

    def test_directory_entry_with_payload_is_rejected(self):
        with self.assertRaises(ValidationFailure):
            validator.validate_runtime_package(
                PLUGIN,
                RELEASE,
                create_zip(directory_payload=True),
            )

    def test_manifest_author_length_is_bounded(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["authors"] = ["x" * 257]
        with self.assertRaises(ValidationFailure):
            validator.validate_runtime_package(
                PLUGIN,
                RELEASE,
                create_zip(manifest=manifest),
            )

    def test_manifest_entry_type_is_required(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest.pop("entryType")
        with self.assertRaises(ValidationFailure):
            validator.validate_runtime_package(
                PLUGIN,
                RELEASE,
                create_zip(manifest=manifest),
            )


class StrictContractTests(unittest.TestCase):
    def test_semver_rejects_numeric_prerelease_leading_zero(self):
        self.assertIsNone(validator.match_semver("1.2.3-01"))
        self.assertIsNotNone(validator.match_semver("1.2.3-0.alpha+build.7"))

    def test_utc_timestamp_is_exact_and_calendar_valid(self):
        path = validator.ROOT / "repository.json"
        for value in (
            "2026-08-13T00:00:00+00:00",
            "2026-08-13T00:00:00.000Z",
            "2026-02-30T00:00:00Z",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationFailure):
                validator.validate_utc_timestamp(path, "publishedAt", value)

    def test_https_rejects_credentials_and_non_443_ports(self):
        path = validator.ROOT / "repository.json"
        for value in (
            "https://example.com:444/file",
            "https://user@example.com/file",
            "https://example.com:not-a-port/file",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationFailure):
                validator.require_https(path, "url", value)
        self.assertEqual(
            validator.require_https(path, "url", "https://example.com:443/file"),
            "https://example.com:443/file",
        )

    def test_index_size_is_bounded_in_utf8_bytes(self):
        with patch.object(validator, "MAXIMUM_INDEX_BYTES", 16):
            with self.assertRaises(ValidationFailure):
                validator.render({"text": "猫" * 16})


class RegistryGenerationTests(unittest.TestCase):
    def build_fixture(self, fixture: RegistryFixture):
        with patch.object(validator, "ROOT", fixture.root):
            return validator.build_index()

    def test_review_is_hash_bound_and_mapped_to_launcher_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            index = self.build_fixture(fixture)

            plugin = index["plugins"][0]
            release = plugin["releases"][0]
            self.assertNotIn("schemaVersion", plugin)
            self.assertNotIn("schemaVersion", release)
            self.assertEqual(
                release["review"],
                {
                    "status": "verified",
                    "sha256": fixture.sha256,
                    "reviewedBy": "TouristH",
                    "reviewedAt": "2026-08-13T00:00:00Z",
                    "notes": "Exact artifact reviewed.",
                },
            )
            self.assertNotIn("pluginId", release["review"])
            self.assertNotIn("version", release["review"])
            self.assertNotIn("reviewer", release["review"])
            self.assertNotIn("schemaVersion", release["review"])

    def test_review_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            review = json.loads(fixture.review_path.read_text(encoding="utf-8"))
            review["sha256"] = "b" * 64
            write_json(fixture.review_path, review)
            with self.assertRaises(ValidationFailure):
                self.build_fixture(fixture)

    def test_untrusted_reviewer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            review = json.loads(fixture.review_path.read_text(encoding="utf-8"))
            review["reviewer"] = "NotTrusted"
            write_json(fixture.review_path, review)
            with self.assertRaises(ValidationFailure):
                self.build_fixture(fixture)

    def test_boolean_integer_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            release = json.loads(fixture.release_path.read_text(encoding="utf-8"))
            release["download"]["size"] = True
            write_json(fixture.release_path, release)
            with self.assertRaises(ValidationFailure):
                self.build_fixture(fixture)

    def test_schema_reference_must_be_json_string(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            plugin = json.loads(fixture.plugin_path.read_text(encoding="utf-8"))
            plugin["$schema"] = True
            write_json(fixture.plugin_path, plugin)
            with self.assertRaises(ValidationFailure):
                self.build_fixture(fixture)

    def test_catalog_author_length_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            plugin = json.loads(fixture.plugin_path.read_text(encoding="utf-8"))
            plugin["authors"] = ["x" * 257]
            write_json(fixture.plugin_path, plugin)
            with self.assertRaises(ValidationFailure):
                self.build_fixture(fixture)

    def test_total_capability_count_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RegistryFixture(Path(directory))
            release = json.loads(fixture.release_path.read_text(encoding="utf-8"))
            with patch.object(
                validator,
                "KNOWN_CAPABILITIES",
                {f"capability.{index}" for index in range(65)},
            ):
                release["requiredCapabilities"] = [
                    f"capability.{index}" for index in range(33)
                ]
                release["optionalCapabilities"] = [
                    f"capability.{index}" for index in range(33, 65)
                ]
                write_json(fixture.release_path, release)
                with self.assertRaises(ValidationFailure):
                    self.build_fixture(fixture)

    def test_schema_documents_are_valid_json_and_use_canonical_owner(self):
        schemas = validator.ROOT / "schemas"
        for path in schemas.glob("*.schema.json"):
            with self.subTest(path=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("TouristH/NyaLauncher-Plugins", value["$id"])


if __name__ == "__main__":
    unittest.main()
