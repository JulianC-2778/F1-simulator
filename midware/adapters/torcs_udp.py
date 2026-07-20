from __future__ import annotations

import socket
import threading
import time
from typing import Any

from midware.schemas.telemetry import TelemetryFrame
from midware.telemetry import TelemetryStore, parse_udp_packet


def _float(frame: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(frame.get(key, default))
    except (TypeError, ValueError):
        return default


def _int(frame: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(frame.get(key, default)))
    except (TypeError, ValueError):
        return default


class TorcsTelemetryAdapter:
    """Convert TORCS boundary field names to the canonical schema once."""

    @staticmethod
    def convert(frame: dict[str, Any]) -> TelemetryFrame:
        return TelemetryFrame(
            seq=_int(frame, "seq"),
            sim_time_s=_float(frame, "sim_time"),
            lap=_int(frame, "lap"),
            speed_x_kmh=_float(frame, "speedX"),
            speed_y_kmh=_float(frame, "speedY"),
            rpm=_float(frame, "rpm"),
            gear=_int(frame, "gear"),
            fuel_l=_float(frame, "fuel"),
            damage=_float(frame, "damage"),
            track_position=_float(frame, "trackPos"),
            current_lap_time_s=_float(frame, "curLapTime"),
            last_lap_time_s=_float(frame, "lastLapTime"),
            race_position=_int(frame, "racePos"),
            throttle=_float(frame, "throttle"),
            brake=_float(frame, "brake"),
            steer=_float(frame, "steer"),
            distance_from_start_m=_float(frame, "distFromStart"),
            distance_raced_m=_float(frame, "distRaced"),
            track_sensors_m=[_float(frame, f"track_{index}", -1.0) for index in range(19)],
            opponent_sensors_m=[_float(frame, f"opponent_{index}", 200.0) for index in range(36)],
        )


class TorcsUdpAdapter:
    """Lifecycle-owned UDP ingestor. Binding errors are raised during start."""

    def __init__(self, store: TelemetryStore, *, host: str, port: int) -> None:
        self.store = store
        self.host = host
        self.port = port
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.received_frames = 0
        self.parse_failures = 0
        self.last_received_at: float | None = None
        self.last_source: tuple[str, int] | None = None
        self.last_error = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
            sock.settimeout(0.5)
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"Unable to bind telemetry UDP {self.host}:{self.port}: {exc}") from exc
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._listen, daemon=True, name="torcs-telemetry-ingestor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "received_frames": self.received_frames,
            "parse_failures": self.parse_failures,
            "last_received_at": self.last_received_at,
            "last_source": list(self.last_source) if self.last_source else None,
            "last_error": self.last_error,
        }

    def _listen(self) -> None:
        while not self._stop.is_set():
            sock = self._socket
            if sock is None:
                return
            try:
                data, address = sock.recvfrom(65535)
                packet = parse_udp_packet(data)
                if packet.telemetry is None and not packet.rankings:
                    self.parse_failures += 1
                    continue
                self.store.push(packet.telemetry, packet.rankings or None)
                if packet.telemetry is not None:
                    self.received_frames += 1
                self.last_received_at = round(time.time(), 3)
                self.last_source = address
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.last_error = str(exc)
                return
            except Exception as exc:  # malformed packets must not kill ingestion
                self.parse_failures += 1
                self.last_error = str(exc)
