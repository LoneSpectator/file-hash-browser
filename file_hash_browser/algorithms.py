from __future__ import annotations

import importlib
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Protocol

from .config import PluginConfig


ALGORITHM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class HashObject(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


HashFactory = Callable[[], HashObject]


class PluginError(RuntimeError):
    """Raised when a configured hash plugin is invalid."""


@dataclass(frozen=True, slots=True)
class Algorithm:
    id: str
    label: str
    description: str
    digest_length: int
    order: int
    factory: HashFactory

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "digestLength": self.digest_length,
            "order": self.order,
        }


class AlgorithmRegistry:
    """Trusted algorithm registry populated by built-in or administrator plugins."""

    def __init__(self) -> None:
        self._algorithms: dict[str, Algorithm] = {}

    def register(
        self,
        *,
        algorithm_id: str,
        label: str,
        description: str,
        digest_length: int,
        order: int,
        factory: HashFactory,
    ) -> None:
        if not ALGORITHM_ID_PATTERN.fullmatch(algorithm_id):
            raise PluginError(f"invalid algorithm id: {algorithm_id!r}")
        if algorithm_id in self._algorithms:
            raise PluginError(f"duplicate algorithm id: {algorithm_id}")
        if not label.strip() or not description.strip():
            raise PluginError(f"algorithm {algorithm_id} needs a label and description")
        if not 1 <= digest_length <= 4096:
            raise PluginError(f"algorithm {algorithm_id} has an invalid digest length")
        if not callable(factory):
            raise PluginError(f"algorithm {algorithm_id} factory is not callable")
        try:
            probe = factory()
            probe.update(b"")
            digest = probe.hexdigest()
        except Exception as exc:  # plugin boundary
            raise PluginError(f"algorithm {algorithm_id} factory failed its probe") from exc
        if not isinstance(digest, str) or len(digest) != digest_length:
            raise PluginError(
                f"algorithm {algorithm_id} returned a digest with the wrong length"
            )
        self._algorithms[algorithm_id] = Algorithm(
            id=algorithm_id,
            label=label.strip(),
            description=description.strip(),
            digest_length=digest_length,
            order=order,
            factory=factory,
        )

    def restrict_to(self, enabled: tuple[str, ...]) -> None:
        unknown = [item for item in enabled if item not in self._algorithms]
        if unknown:
            raise PluginError(f"unknown enabled algorithm(s): {', '.join(unknown)}")
        enabled_set = set(enabled)
        self._algorithms = {
            key: value for key, value in self._algorithms.items() if key in enabled_set
        }

    def get(self, algorithm_id: str) -> Algorithm:
        try:
            return self._algorithms[algorithm_id]
        except KeyError as exc:
            raise KeyError(f"unknown algorithm: {algorithm_id}") from exc

    def create_hashers(self, algorithm_ids: tuple[str, ...]) -> dict[str, HashObject]:
        return {algorithm_id: self.get(algorithm_id).factory() for algorithm_id in algorithm_ids}

    def public_algorithms(self) -> list[dict[str, object]]:
        return [item.public_dict() for item in self]

    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self)

    def __contains__(self, algorithm_id: object) -> bool:
        return algorithm_id in self._algorithms

    def __len__(self) -> int:
        return len(self._algorithms)

    def __iter__(self):
        return iter(sorted(self._algorithms.values(), key=lambda item: (item.order, item.id)))


def _register_module(module: ModuleType, registry: AlgorithmRegistry, source: str) -> None:
    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginError(f"hash plugin {source} does not define register(registry)")
    try:
        register(registry)
    except PluginError:
        raise
    except Exception as exc:  # plugin boundary
        raise PluginError(f"hash plugin {source} failed during registration") from exc


def _load_external_plugins(directory: Path, registry: AlgorithmRegistry) -> None:
    for path in sorted(directory.glob("*.py"), key=lambda value: value.name.casefold()):
        if path.name.startswith("_"):
            continue
        module_name = f"file_hash_browser_external_{path.stem}_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot load hash plugin {path.name}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # plugin boundary
            raise PluginError(f"hash plugin {path.name} could not be imported") from exc
        _register_module(module, registry, path.name)


def load_registry(config: PluginConfig) -> AlgorithmRegistry:
    registry = AlgorithmRegistry()
    for module_name in ("md5", "sha1", "sha256", "sha512"):
        module = importlib.import_module(f".hash_plugins.{module_name}", __package__)
        _register_module(module, registry, module_name)
    if config.directory is not None:
        _load_external_plugins(config.directory, registry)
    if config.enabled_algorithms is not None:
        registry.restrict_to(config.enabled_algorithms)
    if not len(registry):
        raise PluginError("at least one hash algorithm must be enabled")
    return registry

