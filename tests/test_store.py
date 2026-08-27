from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from file_hash_browser.paths import NodeRef
from file_hash_browser.store import HashResult, Store, WorkItemRecord


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "data" / "hashes.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def complete_hash(
        self,
        ref: NodeRef,
        digest: str,
        *,
        generation: int,
        algorithm: str = "sha256",
        calculated_at: str = "2026-08-27T10:00:00.000Z",
    ) -> None:
        job_id = uuid.uuid4().hex
        job, created = self.store.create_job(
            job_id=job_id,
            idempotency_key=None,
            strategy="recalculate",
            algorithm_ids=(algorithm,),
            selected_count=1,
            max_active_jobs=10,
        )
        self.assertTrue(created)
        item = WorkItemRecord(uuid.uuid4().hex, job.id, ref, (algorithm,))
        self.store.prepare_job(
            job.id,
            [item],
            discovered_files=1,
            skipped_files=0,
            scan_errors=0,
        )
        self.assertTrue(self.store.mark_item_running(item.id))
        self.store.finish_item(
            item.id,
            results=[HashResult(algorithm, digest, 12, calculated_at, generation)],
        )
        self.assertIsNone(self.store.get_job(job.id))

    def test_completed_job_leaves_hash_but_no_task_history(self) -> None:
        ref = NodeRef("test", ("document.bin",), "file")
        self.complete_hash(ref, "a" * 64, generation=1)
        hashes = self.store.hashes_for([ref])[("test", "document.bin")]
        self.assertEqual(hashes[0]["value"], "a" * 64)
        self.assertEqual(self.store.list_jobs(), [])

    def test_older_job_cannot_overwrite_newer_generation(self) -> None:
        ref = NodeRef("test", ("document.bin",), "file")
        self.complete_hash(ref, "b" * 64, generation=200)
        self.complete_hash(ref, "a" * 64, generation=100)
        hashes = self.store.hashes_for([ref])[("test", "document.bin")]
        self.assertEqual(hashes[0]["value"], "b" * 64)

    def test_directory_prune_deletes_only_missing_direct_children(self) -> None:
        directory = NodeRef("test", ("folder",), "directory")
        present = NodeRef("test", ("folder", "present.bin"), "file")
        missing = NodeRef("test", ("folder", "missing.bin"), "file")
        nested = NodeRef("test", ("folder", "child", "nested.bin"), "file")
        self.complete_hash(present, "1" * 64, generation=1)
        self.complete_hash(missing, "2" * 64, generation=2)
        self.complete_hash(nested, "3" * 64, generation=3)

        self.store.prune_directory(
            directory,
            [present],
            scanned_at="9999-12-31T23:59:59.999Z",
        )
        hashes = self.store.hashes_for([present, missing, nested])
        self.assertIn(("test", present.relative_path), hashes)
        self.assertNotIn(("test", missing.relative_path), hashes)
        self.assertIn(("test", nested.relative_path), hashes)

    def test_clear_all_removes_only_tasks(self) -> None:
        ids = []
        for _ in range(2):
            job, _created = self.store.create_job(
                job_id=uuid.uuid4().hex,
                idempotency_key=None,
                strategy="missing-only",
                algorithm_ids=("sha256",),
                selected_count=1,
                max_active_jobs=10,
            )
            ids.append(job.id)
        self.assertEqual(set(self.store.delete_all_jobs()), set(ids))
        self.assertEqual(self.store.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()

