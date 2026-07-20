import asyncio
import unittest

from midware.services.model_broker import ModelBroker, ModelJobStale, ModelQueueFull


class ModelBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_jobs_execute_by_priority(self):
        broker = ModelBroker(max_queue_size=4)
        started = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def blocker():
            started.set()
            await release.wait()
            order.append("active")
            return "active"

        async def record(name):
            order.append(name)
            return name

        active = asyncio.create_task(broker.submit(blocker, feature="coach", task="coach", priority=50, timeout_s=2))
        await started.wait()
        baseline = asyncio.create_task(broker.submit(lambda: record("baseline"), feature="commentary", task="baseline", priority=80, timeout_s=2))
        engineer = asyncio.create_task(broker.submit(lambda: record("engineer"), feature="engineer", task="engineer", priority=20, timeout_s=2))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(active, baseline, engineer), ["active", "baseline", "engineer"])
        self.assertEqual(order, ["active", "engineer", "baseline"])
        await broker.close()

    async def test_full_queue_rejects_lower_priority(self):
        broker = ModelBroker(max_queue_size=1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            started.set()
            await release.wait()
            return "active"

        active = asyncio.create_task(broker.submit(blocker, feature="bot", task="active", priority=10, timeout_s=2))
        await started.wait()
        queued = asyncio.create_task(broker.submit(lambda: asyncio.sleep(0, result="queued"), feature="engineer", task="queued", priority=20, timeout_s=2))
        await asyncio.sleep(0)
        with self.assertRaises(ModelQueueFull):
            await broker.submit(lambda: asyncio.sleep(0, result="low"), feature="commentary", task="low", priority=80, timeout_s=2)
        release.set()
        await active
        await queued
        await broker.close()

    async def test_latest_stale_key_supersedes_queued_job(self):
        broker = ModelBroker(max_queue_size=3)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            started.set()
            await release.wait()
            return "active"

        active = asyncio.create_task(broker.submit(blocker, feature="coach", task="active", priority=10, timeout_s=2))
        await started.wait()
        old = asyncio.create_task(broker.submit(lambda: asyncio.sleep(0, result="old"), feature="bot", task="old", priority=20, timeout_s=2, stale_key="bot:1"))
        new = asyncio.create_task(broker.submit(lambda: asyncio.sleep(0, result="new"), feature="bot", task="new", priority=20, timeout_s=2, stale_key="bot:1"))
        await asyncio.sleep(0)
        release.set()
        await active
        with self.assertRaises(ModelJobStale):
            await old
        self.assertEqual(await new, "new")
        await broker.close()
