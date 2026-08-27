from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from file_hash_browser.config import ConfigError, load_config
from tests.helpers import write_config


class ConfigTests(unittest.TestCase):
    def test_loads_fixed_parallelism_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config, root = write_config(base, parallel_tasks=3)
            self.assertEqual(config.effective_parallel_tasks, 3)
            self.assertEqual(config.parallelism_mode, "fixed")
            self.assertEqual(config.roots[0].path, root.resolve())
            self.assertEqual(config.database_path, (base / "state" / "hashes.sqlite3").resolve())

    def test_rejects_database_inside_authorized_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _config, _root = write_config(base)
            path = base / "config.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["data"]["database"] = "./authorized/private.sqlite3"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "data.database"):
                load_config(path)

    def test_rejects_overlapping_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _config, root = write_config(base)
            nested = root / "nested"
            nested.mkdir()
            path = base / "config.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["roots"].append(
                {"id": "nested", "label": "Nested", "path": "./authorized/nested"}
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "may not overlap"):
                load_config(path)

    def test_rejects_invalid_root_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _config, _root = write_config(base)
            path = base / "config.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["roots"][0]["id"] = "Bad ID"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "lowercase"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()

