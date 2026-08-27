from __future__ import annotations

import hashlib


def register(registry) -> None:
    registry.register(
        algorithm_id="sha512",
        label="SHA-512",
        description="512 位文件摘要",
        digest_length=128,
        order=40,
        factory=hashlib.sha512,
    )

