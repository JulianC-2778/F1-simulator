from __future__ import annotations

from typing import Any

from midware.adapters.torcs_udp import TorcsUdpAdapter
from midware.telemetry import TelemetryStore


class TelemetryService:
    def __init__(self, *, host: str = "0.0.0.0", port: int, window_seconds: float = 30.0) -> None:
        self.store = TelemetryStore(window_seconds=window_seconds)
        self.ingestor = TorcsUdpAdapter(self.store, host=host, port=port)

    def start(self) -> None:
        self.ingestor.start()

    def stop(self) -> None:
        self.ingestor.stop()

    def status(self) -> dict[str, Any]:
        return {"store": self.store.status(), "ingestor": self.ingestor.status()}
