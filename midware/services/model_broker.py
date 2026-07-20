from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


log = logging.getLogger(__name__)

MODEL_PRIORITIES = {
    "bot_strategy": 10,
    "engineer": 20,
    "commentary_event": 30,
    "coach": 50,
    "commentary_manual": 60,
    "commentary_baseline": 80,
}


class ModelQueueFull(RuntimeError):
    pass


class ModelJobStale(asyncio.CancelledError):
    pass


@dataclass(order=True)
class _Job:
    priority: int
    sequence: int
    request_id: str = field(compare=False)
    feature: str = field(compare=False)
    task: str = field(compare=False)
    created_at: float = field(compare=False)
    call: Callable[[], Awaitable[str]] = field(compare=False)
    future: asyncio.Future[str] = field(compare=False)
    timeout_s: float = field(compare=False)
    stale_key: str | None = field(compare=False, default=None)
    generation: int = field(compare=False, default=0)


class ModelBroker:
    """Bounded, single-concurrency priority broker for every model request."""

    def __init__(self, *, max_queue_size: int = 16, max_concurrency: int = 1) -> None:
        self.max_queue_size = max(1, int(max_queue_size))
        self.max_concurrency = max(1, int(max_concurrency))
        self._queue: asyncio.PriorityQueue[_Job] = asyncio.PriorityQueue(self.max_queue_size)
        self._sequence = itertools.count()
        self._workers: list[asyncio.Task[Any]] = []
        self._generations: dict[str, int] = {}
        self._active = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0
        self._last_error = ""

    async def submit(
        self,
        call: Callable[[], Awaitable[str]],
        *,
        feature: str,
        task: str,
        priority: int,
        timeout_s: float,
        stale_key: str | None = None,
        request_id: str | None = None,
    ) -> str:
        self._ensure_workers()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        generation = 0
        if stale_key:
            generation = self._generations.get(stale_key, 0) + 1
            self._generations[stale_key] = generation
        job = _Job(
            priority=int(priority),
            sequence=next(self._sequence),
            request_id=request_id or str(uuid.uuid4()),
            feature=feature,
            task=task,
            created_at=time.monotonic(),
            call=call,
            future=future,
            timeout_s=float(timeout_s),
            stale_key=stale_key,
            generation=generation,
        )
        self._put_bounded(job)
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    def _put_bounded(self, job: _Job) -> None:
        try:
            self._queue.put_nowait(job)
            return
        except asyncio.QueueFull:
            pass

        queued = self._queue._queue  # protected access is localized to atomic eviction
        worst = max(queued, key=lambda item: (item.priority, item.sequence))
        if worst.priority <= job.priority:
            self._dropped += 1
            raise ModelQueueFull(f"model queue full; rejected {job.task}")
        queued.remove(worst)
        heapq.heapify(queued)
        self._dropped += 1
        if not worst.future.done():
            worst.future.set_exception(ModelQueueFull(f"model queue full; evicted {worst.task}"))
        self._queue.put_nowait(job)

    def _ensure_workers(self) -> None:
        self._workers = [worker for worker in self._workers if not worker.done()]
        while len(self._workers) < self.max_concurrency:
            self._workers.append(asyncio.create_task(self._worker()))

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.future.cancelled() or self._is_stale(job):
                    self._dropped += 1
                    if not job.future.done():
                        job.future.set_exception(ModelJobStale(f"stale model job: {job.task}"))
                    continue
                self._active += 1
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(job.call(), timeout=job.timeout_s)
                    if not job.future.done():
                        job.future.set_result(result)
                    self._completed += 1
                    status = "ok"
                except Exception as exc:
                    self._failed += 1
                    self._last_error = str(exc)
                    status = "error"
                    if not job.future.done():
                        job.future.set_exception(exc)
                finally:
                    self._active -= 1
                log.info(
                    "model_request request_id=%s feature=%s task=%s wait_s=%.3f execution_s=%.3f status=%s",
                    job.request_id,
                    job.feature,
                    job.task,
                    started - job.created_at,
                    time.monotonic() - started,
                    status,
                )
            finally:
                self._queue.task_done()

    def _is_stale(self, job: _Job) -> bool:
        return bool(job.stale_key and self._generations.get(job.stale_key) != job.generation)

    def invalidate(self, stale_key: str) -> None:
        """Mark queued jobs for a coalescing key stale without cancelling active work."""
        self._generations[stale_key] = self._generations.get(stale_key, 0) + 1

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def status(self) -> dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "max_queue_size": self.max_queue_size,
            "active": self._active,
            "queued": self._queue.qsize(),
            "completed": self._completed,
            "failed": self._failed,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }
