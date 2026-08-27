from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ConfigError(ValueError):
    """Raised when the server configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    request_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    show_full_filename: bool


@dataclass(frozen=True, slots=True)
class BrowseConfig:
    show_hidden_files: bool
    default_page_size: int
    max_page_size: int
    max_node_tokens: int


@dataclass(frozen=True, slots=True)
class HashingConfig:
    parallel_tasks: int
    chunk_size_bytes: int
    max_files_per_job: int
    max_directory_depth: int
    max_algorithms_per_job: int
    max_selections_per_job: int
    max_active_jobs: int
    queue_multiplier: int


@dataclass(frozen=True, slots=True)
class PluginConfig:
    directory: Path | None
    enabled_algorithms: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class RootConfig:
    id: str
    label: str
    path: Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    title: str
    server: ServerConfig
    privacy: PrivacyConfig
    browse: BrowseConfig
    hashing: HashingConfig
    plugins: PluginConfig
    roots: tuple[RootConfig, ...]
    database_path: Path
    config_path: Path

    @property
    def effective_parallel_tasks(self) -> int:
        if self.hashing.parallel_tasks > 0:
            return self.hashing.parallel_tasks
        return max(1, os.cpu_count() or 1)

    @property
    def parallelism_mode(self) -> str:
        return "fixed" if self.hashing.parallel_tasks > 0 else "auto"


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be an object")
    return value


def _string(value: Any, key: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _integer(value: Any, key: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, key: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigError(f"{key} must be between {minimum:g} and {maximum:g}")
    return result


def _resolve_path(raw: Any, key: str, base: Path, *, must_exist: bool) -> Path:
    value = _string(raw, key)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        requirement = "an existing path" if must_exist else "a valid path"
        raise ConfigError(f"{key} must point to {requirement}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {path}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    return _mapping(value, "configuration")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser()
    try:
        config_path = config_path.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"configuration file does not exist: {config_path}") from exc

    data = _load_json(config_path)
    base = config_path.parent

    title = _string(data.get("title", "File Hash Browser"), "title")

    server_data = _mapping(data.get("server", {}), "server")
    host = _string(server_data.get("host", "127.0.0.1"), "server.host")
    port = _integer(server_data.get("port", 8080), "server.port", 1, 65535)
    allowed_hosts_raw = server_data.get(
        "allowed_hosts", ["localhost", "127.0.0.1", "[::1]"]
    )
    if not isinstance(allowed_hosts_raw, list) or not allowed_hosts_raw:
        raise ConfigError("server.allowed_hosts must be a non-empty array")
    allowed_hosts: list[str] = []
    for index, item in enumerate(allowed_hosts_raw):
        allowed_hosts.append(_string(item, f"server.allowed_hosts[{index}]").lower())
    request_timeout = _number(
        server_data.get("request_timeout_seconds", 30),
        "server.request_timeout_seconds",
        1,
        300,
    )

    privacy_data = _mapping(data.get("privacy", {}), "privacy")
    privacy = PrivacyConfig(
        show_full_filename=_boolean(
            privacy_data.get("show_full_filename", True),
            "privacy.show_full_filename",
        )
    )

    browse_data = _mapping(data.get("browse", {}), "browse")
    default_page_size = _integer(
        browse_data.get("default_page_size", 200),
        "browse.default_page_size",
        10,
        1000,
    )
    max_page_size = _integer(
        browse_data.get("max_page_size", 500),
        "browse.max_page_size",
        10,
        2000,
    )
    if default_page_size > max_page_size:
        raise ConfigError("browse.default_page_size cannot exceed browse.max_page_size")
    browse = BrowseConfig(
        show_hidden_files=_boolean(
            browse_data.get("show_hidden_files", False),
            "browse.show_hidden_files",
        ),
        default_page_size=default_page_size,
        max_page_size=max_page_size,
        max_node_tokens=_integer(
            browse_data.get("max_node_tokens", 250_000),
            "browse.max_node_tokens",
            1_000,
            5_000_000,
        ),
    )

    hashing_data = _mapping(data.get("hashing", {}), "hashing")
    hashing = HashingConfig(
        parallel_tasks=_integer(
            hashing_data.get("parallel_tasks", 0),
            "hashing.parallel_tasks",
            0,
            1024,
        ),
        chunk_size_bytes=_integer(
            hashing_data.get("chunk_size_bytes", 1_048_576),
            "hashing.chunk_size_bytes",
            4_096,
            67_108_864,
        ),
        max_files_per_job=_integer(
            hashing_data.get("max_files_per_job", 100_000),
            "hashing.max_files_per_job",
            1,
            10_000_000,
        ),
        max_directory_depth=_integer(
            hashing_data.get("max_directory_depth", 256),
            "hashing.max_directory_depth",
            1,
            4_096,
        ),
        max_algorithms_per_job=_integer(
            hashing_data.get("max_algorithms_per_job", 16),
            "hashing.max_algorithms_per_job",
            1,
            256,
        ),
        max_selections_per_job=_integer(
            hashing_data.get("max_selections_per_job", 1_000),
            "hashing.max_selections_per_job",
            1,
            100_000,
        ),
        max_active_jobs=_integer(
            hashing_data.get("max_active_jobs", 32),
            "hashing.max_active_jobs",
            1,
            10_000,
        ),
        queue_multiplier=_integer(
            hashing_data.get("queue_multiplier", 4),
            "hashing.queue_multiplier",
            1,
            100,
        ),
    )

    plugin_data = _mapping(data.get("plugins", {}), "plugins")
    plugin_directory_raw = plugin_data.get("directory")
    plugin_directory: Path | None = None
    if plugin_directory_raw is not None:
        plugin_directory = _resolve_path(
            plugin_directory_raw,
            "plugins.directory",
            base,
            must_exist=True,
        )
        if not plugin_directory.is_dir():
            raise ConfigError("plugins.directory must be a directory")
    enabled_raw = plugin_data.get("enabled_algorithms")
    enabled_algorithms: tuple[str, ...] | None = None
    if enabled_raw is not None:
        if not isinstance(enabled_raw, list) or not enabled_raw:
            raise ConfigError("plugins.enabled_algorithms must be a non-empty array")
        enabled_algorithms = tuple(
            _string(item, f"plugins.enabled_algorithms[{index}]")
            for index, item in enumerate(enabled_raw)
        )
        if len(set(enabled_algorithms)) != len(enabled_algorithms):
            raise ConfigError("plugins.enabled_algorithms contains duplicates")
    plugins = PluginConfig(plugin_directory, enabled_algorithms)

    roots_raw = data.get("roots")
    if not isinstance(roots_raw, list) or not roots_raw:
        raise ConfigError("roots must be a non-empty array")
    roots: list[RootConfig] = []
    root_ids: set[str] = set()
    root_paths: set[str] = set()
    for index, raw_root in enumerate(roots_raw):
        root_data = _mapping(raw_root, f"roots[{index}]")
        root_id = _string(root_data.get("id"), f"roots[{index}].id")
        if not ROOT_ID_PATTERN.fullmatch(root_id):
            raise ConfigError(
                f"roots[{index}].id must start with a lowercase letter and contain "
                "only lowercase letters, digits, underscores, or hyphens"
            )
        if root_id in root_ids:
            raise ConfigError(f"duplicate root id: {root_id}")
        root_ids.add(root_id)
        label = _string(root_data.get("label", root_id), f"roots[{index}].label")
        root_path = _resolve_path(
            root_data.get("path"), f"roots[{index}].path", base, must_exist=True
        )
        if not root_path.is_dir():
            raise ConfigError(f"roots[{index}].path must be a directory")
        normalized_path = os.path.normcase(str(root_path))
        if normalized_path in root_paths:
            raise ConfigError(f"duplicate authorized root path at roots[{index}]")
        root_paths.add(normalized_path)
        roots.append(RootConfig(root_id, label, root_path))

    data_config = _mapping(data.get("data", {}), "data")
    database_path = _resolve_path(
        data_config.get("database", "./data/file-hash-browser.sqlite3"),
        "data.database",
        base,
        must_exist=False,
    )
    if database_path.exists() and database_path.is_dir():
        raise ConfigError("data.database must be a file path, not a directory")

    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left.path.is_relative_to(right.path) or right.path.is_relative_to(left.path):
                raise ConfigError(
                    f"authorized roots may not overlap: {left.id} and {right.id}"
                )
        if config_path.is_relative_to(left.path):
            raise ConfigError(
                f"configuration file must not be inside authorized root: {left.id}"
            )
        if database_path.is_relative_to(left.path):
            raise ConfigError(
                f"data.database must not be inside authorized root: {left.id}"
            )
        if plugin_directory is not None and plugin_directory.is_relative_to(left.path):
            raise ConfigError(
                f"plugins.directory must not be inside authorized root: {left.id}"
            )

    return AppConfig(
        title=title,
        server=ServerConfig(host, port, tuple(allowed_hosts), request_timeout),
        privacy=privacy,
        browse=browse,
        hashing=hashing,
        plugins=plugins,
        roots=tuple(roots),
        database_path=database_path,
        config_path=config_path,
    )
