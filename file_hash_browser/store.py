from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Sequence

from .paths import NodeRef


ACTIVE_STATUSES = ("queued", "enumerating", "running")
TERMINAL_STATUSES = (
    "completed",
    "completed_with_errors",
    "failed",
    "interrupted",
)


class StoreError(RuntimeError):
    pass


class ActiveJobLimitError(StoreError):
    pass


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    idempotency_key: str | None
    status: str
    strategy: str
    algorithm_ids: tuple[str, ...]
    generation: int
    created_at: str
    started_at: str | None
    completed_at: str | None
    selected_count: int
    discovered_files: int
    succeeded_files: int
    failed_files: int
    skipped_files: int
    scan_errors: int
    error_code: str | None

    @property
    def processed_files(self) -> int:
        return self.succeeded_files + self.failed_files + self.skipped_files

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "strategy": self.strategy,
            "algorithmIds": list(self.algorithm_ids),
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "selectedCount": self.selected_count,
            "progress": {
                "discovered": self.discovered_files,
                "processed": self.processed_files,
                "succeeded": self.succeeded_files,
                "failed": self.failed_files,
                "skipped": self.skipped_files,
                "scanErrors": self.scan_errors,
            },
            "errorCode": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    id: str
    job_id: str
    ref: NodeRef
    algorithm_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HashResult:
    algorithm_id: str
    digest: str
    size: int
    calculated_at: str
    generation: int


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    """Thread-safe-by-connection SQLite persistence for hashes and jobs."""

    SCHEMA_VERSION = 2

    def __init__(self, database_path: Path):
        self.path = database_path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise StoreError("cannot create the data directory") from exc
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, self.SCHEMA_VERSION):
                    raise StoreError(f"unsupported database schema version: {version}")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS hash_results (
                        root_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        parent_path TEXT NOT NULL,
                        algorithm_id TEXT NOT NULL,
                        digest TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        calculated_at TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        PRIMARY KEY (root_id, relative_path, algorithm_id)
                    ) WITHOUT ROWID;

                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        idempotency_key TEXT UNIQUE,
                        status TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        algorithm_ids TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        selected_count INTEGER NOT NULL,
                        discovered_files INTEGER NOT NULL DEFAULT 0,
                        succeeded_files INTEGER NOT NULL DEFAULT 0,
                        failed_files INTEGER NOT NULL DEFAULT 0,
                        skipped_files INTEGER NOT NULL DEFAULT 0,
                        scan_errors INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT
                    );

                    CREATE TABLE IF NOT EXISTS job_items (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        root_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        algorithm_ids TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_code TEXT,
                        UNIQUE (job_id, root_id, relative_path)
                    );

                    CREATE INDEX IF NOT EXISTS jobs_created_idx
                        ON jobs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS job_items_job_idx
                        ON job_items(job_id, status);
                    """
                )
                if version == 1:
                    connection.execute(
                        "ALTER TABLE hash_results ADD COLUMN parent_path TEXT NOT NULL DEFAULT ''"
                    )
                    rows = connection.execute(
                        "SELECT DISTINCT root_id, relative_path FROM hash_results"
                    ).fetchall()
                    connection.executemany(
                        """
                        UPDATE hash_results SET parent_path = ?
                        WHERE root_id = ? AND relative_path = ?
                        """,
                        [
                            (
                                row["relative_path"].rpartition("/")[0],
                                row["root_id"],
                                row["relative_path"],
                            )
                            for row in rows
                        ],
                    )
                connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
                # Jobs are an active queue, not history. A process restart interrupts
                # them, while ON DELETE CASCADE removes their private work items.
                connection.execute("DELETE FROM jobs")
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        except sqlite3.DatabaseError as exc:
            raise StoreError("cannot initialize the database") from exc

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            strategy=row["strategy"],
            algorithm_ids=tuple(json.loads(row["algorithm_ids"])),
            generation=row["generation"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            selected_count=row["selected_count"],
            discovered_files=row["discovered_files"],
            succeeded_files=row["succeeded_files"],
            failed_files=row["failed_files"],
            skipped_files=row["skipped_files"],
            scan_errors=row["scan_errors"],
            error_code=row["error_code"],
        )

    def create_job(
        self,
        *,
        job_id: str,
        idempotency_key: str | None,
        strategy: str,
        algorithm_ids: tuple[str, ...],
        selected_count: int,
        max_active_jobs: int,
    ) -> tuple[JobRecord, bool]:
        created_at = utc_now()
        generation = time.time_ns()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if idempotency_key is not None:
                    existing = connection.execute(
                        "SELECT * FROM jobs WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        return self._job_from_row(existing), False
                placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
                active = connection.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE status IN ({placeholders})",
                    ACTIVE_STATUSES,
                ).fetchone()[0]
                if active >= max_active_jobs:
                    raise ActiveJobLimitError("too many active jobs")
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, idempotency_key, status, strategy, algorithm_ids,
                        generation, created_at, selected_count
                    ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        idempotency_key,
                        strategy,
                        json.dumps(algorithm_ids, separators=(",", ":")),
                        generation,
                        created_at,
                        selected_count,
                    ),
                )
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return self._job_from_row(row), True
        except sqlite3.DatabaseError as exc:
            raise StoreError("could not create job") from exc

    def mark_enumerating(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'enumerating', started_at = COALESCE(started_at, ?)
                WHERE id = ? AND status = 'queued'
                """,
                (utc_now(), job_id),
            )

    def prepare_job(
        self,
        job_id: str,
        work_items: Sequence[WorkItemRecord],
        *,
        discovered_files: int,
        skipped_files: int,
        scan_errors: int,
    ) -> None:
        terminal_status = None
        if not work_items:
            terminal_status = "completed_with_errors" if scan_errors else "completed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT INTO job_items (
                    id, job_id, root_id, relative_path, algorithm_ids, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                [
                    (
                        item.id,
                        item.job_id,
                        item.ref.root_id,
                        item.ref.relative_path,
                        json.dumps(item.algorithm_ids, separators=(",", ":")),
                    )
                    for item in work_items
                ],
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, discovered_files = ?, skipped_files = ?, scan_errors = ?,
                    completed_at = CASE WHEN ? IS NULL THEN completed_at ELSE ? END
                WHERE id = ? AND status IN ('queued', 'enumerating')
                """,
                (
                    terminal_status or "running",
                    discovered_files,
                    skipped_files,
                    scan_errors,
                    terminal_status,
                    utc_now(),
                    job_id,
                ),
            )
            if terminal_status is not None:
                connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def fail_job(self, job_id: str, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def mark_item_running(self, item_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE job_items SET status = 'running' WHERE id = ? AND status = 'pending'",
                (item_id,),
            )
            return cursor.rowcount == 1

    def finish_item(
        self,
        item_id: str,
        *,
        results: Sequence[HashResult] = (),
        error_code: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT job_id, root_id, relative_path, status FROM job_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None or item["status"] not in ("pending", "running"):
                return
            success = error_code is None
            if success:
                for result in results:
                    connection.execute(
                        """
                        INSERT INTO hash_results (
                            root_id, relative_path, parent_path, algorithm_id, digest, size,
                            calculated_at, generation
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(root_id, relative_path, algorithm_id) DO UPDATE SET
                            digest = excluded.digest,
                            size = excluded.size,
                            calculated_at = excluded.calculated_at,
                            generation = excluded.generation
                        WHERE excluded.generation >= hash_results.generation
                        """,
                        (
                            item["root_id"],
                            item["relative_path"],
                            item["relative_path"].rpartition("/")[0],
                            result.algorithm_id,
                            result.digest,
                            result.size,
                            result.calculated_at,
                            result.generation,
                        ),
                    )
            connection.execute(
                "UPDATE job_items SET status = ?, error_code = ? WHERE id = ?",
                ("completed" if success else "failed", error_code, item_id),
            )
            counter = "succeeded_files" if success else "failed_files"
            connection.execute(
                f"UPDATE jobs SET {counter} = {counter} + 1 WHERE id = ?",
                (item["job_id"],),
            )
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (item["job_id"],)
            ).fetchone()
            processed = (
                job["succeeded_files"] + job["failed_files"] + job["skipped_files"]
            )
            if processed >= job["discovered_files"]:
                has_errors = job["failed_files"] > 0 or job["scan_errors"] > 0
                connection.execute(
                    "UPDATE jobs SET status = ?, completed_at = ? WHERE id = ?",
                    (
                        "completed_with_errors" if has_errors else "completed",
                        utc_now(),
                        item["job_id"],
                    ),
                )
                connection.execute("DELETE FROM jobs WHERE id = ?", (item["job_id"],))

    def existing_algorithms(
        self, refs: Sequence[NodeRef], algorithm_ids: tuple[str, ...]
    ) -> dict[tuple[str, str], set[str]]:
        result: dict[tuple[str, str], set[str]] = {}
        if not refs or not algorithm_ids:
            return result
        grouped: dict[str, list[str]] = {}
        for ref in refs:
            grouped.setdefault(ref.root_id, []).append(ref.relative_path)
        with self._connect() as connection:
            for root_id, paths in grouped.items():
                for start in range(0, len(paths), 400):
                    batch = paths[start : start + 400]
                    path_marks = ",".join("?" for _ in batch)
                    algorithm_marks = ",".join("?" for _ in algorithm_ids)
                    rows = connection.execute(
                        f"""
                        SELECT root_id, relative_path, algorithm_id FROM hash_results
                        WHERE root_id = ?
                          AND relative_path IN ({path_marks})
                          AND algorithm_id IN ({algorithm_marks})
                        """,
                        (root_id, *batch, *algorithm_ids),
                    )
                    for row in rows:
                        result.setdefault(
                            (row["root_id"], row["relative_path"]), set()
                        ).add(row["algorithm_id"])
        return result

    def prune_directory(
        self,
        directory: NodeRef,
        present_files: Sequence[NodeRef],
        *,
        scanned_at: str,
    ) -> int:
        """Delete stale direct-child file records observed missing during a full scan."""
        if directory.kind != "directory":
            return 0
        present_paths = [ref.relative_path for ref in present_files]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if present_paths:
                deleted = 0
                present_set = set(present_paths)
                # Stay below SQLite's host-parameter limit while keeping one transaction.
                rows = connection.execute(
                    """
                    SELECT DISTINCT relative_path FROM hash_results
                    WHERE root_id = ? AND parent_path = ? AND calculated_at <= ?
                    """,
                    (directory.root_id, directory.relative_path, scanned_at),
                ).fetchall()
                stale = [
                    row["relative_path"]
                    for row in rows
                    if row["relative_path"] not in present_set
                ]
                for start in range(0, len(stale), 400):
                    batch = stale[start : start + 400]
                    marks = ",".join("?" for _ in batch)
                    cursor = connection.execute(
                        f"""
                        DELETE FROM hash_results
                        WHERE root_id = ? AND parent_path = ?
                          AND relative_path IN ({marks}) AND calculated_at <= ?
                        """,
                        (
                            directory.root_id,
                            directory.relative_path,
                            *batch,
                            scanned_at,
                        ),
                    )
                    deleted += cursor.rowcount
                return deleted
            cursor = connection.execute(
                """
                DELETE FROM hash_results
                WHERE root_id = ? AND parent_path = ? AND calculated_at <= ?
                """,
                (directory.root_id, directory.relative_path, scanned_at),
            )
            return cursor.rowcount

    def hashes_for(
        self, refs: Iterable[NodeRef]
    ) -> dict[tuple[str, str], list[dict[str, object]]]:
        grouped: dict[str, list[str]] = {}
        for ref in refs:
            if ref.kind == "file":
                grouped.setdefault(ref.root_id, []).append(ref.relative_path)
        result: dict[tuple[str, str], list[dict[str, object]]] = {}
        if not grouped:
            return result
        with self._connect() as connection:
            for root_id, paths in grouped.items():
                unique_paths = list(dict.fromkeys(paths))
                for start in range(0, len(unique_paths), 400):
                    batch = unique_paths[start : start + 400]
                    marks = ",".join("?" for _ in batch)
                    rows = connection.execute(
                        f"""
                        SELECT root_id, relative_path, algorithm_id, digest, size, calculated_at
                        FROM hash_results
                        WHERE root_id = ? AND relative_path IN ({marks})
                        ORDER BY algorithm_id
                        """,
                        (root_id, *batch),
                    )
                    for row in rows:
                        result.setdefault(
                            (row["root_id"], row["relative_path"]), []
                        ).append(
                            {
                                "algorithmId": row["algorithm_id"],
                                "value": row["digest"],
                                "size": row["size"],
                                "calculatedAt": row["calculated_at"],
                            }
                        )
        return result

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs WHERE status IN ({marks})
                ORDER BY created_at DESC LIMIT ?
                """,
                (*ACTIVE_STATUSES, limit),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def delete_job(self, job_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cursor.rowcount == 1

    def delete_all_jobs(self) -> tuple[str, ...]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT id FROM jobs WHERE status IN ({marks})", ACTIVE_STATUSES
            ).fetchall()
            ids = tuple(row["id"] for row in rows)
            if ids:
                connection.execute(
                    f"DELETE FROM jobs WHERE status IN ({marks})", ACTIVE_STATUSES
                )
            return ids

    def active_job_count(self) -> int:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})", ACTIVE_STATUSES
            ).fetchone()[0]
