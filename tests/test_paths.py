from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from file_hash_browser.config import (
    AppConfig,
    BrowseConfig,
    HashingConfig,
    PluginConfig,
    PrivacyConfig,
    RootConfig,
    ServerConfig,
)
from file_hash_browser.paths import PathAuthority, display_name


def _config_for(root: Path, *, show_full_filename: bool = False) -> AppConfig:
    return AppConfig(
        title="Test File Hash Browser",
        server=ServerConfig(
            host="127.0.0.1",
            port=8080,
            allowed_hosts=("localhost",),
            request_timeout_seconds=30.0,
        ),
        privacy=PrivacyConfig(show_full_filename=show_full_filename),
        browse=BrowseConfig(
            show_hidden_files=True,
            default_page_size=10,
            max_page_size=100,
            max_node_tokens=1_000,
        ),
        hashing=HashingConfig(
            parallel_tasks=1,
            chunk_size_bytes=4_096,
            max_files_per_job=100,
            max_directory_depth=16,
            max_algorithms_per_job=4,
            max_selections_per_job=10,
            max_active_jobs=2,
            queue_multiplier=1,
        ),
        plugins=PluginConfig(directory=None, enabled_algorithms=None),
        roots=(RootConfig(id="test-root", label="Sensitive Root", path=root),),
        database_path=root.parent / "test-hashes.sqlite3",
        config_path=root.parent / "test-config.json",
    )


class DisplayNameTests(unittest.TestCase):
    def test_half_masking_uses_the_leading_half(self) -> None:
        self.assertEqual(
            display_name("abcdef", show_full_filename=False),
            ("abc\u2026", True),
        )
        self.assertEqual(
            display_name("abcde", show_full_filename=False),
            ("abc\u2026", True),
        )

    def test_single_character_names_are_not_masked(self) -> None:
        self.assertEqual(
            display_name("x", show_full_filename=False),
            ("x", False),
        )


class PathAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()

    def _authority(self, *, show_full_filename: bool = False) -> PathAuthority:
        authority = PathAuthority(
            _config_for(self.root, show_full_filename=show_full_filename)
        )
        self.addCleanup(authority.close)
        return authority

    @staticmethod
    def _names(page) -> list[str]:
        return [item.ref.parts[-1] for item in page.items]

    def test_directory_and_file_names_are_both_masked(self) -> None:
        (self.root / "foldersix").mkdir()
        (self.root / "secret.txt").write_bytes(b"content")
        authority = self._authority()

        root_token = str(authority.roots_public()[0]["id"])
        page = authority.list_children(root_token, offset=0, limit=10)
        by_kind = {item.ref.kind: item for item in page.items}

        self.assertEqual(by_kind["directory"].display_name, "folde\u2026")
        self.assertTrue(by_kind["directory"].masked)
        self.assertEqual(by_kind["file"].display_name, "secre\u2026")
        self.assertTrue(by_kind["file"].masked)

    def test_single_character_directory_and_file_names_remain_distinct(self) -> None:
        (self.root / "d").mkdir()
        (self.root / "f").write_bytes(b"content")
        authority = self._authority()

        root_token = str(authority.roots_public()[0]["id"])
        page = authority.list_children(root_token, offset=0, limit=10)

        self.assertEqual(
            {(item.ref.kind, item.display_name, item.masked) for item in page.items},
            {("directory", "d", False), ("file", "f", False)},
        )

    def test_public_node_tokens_are_opaque_and_do_not_leak_paths(self) -> None:
        directory_name = "UltravioletArchive"
        file_name = "QuarterlySecrets.bin"
        (self.root / directory_name).mkdir()
        (self.root / file_name).write_bytes(b"content")
        authority = self._authority()

        root_payload = authority.roots_public()[0]
        root_token = str(root_payload["id"])
        self.assertRegex(root_token, r"^root_[A-Za-z0-9_-]+$")
        self.assertNotIn("test-root", root_token)
        self.assertNotIn("Sensitive Root", root_token)
        self.assertNotIn(str(self.root), root_token)

        page = authority.list_children(root_token, offset=0, limit=10)
        for item in page.items:
            with self.subTest(name=item.ref.parts[-1]):
                self.assertRegex(item.node_id, r"^node_[A-Za-z0-9_-]+$")
                self.assertNotIn(item.ref.parts[-1], item.node_id)
                public_json = json.dumps(item.public_dict(), ensure_ascii=False)
                self.assertNotIn(item.ref.parts[-1], public_json)
                self.assertNotIn(str(self.root), public_json)
                self.assertNotIn("relative_path", public_json)

    def test_directory_listing_is_paginated_in_stable_order(self) -> None:
        for name in ("beta-dir", "alpha-dir"):
            (self.root / name).mkdir()
        for name in ("echo.txt", "charlie.txt", "delta.txt"):
            (self.root / name).write_bytes(name.encode("ascii"))
        authority = self._authority(show_full_filename=True)
        root_token = str(authority.roots_public()[0]["id"])

        first = authority.list_children(root_token, offset=0, limit=2)
        second = authority.list_children(root_token, offset=2, limit=2)
        third = authority.list_children(root_token, offset=4, limit=2)

        self.assertEqual(self._names(first), ["alpha-dir", "beta-dir"])
        self.assertEqual(self._names(second), ["charlie.txt", "delta.txt"])
        self.assertEqual(self._names(third), ["echo.txt"])
        self.assertEqual((first.total, second.total, third.total), (5, 5, 5))
        self.assertEqual((first.next_offset, second.next_offset, third.next_offset), (2, 4, None))
        self.assertEqual(first.offset, 0)
        self.assertEqual(second.offset, 2)
        self.assertEqual(third.offset, 4)
        self.assertEqual(
            {ref.parts[-1] for ref in first.present_files},
            {"charlie.txt", "delta.txt", "echo.txt"},
        )

    def test_symlink_entries_are_skipped_when_supported(self) -> None:
        target_file = self.root / "real-file.txt"
        target_directory = self.root / "real-directory"
        target_file.write_bytes(b"content")
        target_directory.mkdir()

        links: list[Path] = []
        for link, target, is_directory in (
            (self.root / "file-link", target_file, False),
            (self.root / "directory-link", target_directory, True),
        ):
            try:
                link.symlink_to(target, target_is_directory=is_directory)
            except (NotImplementedError, OSError):
                continue
            links.append(link)
        if not links:
            self.skipTest("symbolic links are not available for this test user")

        authority = self._authority(show_full_filename=True)
        root_token = str(authority.roots_public()[0]["id"])
        listed_names = set(self._names(authority.list_children(root_token, 0, 20)))

        self.assertIn(target_file.name, listed_names)
        self.assertIn(target_directory.name, listed_names)
        for link in links:
            self.assertNotIn(link.name, listed_names)

    @unittest.skipUnless(
        os.name == "nt" and hasattr(os.path, "isjunction"),
        "directory junctions are Windows-specific",
    )
    def test_junction_entries_are_skipped_when_supported(self) -> None:
        target = self.root / "junction-target"
        junction = self.root / "junction-entry"
        target.mkdir()
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0 or not os.path.isjunction(junction):
            self.skipTest("directory junction creation is not available")

        authority = self._authority(show_full_filename=True)
        root_token = str(authority.roots_public()[0]["id"])
        listed_names = set(self._names(authority.list_children(root_token, 0, 20)))

        self.assertIn(target.name, listed_names)
        self.assertNotIn(junction.name, listed_names)


if __name__ == "__main__":
    unittest.main()
