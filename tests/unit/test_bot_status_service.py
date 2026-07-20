import unittest

from midware.schemas.bot import BotStatusUpdate
from midware.services.bot_status_service import BotStatusService


class BotStatusServiceTests(unittest.TestCase):
    def test_heartbeat_older_than_five_seconds_is_disconnected(self):
        service = BotStatusService()
        service.update(BotStatusUpdate(connected=True), received_at=10.0)
        self.assertEqual(service.snapshot(now=16.0).health, "disconnected")
        self.assertFalse(service.snapshot(now=16.0).active)

    def test_server_receive_time_controls_health(self):
        service = BotStatusService()
        service.update(BotStatusUpdate(connected=True), received_at=10.0)
        self.assertEqual(service.snapshot(now=11.0).health, "healthy")
        self.assertEqual(service.snapshot(now=13.0).health, "degraded")
