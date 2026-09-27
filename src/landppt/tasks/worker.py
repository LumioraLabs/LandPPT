"""Worker process for queued LandPPT tasks."""

from __future__ import annotations

import asyncio
import logging
import signal

from ..database.database import close_db, init_db
from ..services.background_tasks import TaskStatus, get_task_manager
from ..services.cache_service import close_cache_service
from .queue import TaskQueueUnavailable, dequeue_task
from .registry import get_handler

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, queue_name: str | None = None):
        self.queue_name = queue_name
        self._stopping = asyncio.Event()
        self.task_manager = get_task_manager()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        await init_db(bootstrap_admin=False)
        from ..services.credits_service import sweep_expired_credit_reservations

        sweep_task = asyncio.create_task(sweep_expired_credit_reservations())
        logger.info("LandPPT worker started queue=%s", self.queue_name or "default")
        try:
            await self._consume_until_stopped()
        finally:
            sweep_task.cancel()
            try:
                await sweep_task
            except asyncio.CancelledError:
                pass
            await close_cache_service()
            await close_db()

    async def _consume_until_stopped(self) -> None:
        while not self._stopping.is_set():
            try:
                message = await dequeue_task(self.queue_name)
            except TaskQueueUnavailable as exc:
                logger.error("Task queue unavailable: %s", exc)
                await asyncio.sleep(5)
                continue
            if message is None:
                continue

            task = await self.task_manager.get_task_async(message.task_id)
            if not task:
                logger.warning("Queued task metadata not found: %s", message.task_id)
                continue
            if task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                logger.info("Skipping task %s with status %s", task.task_id, task.status)
                continue

            try:
                handler = get_handler(message.task_type)
            except KeyError as exc:
                await self.task_manager.update_task_status_async(message.task_id, TaskStatus.FAILED, error=str(exc))
                continue
            await self.task_manager.execute_task(message.task_id, handler, task)


async def run_worker(queue_name: str | None = None) -> None:
    worker = Worker(queue_name=queue_name)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            pass
    await worker.run_forever()

