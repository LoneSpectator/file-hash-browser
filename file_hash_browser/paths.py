from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import secrets
import stat
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from .config import AppConfig, RootConfig


class PathAccessError(RuntimeError):
    """A public-safe error for a stale, invalid, or inaccessible node."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SelectionTooLarge(PathAccessError):
    def __init__(self) -> None:
        super().__init__("selection_too_large", "选择包含的文件数量超过服务端限制")


@dataclass(frozen=True, slots=True)
class NodeRef:
    root_id: str
    parts: tuple[str, ...]
    kind: str

    @property
    def relative_path(self) -> str:
        return "/".join(self.parts)


@dataclass(frozen=True, slots=True)
class NodeInfo:
    ref: NodeRef
    node_id: str
    display_name: str
    masked: bool
    size: int | None
    modified_at: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "kind": self.ref.kind,
            "displayName": self.display_name,
            "masked": self.masked,
            "size": self.size,
            "modifiedAt": self.modified_at,
            "hasChildren": self.ref.kind == "directory",
        }


@dataclass(frozen=True, slots=True)
class DirectoryPage:
    items: tuple[NodeInfo, ...]
    present_files: tuple[NodeRef, ...]
    offset: int
    total: int
    next_offset: int | None
    scanned_at: str


@dataclass(frozen=True, slots=True)
class ExpansionResult:
    files: tuple[NodeRef, ...]
    scan_errors: int


def _safe_text(value: str) -> str:
    # Control and formatting characters can make a harmless text node misleading.
    return "".join(
        "�" if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in value
    )


def display_name(name: str, *, show_full_filename: bool) -> tuple[str, bool]:
    if show_full_filename:
        return _safe_text(name), False
    if len(name) <= 1:
        return _safe_text(name), False
    visible_length = (len(name) + 1) // 2
    return f"{_safe_text(name[:visible_length])}…", True


def _iso_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class _TokenRegistry:
    def __init__(self, roots: tuple[RootConfig, ...], maximum: int):
        self._secret = secrets.token_bytes(32)
        self._maximum = maximum
        self._lock = threading.RLock()
        self._nodes: OrderedDict[str, NodeRef] = OrderedDict()
        self._roots: dict[str, NodeRef] = {}
        for root in roots:
            ref = NodeRef(root.id, (), "directory")
            self._roots[self.token_for(ref, prefix="root")] = ref

    def token_for(self, ref: NodeRef, *, prefix: str = "node") -> str:
        payload = "\0".join((ref.root_id, ref.kind, *ref.parts)).encode(
            "utf-8", errors="surrogatepass"
        )
        digest = hmac.new(self._secret, payload, hashlib.sha256).digest()[:18]
        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"{prefix}_{encoded}"

    def register(self, ref: NodeRef) -> str:
        token = self.token_for(ref)
        with self._lock:
            self._nodes[token] = ref
            self._nodes.move_to_end(token)
            while len(self._nodes) > self._maximum:
                self._nodes.popitem(last=False)
        return token

    def root_token(self, root_id: str) -> str:
        for token, ref in self._roots.items():
            if ref.root_id == root_id:
                return token
        raise KeyError(root_id)

    def resolve(self, token: str) -> NodeRef:
        with self._lock:
            root_ref = self._roots.get(token)
            if root_ref is not None:
                return root_ref
            try:
                ref = self._nodes[token]
            except KeyError as exc:
                raise PathAccessError("invalid_node", "节点无效或已过期") from exc
            self._nodes.move_to_end(token)
            return ref


class PathAuthority:
    """Maps opaque public nodes to paths and opens only authorized regular files."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._roots = {root.id: root for root in config.roots}
        self._tokens = _TokenRegistry(config.roots, config.browse.max_node_tokens)
        self._secure_dir_fd = (
            os.name == "posix"
            and os.open in os.supports_dir_fd
            and hasattr(os, "O_DIRECTORY")
            and hasattr(os, "O_NOFOLLOW")
        )
        self._root_fds: dict[str, int] = {}
        if self._secure_dir_fd:
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
            try:
                for root in config.roots:
                    self._root_fds[root.id] = os.open(root.path, flags)
            except OSError:
                self.close()
                raise PathAccessError("root_unavailable", "授权目录暂时不可用")

    def close(self) -> None:
        for fd in self._root_fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._root_fds.clear()

    def roots_public(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for root in self._config.roots:
            shown_name, masked = display_name(
                root.label,
                show_full_filename=self._config.privacy.show_full_filename,
            )
            result.append(
                {
                    "id": self._tokens.root_token(root.id),
                    "kind": "directory",
                    "displayName": shown_name,
                    "masked": masked,
                    "size": None,
                    "modifiedAt": None,
                    "hasChildren": True,
                }
            )
        return result

    def resolve_tokens(self, tokens: list[str]) -> tuple[NodeRef, ...]:
        refs: list[NodeRef] = []
        seen: set[tuple[str, tuple[str, ...], str]] = set()
        for token in tokens:
            ref = self._tokens.resolve(token)
            key = (ref.root_id, ref.parts, ref.kind)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
        return tuple(refs)

    def node_id(self, ref: NodeRef) -> str:
        if not ref.parts:
            return self._tokens.root_token(ref.root_id)
        return self._tokens.register(ref)

    def list_children(self, parent_token: str, offset: int, limit: int) -> DirectoryPage:
        parent = self._tokens.resolve(parent_token)
        if parent.kind != "directory":
            raise PathAccessError("not_a_directory", "所选节点不是目录")
        scanned_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            entries = self._scan_directory(parent)
        except (OSError, ValueError) as exc:
            raise PathAccessError("directory_unavailable", "目录暂时无法读取") from exc
        entries.sort(key=lambda item: (item[0].kind != "directory", item[2].casefold(), item[2]))
        total = len(entries)
        selected = entries[offset : offset + limit]
        nodes: list[NodeInfo] = []
        for ref, item_stat, raw_name in selected:
            shown_name, masked = display_name(
                raw_name,
                show_full_filename=self._config.privacy.show_full_filename,
            )
            nodes.append(
                NodeInfo(
                    ref=ref,
                    node_id=self._tokens.register(ref),
                    display_name=shown_name,
                    masked=masked,
                    size=item_stat.st_size if ref.kind == "file" else None,
                    modified_at=_iso_from_timestamp(item_stat.st_mtime),
                )
            )
        next_offset = offset + len(selected)
        return DirectoryPage(
            tuple(nodes),
            tuple(item[0] for item in entries if item[0].kind == "file"),
            offset,
            total,
            next_offset if next_offset < total else None,
            scanned_at,
        )

    def expand_selection(
        self,
        selected: tuple[NodeRef, ...],
        *,
        max_files: int,
        max_depth: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> ExpansionResult:
        files: list[NodeRef] = []
        seen_files: set[tuple[str, tuple[str, ...]]] = set()
        seen_directories: set[tuple[str, tuple[str, ...]]] = set()
        scan_errors = 0
        stack = list(reversed(selected))
        while stack:
            if should_stop is not None and should_stop():
                return ExpansionResult(tuple(files), scan_errors)
            ref = stack.pop()
            if ref.kind == "file":
                key = (ref.root_id, ref.parts)
                if key not in seen_files:
                    try:
                        self._validate_regular_file(ref)
                    except (OSError, PathAccessError):
                        scan_errors += 1
                        continue
                    seen_files.add(key)
                    files.append(ref)
                    if len(files) > max_files:
                        raise SelectionTooLarge()
                continue

            directory_key = (ref.root_id, ref.parts)
            if directory_key in seen_directories:
                continue
            seen_directories.add(directory_key)
            if len(ref.parts) > max_depth:
                scan_errors += 1
                continue
            try:
                entries = self._scan_directory(ref)
            except (OSError, ValueError, PathAccessError):
                scan_errors += 1
                continue
            for child, _item_stat, _name in reversed(entries):
                stack.append(child)
        return ExpansionResult(tuple(files), scan_errors)

    @contextlib.contextmanager
    def open_file(self, ref: NodeRef) -> Iterator[BinaryIO]:
        if ref.kind != "file" or not ref.parts:
            raise PathAccessError("not_a_file", "所选节点不是普通文件")
        if self._secure_dir_fd:
            fd = self._open_file_at_root(ref)
        else:
            fd = self._open_file_fallback(ref)
        try:
            item_stat = os.fstat(fd)
            if not stat.S_ISREG(item_stat.st_mode):
                raise PathAccessError("not_a_file", "所选节点不是普通文件")
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = -1
                yield handle
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _validate_regular_file(self, ref: NodeRef) -> None:
        with self.open_file(ref):
            return

    def _directory_flags(self) -> int:
        return (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )

    def _open_directory_at_root(self, ref: NodeRef) -> int:
        try:
            current = os.dup(self._root_fds[ref.root_id])
        except (KeyError, OSError) as exc:
            raise PathAccessError("invalid_node", "节点无效或已过期") from exc
        try:
            for part in ref.parts:
                next_fd = os.open(part, self._directory_flags(), dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                os.close(current)
            raise

    def _open_file_at_root(self, ref: NodeRef) -> int:
        parent = NodeRef(ref.root_id, ref.parts[:-1], "directory")
        parent_fd = self._open_directory_at_root(parent)
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            return os.open(ref.parts[-1], flags, dir_fd=parent_fd)
        except (OSError, ValueError) as exc:
            raise PathAccessError("file_unavailable", "文件暂时无法读取") from exc
        finally:
            os.close(parent_fd)

    def _path_for_fallback(self, ref: NodeRef) -> Path:
        try:
            root = self._roots[ref.root_id]
        except KeyError as exc:
            raise PathAccessError("invalid_node", "节点无效或已过期") from exc
        if any(part in {"", ".", ".."} or "\x00" in part for part in ref.parts):
            raise PathAccessError("invalid_node", "节点无效或已过期")
        candidate = root.path
        try:
            for part in ref.parts:
                candidate = candidate / part
                component_stat = candidate.lstat()
                if stat.S_ISLNK(component_stat.st_mode) or self._is_reparse_path(
                    candidate, component_stat
                ):
                    raise PathAccessError("invalid_node", "节点无效或已过期")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.path)
        except PathAccessError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise PathAccessError("invalid_node", "节点无效或已过期") from exc
        return resolved

    def _open_file_fallback(self, ref: NodeRef) -> int:
        path = self._path_for_fallback(ref)
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or self._is_reparse_path(path, before):
                raise PathAccessError("not_a_file", "所选节点不是普通文件")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(fd)
                raise PathAccessError("file_changed", "文件在打开时发生变化，请重试")
            return fd
        except PathAccessError:
            raise
        except OSError as exc:
            raise PathAccessError("file_unavailable", "文件暂时无法读取") from exc

    def _scan_directory(self, ref: NodeRef) -> list[tuple[NodeRef, os.stat_result, str]]:
        if ref.kind != "directory":
            raise PathAccessError("not_a_directory", "所选节点不是目录")
        if self._secure_dir_fd:
            directory_fd = self._open_directory_at_root(ref)
            try:
                with os.scandir(directory_fd) as iterator:
                    return self._collect_entries(ref, iterator)
            finally:
                os.close(directory_fd)
        path = self._path_for_fallback(ref)
        with os.scandir(path) as iterator:
            return self._collect_entries(ref, iterator)

    def _collect_entries(
        self, parent: NodeRef, iterator: os.ScandirIterator
    ) -> list[tuple[NodeRef, os.stat_result, str]]:
        result: list[tuple[NodeRef, os.stat_result, str]] = []
        for entry in iterator:
            try:
                if self._is_hidden(entry):
                    continue
                item_stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or self._is_reparse_entry(entry, item_stat):
                    continue
                if stat.S_ISDIR(item_stat.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(item_stat.st_mode):
                    kind = "file"
                else:
                    continue
                child = NodeRef(parent.root_id, parent.parts + (entry.name,), kind)
                result.append((child, item_stat, entry.name))
            except (OSError, ValueError):
                continue
        return result

    def _is_hidden(self, entry: os.DirEntry) -> bool:
        if self._config.browse.show_hidden_files:
            return False
        if entry.name.startswith("."):
            return True
        try:
            attributes = entry.stat(follow_symlinks=False).st_file_attributes
        except (AttributeError, OSError):
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0))

    @staticmethod
    def _is_reparse_entry(entry: os.DirEntry, item_stat: os.stat_result) -> bool:
        attributes = getattr(item_stat, "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            return True
        is_junction = getattr(os.path, "isjunction", None)
        return bool(is_junction and is_junction(entry.path))

    @staticmethod
    def _is_reparse_path(path: Path, item_stat: os.stat_result) -> bool:
        attributes = getattr(item_stat, "st_file_attributes", 0)
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            return True
        is_junction = getattr(os.path, "isjunction", None)
        return bool(is_junction and is_junction(path))
