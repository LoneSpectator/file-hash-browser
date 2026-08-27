from __future__ import annotations

import hashlib


def register(registry) -> None:
    registry.register(
        algorithm_id="sha1",
        label="SHA-1",
        description="160 位文件摘要",
        digest_length=40,
        order=20,
        factory=lambda: hashlib.sha1(usedforsecurity=False),
    )

