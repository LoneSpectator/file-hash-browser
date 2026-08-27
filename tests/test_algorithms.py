from __future__ import annotations

import unittest

from file_hash_browser.algorithms import load_registry
from file_hash_browser.config import PluginConfig


class BuiltInAlgorithmTests(unittest.TestCase):
    VECTORS = {
        "md5": "900150983cd24fb0d6963f7d28e17f72",
        "sha1": "a9993e364706816aba3e25717850c26c9cd0d89d",
        "sha256": (
            "ba7816bf8f01cfea414140de5dae2223"
            "b00361a396177a9cb410ff61f20015ad"
        ),
        "sha512": (
            "ddaf35a193617abacc417349ae204131"
            "12e6fa4e89a97ea20a9eeee64b55d39a"
            "2192992a274fc1a836ba3c23a3feebbd"
            "454d4423643ce80e2a9ac94fa54ca49f"
        ),
    }

    def test_builtin_plugins_match_standard_abc_vectors(self) -> None:
        registry = load_registry(
            PluginConfig(directory=None, enabled_algorithms=None)
        )

        self.assertEqual(registry.ids(), tuple(self.VECTORS))
        hashers = registry.create_hashers(tuple(self.VECTORS))
        for algorithm_id, expected in self.VECTORS.items():
            with self.subTest(algorithm=algorithm_id):
                hashers[algorithm_id].update(b"abc")
                digest = hashers[algorithm_id].hexdigest()
                self.assertEqual(digest, expected)
                self.assertEqual(
                    registry.get(algorithm_id).digest_length,
                    len(expected),
                )


if __name__ == "__main__":
    unittest.main()
