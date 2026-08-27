from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from file_hash_browser.algorithms import load_registry
from file_hash_browser.jobs import JobManager
from file_hash_browser.paths import PathAuthority
from file_hash_browser.store import Store
from tests.helpers import write_config


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SlowHash:
    def __init__(self, started: threading.Event):
        self._hash = hashlib.sha256()
        self._started = started

    def update(self, data: bytes) -> None:
        if data:
            self._started.set()
            time.sleep(0.03)
        self._hash.update(data)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class JobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.config, self.root = write_config(base, parallel_tasks=2, chunk_size_bytes=4096)
        self.file = self.root / "payload.bin"
        self.file.write_bytes((b"file-hash-browser\0" * 8192))
        self.registry = load_registry(self.config.plugins)
        self.store = Store(self.config.database_path)
        self.authority = PathAuthority(self.config)
        self.manager = JobManager(self.config, self.authority, self.registry, self.store)

    def tearDown(self) -> None:
        self.manager.shutdown(wait=True)
        self.authority.close()
        self.temporary.cleanup()

    def file_node(self):
        root_id = self.authority.roots_public()[0]["id"]
        page = self.authority.list_children(root_id, 0, 20)
        return next(item for item in page.items if item.ref.kind == "file")

    def test_background_job_calculates_multiple_algorithms_and_removes_task(self) -> None:
        node = self.file_node()
        job, created = self.manager.submit(
            entry_ids=[node.node_id],
            algorithm_ids=["md5", "sha256"],
            strategy="recalculate",
            idempotency_key="integration-job-1",
        )
        self.assertTrue(created)

        def completed() -> bool:
            values = self.store.hashes_for([node.ref]).get(
                (node.ref.root_id, node.ref.relative_path), []
            )
            return len(values) == 2 and self.store.get_job(job.id) is None

        self.assertTrue(wait_until(completed), "background calculation did not complete")
        values = {
            item["algorithmId"]: item["value"]
            for item in self.store.hashes_for([node.ref])[
                (node.ref.root_id, node.ref.relative_path)
            ]
        }
        payload = self.file.read_bytes()
        self.assertEqual(values["md5"], hashlib.md5(payload, usedforsecurity=False).hexdigest())
        self.assertEqual(values["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.store.list_jobs(), [])

    def test_running_job_can_be_interrupted_without_writing_result(self) -> None:
        started = threading.Event()
        self.registry.register(
            algorithm_id="slowsha",
            label="Slow SHA",
            description="Test-only slow hash",
            digest_length=64,
            order=99,
            factory=lambda: SlowHash(started),
        )
        node = self.file_node()
        job, _created = self.manager.submit(
            entry_ids=[node.node_id],
            algorithm_ids=["slowsha"],
            strategy="recalculate",
            idempotency_key="integration-job-cancel",
        )
        self.assertTrue(started.wait(3), "worker never started")
        self.assertTrue(self.manager.cancel(job.id))
        self.assertTrue(wait_until(lambda: self.store.get_job(job.id) is None))
        time.sleep(0.08)
        values = self.store.hashes_for([node.ref]).get(
            (node.ref.root_id, node.ref.relative_path), []
        )
        self.assertFalse(any(item["algorithmId"] == "slowsha" for item in values))


if __name__ == "__main__":
    unittest.main()

