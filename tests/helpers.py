from __future__ import annotations

import json
from pathlib import Path

from file_hash_browser.config import AppConfig, load_config


def write_config(
    base: Path,
    *,
    show_full_filename: bool = False,
    parallel_tasks: int = 2,
    chunk_size_bytes: int = 4096,
    label: str = "Authorized Library",
) -> tuple[AppConfig, Path]:
    root = base / "authorized"
    root.mkdir(parents=True, exist_ok=True)
    config_path = base / "config.json"
    payload = {
        "title": "Test Hash Browser",
        "server": {
            "host": "127.0.0.1",
            "port": 8080,
            "allowed_hosts": ["localhost", "127.0.0.1"],
            "request_timeout_seconds": 10,
        },
        "privacy": {"show_full_filename": show_full_filename},
        "browse": {
            "show_hidden_files": False,
            "default_page_size": 20,
            "max_page_size": 100,
            "max_node_tokens": 2000,
        },
        "hashing": {
            "parallel_tasks": parallel_tasks,
            "chunk_size_bytes": chunk_size_bytes,
            "max_files_per_job": 1000,
            "max_directory_depth": 64,
            "max_algorithms_per_job": 16,
            "max_selections_per_job": 100,
            "max_active_jobs": 8,
            "queue_multiplier": 2,
        },
        "plugins": {
            "directory": None,
            "enabled_algorithms": ["md5", "sha1", "sha256", "sha512"],
        },
        "data": {"database": "./state/hashes.sqlite3"},
        "roots": [{"id": "test", "label": label, "path": "./authorized"}],
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_config(config_path), root

