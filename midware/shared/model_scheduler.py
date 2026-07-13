from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar


T = TypeVar("T")

MODEL_PRIORITIES = {
    "engineer": 10,
    "bot": 15,
    "commentary_event": 30,
    "commentary": 50,
    "coach": 70,
    "commentary_baseline": 80,
}


@dataclass(order=True)
class _ModelJob:
    priority: int
    sequence: int
    task: str = field(compare=False)
    created_at: float = field(compare=False)
    call: Callable[[], Awaitable[Any]] = field(compare=False)
    future: asyncio.Future[Any] = field(compare=False)
    timeout: float | None = field(compare=False, default=None)


class ModelScheduler:
    """Priority queue for local model requests.

    The default concurrency is intentionally 1 because local LM Studio-style
    runtimes usually degrade sharply when multiple long generations overlap.
    """

    def __init__(self, *, max_concurrency: int = 1) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self._queue: asyncio.PriorityQueue[_ModelJob] = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._workers_started = False
        self._workers: list[asyncio.Task[Any]] = []
        self._active = 0
        self._completed = 0
        self._failed = 0
        self._last_error = ""

    def _ensure_workers(self) -> None:
        if self._workers_started:
            return
        self._workers_started = True
        for index in range(self.max_concurrency):
            self._workers.append(asyncio.create_task(self._worker(index)))

    async def run(
        self,
        call: Callable[[], Awaitable[T]],
        *,
        task: str,
        priority: int,
        timeout: float | None = None,
    ) -> T:
        self._ensure_workers()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put(
            _ModelJob(
                priority=priority,
                sequence=next(self._sequence),
                task=task,
                created_at=time.time(),
                call=call,
                future=future,
                timeout=timeout,
            )
        )
        return await future

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            self._active += 1
            try:
                if job.timeout:
                    result = await asyncio.wait_for(job.call(), timeout=job.timeout)
                else:
                    result = await job.call()
                if not job.future.done():
                    job.future.set_result(result)
                self._completed += 1
            except Exception as exc:
                self._failed += 1
                self._last_error = str(exc)
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self._active -= 1
                self._queue.task_done()

    def status(self) -> dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "active": self._active,
            "queued": self._queue.qsize(),
            "completed": self._completed,
            "failed": self._failed,
            "last_error": self._last_error,
        }


default_model_scheduler = ModelScheduler(max_concurrency=1)
