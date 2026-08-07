#!/usr/bin/env python3
"""Capture the two commentary latency stages the backend cannot see -- t4
(caption displayed) and t5 (TTS playback started) -- by timing when the
corresponding WebSocket messages arrive at a client.

commentary_test_plan.md work package C needs t0-t5, but midware only logs
t0-t3 (midware/latency_log.py). t4/t5 happen in the browser dashboard. This
script stands in for that browser: it subscribes to midware's /ws feed and
stamps the arrival of each `ai_done` (t4) and `tts_audio` (t5).

WHAT THE NUMBERS MEAN -- read before quoting them in the paper
--------------------------------------------------------------
t4/t5 here are *delivery* times, not paint/playback times: the instant the
message reached a local WebSocket client, which is a lower bound on when a
real browser finished rendering the caption or began playing audio. On
loopback the gap is sub-millisecond, so it is a good approximation, but it
is an approximation and must be disclosed as one.

Two structural properties of the backend that shape these numbers:

  * `tts_audio` is broadcast immediately after `ai_done` from the same
    `_commit()` block (midware/runtime.py), and the audio was already
    synthesised before either was sent. So t5 - t4 measures a broadcast gap,
    NOT a playback pipeline, and when TTS is on the synthesis cost is already
    inside t3/t4. Do not present t5 - t4 as "audio startup cost".
  * A commentary suppressed by the display-time dedup arrives as `ai_done`
    with `duplicate: true` and empty content, and no `tts_audio` follows. It
    never becomes a visible caption, so it has no caption latency. Those are
    recorded here with `duplicate: true` so build_latency_csv.py can account
    for them explicitly rather than mistaking them for capture failures.

CLOCK
-----
Timestamps use `time.monotonic()`, which on Linux is CLOCK_MONOTONIC and is
shared system-wide -- verified to equal /proc/uptime across processes and
across interpreters on the test machine. That is what makes these rows
directly joinable with midware's latency JSONL with no conversion. It also
means the join is only valid within one boot: reboot the machine (or
`wsl.exe --shutdown`) mid-experiment and every timestamp before it is
incomparable to every timestamp after.

Usage:
    python capture_display_latency.py --out capture.jsonl --session-id S1
    # drive, trigger events, then Ctrl-C to stop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover - dependency is present in the midware venv
    print(
        "websockets is not installed in this interpreter; run with the midware venv, "
        "e.g. .venv/bin/python evaluation/commentary/scripts/capture_display_latency.py",
        file=sys.stderr,
    )
    raise SystemExit(2)

# Message type -> latency stage it establishes.
STAGE_FOR_TYPE = {
    "ai_done": "t4_caption_displayed",
    "tts_audio": "t5_tts_started",
}


class Capture:
    """Turns the /ws message stream into latency rows.

    `event_detected` carries the event type but no request_id; every later
    message for that same commentary carries the request_id but not the event
    type. They arrive strictly in that order (runtime.py broadcasts
    event_detected before scheduling generation), so the pending event type is
    bound to the first request_id seen after it -- that is the only link
    between an event and its request, and event_id is a required column of
    LATENCY_SCHEMA.
    """

    def __init__(self, out_path: Path, session_id: str, verbose: bool = True):
        self.out_path = out_path
        self.session_id = session_id
        self.verbose = verbose
        self._pending_event_type: str | None = None
        self._event_type_for_request: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._fh = out_path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._fh.close()

    def _write(self, row: dict) -> None:
        self._fh.write(json.dumps(row) + "\n")
        self._fh.flush()  # a mid-session crash must not cost the whole run

    def _bind_event_type(self, request_id: str) -> str:
        if request_id not in self._event_type_for_request and self._pending_event_type:
            self._event_type_for_request[request_id] = self._pending_event_type
            self._pending_event_type = None
        return self._event_type_for_request.get(request_id, "")

    def handle(self, raw: str, arrived_at: float) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("type")

        if msg_type == "event_detected":
            # The key is `event_type`, not `type` -- CommentaryEngine.detect_event
            # builds {"event_type": ..., "reason": ..., "priority": ...} and the
            # broadcast nests it under "event". Reading "type" here silently
            # yielded empty event types for a whole collection run.
            event = msg.get("event") or {}
            self._pending_event_type = str(event.get("event_type") or "") or None
            return

        request_id = msg.get("request_id")
        if not request_id:
            return
        event_type = self._bind_event_type(str(request_id))

        if msg_type == "error":
            self._write({
                "session_id": self.session_id,
                "request_id": request_id,
                "event_type": event_type,
                "stage": "failure",
                "timestamp": arrived_at,
                "reason": str(msg.get("message") or "unspecified error")[:300],
            })
            self._bump("failure")
            return

        stage = STAGE_FOR_TYPE.get(str(msg_type))
        if stage is None:
            return

        row = {
            "session_id": self.session_id,
            "request_id": request_id,
            "event_type": event_type,
            "stage": stage,
            "timestamp": arrived_at,
        }
        if msg_type == "ai_done":
            row["duplicate"] = bool(msg.get("duplicate"))
        self._write(row)
        self._bump(stage if not row.get("duplicate") else "deduplicated")

    def _bump(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        if self.verbose:
            captions = self._counts.get("t4_caption_displayed", 0)
            print(
                f"\r  captions={captions}  audio={self._counts.get('t5_tts_started', 0)}  "
                f"deduped={self._counts.get('deduplicated', 0)}  "
                f"failures={self._counts.get('failure', 0)}   ",
                end="",
                file=sys.stderr,
                flush=True,
            )

    def summary(self) -> str:
        if not self._counts:
            return "no messages captured"
        return ", ".join(f"{k}={v}" for k, v in sorted(self._counts.items()))


async def run(url: str, capture: Capture, reconnect: bool) -> None:
    while True:
        try:
            async with websockets.connect(url, max_size=None) as ws:
                print(f"connected to {url}", file=sys.stderr)
                async for raw in ws:
                    capture.handle(raw, time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A dropped connection during a frozen-config run is itself a
            # result: note it in the file so gaps in the data are explained
            # rather than silently attributed to "no events happened".
            capture._write({
                "session_id": capture.session_id,
                "request_id": "",
                "event_type": "",
                "stage": "disconnected",
                "timestamp": time.monotonic(),
                "reason": f"{type(exc).__name__}: {exc}"[:300],
            })
            print(f"\nwebsocket disconnected: {exc}", file=sys.stderr)
            if not reconnect:
                return
            await asyncio.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:8880/ws")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--no-reconnect", action="store_true",
                        help="stop on first disconnect instead of retrying")
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    capture = Capture(args.out, args.session_id)
    print(f"writing to {args.out} (Ctrl-C to stop)", file=sys.stderr)
    try:
        asyncio.run(run(args.url, capture, reconnect=not args.no_reconnect))
    except KeyboardInterrupt:
        pass
    finally:
        capture.close()
        print(f"\ncaptured: {capture.summary()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
