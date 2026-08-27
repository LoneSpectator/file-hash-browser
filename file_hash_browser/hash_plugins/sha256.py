from __future__ import annotations

import hashlib


def register(registry) -> None:
    registry.register(
        algorithm_id="sha256",
        label="SHA-256",
        description="256 位文件摘要",
        digest_length=64,
        order=30,
        factory=hashlib.sha256,
    )

