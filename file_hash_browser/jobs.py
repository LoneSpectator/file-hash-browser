from __future__ import annotations

import os
import queue
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from .algorithms import AlgorithmRegistry
from .config import AppConfig
from .paths import NodeRef, PathAccessError, PathAuthority, SelectionTooLarge
from .store import (
    ActiveJobLimitError,
    HashResult,
    JobRecord,
    Store,
    StoreError,
    WorkItemRecord,
    utc_now,
)


class JobRequestError(RuntimeError):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class _JobSpec:
    record: JobRecord
    selected: tuple[NodeRef, ...]


@dataclass(frozen=True, slots=True)
class _QueuedWork:
    item: WorkItemRecord
    generation: int


class JobManager:
    """Application-owned background enumerator and bounded global worker pool."""

    def __init__(
        self,
        config: AppConfig,
        authority: PathAuthority,
        registry: AlgorithmRegistry,
        store: Store,
    ) -> None:
        self._config = config
        self._authority = authority
        self._registry = registry
        self._store = store
        self._stop = threading.Event()
        self._cancel_lock = threading.Lock()
        self._cancelled: OrderedDict[str, None] = OrderedDict()
        self._preparation_queue: queue.Queue[_JobSpec | None] = queue.Queue(
            maxsize=config.hashing.max_active_jobs
        )
        work_queue_size = max(
            config.effective_parallel_tasks,
            config.effective_parallel_tasks * config.hashing.queue_multiplier,
        )
        self._work_queue: queue.Queue[_QueuedWork | None] = queue.Queue(
            maxsize=work_queue_size
        )
        self._preparer = threading.Thread(
            target=self._preparation_loop,
            name="hash-job-enumerator",
            daemon=True,
        )
        self._workers = [
            threading.Thread(
                target=self._worker_loop,
                name=f"hash-worker-{index + 1}",
                daemon=True,
            )
            for index in range(config.effective_parallel_tasks)
        ]
        self._preparer.start()
        for worker in self._workers:
            worker.start()

    def submit(
        self,
        *,
        entry_ids: list[str],
        algorithm_ids: list[str],
        strategy: str,
        idempotency_key: str | None,
    ) -> tuple[JobRecord, bool]:
        if strategy not in {"missing-only", "recalculate"}:
            raise JobRequestError("invalid_strategy", "计算策略无效")
        if not entry_ids:
            raise JobRequestError("empty_selection", "请至少选择一个文件或目录")
        if len(entry_ids) > self._config.hashing.max_selections_per_job:
            raise JobRequestError("too_many_selections", "选择的节点数量超过服务端限制", 413)
        if not algorithm_ids:
            raise JobRequestError("empty_algorithms", "请至少选择一种哈希算法")
        if len(algorithm_ids) > self._config.hashing.max_algorithms_per_job:
            raise JobRequestError("too_many_algorithms", "选择的算法数量超过服务端限制", 413)
        if len(set(algorithm_ids)) != len(algorithm_ids):
            raise JobRequestError("duplicate_algorithm", "哈希算法不能重复选择")
        unknown = [item for item in algorithm_ids if item not in self._registry]
        if unknown:
            raise JobRequestError("unknown_algorithm", "包含服务端未启用的哈希算法")
        try:
            selected = self._authority.resolve_tokens(entry_ids)
        except PathAccessError as exc:
            raise JobRequestError(exc.code, str(exc), 404) from exc
        if not selected:
            raise JobRequestError("empty_selection", "请至少选择一个文件或目录")

        ordered_algorithms = tuple(
            algorithm.id for algorithm in self._registry if algorithm.id in set(algorithm_ids)
        )
        try:
            record, created = self._store.create_job(
                job_id=uuid.uuid4().hex,
                idempotency_key=idempotency_key,
                strategy=strategy,
                algorithm_ids=ordered_algorithms,
                selected_count=len(selected),
                max_active_jobs=self._config.hashing.max_active_jobs,
            )
        except ActiveJobLimitError as exc:
            raise JobRequestError("job_queue_full", "后台任务队列已满，请稍后再试", 429) from exc
        except StoreError as exc:
            raise JobRequestError("storage_unavailable", "任务暂时无法保存", 503) from exc
        if not created:
            return record, False
        try:
            self._preparation_queue.put_nowait(_JobSpec(record, selected))
        except queue.Full as exc:
            self._store.fail_job(record.id, "job_queue_full")
            raise JobRequestError("job_queue_full", "后台任务队列已满，请稍后再试", 429) from exc
        return record, True

    def cancel(self, job_id: str) -> bool:
        self._remember_cancelled(job_id)
        return self._store.delete_job(job_id)

    def clear_all(self) -> int:
        job_ids = self._store.delete_all_jobs()
        for job_id in job_ids:
            self._remember_cancelled(job_id)
        return len(job_ids)

    def shutdown(self, wait: bool = True) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._put_sentinel(self._preparation_queue)
        for _ in self._workers:
            self._put_sentinel(self._work_queue)
        if wait:
            self._preparer.join(timeout=10)
            for worker in self._workers:
                worker.join(timeout=10)

    def _put_sentinel(self, target: queue.Queue) -> None:
        try:
            target.put_nowait(None)
        except queue.Full:
            # Daemon threads are allowed to stop with the process; startup recovery
            # marks any persisted active task as interrupted.
            return

    def _remember_cancelled(self, job_id: str) -> None:
        with self._cancel_lock:
            self._cancelled[job_id] = None
            self._cancelled.move_to_end(job_id)
            while len(self._cancelled) > 10_000:
                self._cancelled.popitem(last=False)

    def _is_cancelled(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancelled

    def _preparation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                spec = self._preparation_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if spec is None:
                self._preparation_queue.task_done()
                return
            try:
                self._prepare(spec)
            finally:
                self._preparation_queue.task_done()

    def _prepare(self, spec: _JobSpec) -> None:
        if self._is_cancelled(spec.record.id):
            return
        self._store.mark_enumerating(spec.record.id)
        try:
            expanded = self._authority.expand_selection(
                spec.selected,
                max_files=self._config.hashing.max_files_per_job,
                max_depth=self._config.hashing.max_directory_depth,
                should_stop=lambda: self._stop.is_set()
                or self._is_cancelled(spec.record.id),
            )
            existing = (
                self._store.existing_algorithms(expanded.files, spec.record.algorithm_ids)
                if spec.record.strategy == "missing-only"
                else {}
            )
            if self._is_cancelled(spec.record.id):
                return
            work_items: list[WorkItemRecord] = []
            skipped = 0
            for ref in expanded.files:
                if spec.record.strategy == "missing-only":
                    available = existing.get((ref.root_id, ref.relative_path), set())
                    algorithms = tuple(
                        item for item in spec.record.algorithm_ids if item not in available
                    )
                else:
                    algorithms = spec.record.algorithm_ids
                if not algorithms:
                    skipped += 1
                    continue
                work_items.append(
                    WorkItemRecord(
                        id=uuid.uuid4().hex,
                        job_id=spec.record.id,
                        ref=ref,
                        algorithm_ids=algorithms,
                    )
                )
            self._store.prepare_job(
                spec.record.id,
                work_items,
                discovered_files=len(expanded.files),
                skipped_files=skipped,
                scan_errors=expanded.scan_errors,
            )
            for item in work_items:
                while not self._stop.is_set():
                    if self._is_cancelled(spec.record.id):
                        return
                    try:
                        self._work_queue.put(
                            _QueuedWork(item, spec.record.generation), timeout=0.5
                        )
                        break
                    except queue.Full:
                        continue
                else:
                    self._store.fail_job(spec.record.id, "server_stopping")
                    return
        except SelectionTooLarge:
            self._store.fail_job(spec.record.id, "selection_too_large")
        except (PathAccessError, StoreError):
            self._store.fail_job(spec.record.id, "enumeration_failed")
        except Exception:
            # Do not let an unexpected filesystem/plugin error kill the enumerator.
            self._store.fail_job(spec.record.id, "internal_error")

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                queued = self._work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if queued is None:
                self._work_queue.task_done()
                return
            try:
                self._calculate(queued)
            finally:
                self._work_queue.task_done()

    def _calculate(self, queued: _QueuedWork) -> None:
        item = queued.item
        try:
            if self._is_cancelled(item.job_id):
                return
            if not self._store.mark_item_running(item.id):
                return
            hashers = self._registry.create_hashers(item.algorithm_ids)
            bytes_read = 0
            with self._authority.open_file(item.ref) as handle:
                # Stop at the size observed on the opened handle so a concurrently
                # growing file cannot occupy a worker forever.
                remaining = max(0, os.fstat(handle.fileno()).st_size)
                while remaining:
                    if self._is_cancelled(item.job_id):
                        return
                    block = handle.read(min(self._config.hashing.chunk_size_bytes, remaining))
                    if not block:
                        break
                    for hasher in hashers.values():
                        hasher.update(block)
                    block_size = len(block)
                    bytes_read += block_size
                    remaining -= block_size
            calculated_at = utc_now()
            if self._is_cancelled(item.job_id):
                return
            results = [
                HashResult(
                    algorithm_id=algorithm_id,
                    digest=hasher.hexdigest().lower(),
                    size=bytes_read,
                    calculated_at=calculated_at,
                    generation=queued.generation,
                )
                for algorithm_id, hasher in hashers.items()
            ]
            self._store.finish_item(item.id, results=results)
        except (OSError, PathAccessError):
            self._store.finish_item(item.id, error_code="file_unavailable")
        except Exception:
            # Public job details expose only this stable code, never path/plugin text.
            try:
                self._store.finish_item(item.id, error_code="calculation_failed")
            except Exception:
                return
