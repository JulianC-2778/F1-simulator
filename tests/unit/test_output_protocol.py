import unittest

from midware.shared.output_bus import OutputBus


class OutputProtocolTests(unittest.TestCase):
    def test_stream_has_stable_request_and_incrementing_sequence(self):
        bus = OutputBus()
        start = bus.normalize({"type": "ai_start", "source": "commentary"})
        token = bus.normalize({"type": "token", "source": "commentary", "text": "hi"})
        done = bus.normalize({"type": "ai_done", "source": "commentary", "content": "hi"})
        self.assertEqual({start["request_id"], token["request_id"], done["request_id"]}, {start["request_id"]})
        self.assertEqual([start["sequence"], token["sequence"], done["sequence"]], [0, 1, 2])
        self.assertEqual(done["version"], 1)
        self.assertEqual(done["type"], "ai_done")
        self.assertEqual(done["protocol_type"], "ai.done")
        self.assertEqual(token["text"], "hi")

    def test_unknown_source_is_not_forwarded(self):
        message = OutputBus().normalize({"type": "message", "source": "untrusted"})
        self.assertEqual(message["source"], "system")

    def test_explicit_ids_do_not_cross_concurrent_streams(self):
        bus = OutputBus()
        first_start = bus.normalize({"type": "ai_start", "source": "commentary", "request_id": "first"})
        second_start = bus.normalize({"type": "ai_start", "source": "commentary", "request_id": "second"})
        first_token = bus.normalize({"type": "token", "source": "commentary", "request_id": "first", "text": "a"})
        second_token = bus.normalize({"type": "token", "source": "commentary", "request_id": "second", "text": "b"})
        self.assertEqual((first_start["sequence"], first_token["sequence"]), (0, 1))
        self.assertEqual((second_start["sequence"], second_token["sequence"]), (0, 1))
