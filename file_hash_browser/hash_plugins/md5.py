from __future__ import annotations

import hashlib


def register(registry) -> None:
    registry.register(
        algorithm_id="md5",
        label="MD5",
        description="128 位文件摘要",
        digest_length=32,
        order=10,
        factory=lambda: hashlib.md5(usedforsecurity=False),
    )

