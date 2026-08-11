#!/usr/bin/env python3
"""
Feature 4: Granite-powered AI racing bot.

Steps implemented:
  1  parse_scr_state()    — decode TORCS SCR sensor string → Python dict
  2  format_scr_control() — encode control dict → TORCS SCR wire string
  3  ScrClient            — UDP handshake + main receive/send loop
     run_bot()            — connect to TORCS and drive
  4  compute_control()    — strategy-parameterized low-level controller
                            ATTACK / NORMAL / DEFEND / SAVE_FUEL / PIT / BLOCK
                            (BLOCK is system-only — see _GRANITE_STRATEGIES)
"""

from __future__ import annotations

import json
import math
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import atexit
from dataclasses import dataclass
from typing import Any

import config

try:
    from telemetry_common import (
        clamp, parse_float, parse_int,
        LatestTaskRunner, extract_json_object,
        compact_track_profile, compact_opponent_profile,
    )
    _TELEMETRY_AVAILABLE = True
except ImportError:
    _TELEMETRY_AVAILABLE = False
    # telemetry_common requires openai; define the three helpers locally
    # so tests can run without any extra dependencies installed.
    def parse_float(value: str, default: float = 0.0) -> float:  # type: ignore[misc]
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def parse_int(value: str, default: int = 0) -> int:  # type: ignore[misc]
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def clamp(value: float, low: float, high: float) -> float:  # type: ignore[misc]
        return max(low, min(high, value))

    def extract_json_object(text: str) -> dict[str, Any] | None:  # type: ignore[misc]
        """Minimal fallback: find first {...} block and parse it."""
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None

    def compact_track_profile(track: list[float]) -> dict[str, Any]:  # type: ignore[misc]
        if not track:
            return {}
        return {"min": round(min(track), 1), "max": round(max(track), 1),
                "fwd": round(track[9], 1) if len(track) > 9 else 0.0}

    def compact_opponent_profile(opponents: list[float]) -> dict[str, Any]:  # type: ignore[misc]
        if not opponents:
            return {}
        close = [o for o in opponents if o < 30.0]
        return {"closest": round(min(opponents), 1), "close_count": len(close)}

try:
    from track_model import load_track_model
    _TRACK_MODEL_AVAILABLE = True
except ImportError:
    # The pre-race map is optional: without track_model.py the bot drives on
    # sensors alone, exactly as before.
    _TRACK_MODEL_AVAILABLE = False


# ---------------------------------------------------------------------------
# SCR field metadata
# ---------------------------------------------------------------------------

_FIELD_MAP: dict[str, str] = {
    "angle":         "angle",
    "curLapTime":    "cur_lap_time",
    "damage":        "damage",
    "distFromStart": "dist_from_start",
    "distRaced":     "dist_raced",
    "fuel":          "fuel",
    "gear":          "gear",
    "lastLapTime":   "last_lap_time",
    "opponents":     "opponents",
    "racePos":       "race_pos",
    "rpm":           "rpm",
    "speedX":        "speed_x",
    "speedY":        "speed_y",
    "speedZ":        "speed_z",
    "track":         "track",
    "trackPos":      "track_pos",
    "wheelSpinVel":  "wheel_spin_vel",
    "z":             "z",
    "focus":         "focus",
    "x":             "x",
    "y":             "y",
    "roll":          "roll",
    "pitch":         "pitch",
    "yaw":           "yaw",
    "speedGlobalX":  "speed_global_x",
    "speedGlobalY":  "speed_global_y",
    "pitEntry":      "pit_entry",
    "pitStart":      "pit_start",
    "pitEnd":        "pit_end",
    "pitExit":       "pit_exit",
    "pitSpeedLimit": "pit_speed_limit",
    "pitSide":       "pit_side",
    "pitBoxOffset":  "pit_box_offset",
    "inPitStop":     "in_pit_stop",
    "trackLength":   "track_length",
    "remainingLaps": "remaining_laps",
    "lapsBehindLeader": "laps_behind_leader",
    "segWidth":      "seg_width",
}

_ARRAY_FIELDS: frozenset[str] = frozenset({"opponents", "track", "wheelSpinVel", "focus"})
_INT_FIELDS:   frozenset[str] = frozenset({"gear", "racePos", "inPitStop", "remainingLaps", "lapsBehindLeader", "pitSide"})

_ARRAY_LENGTHS: dict[str, int] = {
    "opponents": 36, "track": 19, "wheelSpinVel": 4, "focus": 5,
}
_ARRAY_DEFAULTS: dict[str, float] = {
    "opponents": 200.0, "track": -1.0, "wheelSpinVel": 0.0, "focus": -1.0,
}

# Per-field float defaults used when a field is absent from the packet (e.g.
# a scr_server build predating the pit-lane fields).  -1 means "no pit lane
# data" for the distance fields, so callers can tell that apart from a
# legitimate distance-from-start of 0.
_FLOAT_DEFAULTS: dict[str, float] = {
    "pitEntry": -1.0, "pitStart": -1.0, "pitEnd": -1.0, "pitExit": -1.0,
    "pitSpeedLimit": 0.0, "trackLength": -1.0,
    "pitBoxOffset": 0.0, "segWidth": -1.0,
}

# Same idea for int fields: a missing remainingLaps/lapsBehindLeader (older
# scr_server build) must default to -1 ("lap-count data unavailable"), never
# 0 -- 0 could be misread as "0 laps left" and force PIT logic that depends
# on it into a false trigger.  pitSide likewise: -1 means "no pit data",
# never a value that collides with the real TR_RGT(1)/TR_LFT(2) codes.
_INT_DEFAULTS: dict[str, int] = {
    "remainingLaps": -1, "lapsBehindLeader": 0, "pitSide": -1,
}

_REQUIRED_KEYS: frozenset[str] = frozenset({"speedX", "fuel", "gear", "track"})
_SCR_TOKEN = re.compile(r"\((\w+)\s+([^)]*)\)")


# ---------------------------------------------------------------------------
# Step 1: SCR state parser
# ---------------------------------------------------------------------------

def parse_scr_state(message: str) -> dict[str, Any] | None:
    """Decode a TORCS SCR sensor string into a Python dict.

    Returns a dict with snake_case keys, or None if the string is empty,
    unparseable, or is missing required fields.
    """
    if not message:
        return None

    raw: dict[str, str] = {}
    for match in _SCR_TOKEN.finditer(message):
        raw[match.group(1)] = match.group(2).strip()

    if not raw:
        return None
    if not _REQUIRED_KEYS.issubset(raw):
        return None

    state: dict[str, Any] = {}
    for scr_name, py_name in _FIELD_MAP.items():
        raw_value = raw.get(scr_name, "")
        if scr_name in _ARRAY_FIELDS:
            parts    = raw_value.split() if raw_value else []
            expected = _ARRAY_LENGTHS[scr_name]
            fill     = _ARRAY_DEFAULTS[scr_name]
            values   = [parse_float(p, fill) for p in parts]
            if len(values) < expected:
                values.extend([fill] * (expected - len(values)))
            state[py_name] = values[:expected]
        elif scr_name in _INT_FIELDS:
            state[py_name] = parse_int(raw_value, _INT_DEFAULTS.get(scr_name, 0))
        else:
            state[py_name] = parse_float(raw_value, _FLOAT_DEFAULTS.get(scr_name, 0.0))

    return state


# ---------------------------------------------------------------------------
# Step 2: control serializer
# ---------------------------------------------------------------------------

def format_scr_control(
    *,
    accel:  float = 0.0,
    brake:  float = 0.0,
    gear:   int   = 1,
    steer:  float = 0.0,
    clutch: float = 0.0,
    focus:  int   = 0,
    meta:   int   = 0,
    pit_request: bool = False,
) -> str:
    """Encode a control action into the TORCS SCR wire format.

    All values are clamped to their legal ranges before serialisation.
    """
    accel  = clamp(accel,  0.0,  1.0)
    brake  = clamp(brake,  0.0,  1.0)
    steer  = clamp(steer, -1.0,  1.0)
    clutch = clamp(clutch, 0.0,  1.0)
    focus  = int(clamp(float(focus), -90.0, 90.0))
    gear   = int(gear)
    meta   = 1 if meta else 0
    pit_req = 1 if pit_request else 0
    # Single choke point for every control we emit — capture it for the drive
    # log so a stuck car can be diagnosed from what was actually COMMANDED.
    _dbg.update(cmd_accel=accel, cmd_brake=brake, cmd_gear=gear, cmd_steer=steer,
                cmd_clutch=clutch, cmd_pit_request=pit_req)
    return (
        f"(accel {accel:.3f})"
        f"(brake {brake:.3f})"
        f"(gear {gear})"
        f"(steer {steer:.3f})"
        f"(clutch {clutch:.3f})"
        f"(focus {focus})"
        f"(meta {meta})"
        f"(pitRequest {pit_req})"
    )


# ---------------------------------------------------------------------------
# Step 3: SCR UDP client
# ---------------------------------------------------------------------------

# 19 track-sensor angles sent during the SCR handshake.
_INIT_ANGLES: tuple[int, ...] = (
    -90, -75, -60, -45, -30, -20, -15, -10, -5, 0, 5, 10, 15, 20, 30, 45, 60, 75, 90
)
_SCR_BUF           = 1000
_HANDSHAKE_RETRIES = 5
_HANDSHAKE_TIMEOUT = 5.0    # seconds per attempt
_STEP_TIMEOUT      = 0.1    # seconds; per-step recv timeout
_LOCAL_PORT_BASE   = 3200   # base for the duplicate-instance guard port (see
                             # TorcsClient.connect).  Must NOT be 3100: that
                             # yields 3101 for the default SCR port 3001, and
                             # UDP 3101 is the middleware's telemetry ingest
                             # port — the bot would silently squat on it and
                             # midware would fail to start with EADDRINUSE.


class ScrClient:
    """UDP client for the TORCS SCR protocol.

    Usage::

        with ScrClient(host="localhost", port=3001) as client:
            client.connect()          # handshake
            while True:
                state = client.receive_state()
                if state is None:     # race ended / restarted
                    break
                if not state:         # timeout — keep waiting (NEVER re-send:
                    continue          # the server reuses old controls itself,
                                      # and extra packets make it run behind)
                client.send_control(format_scr_control(...))
    """

    def __init__(self, host: str = "localhost", port: int = config.SCR_UDP_PORT) -> None:
        self._addr = (host, port)
        self._sock: socket.socket | None = None
        self._done = False

    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Send SCR(init …) and wait for ***identified***."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Claim a fixed local port so a SECOND bot instance for the same TORCS
        # slot fails fast here instead of silently splitting the packet stream
        # with the first one.  Two clients on one slot means interleaved
        # controls — a car that crawls at ~50 km/h at "full throttle", gears
        # that flap for no reason, and races that die to a stray meta/restart.
        guard_port = _LOCAL_PORT_BASE + (self._addr[1] % 100)
        try:
            self._sock.bind(("", guard_port))
        except OSError as e:
            self._sock.close()
            self._sock = None
            raise ConnectionError(
                f"Another bot instance appears to be connected to this TORCS "
                f"slot (local port {guard_port} busy). "
                f"Kill the other ai_bot process first: {e}"
            )
        self._sock.settimeout(_HANDSHAKE_TIMEOUT)

        payload = ("SCR(init " + " ".join(str(a) for a in _INIT_ANGLES) + ")").encode()

        for attempt in range(1, _HANDSHAKE_RETRIES + 1):
            self._sock.sendto(payload, self._addr)
            try:
                data, _ = self._sock.recvfrom(_SCR_BUF)
            except socket.timeout:
                print(f"  [scr] handshake attempt {attempt}/{_HANDSHAKE_RETRIES} timed out")
                continue

            if data.rstrip(b"\x00").decode(errors="replace") == "***identified***":
                self._sock.connect(self._addr)          # fix default peer → use send/recv
                self._sock.settimeout(_STEP_TIMEOUT)
                return

        raise ConnectionError(
            f"TORCS did not respond at {self._addr[0]}:{self._addr[1]} "
            f"after {_HANDSHAKE_RETRIES} attempts"
        )

    def receive_state(self) -> dict[str, Any] | None:
        """Receive one simulation step from TORCS.

        Returns:
            Parsed state dict  — normal packet.
            Empty dict {}      — recv timed out; caller should resend last control.
            None               — race ended (***shutdown***) or restarted (***restart***).
        """
        if self._sock is None:
            raise RuntimeError("Not connected — call connect() first")

        try:
            data = self._sock.recv(_SCR_BUF)
        except socket.timeout:
            return {}
        except ConnectionRefusedError:
            # TORCS closed the port (race ended or simulator quit).
            self._done = True
            return None

        # Drain any backlog and act on the NEWEST state only.  Under GUI
        # real-time mode a brief scheduling stall (WSLg's llvmpipe software
        # rendering hogs every core) queues several states; answering them
        # one-by-one puts the client permanently behind, and the server then
        # drives on stale controls — observed as a car crawling at ~52 km/h
        # with gears flapping while we "send" full throttle.
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    data = self._sock.recv(_SCR_BUF)
                except (BlockingIOError, InterruptedError):
                    break
                except ConnectionRefusedError:
                    self._done = True
                    return None
        finally:
            self._sock.settimeout(_STEP_TIMEOUT)

        text = data.rstrip(b"\x00").decode(errors="replace")

        if text.startswith("***shutdown***"):
            self._done = True
            return None
        if text.startswith("***restart***"):
            return None

        return parse_scr_state(text)

    def send_control(self, ctrl: str) -> None:
        if self._sock is None:
            raise RuntimeError("Not connected")
        self._sock.send(ctrl.encode())

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    @property
    def is_shutdown(self) -> bool:
        return self._done

    def __enter__(self) -> "ScrClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Gear shifting — two implementations
# ---------------------------------------------------------------------------

# --- RPM-based (used by _simple_autopilot legacy stub) ---
_GEAR_UP_RPM   = 7500
_GEAR_DOWN_RPM = 3000
_MAX_GEAR      = 6


def _auto_gear(current: int, rpm: float) -> int:
    if current <= 0:
        return 1
    if rpm > _GEAR_UP_RPM and current < _MAX_GEAR:
        return current + 1
    if rpm < _GEAR_DOWN_RPM and current > 1:
        return current - 1
    return current


# --- Speed-based (used by compute_control, ported from snakeoil.py) ---
# km/h thresholds; gear n shifts up when speed > _UP[n], down when < _DOWN[n].
# Index = current gear, so we need an entry for every gear up to _MAX_GEAR (6).
# The index-6 entries were MISSING before: once the car got fast enough to reach
# 6th (speed > 140), _DOWN_SPEED[6] raised IndexError, compute_control crashed,
# the drive loop died and TORCS kept repeating the last control — the car drove
# dead-straight off the track.  9999 = "never upshift past top gear".
_UP_SPEED   = (0, 35, 60, 85, 115, 140, 9999)   # index = current gear
_DOWN_SPEED = (0,  0, 28, 50,  72,  95,  120)


def _gear_from_speed(gear: int, speed: float) -> int:
    """Speed-based gear selector (fallback when RPM is unavailable)."""
    if gear <= 0:
        return 1
    g = min(gear, _MAX_GEAR)                      # guard table lookups against any out-of-range gear
    if gear < _MAX_GEAR and speed > _UP_SPEED[g]:
        return gear + 1
    if gear > 1 and speed < _DOWN_SPEED[g]:
        return gear - 1
    return gear


# RPM-first shifting: the speed table short-shifted badly (2nd at 35 km/h with
# the engine barely spinning), which is why acceleration felt flat.  Ride each
# gear out to high revs, downshift on falling revs for engine braking; fall
# back to the speed table only when the packet carries no usable rpm.
_RPM_UP   = 8500.0
_RPM_DOWN = 4000.0   # was dropped to 3500 to stop gearbox hunting, but the
                     # speed guard in _gear_shift now blocks the hunt case
                     # directly, so 4000 is safe again — and it stops the car
                     # lugging out of slow corners in 5th at ~3900 rpm

# NOTE: a "speed-based upshift backstop" (shift if rpm merely healthy and road
# speed past the gear) was tried here twice to cure a damaged car pinned at
# the limiter in 1st — BOTH attempts coincided with the car launching to only
# ~52-56 km/h and going no further.  Reverted to the pure-rpm rule that
# verifiably reached 215 km/h while the launch problem is instrumented (see
# the mode/acc/brk fields in the drive log).  Do not re-add without that data.


def _gear_shift(gear: int, rpm: float, speed: float) -> int:
    if gear <= 0:
        return 1
    if rpm <= 0.0:
        return _gear_from_speed(gear, speed)
    if gear < _MAX_GEAR and rpm > _RPM_UP:
        return gear + 1
    # Downshift needs BOTH low revs and a road speed the lower gear can
    # actually carry (its own upshift point, plus margin).  The rpm reading
    # alone can be stale by a few ticks when the machine is loaded (race-start
    # rendering spike) — without the speed guard that briefly-stale rpm
    # bounced the box 1st↔2nd at 51 km/h and strangled the launch.
    if gear > 1 and rpm < _RPM_DOWN and speed < _UP_SPEED[gear - 1] + 10.0:
        return gear - 1
    return gear


# ---------------------------------------------------------------------------
# ABS and traction control — ported from snakeoil.py (SCR reference client)
# ---------------------------------------------------------------------------

_WHEEL_RADIUS = 0.33    # metres (approximate for trb1/sc cars)

_ABS_SLIP  = 2.0        # m/s: wheel-lock slip to start reducing brake
_ABS_RANGE = 5.0        # m/s: full ABS modulation range

_TCL_SLIP  = 2.0        # m/s: wheel-spin slip to start reducing throttle
_TCL_RANGE = 10.0       # m/s: full TCL modulation range


def _apply_abs(brake: float, speed_kmh: float, wheel_vels: list[float]) -> float:
    """Reduce brake pressure when wheels are locking up."""
    speed_ms = speed_kmh / 3.6
    if speed_ms < 3.0 or not wheel_vels:
        return brake
    wheel_speed_ms = (sum(wheel_vels) / len(wheel_vels)) * _WHEEL_RADIUS
    slip = speed_ms - wheel_speed_ms
    if slip > _ABS_SLIP:
        brake *= max(0.0, 1.0 - (slip - _ABS_SLIP) / _ABS_RANGE)
    return brake


def _apply_tcl(accel: float, speed_kmh: float, wheel_vels: list[float]) -> float:
    """Reduce throttle when rear wheels are spinning."""
    speed_ms = speed_kmh / 3.6
    if len(wheel_vels) < 4:
        return accel
    rear_ms = (wheel_vels[2] + wheel_vels[3]) / 2.0 * _WHEEL_RADIUS
    slip = rear_ms - speed_ms
    if slip > _TCL_SLIP:
        accel *= max(0.0, 1.0 - (slip - _TCL_SLIP) / _TCL_RANGE)
    return accel


# 2026-08-09: track-hold throttle cut, ported from bt's Driver::filterTrk
# (driver.cpp) — bt cuts throttle to ZERO the moment the car is running wide
# toward the edge at speed, BEFORE it actually crosses it, rather than only
# reacting once track_pos has already blown past the recovery threshold
# (_RECOVER_ENTER_TPOS=1.15 below). bt checks the true velocity-vector angle
# against the track (its `speedangle`, from full sim state) and the car's
# position against the actual segment width — we have neither (SCR gives no
# track geometry or true velocity heading). Substitutes the signal we DO
# have every tick instead: is |track_pos| itself getting worse right now.
# Same tick-to-tick trend pattern as _close_rate_lp/_side_close_rate_lp
# elsewhere in this file, rather than reconstructed vehicle-dynamics math
# from an imperfect telemetry proxy.
_TRACK_HOLD_MIN_KMH = 20.0        # km/h: below this, don't bother — mirrors
                                   # bt's "too slow" bypass (MAX_UNSTUCK_SPEED)
_tpos_prev: float | None = None   # module state: track_pos one tick ago


def _apply_track_hold(accel: float, speed_kmh: float, tpos: float) -> float:
    """Cut throttle to zero if already near the edge AND still drifting further out.

    Mirrors bt's filterTrk: only intervenes past the apex-free band
    (_EDGE_FREE) that a legitimate corner line is allowed to use, and only
    while track_pos is actively getting worse tick over tick — a car that's
    out near the edge but already curving back toward centre is left alone,
    same as bt letting a speed vector pointed "toward the inside of the
    turn" through untouched.
    """
    global _tpos_prev
    prev = _tpos_prev
    _tpos_prev = tpos
    if prev is None or speed_kmh < _TRACK_HOLD_MIN_KMH or abs(tpos) < _EDGE_FREE:
        return accel
    drifting_out = tpos * (tpos - prev) > 0.0
    return 0.0 if drifting_out else accel


# ---------------------------------------------------------------------------
# Physics-derived brake distance — step 2/3 of the bt pace comparison (see
# conversation history: step 1 was the throttle ease-off band, step 3 is the
# corner-speed model itself). Ported from bt's Driver::brakedist()
# (src/drivers/bt/driver.cpp) — a closed-form solution for the distance
# needed to decelerate from v1 to v2 under constant tyre friction plus
# quadratic aero drag.  bt computes this from ground-truth mass/mu/CA/CW
# every tick; we don't have live mu (SCR exposes no equivalent of TORCS's
# real segment->surface->kFriction or tire mu), so these are one-time
# constants for car1-trb1 specifically (the only car scr_server ever drives,
# see scr_server.xml) derived from its actual XML, EXCEPT _BRAKE_MU, which
# is a guess — the single biggest source of error here, unvalidated live.
_CW = 0.645 * 0.35 * 1.92     # bt's CW = 0.645*Cx*frontArea (car1-trb1.xml
                               # Aerodynamics: Cx=0.35, front area=1.92 m2)
_CA = 2.79                    # bt's CA: wing downforce + ride-height ground
                               # effect, from car1-trb1.xml Front/Rear Wing
                               # (area 0.25/0.7 m2, angle 6/14 deg) and the
                               # four wheels' ride height (90/90/105/105 mm)
_CAR_MASS_BASE = 1150.0       # kg: car1-trb1.xml dry mass; +fuel litres at
                               # call time mirrors bt's mass = CARMASS+_fuel
_BRAKE_MU = 1.0                # UNVALIDATED: assumed combined tyre+track
                               # friction — bt reads this live per segment/
                               # tyre, we have no telemetry equivalent


def _brake_dist(v1_kmh: float, v2_kmh: float, mass: float) -> float:
    """Metres needed to decelerate from v1_kmh to v2_kmh (0.0 if v1<=v2)."""
    v1 = max(v1_kmh, 0.0) / 3.6
    v2 = max(v2_kmh, 0.0) / 3.6
    if v1 <= v2:
        return 0.0
    c = _BRAKE_MU * 9.81
    d = (_CA * _BRAKE_MU + _CW) / mass
    return -math.log((c + v2 * v2 * d) / (c + v1 * v1 * d)) / (2.0 * d)


def _simple_autopilot(state: dict[str, Any]) -> str:
    """Rule-based controller — drives forward for Step 3 integration testing."""
    speed = state.get("speed_x", 0.0)
    rpm   = state.get("rpm", 0.0)
    gear  = state.get("gear", 0)
    angle = state.get("angle", 0.0)     # radians, car vs track axis
    tpos  = state.get("track_pos", 0.0) # [-1, 1]; 0 = centre
    track = state.get("track", [])

    gear  = _auto_gear(gear, rpm)
    steer = angle * 10.0 / math.pi - tpos * 0.5   # align + return to centre

    front = track[9] if len(track) > 9 else 100.0  # index 9 = 0° straight ahead

    if abs(tpos) > 1.0:                 # off track — recover
        accel, brake = 0.0, 0.5
        steer = -tpos * 0.8
    elif front < 20.0:                  # obstacle close ahead
        accel, brake = 0.3, 0.3
    elif front < 50.0 or speed > 150.0: # slow for corner / speed limit
        accel, brake = 0.6, 0.0
    else:
        accel, brake = 1.0, 0.0

    return format_scr_control(accel=accel, brake=brake, gear=gear, steer=steer)


# ---------------------------------------------------------------------------
# Step 4: Strategy-parameterised low-level controller
# ---------------------------------------------------------------------------

ATTACK    = "ATTACK"
NORMAL    = "NORMAL"
DEFEND    = "DEFEND"
SAVE_FUEL = "SAVE_FUEL"
PIT       = "PIT"
BLOCK     = "BLOCK"   # position-defence — see _GRANITE_STRATEGIES below

# TEMP (pit-system testing, 2026-08-09): SAVE_FUEL disabled so low fuel always
# resolves to PIT instead of Granite hedging with economical driving first.
# Flip back to True to restore normal behaviour.
_SAVE_FUEL_ENABLED = False

# Strategies Granite is allowed to freely choose. BLOCK is deliberately
# excluded: it is a deterministic, per-frame reflex (triggered in
# safety_filter from the raw rear-gap sensor, same as PIT-on-fuel and
# DEFEND-on-damage) rather than a strategic judgement call, and a 5 s LLM
# poll is far too slow to react to a car that is already closing in behind.
# If Granite's JSON ever says "BLOCK" anyway (hallucination), it must be
# rejected like any other invalid strategy, not honoured.
_GRANITE_STRATEGIES: frozenset[str] = frozenset(
    {ATTACK, NORMAL, DEFEND, PIT} | ({SAVE_FUEL} if _SAVE_FUEL_ENABLED else set())
)

# All strategies compute_control()/safety_filter() understand, Granite-chosen
# or system-only.
_ALL_STRATEGIES: frozenset[str] = _GRANITE_STRATEGIES | {BLOCK}


@dataclass(frozen=True)
class _DriveParams:
    max_speed:    float  # km/h absolute ceiling
    accel_limit:  float  # maximum accel command [0, 1]
    brake_gain:   float  # multiplier when speed exceeds target
    steer_gain:   float  # angle * steer_gain → "align with track" steer term.
                         # angle is in radians (~[-0.5, 0.5] during normal driving).
                         # Higher = sharper corner turn-in but more twitchy.
    center_gain:  float  # track_pos * center_gain → "return to centre" steer term.
                         # Keep small (~0.1-0.3): tpos is [-1, 1], so a large gain
                         # fights the alignment term and causes weaving.
    speed_factor: float  # corner_speed_kmh = sqrt(min_fwd_m * speed_factor)


#                          max_spd  accel  brake_g  steer_g  cntr_g  spd_factor
# NORMAL runs full throttle too — the strategies differ in top speed, corner
# speed (spd_factor) and brake gain, not in dribbling the pedal on straights.
# ATTACK pushed further (was 300/1.20/290): higher top speed and corner speed
# (speed_factor), with brake_gain raised to match so the harder corner entry
# is still recoverable — untested at these numbers, watch for corners taken
# too hot (running wide / contact) before pushing further.
_ATTACK_PARAMS = _DriveParams(330,    1.00,   1.35,    0.90,    0.20,  330)

# BLOCK shares ATTACK's params exactly (same object, not a re-typed copy, so
# the two can never drift apart by accident) — per user request, defending a
# position should not mean slowing down.  All of the actual "defend" behaviour
# lives in the line-bias added in compute_control below, gated on
# `strategy == BLOCK`, not in these numbers.
_PARAMS: dict[str, _DriveParams] = {
    ATTACK:    _ATTACK_PARAMS,
    NORMAL:    _DriveParams(250,    1.00,   1.00,    0.85,    0.20,  230),
    DEFEND:    _DriveParams(180,    0.80,   0.90,    0.80,    0.25,  150),
    SAVE_FUEL: _DriveParams(150,    0.65,   0.80,    0.80,    0.20,   80),
    PIT:       _DriveParams( 50,    0.30,   1.50,    0.70,    0.30,   10),
    BLOCK:     _ATTACK_PARAMS,
}

# Lateral-velocity damping: counter-steers against sideways slide to kill the
# snaking/weaving oscillation.  steer -= speed_y_ms * _STEER_DAMP.
# IMPORTANT: the SCR server sends speedY in km/h (scr_server.cpp multiplies the
# native m/s by 3.6).  We convert back to m/s before applying this gain — the
# old code used the raw km/h value, making the damping ~3.6× too strong, which
# *caused* violent counter-steering / head-shaking instead of damping it.
# At 0.06: ~3 m/s of slide → ~0.18 of counter-steer.
_STEER_DAMP = 0.06

# Speed-scaled steering authority: divides steer by (1 + speed * k) to trim a
# little turn-in at high speed.  Pure pursuit is geometric and far-aiming so it
# barely snakes on its own; this is kept mild.
#   100 km/h → ×0.83,  250 km/h → ×0.67
_STEER_SPEED_K = 0.002

# Deadzone: ignore micro-corrections so the wheel doesn't chase sensor noise.
# Pure pursuit is smooth, so this can be small.
_STEER_DEADZONE = 0.02

# --- Pure-pursuit steering ----------------------------------------------------
# Aim the car at the direction the track actually goes — a distance-weighted
# average of the 19 beam angles, longer beams pulling the target toward them.
# Geometric path-following, not error-nulling, so it does not snake: on a
# straight every beam is equal → dead ahead; in a corner the long beams point at
# the exit → smooth turn-in; an off-centre car sees more open road to one side
# and is drawn back naturally.
#
# SIGN: the SCR track-sensor angle convention is the OPPOSITE of the steer
# convention.  Verified in sensors.cpp — the +90° beam returns the distance to
# the RIGHT edge (positive sensor angle points RIGHT), whereas +steer / +angle
# mean LEFT.  So we negate the beam angles here; then a left-opening track gives
# a positive target → positive (left) steer, matching the forward-drive convention.
_SENSOR_ANGLES_RAD = tuple(-math.radians(a) for a in _INIT_ANGLES)
_PP_ARC   = range(2, 17)   # beams within ±60°.  Do NOT widen this to ±75: on
                           # corner entry the near-sideways beam grazes the
                           # inside edge tangentially and reads very long, and
                           # the power-4 weighting then drags the aim point
                           # straight into the inside wall (verified on track).
_PP_POWER = 4.0            # >1 sharpens the weighting toward the longest beams
_PP_GAIN  = 1.0            # target heading (rad) → steer command

# Soft deadband on the pursuit STEER (speed planning has its own _SHARP_FREE).
# Geometry: on a straight only the 0° beam saturates; the ±5°/±10° beams graze
# the edges, so their lengths swing with lateral position and edge raggedness,
# and the d⁴ weighting turns that into a small wandering aim (±0.10-0.17 rad
# observed at ~200 km/h) which the car chased into a growing pendulum.  A real
# driver holds the line on a straight instead of hunting the widest gap — so
# aim contributions below this band are ignored; genuine corners command
# 0.3-0.8 rad and lose only the band width.
_PP_FREE = 0.10

# Gentle recentring while holding the line.  The aim deadband plus the high
# edge-barrier threshold (0.85) left a dead zone in |tpos| 0–0.85 where NOTHING
# pulled the car inward — lateral offset carried out of corners just persisted
# down the straight until the car clipped a kerb at speed and got yanked back.
# While the aim is inside its deadband (= open road) drift softly toward the
# centre line; the term fades out continuously as a corner's aim signal builds,
# so the apex stays free.  Keep this SMALL: it is a slow drift, not a pull —
# a big gain here would re-create the pendulum the deadband cured.
_HOLD_CENTRE = 0.08

# A+ racing line: pull strength toward the map's entry-line setpoint (outside
# edge on the approach to a mapped corner — out-in-out).  Stronger than the
# plain hold-centre drift because it must actually MOVE the car across the
# road on the straight, but still a suggestion, not a command: it rides the
# same fade as hold-centre (dies as soon as the pursuit aim wakes up), so
# mid-corner steering stays with pursuit + sensors alone.
_LINE_GAIN = 0.18

# The raw setpoint FLIPS sides between alternating corners; feeding those
# steps straight into the steering re-created the twisty-section weave that
# was cured before P1.  Slew-limit the setpoint instead: full swing takes
# ~1 s, so repositioning is a drift, never a dart.
_LINE_SLEW = 0.02          # max setpoint change per 20 ms tick
_line_lp   = 0.0           # module state: slewed line setpoint

# 2026-08-09: speed-adaptive entry-zone horizon, bt-inspired (see
# Driver::getTargetPoint's lookahead = LOOKAHEAD_CONST + speed*
# LOOKAHEAD_FACTOR, driver.cpp) — bt looks further down the track the
# faster it's going. Deliberately NOT applied to the sensor-based pursuit
# aim (_PP_ARC/_PP_POWER above carry their own "verified on track" wall-
# drag incident from a past widening attempt — sharpening that weighting
# further at speed would make exactly that failure mode worse exactly when
# it matters most). line_tpos's entry_zone is safe ground for the same
# idea instead: it runs on the map's real corner geometry (dist_from_start,
# corner position), not noisy sensor beams, so there's no grazing-beam
# failure mode to reintroduce — the risk profile that ruled out the sensor
# path simply doesn't apply here.
# 250.0 is line_tpos's own existing default, kept as a floor so low-speed
# behaviour is byte-for-byte unchanged from before this; only speeds above
# that baseline extend the horizon further.
_LINE_ENTRY_ZONE_BASE     = 250.0   # m: unchanged floor (line_tpos's default)
_LINE_ENTRY_ZONE_SPEED_K  = 0.5     # m per km/h added above the floor

# NOTE: a PD upgrade of this term (damping on the low-passed tpos rate) was
# tried and REVERTED twice in one day.  Unguarded, stale corner-sweep rate
# leaked into the straight and threw the car off track; guarded, it measured
# smoother in headless runs but turned into visible SNAKING in the WSLg GUI —
# derivative feedback plus rendering-stall latency ADDS energy instead of
# damping (the rate signal arrives stale).  On this machine the GUI is the
# demo environment, so: no D term here.  Do not re-add without testing under
# GUI load.

# WHY the angle-alignment term (params.steer_gain) is added to pursuit:
# the lateral loop is second-order — offset y feeds the aim, the aim turns the
# heading, the heading integrates back into y.  Pursuit alone supplies the
# "stiffness" (pull toward open road) but almost no damping, so at speed the
# car weaves down straights with irregular edges in a growing pendulum.  The
# heading term angle·steer_gain is the damping of that loop (it resists the
# swing, not the corner: in a steady corner pursuit dominates and the angle
# stays small).  Do NOT try to damp the pendulum with a yaw-RATE term or by
# clipping beam lengths — both were tried on track: rate damping lowers the
# loop's damping ratio (the steer→angle plant is already an integrator), and
# clipping erases the near/far contrast that corner entry aiming and corner
# speed both depend on (the car drove straight off the first corner).

# Edge barrier (replaces the old centre-line pull): don't force the car to the
# middle — let it use the track width (racing line) in the middle band, and only
# gently tuck it back when it's RIGHT at the edge.  No lateral correction while
# |track_pos| < _EDGE_FREE; beyond that a gentle push grows linearly.
# IMPORTANT: keep _EDGE_FREE high (~0.85) and the gain modest — the apex of a
# corner is taken hugging the inside edge (|track_pos| ~0.9), so an early/strong
# barrier would shove the car off the apex toward the OUTSIDE wall mid-corner.
# Genuinely going off-track (|track_pos| > 1) is handled by the recovery branch.
_EDGE_FREE = 0.85          # |track_pos| below this → no centring at all (apex is free)
_EDGE_GAIN = 1.2           # gentle tuck-in once past the free band

# Side-traffic avoidance: nudge away from a car alongside instead of holding the
# racing line through it.  track_pos never leaves the normal driving band during
# a start-grid scrap (the car isn't off line or on a kerb), so nothing else in
# this function reacts to it — pursuit/barrier only see the road, not other
# cars — and the pack rubs damage into the door for as long as the opponent
# stays alongside.  Rear cone excluded (a car that has passed is no longer a
# side risk); TORCS opponent sensor indices follow the same angle = -180+10*i,
# positive = RIGHT convention as the track sensor (see _SENSOR_ANGLES_RAD).
#
# LEFT/RIGHT split exactly at dead-ahead (index 18, 0°) — an EARLIER version
# excluded indices 16-19 from both windows on the theory that dead-ahead was
# "pursuit/sight's job".  It is not: those only see the road, never other
# cars, so a car sitting exactly in that gap read left_gap == right_gap ==
# 200 (invisible) — no side looked closer, so neither the avoidance nudge
# below nor the front-follow boxed-in check ever fired, and the car drove
# straight through it for 600 ticks, taking damage 0 → 7000+ (verified live:
# opponent dead centre at 4.5-9 m, obnd stayed 0 the whole time).  There must
# be no gap between the two windows — every angle from just-behind-left to
# just-behind-right has to land in at least one of them.
# 10.0 → 14.0 (2026-08-06): a live crash showed rgap=5.4 (already inside the
# old 10 m trigger, so the nudge WAS active) go to a full off-track hit just
# 2 seconds later — the gap was closing too fast for the reaction time the
# old distance gave it.  Widened distance (more warning, gentler correction
# starting earlier) rather than raising _AVOID_GAIN (same reaction, just
# later and harsher, which doesn't fix a timing problem and risks a sharper
# swerve at speed).  One variable at a time — leave _AVOID_GAIN alone until
# this alone is confirmed to help or not over several races.
# 0.15 → 0.22 (2026-08-06): the 14 m distance alone wasn't enough — logged
# live (alongside the new _SIDE_EASE_GAIN throttle cut, see below): rgap
# closed from 8.8 m to 4.4 m over 16 straight ticks with the nudge active
# the whole time and made contact anyway. The gain, deliberately left alone
# above, is the other half of "widen the warning, then confirm gain doesn't
# need touching too" — that confirmation came back negative, so this is the
# next single-variable step, not a combined change.
# 0.22 → 0.45 (2026-08-06): 0.22 changed NOTHING live — same crash, same
# step, damage 294 vs 295, tpos trajectory nearly identical. Root cause:
# `steer /= (1 + speed * _STEER_SPEED_K)` divides every steer term by ~1.35
# at 175 km/h, so at rgap=4.4 m (well inside _AVOID_DIST) the post-atten
# contribution only went from ~0.076 to ~0.112 — a ~0.036 nudge on a [-1,1]
# scale, invisible next to the pursuit/centre terms holding the racing line.
# For the avoid term to actually read as a real steering input (~0.25, the
# same ceiling the centre/hold-line term clamps to) at that speed and gap
# needs gain ≈0.25*1.35/(1-4.4/14) ≈ 0.49 — 0.45 targets that order of
# magnitude. This is deliberately the "sharper swerve at speed" risk flagged
# above and not yet tested at all — validate for oversteer/instability at
# high speed, not just whether it stops the graze.
# 0.45 validated live (2026-08-06): the dist~150 m straight-line graze that
# crashed identically at 0.15 and 0.22 (damage ~294-295 both times) did NOT
# recur at 0.45 — lgap bottomed at 3.5 m and recovered to 49.8 m, damage 0.
# No sign of the oversteer/instability this jump was flagged as risking.
#
# But a SEPARATE contact showed up later the same race (dist~3807, step 3675,
# damage 0->331, contained — no further growth) while cornering (why=map-trust
# going in). This is not a "gain still too small" case: `avoid *= fade` below
# means steer authority here is capped by cornering demand regardless of gain.
# Measured through that incident: open_angle(=|pursuit|) was 0.01 at the first
# close tick, only reaching 0.19+ (fade=0) a few ticks AFTER contact — so
# avoid ran at roughly 40-90% strength (fade 0.4-0.9) through the whole
# approach, not zero, and that reduced-but-nonzero authority plus losing the
# wheel to cornering the rest of the time still wasn't enough. See
# _AVOID_FADE_FLOOR below for the fix aimed at this category.
# 0.45 → 0.22 (2026-08-06): 0.45 also turned out to be the reason a same-tick
# left/right gap flip became a real steer-sign reversal mid-overtake (see
# _AVOID_SLEW below — that's the actual fix for the reversal). With the slew
# limiter now in place regardless of gain, trying the lower, less oversteer-
# prone value again to see whether 0.22 + the slew limiter (and everything
# else fixed since the original 0.22 test: reverse cap, stabilize latch,
# side-ease, fade floor, blind-mode avoidance) is enough on its own, without
# needing 0.45's sharper-swerve risk.
_AVOID_DIST  = 14.0                 # m: side gap closer than this triggers a nudge
_AVOID_GAIN  = 0.22                 # steer authority at zero gap (~centre-term scale)
_AVOID_LEFT  = range(9, 18)         # ~-90° to -10°: opponent ahead-left/alongside/just behind
_AVOID_RIGHT = range(18, 28)        # ~0° to +90°: opponent ahead-right/alongside/just behind
                                     # (index 18, dead ahead, arbitrarily assigned here — the
                                     # point is the split, not which side owns the boundary)

# 2026-08-08: convergence gate, borrowed from bt's Driver::filterSColl
# (driver.cpp) — bt only steers to correct a side threat when the two cars'
# headings show them actually converging (diffangle*sideDist < 0); a car
# sitting alongside on a parallel, non-converging path gets no correction at
# all. `avoid` above has no such gate — it pushes proportionally to raw
# distance alone, so a neighbour holding a STABLE ~6-9 m gap for many
# seconds (never actually closing) gets the exact same steady push as a
# genuine emergency, with nothing to ever stop it — verified live: this is
# what steered the car toward the track edge on its own, not just a
# tug-of-war with the edge barrier (see room_taper below, which only treats
# the symptom once already near the edge). SCR gives no opponent heading to
# replicate diffangle directly, so this approximates "actually converging"
# with the same closing-rate technique already validated for the front
# overtake trigger: whichever of left_gap/right_gap is tighter, track
# whether IT is shrinking. Full avoid authority when genuinely closing.
# 2026-08-09: floor dropped 0.4 -> 0.0 — went back to match bt's actual
# all-or-nothing gate (a car confirmed NOT converging gets zero correction,
# full stop) instead of the softened "some margin anyway" compromise this
# started as. The 0.4 floor was itself the bug: a neighbour sitting at a
# stable ~9-10 m gap for 15+ seconds (ai_bot log, steps 339-355, tpos crept
# -0.30 -> -0.87) never dropped below 0.4x authority because it was never
# closing fast enough to satisfy _SIDE_CLOSE_RATE_MIN, so the steady push
# never actually stopped — room_taper only caught it once already almost at
# the edge. A car that isn't closing on us is not a steering problem; the
# speed-side response (_SIDE_EASE_GAIN / standoff breaker below) still
# handles a persistent close-but-stable neighbour by shedding pace instead
# of swerving, same division of labor bt uses (filterSColl vs OPP_LETPASS).
_SIDE_CLOSE_RATE_MIN = 0.5           # m/s: side gap must shrink at least
                                      # this fast to count as converging
_AVOID_CONVERGE_FLOOR = 0.0          # min fraction of avoid's authority kept
                                      # even when the gap isn't closing —
                                      # 0.0 = bt-style all-or-nothing
_side_gap_prev: float | None = None  # module state: min(left_gap,right_gap)
                                      # one tick ago
_side_close_rate_lp: float = 0.0     # module state: smoothed closing rate

# 2026-08-06: `avoid *= fade` (see compute_control) shares the same cornering
# fade as `centre` (the racing-line hold), on the reasoning that neither
# should fight the driver for the wheel mid-corner. That's right for `centre`
# — it's about racing line, and the corner's own geometry is better
# information than a guessed hold-line setpoint. It's wrong for `avoid` —
# its only job is not getting hit, which does not stop mattering because a
# corner is happening. Logged live: a car alongside mid-corner (dist~3807,
# see _AVOID_GAIN history above) took damage while avoid ran at a reduced
# 40-90% (fade 0.4-0.9) and got no floor once fade fell further as the
# corner deepened. This floor gives `avoid` — and only `avoid`, `centre` is
# untouched — a guaranteed minimum so a car alongside is never completely
# defenseless just because a corner is also happening.
_AVOID_FADE_FLOOR = 0.3             # min fraction of avoid's authority kept
                                     # even at fade=0 (full corner)

# 2026-08-06: raising _AVOID_GAIN to 0.45 (see history above) turned a
# pre-existing wrinkle into a real problem. left_gap/right_gap are recomputed
# fresh every tick with no memory, and mid-overtake it's normal for which
# side reads "closer" to flip as the two cars' relative angle sweeps past —
# logged live: lgap=164.7/rgap=9.0 (right close) at one tick, lgap=9.1/
# rgap=200.0 (left close) the very next. At the old gain that flip was too
# small to feel; at 0.45 it's a real steer-sign reversal in a single 20 ms
# tick, right in the middle of a fast pass — likely what set off the
# wrong-way spin logged a few ticks later. `centre` already has exactly this
# problem solved via `_line_lp`/`_LINE_SLEW` (slew the setpoint, never dart);
# `avoid` gets the same treatment here rather than clawing gain back.
_AVOID_SLEW = 0.05                  # max avoid change per 20 ms tick — full
                                     # swing (~2x gain) takes ~0.35-0.4 s,
                                     # so a same-tick side flip becomes a
                                     # fast correction, not a reversal
_avoid_lp   = 0.0                   # module state: slewed avoid value

# Side-avoidance throttle ease: the steer nudge above is the only reaction to
# a car alongside — accel/brake never see left_gap/right_gap at all — so a
# tight side gap that isn't closing on its own just gets held, nose-to-nose,
# for as long as the opponent stays there. Logged live: a crash where ogap
# sat at 4.3-4.7 m (well inside _AVOID_DIST) for dozens of consecutive ticks
# at acc=1.00 the whole time — the car never actually tried to open the gap,
# just nudged its heading a few degrees while pinned alongside — until contact
# built up to real damage. This eases target_speed (not a hard brake) so the
# car can actually fall back or ease off rather than just steering while
# still glued to the opponent's pace. Deliberately NOT applied while
# `launching` — the launch's widened _START_AVOID_DIST already means most of
# the grid reads "close" for the first stretch, and that startup avoidance
# tuning is separately validated; don't perturb it here.
_SIDE_EASE_GAIN = 0.3                # fraction of target_speed shed at zero side gap

# 2026-08-08: standoff breaker, borrowed from bt's OPP_LETPASS (opponent.cpp)
# — bt tracks how long a faster car has sat behind it (overlaptimer) and
# yields once that exceeds OVERLAP_WAIT_TIME=5s, rather than holding pace
# with them indefinitely. _SIDE_EASE_GAIN above already eases target_speed
# proportionally to how tight the gap is, but that's a PASSIVE, graduated
# response — verified live it can settle into a standoff that never
# resolves: a neighbour sitting at a roughly stable ~6-9 m side gap for an
# extended stretch (neither closing nor opening) kept the passive ease
# constant too, so neither car pulled away. This is also what fed the
# avoid/barrier edge tug-of-war (see the room_taper comment above) — a side
# neighbour that's actually a car ahead going around a corner reads as a
# persistent side threat the whole time, not a brief pass. Past
# _STANDOFF_TIME of continuous closeness, escalate to a much stronger ease
# — deliberately unilateral (we control our own braking; committing to
# "push past" instead would mean accelerating alongside a car already
# inside avoid_dist, which is a worse bet) — to definitively break the
# deadlock instead of continuing the same passive nudge indefinitely.
_STANDOFF_TIME      = 4.0            # s: bt's OVERLAP_WAIT_TIME is 5.0; ours
                                      # is a bit tighter since the standoff
                                      # also feeds the edge tug-of-war, not
                                      # just lost time
_STANDOFF_EASE_GAIN = 0.6            # fraction of target_speed shed once the
                                      # standoff timer expires — stronger
                                      # than the passive _SIDE_EASE_GAIN
_standoff_timer: float = 0.0         # module state: seconds side_gap has
                                      # stayed inside _AVOID_DIST continuously

# Start-of-race caution: the whole grid launches together into a narrowing
# racing line (see quickrace.xml's 2-row grid), so the first stretch sees far
# more cars converging from the side, far closer together, than any point in
# open racing — logged live: a car swapped from the right cone to the left
# cone between two 100-step samples right at the green light and the door
# got clipped (damage 0 → 1247 by dist_raced ~60 m, EVERY subsequent lap in
# this session carried that hit as a permanent handicap).  _AVOID_DIST/_GAIN
# above are tuned for cars already spread out at racing speed; they're too
# tight/weak for a shoulder-to-shoulder launch.  Widen and strengthen both,
# and take a little heat off the throttle, only while dist_raced is small —
# this fades back to normal full-send racing well before the first braking
# zone on any real track, so it costs no meaningful lap time.
# 150 → 100 → 150 (2026-08-06): 100 was a deliberate repro knob, not a
# tuning value — shrinking the launch window made the post-crash
# stuck/no-recovery bug trigger reliably instead of intermittently so it
# could actually be debugged. That bug is now fixed (_TA_REV_MAX_KMH,
# _stabilize_bled, and the avoidance work below — see commit history),
# so this reverts to the known-good 150 as planned.
_START_CAUTION_DIST = 150.0         # m: dist_raced below this = still launching
_START_AVOID_DIST   = 25.0          # m: replaces _AVOID_DIST during the launch
_START_AVOID_GAIN   = 0.35          # replaces _AVOID_GAIN during the launch
# 2026-08-08: 0.75 assumed the rest of the grid also held back at the start,
# so trading pace for margin was free. It doesn't — TORCS's built-in bots
# (bt, berniw, inferno, ...) have no equivalent launch throttle cap at all
# (see src/drivers/bt/driver.cpp Driver::getAccel(): full 1.0 whenever
# below the corner's allowed speed, no launch-specific case), so holding
# OUR car back 25% just made it the one slow car in a field that's
# otherwise launching flat out — arguably WORSE for the exact "car cutting
# across" risk this was meant to guard against, since a slower car gets
# closed in on from more directions during the merge. Matching bt: no cap.
# The actual safety net for the launch window is _START_AVOID_DIST/_GAIN
# above (unchanged, still active) — those react to closing traffic
# regardless of throttle level, so removing the throttle cap doesn't
# remove the protection the 2026-08-06 incident led to, just the pace
# penalty. Unvalidated live — watch dist_raced<150 damage on the next race.
_START_ACCEL_CAP    = 1.0           # no launch throttle cap (matches bt)

# 2026-08-08: launch clutch control, borrowed from TORCS's built-in "bt"
# robot (src/drivers/bt/driver.cpp Driver::getClutch()). `clutch` was never
# set anywhere in this file — always the default 0.0 (fully engaged) — which
# is harmless everywhere EXCEPT the standing start: while the race engine
# holds the car in forced neutral (reported gear 0) before the green light,
# our own full-throttle command free-revs the engine toward redline
# (verified live: 942 -> 9611 rpm over the pre-start hold). The instant
# TORCS actually connects 1st gear, clutch=0.0 rigidly locks the engine to
# the still-stationary wheels in a single tick — verified live: rpm crashed
# 9611 -> 956 in one frame, so the launch starts from ~1/3 of this car's
# peak torque (160 N·m @ ~950rpm vs 483 N·m @ 8000rpm, car1-trb1's curve)
# instead of the revs built up during the hold. bt avoids this by feathering
# the clutch in continuously off live rpm/gear-ratio/redline data; SCR
# telemetry doesn't expose gear ratios or redline rpm to do the same exact
# computation, so this approximates the same effect with a simple time-based
# ramp measured from the moment the SIM reports 1st gear (not our own
# always-commands-1st gear command, which would start the ramp a second
# early, during the neutral hold, and have it already half-decayed by the
# time the clutch actually needs to be open). Gated on `launching` so it
# only touches the standing start — mid-race gear changes are untouched.
_CLUTCH_RAMP_TIME = 1.5             # s: time to feather from full slip to
                                     # fully engaged once 1st gear connects
_launch_clutch_timer: float = 0.0   # module state: seconds since 1st gear
                                     # connected during the launch window

# 2026-08-09: slip-fed launch clutch, closing the gap the 2026-08-08 comment
# above flagged — bt meters clutch release off engine rpm vs. the wheel
# speed that rpm SHOULD correspond to in gear 1 (needs live gear ratio +
# redline, which SCR doesn't expose, hence the plain time ramp above). We
# don't need to reconstruct that indirection: SCR DOES give wheel_spin_vel
# directly — the same signal _apply_tcl already trusts for mid-race
# wheelspin — so comparing rear wheel ground-equivalent speed to actual
# chassis speed IS the slip bt is really after, no rpm/gear-ratio math
# required. A pure time ramp can't tell "gripping fine, could close faster"
# from "still spinning, needs to hold" — it runs the same schedule either
# way; this adds a floor so it holds open at LEAST as long as the rear
# wheels are still measurably outrunning the chassis, and closes faster
# than the flat schedule once they're not.
# Guard: the instant 1st gear connects, wheel_spin_vel can read ~0 (nothing
# transferred yet) — same reading as "already gripping perfectly", the
# exact ambiguity that caused the original rpm-crash incident (9611->956)
# if misread as safe to lock up. Only trust the slip signal once it's
# genuinely POSITIVE (wheels already outrunning the car, i.e. torque IS
# getting through); otherwise fall back to the plain time ceiling.
_LAUNCH_SLIP_BAND = 3.0   # m/s: rear-wheel-vs-chassis slip band the release
                           # rate is scaled over, once slip is confirmed


def _launch_clutch(time_ceiling: float, speed_kmh: float,
                    wheel_vels: list[float]) -> float:
    """Clutch command for the launch ramp: time ceiling, tightened by live wheel slip."""
    if len(wheel_vels) < 4:
        return time_ceiling
    rear_ms = (wheel_vels[2] + wheel_vels[3]) / 2.0 * _WHEEL_RADIUS
    slip_ms = rear_ms - speed_kmh / 3.6
    if slip_ms <= 0.0:
        return time_ceiling
    slip_factor = clamp(slip_ms / _LAUNCH_SLIP_BAND, 0.0, 1.0)
    return min(time_ceiling, slip_factor)


# Front-opponent following/overtake: the "track" beams only see the road, so a
# slower car sitting dead ahead is otherwise invisible to this function — it
# just floors the throttle into their bumper.  Two effects, both gated on
# _FRONT_CONE distance (roughly ±30° ahead):
#   1. line bias (proactive): well before contact risk, nudge the hold-line
#      setpoint toward whichever side has more room, the same slewed
#      mechanism the map's out-in-out entry line already uses — so the car
#      eases out to pass instead of parking in the draft.
#   2. follow cap (reactive, LAST resort): only when genuinely boxed in — a
#      tight gap ahead AND no room to either side — cap target speed with the
#      same sqrt curve corners use.  First cut of this braked for ANY close
#      front gap regardless of whether a side was open, which fired constantly
#      in ordinary racing (any car a length ahead in a pack) and cost far more
#      laptime than the rear-end risk it prevented — measured a lap full of
#      podium finishes turn into a last-place finish.  Gating on "no escape"
#      makes the brake genuinely rare: it only fires when easing aside (1)
#      can't help, which is the only situation additional speed loss buys
#      anything.
_FRONT_CONE           = range(15, 22)   # ~-30° to +30°: opponent roughly in-lane ahead
_OVERTAKE_TRIGGER_M   = 55.0             # m: start easing the line out this far back
                                          # (was 40 — pushed out so the move starts earlier
                                          # and commits before the follow cap's 10 m window)
_OVERTAKE_BIAS        = 0.50             # tpos units: how far off-centre to ease (was 0.35 —
                                          # more decisive commitment to the gap, untested)
_OVERTAKE_ROOM_MARGIN = 5.0              # m: one side must be clearly more open to commit
_FRONT_BRAKE_M        = 10.0             # m: only consider braking for a gap this tight
_FRONT_ESCAPE_M       = 20.0             # m: either side clearer than this = not boxed in
_FRONT_FLOOR_KMH      = 25.0             # km/h: cap floor at zero gap
_FRONT_FACTOR         = 40.0             # sqrt curve steepness inside the brake window

# 2026-08-08: the overtake trigger above only ever looked at absolute
# front_gap, so a car perfectly pace-matched in a train (front_gap constant,
# never closing) got the exact same line-bias treatment as a car we're
# actually catching — the car drifts off the racing line toward "the open
# side" for zero passing benefit, because there is nothing to pass at matched
# pace. SCR's opponent sensor reports distance only, never relative speed, so
# there is no field to read directly — but d(front_gap)/dt IS the relative
# closing speed, and it costs nothing new to compute: just remember last
# tick's front_gap. A single-tick derivative is too noisy to gate on directly
# (opponent sensor noise + the other car's own weaving), so it is EMA-smoothed
# before the gate checks it — trading ~0.1-0.2 s of trigger latency for a
# stable estimate.
_TICK_S                   = 0.02   # s: sim tick period (50 Hz) — same assumption
                                    # _LINE_SLEW/_TARGET_RISE already bake in
_OVERTAKE_CLOSE_RATE_MIN  = 1.5    # m/s: front_gap must be shrinking at least
                                    # this fast before the line-bias arms —
                                    # filters matched-pace trains, not just
                                    # "car happens to be near"
_CLOSE_RATE_ALPHA         = 0.1    # EMA smoothing factor on the raw per-tick
                                    # derivative of front_gap
# 2026-08-08 (live capture): front_gap is a MIN over whichever _FRONT_CONE
# beams currently intersect something, not a continuous track of one object —
# so when a car's bearing crosses the cone's ±30° edge, front_gap jumps
# discontinuously even though the car's true distance barely moved. Logged
# live: ogap 23.7 -> 7.5 m in one 20 ms tick (implied ~970 m/s closing) while
# right_gap, a different sensor window watching the SAME car, only moved
# 7.3 -> 7.5 m that tick. The naive derivative read that as a genuine closing
# spike and armed the overtake trigger for ~0.7 s of EMA decay. A per-tick
# jump bigger than this is cone-boundary noise, not two cars actually
# converging at highway-merge speed — treated as no new information (raw
# rate stays 0, letting the EMA decay toward 0) rather than absorbed.
_CLOSE_RATE_SANITY_MAX    = 50.0   # m/s (~180 km/h): implausible per-tick
                                    # front_gap jump — reject, don't smooth
_front_gap_prev: float | None = None   # module state: front_gap one tick ago
_close_rate_lp:  float        = 0.0    # module state: smoothed closing rate
                                        # (m/s, positive = gap shrinking)

# 2026-08-07: BLOCK — position-defence against a car closing in from behind.
# Mirror image of the front-overtake bias above: instead of easing AWAY from
# the tighter side (to slip past a car ahead), ease TOWARD the tighter side
# (to make a chasing car go further round to complete a pass). Reuses the
# same left_gap/right_gap cones and the same slewed-line/`fade` machinery in
# compute_control, so it inherits the existing safety properties for free:
# never fights a mapped corner entry (line_raw==0.0 gate), never darts
# (_LINE_SLEW), and fades out mid-corner (`fade` in compute_control).
# This is brand new behaviour and untested on-track — start small and only
# raise it after several clean live races, same discipline as the
# _A_LAT/_A_BRAKE corner-speed ladder in track_model.py.
_BLOCK_TRIGGER_GAP = 20.0   # m: rear gap this close switches safety_filter's
                             # output to BLOCK, every frame — see safety_filter.
                             # No need to wait for Granite's slower poll.
_BLOCK_GAIN        = 0.15   # tpos units: how far to ease toward the threatened
                             # side. Deliberately far smaller than _OVERTAKE_BIAS
                             # (0.50) for the first on-track test.
_BLOCK_ROOM_MARGIN = 5.0    # m: same idea as _OVERTAKE_ROOM_MARGIN, kept as its
                             # own constant so it can be tuned independently.

# Corner-speed sharpness: target speed depends on BOTH how far the road is clear
# AND how sharp the corner is.  Sharpness = angle of the most-open direction off
# straight-ahead (|pursuit target|): a 90° corner has a big angle and must be
# taken far slower than a gentle bend with the same sight distance.
#   corner_speed = sqrt(floor² + sight·factor) / (1 + _CORNER_SHARPNESS·sharpness)
_CORNER_SHARPNESS = 1.3

# Corner-speed floor, as a fraction of the strategy's max_speed.  Without it
# the braking curve sqrt(sight·factor) ends at ZERO — "must be able to come to
# a complete stop within sight" — which is wildly conservative: with ~100 m of
# sight it capped the car at ~150 km/h on a near-straight, lifting half a
# straight early.  A racing car only ever needs to slow to the CORNER speed;
# the floor moves the curve's endpoint there.  Genuinely sharp corners are
# still slowed further by the sharpness divisor.
_CORNER_FLOOR = 0.35
_STRAIGHT_ANGLE   = 0.20   # rad (~11°): below this the open road counts as straight

# Sharpness free band: |pursuit| below this is the off-centre pull (pursuit is
# nonzero whenever the car is off the centre line — that is HOW it re-centres),
# not track curvature.  Penalising it cut the throttle on straights whenever
# the car ran off-centre; only the excess above this band counts as a corner.
# 0.10 → 0.14 (2026-08-06): user reported gentle bends braking noticeably even
# though they aren't real corners — widened so more mild curvature reads as
# "not a corner" and skips the _CORNER_SHARPNESS divisor entirely.  Deliberately
# a small step and _CORNER_SHARPNESS itself is untouched, so genuinely sharp
# corners (open_angle well above this band either way) keep their full
# penalty — this only trims the free band, it doesn't make the curve gentler
# overall.  Verify on track before widening further: this sensor curve is the
# only defense against corners the map's own profile doesn't catch (see the
# blind-corner incidents in project memory) — do not loosen it aggressively.
_SHARP_FREE = 0.14

# Forward sight (m) at/above which the road is treated as an open straight
# and the corner-speed cap is lifted (track sensors saturate ~200 m).
_STRAIGHT_CLEAR = 180.0

# Throttle ease-off band: proportional throttle within this many km/h of the
# target instead of bang-bang.  Full-below/zero-above pulsed the pedal the
# whole way down a straight once the car touched its top speed — cruise is a
# steady partial throttle, not taps.
# 2026-08-08 (step 1/3 of the bt pace comparison — see conversation history):
# bt's equivalent margin (FULL_ACCEL_MARGIN, driver.cpp) is 1.0 m/s (~3.6
# km/h) and its blend within that margin is computed from actual gear-ratio/
# redline physics, so it can afford to hold full throttle almost to the
# limit without oscillating. We don't have that continuous physical relation
# — our target_speed is a noisier sensor/heuristic estimate — so shrinking
#_ACCEL_BAND too far risks reintroducing the pedal-tapping bug this band was
# added to fix. 15 -> 6 is a partial step (not all the way to bt's 3.6):
# still 4x tighter than before, but with more margin than bt's exact number
# to absorb our target-speed noise. Unvalidated live — watch for throttle
# oscillation on a straight at the speed cap before shrinking further.
_ACCEL_BAND = 6.0

# Target-speed smoothing (asymmetric): drops apply INSTANTLY so braking never
# lags a corner, but the target may only climb this many km/h per tick
# (~150 km/h/s at 50 Hz — still faster than the car can accelerate).  Kills
# the per-tick flicker between the straight cap and the corner curve near a
# threshold crossing, which was the other source of throttle stutter.
_TARGET_RISE = 3.0
_target_lp: float | None = None   # module state: smoothed target speed

# Pre-race track map (P1): distFromStart → speed-limit lookup built from the
# track's own XML before the race, exactly the way a human driver studies the
# circuit map.  It caps the target speed via min() — the map can only slow
# the car for corners the sensors cannot see yet, never speed it up, so a
# missing or misaligned map degrades to plain sensor driving, not a crash.
_track_model: Any = None          # module state: TrackModel or None

# Map TRUST mode: min() alone can never beat the sensors, so on stretches
# where the map is provably reliable it is allowed to RAISE the target too —
# cancelling the sensors' false straight-line lifts (the 0° beam grazing the
# edge misreads sight and taps the brake at 200+ km/h several times a lap)
# and braking later into known corners.  Five gates, ALL required; any
# failure falls back to the plain min() behaviour:
#   1. the practice-lap odometer calibration has completed (real_lap set);
#   2. the car is on line and aligned (|tpos|, |angle| small);
#   3. no opponent in the forward cone — the map cannot see traffic;
#   4. sensor sight is consistent with the map's next-corner distance —
#      if the road looks far more blocked than the map predicts, something
#      is out there that the map does not know about;
#   5. the local map limit is fast (≥ _TRUST_MIN_KMH): slow corners and
#      apexes are NEVER trusted — they stay with the sensors entirely.
_TRUST_MIN_KMH    = 150.0   # gate 5: never trust the map below this limit
_TRUST_TPOS       = 0.9     # gate 2: max |track_pos| to count as "on line"
_TRUST_ANGLE      = 0.3     # gate 2: max |angle| (rad) to count as aligned
_TRUST_OPP_CLEAR  = 100.0   # gate 3: forward cone must be clear this far (m)
_TRUST_SIGHT_FRAC = 0.6     # gate 4: sight ≥ frac · min(next corner, 200 m)
_TRUST_MARGIN     = 0.97    # trusted target = map limit × this safety margin

# Brake-point mode: when the MAP's braking curve is what limits the target,
# drive it like a racing driver — hold FULL throttle until the curve is
# reached, then brake firmly to track it.  The gentle throttle-easing band
# (_ACCEL_BAND) exists to stop pedal-tapping while cruising at a cap; applied
# to a descending braking curve it made the car lift ~180 m out and coast to
# the corner instead of powering to the brake point (user-visible, and slow).
_MAP_THROTTLE_BAND  = 0.97  # full throttle below this fraction of the curve;
                            # the 3% neutral gap prevents throttle/brake sawtooth
_MAP_BRAKE_RESPONSE = 5.0   # curve needs ~11 m/s²; the cruise gain (3.0) let
                            # the car ride 20-30% hot into every corner.  5.0
                            # is safe HERE because it only applies on the map
                            # curve (straight-line braking) and the trail-brake
                            # steer cut still protects corner entry.


def set_track_model(model: Any) -> None:
    """Install the pre-race track map (None disables it)."""
    global _track_model
    _track_model = model

# Last computed speed-planning values, surfaced in run_bot's periodic log so a
# throttle lift on track can be traced to its cause (short sight? big angle?).
_dbg: dict[str, float] = {}

# Brake deadband: tolerate a small overspeed before touching the brakes.
# Kept SMALL: the anti-tap job this used to do is now handled by the smoothed
# target (_TARGET_RISE) and the hold-line rule, while a wide band let the car
# ride 5-15% above the braking curve all the way into a tightening corner and
# arrive too hot (the same corner claimed it two runs in a row).
_BRAKE_DEADBAND = 0.02

# Brake response: how hard the brake ramps once past the deadband.  The target
# speed curve sqrt(d·factor) implies ~9-11 m/s² of deceleration — near full
# braking — but a proportional term with gain ~1 commanded only ~15% brake at
# 15% overspeed, so the car tracked the curve arriving into corners far too hot
# and ran wide.  Do NOT raise this much further: the corner-sharpness factor
# lowers the target DURING turn-in, so an over-eager response slams the brakes
# while the wheel is turned and spins the car into the inside wall (verified —
# 5.0 was undrivable).  _BRAKE_STEER_CUT below is the matching protection and
# is what makes 3.0 safe where the unguarded 5.0 was not.
_BRAKE_RESPONSE = 3.0

# Trail-brake protection: release the brake as steering builds.  Hard braking
# with the wheel turned unloads the rear axle and snaps the car toward the
# apex; brake hard in a straight line, gently once turned in.
#   brake *= 1 − cut·|steer|   → full steer keeps 40% of the brake.
_BRAKE_STEER_CUT = 0.6

# Stuck / crash recovery: if the car sits at a crawl for a sustained spell while
# JAMMED (nose into a wall/car, or pinned at the track edge), back up for a
# fixed burst, then try again.  Works on OR off track.  The "jammed" gate is
# what stops it firing on a clear standing start or in the pits, where the car
# is briefly slow but the road ahead is open.
_STUCK_SPEED    = 5.0     # km/h: below this we *might* be stuck
_STUCK_WALL     = 8.0     # m: front sensor below this = something right in front
_STUCK_FRAMES   = 60      # consecutive jammed frames before we decide we're stuck
_stuck_frames   = 0       # module state: consecutive jammed frames seen

# 2026-08-09: bt-style continuous re-check, replacing the old fixed
# _REVERSE_FRAMES=40 blind burst. bt's Driver::isStuck() (driver.cpp) is
# re-evaluated every single tick while reversing, and drive() drops straight
# back to normal driving the instant it goes false — no committed duration.
# Our old burst reversed for a flat 40 frames (0.8 s) no matter what: a car
# freed after 3 frames still reversed for the other 37, wasting time at best
# and backing into a NEW problem (another wall, a car behind) at worst.
# `_bursting` is now a latch like `_recovering`/`_turnaround` elsewhere in
# this file: stays true only while the SAME jam signal that triggered it
# (front blocked or pinned at the edge) is still true, re-checked every tick.
# Two things bt doesn't need but our proxy does:
#   - _UNSTUCK_MIN_FRAMES: a single 20 ms tick of reverse can't have moved
#     far enough for the front sensor to mean anything yet — this isn't a
#     commitment to reverse regardless of state, just "don't trust a sample
#     that's one tick old" before allowing the first exit check.
#   - _UNSTUCK_MAX_FRAMES: safety backstop in case the jam signal never
#     clears (wedged against something reverse can't back away from) — same
#     role as the turnaround's _TA_REV_MAX_FRAMES cap below.
_bursting           = False   # module state: currently reversing out of a jam
_UNSTUCK_MIN_FRAMES = 10      # frames before the burst is even allowed to exit
_UNSTUCK_MAX_FRAMES = 150     # hard cap (3 s @ 50 Hz) if the jam never clears
_burst_frames       = 0       # module state: frames elapsed in the current burst

# Recovery mode: off-track re-entry and wrong-way turn-around.  Track sensors
# read -1 out there, so this mode drives purely on angle + track_pos.
# ENTER threshold must sit clearly above 1.0: riding the apex kerb legitimately
# pushes |track_pos| just past 1 (the racing line is SUPPOSED to do that), and
# a recovery grab there yanked the car to the centre mid-corner and threw it
# off the outside.  The 1.0–1.15 band is covered by the blind-sensor fallback
# in compute_control, which is gentle and stateless.
_RECOVERY_MAX_KMH   = 55.0          # speed cap while returning to the track
_RECOVER_ENTER_TPOS = 1.15          # genuinely off (wheels on the grass), not a kerb
_RECOVER_EXIT_TPOS  = 0.85          # hand back to normal driving only once well
_RECOVER_EXIT_ANGLE = 0.35          #   inside the track AND roughly aligned
_WRONG_WAY          = math.pi / 2   # |angle| beyond this = facing the wrong way
_TURNAROUND_EXIT    = 0.5           # rad: keep turning until this aligned (hysteresis)
_TA_JAM_FRAMES      = 40            # jammed-in-reverse frames before a forward leg
_TA_FWD_FRAMES      = 40            # length of the forward leg (three-point turn)
_TA_JAM_SPEED       = 6.0           # km/h: below this a turnaround leg counts as
                                    # jammed.  Was 2.0 — a car rocking against a
                                    # wall bounced to 3-7 km/h, kept resetting
                                    # the counter, and sat in reverse for the
                                    # rest of the race; 6 lets the three-point
                                    # turn actually alternate and rock free.
_TA_REV_MAX_FRAMES  = 120           # hard cap on one continuous reverse leg (2.4 s
                                    # @ 50 Hz), regardless of _ta_jam.  _ta_jam only
                                    # counts frames where speed < _TA_JAM_SPEED, so a
                                    # car reversing FREELY (not stuck) but whose angle
                                    # just isn't converging never tripped it — logged
                                    # live: one recovery backed the car up ~40 m before
                                    # anything forced a forward leg.  This is a second,
                                    # independent backstop: however well the reverse is
                                    # going, force a forward leg after 2.4 s so no
                                    # single reverse attempt can travel far.
_TA_REV_MAX_KMH     = 25.0          # reverse speed cap for the turn-rev leg — it had
                                    # none, so accel=0.5 held for the full 2.4 s window
                                    # let a freely-reversing car build to 60+ km/h;
                                    # logged live: that carried track_pos across the
                                    # entire track width (-0.68 to +1.06) and into a
                                    # second collision (damage 765 -> 1526) that then
                                    # needed a long stabilize crawl to undo. A slow,
                                    # controlled reverse is the point of a three-point
                                    # turn, not distance covered.

# Extreme-excursion stabilize gate (checked at the very top of compute_control,
# ahead of BOTH the stuck-jam burst and the wrong-way turnaround): a violent
# impact can fling the car many track-widths off (observed live: track_pos hit
# +7 after a high-speed hit, vs. the normal 1.15-2 range for a plain off-track
# excursion) — and can leave it either still carrying impact speed OR already
# nearly stationary.  First cut of this only handled the "still fast" half
# (gated on speed > _EXTREME_STOP_SPEED) and left it inside _recovery_control;
# a live incident showed the *already slow* half never triggers that gate at
# all, so the car fell straight through to the stuck-jam burst and wrong-way
# turnaround fighting each other — neither knows the other exists, and
# neither factors in how extreme track_pos actually is — for 30+ seconds
# while track_pos kept climbing instead of recovering.  Moved to the top of
# compute_control and widened to cover both cases so it pre-empts every tick
# the position stays this extreme, not just the high-speed moment.
_EXTREME_TPOS       = 2.5           # track_pos units: this far off is not a normal
                                    # kerb/off-track case (those top out ~1.15-2);
                                    # it means the car was thrown clear of the track.
_EXTREME_STOP_SPEED = 10.0          # km/h: above this, brake off the impact speed
                                    # first; at or below it, hold a single steady
                                    # reverse-toward-centre creep instead (see
                                    # compute_control) rather than sitting idle.

# No-progress watchdog: track_pos alone isn't a reliable "is this recovery
# attempt actually working" signal — logged live, a car wedged at track_pos
# ~2.3 (just UNDER the 2.5 extreme-excursion gate above) sat with
# dist_from_start frozen and the stuck-jam burst / wrong-way turnaround
# cycling between each other for 46+ real seconds without ever escaping.
# Position alone can't tell "off to the side but free to manoeuvre" apart
# from "wedged against a wall no matter which way you point the wheels" —
# actual forward progress can.  So: whenever the mode from the previous tick
# wasn't plain "race" (i.e. some recovery branch is active), watch
# dist_from_start; if it hasn't moved _NO_PROGRESS_DIST in _NO_PROGRESS_FRAMES,
# whatever's running clearly isn't working — escalate to the same stabilize
# action as the extreme-excursion gate, regardless of how far off-centre the
# car actually is.
_NO_PROGRESS_FRAMES = 200           # 4 s @ 50 Hz before a stalled recovery escalates
_NO_PROGRESS_DIST   = 5.0           # m: must gain at least this much in that window

_recovering = False   # module state: in off-track re-entry (with hysteresis)
_turnaround = False   # module state: executing a wrong-way turn-around
_ta_fwd     = 0       # module state: forward-leg frames remaining
_ta_jam     = 0       # module state: consecutive jammed frames while reversing
_ta_rev     = 0       # module state: consecutive frames in the current reverse leg
_stuck_progress_dist   = None   # module state: dist_from_start when the current
                                # no-progress watch window started (None = not watching)
_stuck_progress_frames = 0      # module state: frames elapsed in that window
_stabilizing = False   # module state: latched in the stabilize action (either
                       # trigger) until track_pos/angle are genuinely safe again
_stabilize_bled = False   # module state: has this stabilize episode already
                          # braked off the post-impact speed once? Re-armed
                          # alongside _stabilizing on each fresh entry.

# 2026-08-09: stabilize's own no-progress watchdog.  _stabilize_action's
# wrong-way branch (see below) always reverses, with no escape if reverse
# itself is blocked (wedged nose-first into a wall after a spin) -- logged
# live: a car sat at track_pos +1.09, angle +136°, sight 0.1 m, commanding
# accel=0.5/gear=-1 every tick for minutes without moving. The plain
# _stuck_progress_dist/_frames watchdog above can't catch this: it only runs
# BEFORE compute_control's `if _stabilizing: return _stabilize_action(...)`
# gate, so once latched into stabilize it never executes again -- stabilize
# had no watchdog of its own. This one runs INSIDE the latch, using the same
# _NO_PROGRESS_DIST/_FRAMES tuning, and hands off to _recovery_control's
# three-point-turn (_turnaround) escape -- which DOES alternate reverse and
# forward legs -- instead of repeating a reverse that isn't working.
_stabilize_stuck_dist   = None   # module state: dist_from_start when the current
                                  # stabilize-progress window started (None = not watching)
_stabilize_stuck_frames = 0      # module state: frames elapsed in that window

# Pit lane docking (bt parity — see pit.cpp/strategy.cpp).  _pit_docking
# latches once the car actually enters the physical pit lane range while
# strategy is PIT, and stays latched until it exits that range regardless of
# later strategy changes — mirrors real pit rules: once you're in the lane
# you finish the stop, you don't swerve back onto the racing line mid-lane.
# _pit_serviced latches once the engine reports inPitStop=1 at least once
# during the current visit (i.e. the stop was actually captured and the
# refuel/repair applied), so the car resumes toward pit_exit afterwards
# instead of re-braking to a stop it has already completed.
_pit_docking  = False   # module state: committed to the current pit lane visit
_pit_serviced = False   # module state: already captured + serviced this visit
_pit_prev_angle: float | None = None   # module state: angle one tick ago, for the
                                        # yaw-rate damping term in _pit_control's
                                        # steer law (see its 2026-08-10 comment).
                                        # None = no previous sample yet (fresh arm).

# Fuel-per-lap tracking (bt parity — see SimpleStrategy.update(), strategy.cpp).
# bt measures actual fuel burned over the last completed lap and uses that
# (once available) instead of the pre-race estimate; we do the same, keyed
# off last_lap_time changing rather than bt's track-segment-id proximity
# check (SCR gives no segment id) — same "a new lap just completed" signal
# run_bot()'s own [lap] A/B gauge already uses (see run_bot()), but tracked
# independently here since that's local state for a different statistic.
_FUEL_PER_METER      = 0.0008   # L/m: bt's pre-race estimate (strategy.cpp)
_fuel_last_lap_time  = 0.0      # module state: last_lap_time value last seen
_fuel_at_lap_start   = None     # module state: fuel reading at current lap's start
_fuel_per_lap_est    = 0.0      # module state: measured fuel-per-lap, 0 = no data yet


def _update_fuel_model(state: dict[str, Any]) -> None:
    """Update the measured fuel-per-lap estimate from the latest tick.

    Call once per tick, before anything (safety_filter, the Granite prompt)
    reads state["fuel_per_lap"].  Pure bookkeeping — never changes driving
    behaviour by itself.
    """
    global _fuel_last_lap_time, _fuel_at_lap_start, _fuel_per_lap_est
    fuel = state.get("fuel", 0.0)
    if _fuel_at_lap_start is None:
        _fuel_at_lap_start = fuel
    llt = state.get("last_lap_time", 0.0)
    if llt > 0.0 and abs(llt - _fuel_last_lap_time) > 1e-3:
        burned = _fuel_at_lap_start - fuel
        if burned > 0.0:
            _fuel_per_lap_est = burned
        _fuel_at_lap_start  = fuel
        _fuel_last_lap_time = llt


def _reset_driver_state() -> None:
    """Reset all module-level driving state (tests / new race)."""
    global _stuck_frames, _bursting, _burst_frames, _recovering, _turnaround, _ta_fwd, _ta_jam, _ta_rev
    global _target_lp, _line_lp, _stuck_progress_dist, _stuck_progress_frames, _stabilizing
    global _stabilize_bled, _avoid_lp, _front_gap_prev, _close_rate_lp, _launch_clutch_timer
    global _standoff_timer, _side_gap_prev, _side_close_rate_lp, _tpos_prev
    global _pit_docking, _pit_serviced, _pit_prev_angle, _pit_prev_angle
    global _fuel_last_lap_time, _fuel_at_lap_start, _fuel_per_lap_est
    global _stabilize_stuck_dist, _stabilize_stuck_frames
    _stuck_frames = 0
    _bursting = False
    _burst_frames = 0
    _recovering = _turnaround = False
    _ta_fwd = _ta_jam = _ta_rev = 0
    _target_lp = None
    _line_lp = 0.0
    _stuck_progress_dist = None
    _stuck_progress_frames = 0
    _stabilizing = False
    _stabilize_bled = False
    _avoid_lp = 0.0
    _front_gap_prev = None
    _close_rate_lp = 0.0
    _launch_clutch_timer = 0.0
    _standoff_timer = 0.0
    _side_gap_prev = None
    _side_close_rate_lp = 0.0
    _tpos_prev = None
    _pit_docking = False
    _pit_serviced = False
    _pit_prev_angle = None
    _fuel_last_lap_time = 0.0
    _fuel_at_lap_start = None
    _fuel_per_lap_est = 0.0
    _stabilize_stuck_dist = None
    _stabilize_stuck_frames = 0


def _recovery_steer(angle: float, tpos: float) -> float:
    """Steer command for backing out of a crash: de-rotate + drift to centre.
    In reverse the steering effect inverts, so the signs are flipped relative to
    the normal forward correction."""
    return clamp(-angle * 0.5 + tpos * 0.4, -0.6, 0.6)


def _stabilize_action(speed: float, angle: float, tpos: float, gear: int,
                       speed_y: float) -> str:
    """Shared control for the extreme-excursion and no-progress stabilize gates.

    Brake to a stop if still carrying real speed from the impact — checked
    only once per episode via _stabilize_bled, not every tick; without that
    latch this re-triggered the instant the creep below got the car back
    above _EXTREME_STOP_SPEED, capping the whole recovery crawl at ~10 km/h
    (logged live: speed pinned at 9.0-9.7 km/h for 6+ seconds). Once bled,
    creep back toward the centre line — forward if roughly facing the right
    way (reusing the plain off-track re-entry steer formula, the faster way
    back), reverse only if facing badly wrong (forward would just dig the
    hole deeper).
    """
    global _stabilize_bled
    if not _stabilize_bled:
        if abs(speed) > _EXTREME_STOP_SPEED:
            return format_scr_control(accel=0.0, brake=0.9, gear=max(gear, 1), steer=0.0)
        _stabilize_bled = True
    if abs(angle) > _WRONG_WAY:
        return format_scr_control(accel=0.5, brake=0.0, gear=-1,
                                  steer=_recovery_steer(angle, tpos))
    steer = clamp(angle - tpos * 0.5 - speed_y * _STEER_DAMP, -1.0, 1.0)
    fwd_gear = 1 if abs(speed) < 30.0 else _gear_from_speed(max(gear, 1), speed)
    return format_scr_control(accel=0.5, brake=0.0, gear=fwd_gear, steer=steer)


def _recovery_control(state: dict[str, Any]) -> str:
    """Bring the car back to normal driving after an excursion.

    Handles two situations the old code got wrong:
      * off-track re-entry — the old steer aimed at the centre line while
        ignoring the car's heading, so the car crossed the track sideways and
        often shot straight off the opposite edge;
      * facing the wrong way — there was NO wrong-way handling at all: a spun
        car (|angle| > 90°) inside the track fell through to the normal branch,
        whose sensors read -1, and calmly drove off in the reverse direction.
    """
    global _turnaround, _ta_fwd, _ta_jam, _ta_rev

    speed   = state.get("speed_x", 0.0)
    speed_y = state.get("speed_y", 0.0) / 3.6
    gear    = state.get("gear", 1)
    angle   = state.get("angle", 0.0)
    tpos    = clamp(state.get("track_pos", 0.0), -2.0, 2.0)
    wheels  = state.get("wheel_spin_vel", [])

    # --- wrong way: turn the car around (hysteresis: finish the manoeuvre) ---
    if abs(angle) > _WRONG_WAY:
        _turnaround = True
    if _turnaround:
        if abs(angle) < _TURNAROUND_EXIT:
            _turnaround = False              # aligned — fall through to re-entry
            _ta_fwd = _ta_jam = _ta_rev = 0
        elif speed > 15.0:
            # Still rolling forward in the wrong direction — stop first.
            _dbg["mode"] = "turn-stop"
            return format_scr_control(accel=0.0, brake=0.8, gear=max(gear, 1),
                                      steer=clamp(angle * 0.3, -1.0, 1.0))
        elif _ta_fwd > 0:
            # Forward leg of a three-point turn (the reverse leg was blocked).
            _ta_fwd -= 1
            _dbg["mode"] = "turn-fwd"
            return format_scr_control(accel=0.4, brake=0.0, gear=1,
                                      steer=clamp(angle, -1.0, 1.0))
        else:
            # Reverse leg: steering inverts in reverse, so -angle swings the
            # nose toward the track direction while backing off the obstacle.
            if abs(speed) < _TA_JAM_SPEED:
                _ta_jam += 1
            else:
                _ta_jam = 0
            _ta_rev += 1
            if _ta_jam >= _TA_JAM_FRAMES or _ta_rev >= _TA_REV_MAX_FRAMES:
                # Blocked behind too, OR this reverse leg has simply run long
                # enough (car moving fine but angle not converging) → stop
                # backing up regardless and try a forward leg instead.
                _ta_jam = _ta_rev = 0
                _ta_fwd = _TA_FWD_FRAMES
            _dbg["mode"] = "turn-rev"
            if abs(speed) > _TA_REV_MAX_KMH:
                rev_accel, rev_brake = 0.0, 0.5
            else:
                rev_accel, rev_brake = 0.5, 0.0
            return format_scr_control(accel=rev_accel, brake=rev_brake, gear=-1,
                                      steer=clamp(-angle * 0.8 + tpos * 0.3, -1.0, 1.0))

    # --- facing roughly the right way: drive back to the centre line ---
    # Classic (angle − 0.5·track_pos) controller: heading and offset errors
    # balance out so the car approaches the centre line at a shallow angle and
    # straightens as it gets there — no sideways crossing, no overshoot.
    steer = clamp(angle - tpos * 0.5 - speed_y * _STEER_DAMP, -1.0, 1.0)
    if speed > _RECOVERY_MAX_KMH:
        accel, brake = 0.0, 0.5      # too hot for grass/kerbs — shed speed first
    else:
        accel, brake = 0.5, 0.0
    accel = _apply_tcl(accel, speed, wheels)   # grass has next to no grip
    brake = _apply_abs(brake, speed, wheels)
    fwd_gear = 1 if speed < 30.0 else _gear_from_speed(max(gear, 1), speed)
    _dbg["mode"] = "re-entry"
    return format_scr_control(accel=accel, brake=brake, gear=fwd_gear, steer=steer)


def _pursuit_target(track: list[float]) -> float | None:
    """Pure-pursuit heading: the direction (radians, car frame, steer convention)
    the track extends furthest — a distance-weighted average of the beam angles,
    longer beams dominating (``** _PP_POWER``).  None if no beams are usable
    (sensors read -1 off-track / facing backwards — do NOT trust them).
    ``|return|`` doubles as the corner-sharpness measure."""
    num = den = 0.0
    for i in _PP_ARC:
        d = track[i] if i < len(track) else -1.0
        if d <= 0.0:
            continue
        w = d ** _PP_POWER
        num += _SENSOR_ANGLES_RAD[i] * w
        den += w
    return num / den if den > 0.0 else None


# ---------------------------------------------------------------------------
# Pit lane docking (bt parity — see pit.cpp/strategy.cpp for the mechanism
# this is adapted from).  bt reads pit geometry straight off tTrack/tCarElt;
# we get the same numbers over the wire (scr_server.cpp exposes them once
# per race, see newrace()) since the SCR protocol otherwise never told the
# client where the pits even are.
# ---------------------------------------------------------------------------

def _pit_spline_coord(x: float, entry: float, track_length: float) -> float:
    """Distance travelled past pit_entry, wrapped into [0, track_length).

    Mirrors bt's Pit::toSplineCoord (pit.cpp) — the pit lane commonly
    straddles the start/finish line (pitEntry near the end of the lap,
    pitStart near its beginning: true on wheel-1's own track file), so raw
    lgfromstart values can't be compared directly without unwrapping
    relative to a fixed reference first.
    """
    if track_length <= 0.0:
        return x - entry
    return (x - entry) % track_length


def _pit_ease(x: float, a: float, b: float) -> float:
    """Smoothstep 0..1 from a to b; snaps to 1 for a degenerate/zero-length
    span (b <= a) rather than dividing by ~0. Same "ease in / hold / ease
    out" shape bt gets from its 7-point cubic Spline (pit.cpp) — the literal
    spline class isn't what matters here, the shape is."""
    if b <= a:
        return 1.0
    frac = clamp((x - a) / (b - a), 0.0, 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * frac)


def _pit_target_tpos(s_now: float, s_lead: float, s_start: float, s_end: float,
                      s_exit: float, box_tpos: float) -> float:
    """Target lateral track_pos through the pit lane: ease from the racing
    line (0) onto the box's offset over [s_lead, s_start], hold it over
    [s_start, s_end], ease back to the racing line over [s_end, s_exit].

    2026-08-10: s_lead used to be hardcoded to 0.0 (the ease only started at
    pit_entry itself), which on Forza left just the 58 m [pit_entry,
    pit_start] gap to swing ~1.8 track_pos units (~8 m) onto the box offset
    -- verified live: not enough room, the car stopped ~8 m short of the
    box, never satisfying the engine's lateral capture check. Passing a
    negative s_lead (see _PIT_APPROACH_DIST in compute_control) starts the
    ease that much earlier, before the car has even reached pit_entry."""
    if s_now <= s_start:
        return box_tpos * _pit_ease(s_now, s_lead, s_start)
    if s_now <= s_end:
        return box_tpos
    return box_tpos * (1.0 - _pit_ease(s_now, s_end, s_exit))


_PIT_EDGE_TPOS = 0.75   # track_pos units: fallback aim if seg_width data is missing
                        # (older scr_server build) -- just lean toward the pit side
                        # rather than not moving at all. Not used when the precise
                        # conversion below has real data.
_PIT_LOOKAHEAD_M = 8.0  # metres: how far down the pit spline _pit_control aims its
                        # steering target (bt parity: PIT_LOOKAHEAD=6.0 in driver.cpp,
                        # see the 2026-08-10 comment on _pit_control's steer computation).
_PIT_LEADIN_MAP_MARGIN = 0.85   # derate _track_model.limit_kmh by this much during the
                                # pre-entry lead-in -- that limit assumes the racing
                                # line's apex-widened effective radius, which a car
                                # pinned to track_pos=0 (the whole point of the lead-in)
                                # never gets. See the 2026-08-10 comment where it's used.
_PIT_MAX_CRAB_ANGLE = 0.45   # rad (~26 deg): hard cap on aim_angle below. atan2(gap, 8 m)
                             # is unbounded -- with box_tpos several track_pos units off
                             # centreline (forza: ~3.45), the lateral gap stays large for
                             # a long stretch of the approach (it only shrinks as tpos
                             # itself catches up, which takes many ticks), so atan2 keeps
                             # demanding a bigger aim_angle than the car has yet reached.
                             # Verified live on forza: steer and angle grew in lockstep,
                             # smoothly, tick over tick, from steer=-0.02/angle=+0.02 at
                             # pit_entry all the way to steer=+0.84/angle=-1.09 rad (~62
                             # deg) 97 steps later at the moment of impact -- the car was
                             # never fighting the command or failing to respond, it was
                             # faithfully chasing an aim_angle that the formula itself
                             # never stopped raising. Capping aim_angle forces the ease to
                             # take longer (more track distance) to converge instead of
                             # commanding a heading no merge at speed should ever need.
_PIT_ALIGN_TPOS  = 0.05   # track_pos units: lateral error tracked for the pit_aligned debug
                          # field only (see below) — no longer gates the speed target.
_PIT_ALIGN_ANGLE = 0.05   # rad (~3 deg): heading error, same debug-only role as above.
_PIT_YAW_DAMP = 2.0   # steer per (rad/s) of heading rate -- the missing derivative term
                       # in _pit_control's steer law, see its 2026-08-10 comment. Raised
                       # from the first attempt (0.4): verified live it slowed the drift
                       # (angle's tick-over-tick growth was smaller than pre-damping runs)
                       # but didn't stop it -- still net-unstable, just a slower climb to
                       # the same crash. 0.4 wasn't enough authority to cancel whatever is
                       # driving the growth; raised 5x.
_PIT_BOX_OFFSET_M = 50.0   # metres: user-reported live/visual correction -- pit_start and
                           # pit_end as TORCS reports them sit this far past where the pit
                           # box row actually renders on forza. Subtracted from both before
                           # use (see compute_control's pit dispatch). Not applied to
                           # pit_entry/pit_exit -- only pit_start/pit_end were reported off.
_PIT_CREEP_KMH   = 8.0    # km/h: minimum speed kept only during the APPROACH ease
                          # (pit_entry..pit_start, see the s_now <= s_start branch below).
                          # 2026-08-09: originally this floor also applied inside the stop
                          # zone itself, gated on an "aligned" check, to avoid a car getting
                          # stuck mid-turn with no speed left to correct itself — verified
                          # live: a car sat for 6000+ ticks at track_pos+0.76/angle+136
                          # degrees wide of the box, never converging.
                          # 2026-08-10: turned out that "fix" was itself the bigger bug —
                          # re-reading bt (driver.cpp filterBPit) shows it does NOT hold any
                          # creep floor in the stop zone; it brake-distance-computes to a
                          # full stop and, once at/past the pit location, holds brake=1.0
                          # unconditionally regardless of alignment (pit.cpp: "Stop in the
                          # pit"). Our 8 km/h floor sat ABOVE the engine's own capture gate
                          # (car->_speed_x < 1.0 m/s ≈ 3.6 km/h, raceengine.cpp ReManage), so
                          # gating the drop to 0 on alignment meant a car that never
                          # converged laterally would cruise the entire [s_start, s_end] zone
                          # at 8 km/h and sail through uncaptured every single time — a
                          # strictly worse failure than the stuck-mid-turn case it was meant
                          # to fix. The stop zone now always targets 0 (see below); this
                          # constant's only remaining job is the pre-stop approach ease.

def _pit_control(state: dict[str, Any], s_now: float, s_lead: float, s_start: float,
                  s_end: float, s_exit: float, released: bool) -> str:
    """Drive through the pit lane box: hold the pit speed limit, ease onto
    the box's lateral offset, brake to a stop inside [s_start, s_end] (bt:
    filterBPit / getSpeedLimitBrake, driver.cpp), then — once ``released``
    (the engine has already captured and serviced this stop) — resume to
    the pit speed limit and drive out to pit_exit."""
    speed    = state.get("speed_x", 0.0)
    speed_y  = state.get("speed_y", 0.0) / 3.6
    angle    = state.get("angle", 0.0)
    tpos     = state.get("track_pos", 0.0)
    gear     = state.get("gear", 1)
    wheels   = state.get("wheel_spin_vel", [])
    limit_kmh = max(state.get("pit_speed_limit", 60.0), 10.0)

    # pit_box_offset arrives in raw metres (bt parity, pit.cpp: its Spline
    # is built entirely from car->_pit->pos.toMiddle, never normalized).
    # 2026-08-09: this legitimately reads several metres past the normal
    # +-1 track_pos range on real tracks (e.g. ~19 m on forza, whose pit
    # apron is a 15 m widening of an 11 m base track) -- the MAGNITUDE has
    # always checked out, the pit lane really is that far off centreline.
    #
    # 2026-08-10: the SIGN does not check out. Confirmed live watching
    # forza: the track declares its pit side="right" (pit_side reads 1,
    # TR_RGT), but steering toward the raw pit_box_offset's own sign sent
    # the car to the opposite side of the track from the real pit garage,
    # stopping ~50 m from it. track3.cpp's TR_PIT_ON_TRACK_SIDE placement
    # code computes toMiddle in a way that does not match the side it was
    # just told to place the pit on (see the toRight/toLeft trace that
    # found this) -- a bug in that geometry code itself, not something
    # introduced here. pit_side, by contrast, is an unprocessed read of the
    # track file's own declared side and isn't touched by that bug, so it's
    # the trustworthy source for direction; take the engine's real trackPos
    # convention (+ left, - right -- confirmed via scr_server.cpp's
    # unmodified toMiddle->trackPos passthrough on the CAR's own position,
    # a completely different code path from the pit-placement one that's
    # wrong) as ground truth for what that direction should be: right ->
    # negative, left -> positive. Keep pit_box_offset for magnitude only.
    seg_width = state.get("seg_width", -1.0)
    box_m     = state.get("pit_box_offset", 0.0)
    pit_side  = state.get("pit_side", -1)
    sign = -1.0 if pit_side == 1 else (1.0 if pit_side == 2 else 0.0)
    if seg_width > 0.0:
        box_tpos = sign * abs(box_m) / (seg_width / 2.0)
    else:
        # Fallback for a scr_server build predating pitBoxOffset/segWidth:
        # same sign, just no live magnitude to work with.
        box_tpos = sign * _PIT_EDGE_TPOS

    # 2026-08-10: the lateral ease must NOT start at s_lead. s_lead exists
    # purely to buy extra distance for the SPEED ease (see target_speed
    # below) -- it says nothing about where the road actually has room for
    # the car to move sideways. Checked forza's own track file: the segment
    # named "pit entry" is a 58.5 m taper whose right-side extra width goes
    # 2.0 m -> 15.0 m (i.e. box_tpos's ~15 m apron doesn't exist yet at its
    # start and isn't full width until pit_start); every segment BEFORE
    # "pit entry" has a plain wall at the normal track edge, zero extra
    # width. With s_lead=-150 feeding straight into _pit_target_tpos, the
    # smoothstep ease is already ~80% of the way to box_tpos by the time
    # s_now reaches 0 (pit_entry) -- demanding several metres of pavement
    # that, at that point on the track, simply is not there yet. That is
    # what actually put the car into the wall on forza: not a control-loop
    # bug, not a physics limit, but the ease being told to converge onto
    # ground that hadn't started widening. Clamping the lateral ease's
    # start to 0.0 (pit_entry itself, where the taper actually begins)
    # keeps target_tpos pinned to the racing line for the whole lead-in and
    # only lets it grow once there's real road backing it.
    target_tpos = _pit_target_tpos(s_now, 0.0, s_start, s_end, s_exit, box_tpos)
    aligned  = (abs(tpos - target_tpos) < _PIT_ALIGN_TPOS
                and abs(angle) < _PIT_ALIGN_ANGLE)
    # TEMP DEBUG (2026-08-10): TORCS's own pit-capture gate (raceengine.cpp
    # ReManage) requires BOTH fabs(car->_speed_x) < 1.0 m/s (~3.6 km/h --
    # note that's native m/s, stricter than our _PIT_CREEP_KMH=8.0 creep
    # speed) AND the car laterally inside the box width, simultaneously,
    # while pitRequest is set. Neither of those two conditions is visible
    # from the driving log today, which makes "the car passed through pit
    # lane but was never actually serviced" impossible to diagnose from the
    # log alone. Surfacing both here so a live run can show which one (if
    # either) is failing.
    _dbg["pit_target_tpos"] = target_tpos
    _dbg["pit_tpos_err"]    = tpos - target_tpos
    _dbg["pit_aligned"]     = int(aligned)
    _dbg["pit_speed_ok"]    = int(speed < 3.6)   # km/h; TORCS's own gate is 1.0 m/s
    if s_now <= s_start:
        # 2026-08-09: bleed speed down to creep pace over the WHOLE approach
        # (pit_entry..pit_start), not just inside the stop zone -- bt
        # spreads its braking over whatever distance is actually available
        # (brakedist() physics, driver.cpp), so it's already slow well
        # before the box. Verified live: entering the stop zone still near
        # the pit speed limit left too little of [s_start, s_end] to both
        # bleed off speed AND swing tpos onto a target several track-widths
        # away (see pit_box_offset above) -- the car blew straight through
        # the whole zone at ~47 km/h, never converging, and gave up once
        # s_now passed s_end. Arriving at s_start already near creep speed
        # leaves the full zone for lateral convergence instead.
        # 2026-08-10: ease now starts at s_lead (approach lead-in), not 0.0,
        # for the same reason _pit_target_tpos's ease was extended -- more
        # distance to shed speed, on top of more distance to converge.
        ease = _pit_ease(s_now, s_lead, s_start)
        target_speed = limit_kmh + (_PIT_CREEP_KMH - limit_kmh) * ease
        # 2026-08-10: this ease schedule has no idea the track curves here --
        # forza's own lead-in is mostly two real corners (curve 25/26, radii
        # 190.5 m / 410 m) with only 30 m of actual straight before pit
        # entry, not the straight it was designed assuming. Braking hard
        # AND holding track_pos rigidly at 0 through a real corner asks for
        # combined lateral + longitudinal grip a tyre's friction circle
        # can't supply -- verified live: even with the steer-gain fix above,
        # the car still drifted off centre through here and got stuck at low
        # speed on the shoulder. The main driving path already has a
        # corner-aware cap for exactly this (_track_model.limit_kmh, built
        # from the track's real geometry -- same source ATTACK/NORMAL use,
        # see the map-corner branch in compute_control above); pit docking
        # never consulted it because _pit_control used to only run at creep
        # speed, where corner grip was never in question. Now that the
        # lead-in covers real corners at real speed, take whichever target
        # is lower, same as the main path does.
        # 2026-08-10: the map's limit_kmh is calibrated for the RACING LINE
        # (_track_model.line_tpos hugs the apex, widening the effective
        # corner radius -- see the map-corner branch in compute_control
        # above) -- verified live it still let the car do 145-148 km/h
        # through curve 25 (190.5 m radius) while braking hard, pinned to
        # track_pos=0 the whole time. A car glued to centre never gets that
        # apex-widened radius, so the same limit is genuinely too generous
        # for it. Derate it for the lead-in specifically -- PIT never takes
        # the racing line here, so it should never get the racing-line
        # speed either.
        dist_from_start = state.get("dist_from_start", -1.0)
        if _track_model is not None and dist_from_start >= 0.0:
            map_limit = _track_model.limit_kmh(dist_from_start) * _PIT_LEADIN_MAP_MARGIN
            target_speed = min(target_speed, map_limit)
    elif not released:
        # 2026-08-10: restore an alignment-gated creep floor instead of an
        # unconditional 0. The unconditional-0 version existed because the
        # OLD lateral-convergence law never actually converged (wrong
        # pit-side sign, unbounded aim_angle, and the ease starting 150 m
        # before the road had any extra width to swing into -- all fixed
        # above), so gating the drop to 0 on `aligned` back then just meant
        # a car that could never reach box_tpos crept through the whole
        # [s_start, s_end] zone at 8 km/h and sailed out uncaptured every
        # time. Now that the lateral law actually converges, forcing 0 the
        # instant s_now crosses s_start cuts that convergence off early:
        # verified live on forza, the 58.5 m [pit_entry, pit_start] gap
        # isn't enough room to both shed speed AND swing tpos onto box_tpos
        # while capped at _PIT_MAX_CRAB_ANGLE, so the car braked to a dead
        # stop well short of the box, still laterally uncommitted, with no
        # speed left to keep converging. Hold the creep floor until actually
        # aligned, using the rest of [s_start, s_end] (there's ~130 m of it
        # on forza) to finish the swing; past s_end, still target 0
        # regardless of alignment -- same overshoot protection the
        # unconditional version had, just scoped to when the room to
        # converge has actually run out instead of from the first metre.
        # Gated on LATERAL alignment only (tpos, not the full `aligned`
        # which also checks angle) -- once the car has actually reached
        # target_tpos there's no more lateral distance left to buy with a
        # creep floor, so brake hard regardless of residual heading error;
        # it's only *before* reaching target_tpos that continuing to creep
        # instead of stopping dead is the point of this whole change.
        tpos_ok = abs(tpos - target_tpos) < _PIT_ALIGN_TPOS
        target_speed = 0.0 if (tpos_ok or s_now > s_end) else _PIT_CREEP_KMH
    else:
        target_speed = limit_kmh

    # 2026-08-10 (bt-parity concept, driver.cpp getTargetPoint/PIT_LOOKAHEAD):
    # bt never steers off the instantaneous position error -- it aims at a
    # point _PIT_LOOKAHEAD_M ahead on the pit spline and steers toward THAT.
    # bt can't be ported literally (it reads tTrackSeg/Spline objects our
    # SCR client never receives), so this rebuilds the same idea from what we
    # do have: track_pos, angle, and _pit_target_tpos itself.
    #
    # The previous law (angle - (tpos-target)*0.5) fought itself: `angle`
    # pulled toward 0 (straight relative to the track) while the tpos term
    # pulled toward the offset target -- but reaching a lateral offset
    # *requires* a nonzero angle, so the two terms partially cancelled.
    # Verified live: net steer stayed under 0.05 while perr grew from -0.61
    # to -0.74 over ~2000 steps, creeping forward without ever converging.
    #
    # Aiming at a lookahead point removes the conflict: compute the target
    # offset _PIT_LOOKAHEAD_M further down the pit spline, convert the
    # resulting lateral gap to metres (via seg_width, same frame box_tpos
    # above is already converted into), and steer to close the heading gap
    # between that aim direction and the car's actual heading. Once the car
    # is already pointed correctly to converge, this naturally settles near
    # 0 instead of being cancelled by a competing "hold angle 0" objective.
    # 0.0, not s_lead: see the matching comment on target_tpos above -- the
    # lateral ease must not anticipate road width the track doesn't have yet.
    target_tpos_ahead = _pit_target_tpos(s_now + _PIT_LOOKAHEAD_M, 0.0, s_start,
                                          s_end, s_exit, box_tpos)
    # 2026-08-10: during the entry ease (s_now <= s_start) the schedule is
    # monotonic toward box_tpos, so the car's actual tpos should never lead
    # target_tpos_ahead -- if it does (e.g. after swerving hard around an
    # opponent while merging into the pit lane), the fixed 8 m lookahead
    # produces an aim_angle that, by coincidence, ends up close to the car's
    # already-steep heading, so (aim_angle - angle) collapses toward 0 and
    # the steering law stops correcting. Verified live on forza: the car
    # settled into a stable ~52 deg crab angle at 77 km/h with the schedule
    # 2.5 track_pos units behind it (pit_tpos_err +2.55) and coasted straight
    # into the pit-apron wall, never straightening out. Clamping the
    # lookahead target to the car's own current tpos (capped at box_tpos)
    # whenever it's already ahead of schedule makes the aim point "go
    # straight from here" instead of "keep swinging further", which drives
    # (aim_angle - angle) strongly positive again and pulls the heading back
    # down. Only applied during the entry ease -- the hold/exit phases decay
    # tpos back toward 0 on purpose, where "ahead of schedule" is the normal,
    # wanted state, not overshoot.
    if s_now <= s_start:
        if box_tpos >= 0.0:
            target_tpos_ahead = max(target_tpos_ahead, min(tpos, box_tpos))
        else:
            target_tpos_ahead = min(target_tpos_ahead, max(tpos, box_tpos))
    lateral_gap_tpos = target_tpos_ahead - tpos
    half_width = (seg_width / 2.0) if seg_width > 0.0 else 5.5   # 5.5 m: half of an 11 m track, same fallback scale as _PIT_EDGE_TPOS
    lateral_gap_m = lateral_gap_tpos * half_width
    aim_angle = clamp(math.atan2(lateral_gap_m, _PIT_LOOKAHEAD_M),
                       -_PIT_MAX_CRAB_ANGLE, _PIT_MAX_CRAB_ANGLE)
    # 2026-08-10: PIT's steer_gain (1.50, the highest of any strategy) was
    # tuned for the slow, precise final docking move -- the only regime
    # _pit_control used to run in before s_lead got extended to -150 m to
    # buy room to converge (see _pit_target_tpos's history above). That
    # extension put this same 1.50 gain in charge of the car at highway
    # speed too (still ~90-160 km/h through most of the lead-in, since
    # target_tpos is pinned at 0 there and there's nothing to converge onto
    # yet -- see the lateral-ease fix above). Verified live on forza: with
    # a rock-steady target_tpos=0.00 the whole lead-in, the car still
    # oscillated -- tpos swinging 0 -> +0.67 -> -1.16 while angle swung
    # -0.19 -> +1.11 rad, growing each cycle until it crashed, never
    # touching the target. That is a classic overgained feedback loop, not
    # a target-tracking bug: the same gain that is precise at an 8 km/h
    # creep is twitchy enough at 90+ km/h to overshoot and resonate. Use
    # NORMAL's gain (0.85, the same value already trusted to hold a
    # straight line at these speeds) for the lead-in, where the job really
    # is just "hold centre and brake" -- only switch to PIT's higher gain
    # once inside [pit_start, pit_exit], where speed has already bled down
    # to creep pace and the tighter precision is both needed and safe.
    steer_gain = _PARAMS[NORMAL].steer_gain if s_now <= 0.0 else _PARAMS[PIT].steer_gain
    # 2026-08-10: yaw-rate damping. Verified live, at NORMAL's own gain, on
    # a straight (no corner, no speed issue -- s_now already inside the
    # lead-in's clear final stretch): the car still drifted smoothly off a
    # constant target_tpos=0.00, tpos and angle growing together in
    # lockstep from tpos=+0.16/angle=-0.03 out to a crash 30+ ticks later,
    # never correcting. That is not an aim-point or gain-magnitude problem
    # -- it is textbook underdamped overshoot: steering -> yaw rate ->
    # heading -> lateral position is a chain of integrators, and the only
    # damping this law had (speed_y * _STEER_DAMP) acts on lateral
    # velocity, not on how fast the heading itself is rotating. Once angle
    # started swinging through zero under the position correction alone,
    # nothing resisted the swing continuing past it, so it overshot,
    # triggered the opposite correction, overshot further, and so on.
    # Add a term directly proportional to the heading's own rate of
    # change (angle one tick ago vs now, over the fixed 50 Hz tick) so a
    # fast-rotating heading gets pulled up regardless of where target_tpos
    # or aim_angle currently sit -- the missing "D" in this loop's P
    # control.
    global _pit_prev_angle
    angle_rate = 0.0 if _pit_prev_angle is None else (angle - _pit_prev_angle) / _TICK_S
    _pit_prev_angle = angle
    steer = clamp((aim_angle - angle) * steer_gain
                   - speed_y * _STEER_DAMP - angle_rate * _PIT_YAW_DAMP,
                  -1.0, 1.0)

    excess = speed - target_speed
    if excess <= 0.3:
        accel = clamp((target_speed - speed) / _ACCEL_BAND, 0.0, 0.4)
        brake = 0.0
    else:
        accel = 0.0
        brake = clamp(excess / limit_kmh * _BRAKE_RESPONSE, 0.0, 1.0)
    brake = _apply_abs(brake, speed, wheels)
    accel = _apply_tcl(accel, speed, wheels)
    fwd_gear = 1 if speed < 30.0 else _gear_from_speed(max(gear, 1), speed)

    return format_scr_control(accel=accel, brake=brake, gear=fwd_gear, steer=steer,
                              pit_request=not released)


def compute_control(state: dict[str, Any], strategy: str = NORMAL) -> str:
    """Translate a strategy + live sensor state into a concrete SCR control string.

    Called every simulation step. Granite (Step 6) supplies the strategy;
    the safety layer (Step 5) may override it before calling this function.
    """
    params     = _PARAMS.get(strategy, _PARAMS[NORMAL])

    speed      = state.get("speed_x",      0.0)
    speed_y    = state.get("speed_y",      0.0) / 3.6   # SCR sends km/h → m/s for damping
    gear       = state.get("gear",           0)
    rpm        = state.get("rpm",          0.0)
    angle      = state.get("angle",        0.0)
    tpos       = state.get("track_pos",    0.0)
    track      = state.get("track",         [])
    wheel_vels = state.get("wheel_spin_vel", [])
    dist_from_start = state.get("dist_from_start", -1.0)   # -1 = not in packet
    dist_raced = state.get("dist_raced", 1e9)   # missing/huge = never "launching"
    fuel       = state.get("fuel",        50.0)   # litres; feeds _brake_dist's mass estimate
    _dbg["angle"] = angle
    _dbg["dist"] = dist_from_start

    global _stuck_frames, _bursting, _burst_frames, _recovering, _target_lp, _line_lp, _avoid_lp
    global _stuck_progress_dist, _stuck_progress_frames, _stabilizing, _stabilize_bled
    global _front_gap_prev, _close_rate_lp, _launch_clutch_timer, _standoff_timer
    global _side_gap_prev, _side_close_rate_lp
    global _pit_docking, _pit_serviced
    global _stabilize_stuck_dist, _stabilize_stuck_frames, _turnaround, _ta_fwd, _ta_jam, _ta_rev

    # --- stabilize latch: once entered (see the two triggers below), STAYS
    # active until the car is genuinely back under control — not just "moved
    # a bit".  First cut exited as soon as dist_from_start had crept forward
    # _NO_PROGRESS_DIST, but that distance is often just the stabilize creep
    # itself — handing back to the stuck-jam burst / wrong-way turnaround
    # that early let them throw the car right back into trouble, verified
    # live: mode bounced stabilize→turn-fwd→turn-rev→stabilize→... for 30+
    # seconds instead of actually recovering.  Reusing the SAME "genuinely
    # fine" hysteresis the plain off-track re-entry logic already uses
    # (_RECOVER_EXIT_TPOS/_ANGLE) to decide when it's actually done fixes
    # that — it stops handing back control on a technicality.
    if _stabilizing:
        if abs(tpos) <= _RECOVER_EXIT_TPOS and abs(angle) <= _RECOVER_EXIT_ANGLE:
            _stabilizing = False   # genuinely fine now — fall through to normal driving
        else:
            # Stabilize's own no-progress watchdog: _stabilize_action's
            # wrong-way branch always reverses, with no escape if reverse
            # itself is blocked (nose wedged into a wall after a spin) —
            # logged live: track_pos +1.09, angle +136°, sight 0.1 m,
            # accel=0.5/gear=-1 every tick for minutes without moving. The
            # plain _stuck_progress_dist/_frames watchdog above can't catch
            # this — it only runs BEFORE this `if _stabilizing:` gate, so
            # once latched it never executes again. Same _NO_PROGRESS_DIST/
            # _FRAMES tuning, but scoped to this latch; escalates to
            # _recovery_control's three-point-turn (_turnaround), which DOES
            # alternate reverse and forward legs, instead of repeating a
            # reverse that provably isn't working.
            if (_stabilize_stuck_dist is None
                    or abs(dist_from_start - _stabilize_stuck_dist) >= _NO_PROGRESS_DIST):
                _stabilize_stuck_dist, _stabilize_stuck_frames = dist_from_start, 0
            else:
                _stabilize_stuck_frames += 1
                if _stabilize_stuck_frames >= _NO_PROGRESS_FRAMES:
                    _stabilizing = False
                    _stabilize_stuck_dist, _stabilize_stuck_frames = None, 0
                    _turnaround = True
                    _ta_fwd = _ta_jam = _ta_rev = 0
                    _dbg["mode"] = "stabilize-handoff"
                    return _recovery_control(state)
            _dbg["mode"] = "stabilize"
            return _stabilize_action(speed, angle, tpos, gear, speed_y)

    # --- pit lane docking (bt parity — see pit.cpp) ---
    # 2026-08-09: MUST run before every crash-recovery gate below, not
    # after (where this used to sit) — actually reaching the assigned pit
    # box can require track_pos several widths past the normal +-1 range
    # (verified live: forza's pit apron sits ~19 m off centreline, track_pos
    # ~3.45 at 11 m base width; nothing wrong with that number, see
    # _pit_control's pit_box_offset comment). With this dispatch AFTER the
    # extreme-excursion (_EXTREME_TPOS=2.5) and off-track recovery gates,
    # every one of them fired first and yanked control away the moment the
    # car actually started heading for the box, mistaking the deliberate
    # excursion for a crash — the docking code never even got a chance to
    # run. Running it first means those gates simply never see the car
    # while it is intentionally out there; a genuine crash still gets
    # caught by the stabilize latch just above, which is checked before
    # this and takes priority regardless of what pit-docking was doing.
    #
    # Physical presence in the pit-lane distance range must NOT by itself
    # trigger docking: most tracks route the pit lane alongside a section of
    # the main straight, so a normally-racing car passes through this same
    # distFromStart range every lap. Only strategy == PIT (or an
    # already-committed visit, see _pit_docking below) engages it — matches
    # bt's own getPitOffset()/filterBPit() gating on getPitstop(), not just
    # isBetween() alone (pit.cpp).
    pit_entry = state.get("pit_entry", -1.0)
    if pit_entry >= 0.0:
        track_len = state.get("track_length", -1.0)
        raw_s   = _pit_spline_coord(dist_from_start, pit_entry, track_len)
        # 2026-08-10: fold the last _PIT_APPROACH_DIST metres of the lap
        # (i.e. just BEFORE pit_entry) into negative "lead-in" coordinates,
        # so the same ease/steer math that already handles [pit_entry,
        # pit_exit] also covers the approach before entry. Verified live:
        # with only [pit_entry, pit_start] (58 m on Forza) to both shed
        # speed AND swing ~1.8 track_pos units (~8 m) onto the box offset,
        # the car ran out of room and stopped ~8 m off to the side, missing
        # the engine's lateral capture window entirely (pinps stuck at 0).
        # This extends that budget to _PIT_APPROACH_DIST + s_start (~200 m),
        # matching how far out safety_filter already commits to PIT (see
        # _near_pit_lane), so the lane change starts the moment the car
        # actually starts slowing down instead of waiting until it's
        # nearly out of room.
        # 2026-08-10: use _PIT_DOCK_LEAD_DIST here, NOT _PIT_APPROACH_DIST.
        # They used to be the same 150 m constant, but they answer different
        # questions: _PIT_APPROACH_DIST (see _near_pit_lane) is about when
        # the STRATEGY layer commits to PIT at all -- 150 m of early notice
        # is fine and safe, that's just a label. This is about when
        # _pit_control's own rigid, track_pos-pinned docking law takes over
        # from ordinary (curve-aware, pursuit-based) driving. Checked
        # forza's own geometry: the 150 m before pit_entry is mostly two
        # real corners (curve 25/26, radii 190.5 m / 410 m) with only the
        # last 30 m actually straight. Pinning track_pos to 0 through a real
        # corner demands lateral grip a centre-line car doesn't have the
        # apex-widened radius to spare, especially while also braking hard
        # for the pit -- verified live, even after capping speed via the
        # map (see target_speed below) the car still drifted off centre and
        # got stuck there every time. Ordinary driving already corners
        # curve 25/26 correctly (pursuit-based steering, not a rigid pin,
        # using the same map-based corner speed); handing off to the
        # specialised docking law only once inside _PIT_DOCK_LEAD_DIST of
        # pit_entry keeps it entirely on the straight and taper, where
        # holding track_pos steady was always the right idea.
        if track_len > 0.0 and raw_s > track_len - _PIT_DOCK_LEAD_DIST:
            s_now = raw_s - track_len   # negative: metres still to go before entry
        else:
            s_now = raw_s
        s_lead  = -_PIT_DOCK_LEAD_DIST
        # 2026-08-10: user-reported live/visual correction -- the pit box
        # row (pit_start..pit_end) as TORCS reports it sits _PIT_BOX_OFFSET_M
        # further down the track than where it actually renders. Pull both
        # markers back by that much before converting to local coordinates;
        # pit_entry and pit_exit are left alone (not reported as off).
        s_start = _pit_spline_coord(state.get("pit_start", pit_entry) - _PIT_BOX_OFFSET_M,
                                     pit_entry, track_len)
        s_end   = _pit_spline_coord(state.get("pit_end",   pit_entry) - _PIT_BOX_OFFSET_M,
                                     pit_entry, track_len)
        s_exit  = _pit_spline_coord(state.get("pit_exit",  pit_entry), pit_entry, track_len)
        in_lane      = s_now <= s_exit          # lead-in window through the pit lane proper
        in_pit_range = 0.0 <= s_now <= s_exit   # bt's isBetween(): raw [pit_entry, pit_exit] ONLY,
                                                 # excludes the lead-in (s_now < 0 there)
        # TEMP DEBUG (2026-08-10): set every frame pit geometry is known, not
        # just while the in_lane branch below is taken -- these used to only
        # update inside that branch, so the log froze at whatever they last
        # read (often just once, near the start of the race) instead of
        # showing live distance-to-pit, making "is the car ever actually
        # getting close" impossible to answer from the log alone.
        _dbg["pit_s"]      = s_now
        _dbg["pit_s0"]     = s_start
        _dbg["pit_s1"]     = s_end
        _dbg["pit_exit"]   = s_exit
        _dbg["pit_side"]   = state.get("pit_side", -1)
        _dbg["pit_inps"]   = state.get("in_pit_stop", 0)
        _dbg["pit_inlane"] = int(in_lane)
        _dbg["pit_inrange"] = int(in_pit_range)
        if _pit_docking:
            if in_lane:
                if state.get("in_pit_stop", 0):
                    _pit_serviced = True
                _dbg["mode"] = "pit"
                return _pit_control(state, s_now, s_lead, s_start, s_end, s_exit, _pit_serviced)
            else:
                _pit_docking = False
                _pit_serviced = False
        elif strategy == PIT and in_lane and not in_pit_range:
            # bt parity (pit.cpp Pit::setPitstop): a pit commitment can only
            # be newly ARMED while the car is outside the raw [pit_entry,
            # pit_exit] range -- bt's setPitstop() is a no-op if isBetween()
            # is already true when called. Verified live: a standing start
            # whose grid position happens to read as already past pit_start
            # (common -- the pit lane commonly runs alongside the front
            # straight where the grid also sits) got its target speed
            # latched to 0 on step 1, before the car ever moved, because the
            # old code armed docking from raw in-lane presence alone with no
            # regard for whether this was a fresh approach or just where the
            # car happened to already be. Restricting arming to the lead-in
            # (s_now < 0, i.e. genuinely approaching from outside) or to an
            # already-latched visit (handled above) closes that hole while
            # leaving the normal approach path (which always passes through
            # the lead-in first) completely unaffected.
            _pit_docking = True
            _pit_prev_angle = None
            if state.get("in_pit_stop", 0):
                _pit_serviced = True
            _dbg["mode"] = "pit"
            return _pit_control(state, s_now, s_lead, s_start, s_end, s_exit, _pit_serviced)

    # --- extreme excursion: stabilize before either recovery subsystem can
    # compete for control ---
    # A violent impact can fling the car many track-widths off (track_pos hit
    # +7 in one live incident) while ALSO leaving it nearly stationary — that
    # combination falls between the two recovery subsystems below and neither
    # one accounts for just how extreme the position is: the stuck-jam burst
    # only looks at (speed, front-sensor distance), the wrong-way turnaround
    # only looks at (angle, jam-speed).  Logged live: both fired independently
    # and alternated burst/turn-rev/turn-fwd for 30+ seconds while track_pos
    # kept climbing (+3 → +6) instead of coming back, because neither one is
    # even aware the other exists.  This check takes absolute priority over
    # both — checked before the burst logic, so it pre-empts it the moment the
    # position gets this extreme.
    if abs(tpos) > _EXTREME_TPOS:
        _stabilizing = True
        _stabilize_bled = False
        _stabilize_stuck_dist, _stabilize_stuck_frames = None, 0
        _dbg["mode"] = "stabilize"
        return _stabilize_action(speed, angle, tpos, gear, speed_y)

    # --- no-progress watchdog: track_pos alone can't tell "off to the side
    # but free to manoeuvre" apart from "wedged, going nowhere no matter which
    # way you point the wheels" — actual forward progress can.  Logged live: a
    # car wedged at track_pos ~2.3 (just under the 2.5 gate above) sat with
    # dist_from_start frozen while the stuck-jam burst and wrong-way
    # turnaround cycled between each other for 46+ real seconds, never
    # escaping.  So: whenever the previous tick wasn't plain racing, watch
    # dist_from_start — no real progress within _NO_PROGRESS_FRAMES escalates
    # into the stabilize latch above, regardless of track_pos.
    #
    # 2026-08-09: also exempt _pit_docking — this watchdog can't tell
    # "wedged, going nowhere" apart from "correctly stopped dead still in
    # the pit box, waiting for the service to finish" (see _pit_control,
    # which deliberately targets speed 0 and holds there while inPitStop is
    # set). Without this it decided a properly-executing pit stop was a
    # stuck car after 4 s, yanked control into stabilize, and fought the
    # pit-lane code for it every time _pit_docking tried to resume —
    # verified live: strategy=PIT, track_pos sitting at the pit box's own
    # offset (0.76, nowhere near the _EXTREME_TPOS=2.5 gate above), stuck
    # alternating stabilize/pit for thousands of ticks.
    if _dbg.get("mode") == "race" or _pit_docking or dist_from_start < 0.0:
        _stuck_progress_dist, _stuck_progress_frames = None, 0
    else:
        if (_stuck_progress_dist is None
                or abs(dist_from_start - _stuck_progress_dist) >= _NO_PROGRESS_DIST):
            _stuck_progress_dist, _stuck_progress_frames = dist_from_start, 0
        else:
            _stuck_progress_frames += 1
            if _stuck_progress_frames >= _NO_PROGRESS_FRAMES:
                _stabilizing = True
                _stabilize_bled = False
                _dbg["mode"] = "stabilize"
                return _stabilize_action(speed, angle, tpos, gear, speed_y)

    # --- stuck / crash recovery (works on OR off track, takes priority) ---
    # "jammed" = crawling AND something is right in front, or we're pinned at the
    # edge.  The front/edge gate is what prevents a false reverse on a clear
    # standing start or in the pit lane (slow, but open road ahead). Also
    # exempt _pit_docking outright (2026-08-09, see the no-progress watchdog
    # comment above) — a car nose up to the pit box boundary or another
    # parked car can read a short front distance while correctly stopped
    # for service, which would otherwise misread as jammed and reverse out
    # of the box mid-stop.
    front      = track[9] if len(track) > 9 else 200.0
    jam_signal = front < _STUCK_WALL or abs(tpos) > 0.9
    if _bursting:
        # bt-style: re-checked every tick, not run to a fixed count (see the
        # 2026-08-09 comment at _bursting above). Exit the instant the same
        # signal that triggered this is gone, so a car freed after 3 frames
        # doesn't keep reversing for 37 more doing nothing useful.
        _burst_frames += 1
        if _burst_frames >= _UNSTUCK_MIN_FRAMES and (
                not jam_signal or _burst_frames >= _UNSTUCK_MAX_FRAMES):
            _bursting = False   # freed (or safety cap) — fall through to normal driving THIS tick
        else:
            _dbg["mode"] = "burst"
            return format_scr_control(accel=0.5, brake=0.0, gear=-1,
                                      steer=_recovery_steer(angle, tpos))
    else:
        jammed_now = abs(speed) < _STUCK_SPEED and jam_signal and not _pit_docking
        if jammed_now:
            _stuck_frames += 1
        else:
            _stuck_frames = 0
        if _stuck_frames >= _STUCK_FRAMES:
            _stuck_frames = 0
            _bursting     = True
            _burst_frames = 0
            _dbg["mode"] = "burst"
            return format_scr_control(accel=0.5, brake=0.0, gear=-1,
                                      steer=_recovery_steer(angle, tpos))

    # --- recovery gate: off-track, wrong-way, or mid-manoeuvre ---
    # Hysteresis: recovery starts only when genuinely off (kerb-riding at the
    # apex stays with the racing controller) and hands control back once the
    # car is well inside the track AND roughly aligned — otherwise the normal
    # controller grabbed a car still crossing the edge at an angle and fired it
    # straight across to the opposite side.  The |angle| > 90° check is the
    # wrong-way detector: it also catches a car spun around INSIDE the track,
    # which previously fell through here and drove off backwards.
    if abs(tpos) > _RECOVER_ENTER_TPOS:
        _recovering = True
    elif abs(tpos) < _RECOVER_EXIT_TPOS and abs(angle) < _RECOVER_EXIT_ANGLE:
        _recovering = False
    if _recovering or _turnaround or abs(angle) > _WRONG_WAY:
        return _recovery_control(state)

    # --- gear (RPM-first, speed table as fallback) ---
    raw_gear = gear   # sim-reported gear, BEFORE our own always-commands-1st
                       # override below — the launch clutch ramp needs to know
                       # when the SIM actually connects 1st, not when we asked
    gear = _gear_shift(gear, rpm, speed)

    # --- steering: pure pursuit + heading alignment + edge barrier ---
    #   pursuit : aim at the direction the track extends furthest (geometry, so
    #             it follows the road and self-centres softly, without snaking)
    #   align   : angle·steer_gain — damps the lateral loop; see the comment at
    #             _PP_GAIN for why this term (and only this term) stops the
    #             pendulum weave down straights
    #   barrier : no centring in the middle band; only push back near the edge,
    #             so the car is free to use the track width (racing line)
    #   damping : small counter to a sideways slide (speed_y)
    opps      = state.get("opponents", [])
    left_gap  = min((opps[i] for i in _AVOID_LEFT  if i < len(opps)), default=200.0)
    right_gap = min((opps[i] for i in _AVOID_RIGHT if i < len(opps)), default=200.0)
    pursuit = _pursuit_target(track)
    if pursuit is None:
        # Nominally on track yet no usable beams — sensor glitch.  Fall back to
        # the angle/centre controller at a modest pace rather than flooring it
        # blind with steer 0.
        #
        # 2026-08-06: this branch used to skip side-avoidance entirely — logged
        # live: a car alongside (lgap=6.9 m, well inside _AVOID_DIST) hit this
        # exact branch for 5 straight ticks right before contact (tpos frozen
        # at -1.00, lgap frozen at 6.9 the whole time — neither escaping nor
        # closing further, just stuck). Every avoidance fix so far
        # (_AVOID_GAIN, _AVOID_FADE_FLOOR) lives in the normal branch below and
        # none of them ran during those 5 ticks. No fade multiplier here —
        # fade needs `pursuit`, which is exactly what's missing — a car flying
        # blind on sensors still shouldn't be defenceless against a car
        # alongside.
        avoid_blind = 0.0
        if left_gap < _AVOID_DIST:
            avoid_blind -= _AVOID_GAIN * (1.0 - left_gap / _AVOID_DIST)
        if right_gap < _AVOID_DIST:
            avoid_blind += _AVOID_GAIN * (1.0 - right_gap / _AVOID_DIST)
        steer = clamp(angle - clamp(tpos, -2.0, 2.0) * 0.5 - speed_y * _STEER_DAMP + avoid_blind,
                      -1.0, 1.0)
        accel = 0.4 if speed < 60.0 else 0.0
        brake = 0.3 if speed > 80.0 else 0.0
        _dbg["mode"] = "blind"
        return format_scr_control(accel=accel, brake=brake, gear=gear, steer=steer)

    edge    = max(0.0, abs(tpos) - _EDGE_FREE)
    barrier = -math.copysign(edge * _EDGE_GAIN, tpos)
    aim     = math.copysign(max(0.0, abs(pursuit) - _PP_FREE), pursuit)
    front_gap = min((opps[i] for i in _FRONT_CONE  if i < len(opps)), default=200.0)
    # Closing-rate estimate feeding the overtake trigger below (see the
    # 2026-08-08 comment at _OVERTAKE_CLOSE_RATE_MIN): d(front_gap)/dt is the
    # relative closing speed, computed for free from last tick's front_gap.
    # Guarded to 0 unless BOTH ticks actually saw a car (<200 m) so a car
    # freshly entering the cone doesn't read as an instant teleport-speed
    # closure, and EMA-smoothed since a single-tick reading is noisy.
    raw_close_rate = 0.0
    close_rate_known = False   # was this tick's rate an actual measurement,
                                # not a guess? feeds the front-collision brake
                                # check below (see _dbg["close_rate_known"])
    if _front_gap_prev is not None and _front_gap_prev < 200.0 and front_gap < 200.0:
        candidate = (_front_gap_prev - front_gap) / _TICK_S
        if abs(candidate) <= _CLOSE_RATE_SANITY_MAX:
            raw_close_rate = candidate
            close_rate_known = True
        # else: a cone-boundary jump (see _CLOSE_RATE_SANITY_MAX) — leave
        # raw_close_rate at 0 so the EMA decays toward "unknown" instead of
        # absorbing the spike.
    _close_rate_lp += _CLOSE_RATE_ALPHA * (raw_close_rate - _close_rate_lp)
    _front_gap_prev = front_gap
    _dbg["close_rate"] = _close_rate_lp
    _dbg["close_rate_known"] = close_rate_known
    # A+ racing line: hold-line setpoint.  0 (centre) on open road, but on
    # the approach to a mapped corner the map moves it to the OUTSIDE edge
    # (out-in-out entry).  Same fade as before: the term only acts while the
    # pursuit aim is quiet, so it positions the car on straights/braking
    # zones and never wrestles pursuit for the wheel mid-corner.
    line_raw = 0.0
    if _track_model is not None and dist_from_start >= 0.0:
        entry_zone = _LINE_ENTRY_ZONE_BASE + max(0.0, speed) * _LINE_ENTRY_ZONE_SPEED_K
        line_raw = _track_model.line_tpos(dist_from_start, entry_zone=entry_zone)
    # Overtake bias: a slower car dead ahead only counts as "found" once we
    # are close enough that neither side reads open by coincidence; pick
    # whichever side is clearly roomier and ease that way.  Only overrides an
    # already-quiet map line (|line_raw| < 0.05) — a mapped corner's own
    # entry bias carries more information about the road than this guess and
    # must not be fought mid-corner.
    if (line_raw == 0.0 and front_gap < _OVERTAKE_TRIGGER_M
            and _close_rate_lp > _OVERTAKE_CLOSE_RATE_MIN):
        if left_gap > right_gap + _OVERTAKE_ROOM_MARGIN:
            line_raw = _OVERTAKE_BIAS    # more room on the left → tpos positive
        elif right_gap > left_gap + _OVERTAKE_ROOM_MARGIN:
            line_raw = -_OVERTAKE_BIAS   # more room on the right → tpos negative
        elif _track_model is not None and dist_from_start >= 0.0:
            # 2026-08-08: borrowed from TORCS's built-in "bt" robot
            # (src/drivers/bt/driver.cpp Driver::getOffset()) — when the car
            # ahead is dead centre (neither side reads clearly roomier), bt
            # doesn't just sit neutral: it commits to whichever side is the
            # INSIDE of the next corner, since that's the side the racing
            # line wants anyway and a slower car dead centre gives no signal
            # either way.  We have the same map data bt gets from the track
            # geometry (next_corner), so this costs nothing new to compute —
            # it only fires when the room comparison above is a genuine tie,
            # so it can never fight a real "one side is tighter" reading.
            nc = _track_model.next_corner(dist_from_start, horizon=400.0)
            if nc is not None:
                line_raw = _OVERTAKE_BIAS if nc["dir"] == "left" else -_OVERTAKE_BIAS
    # BLOCK: only reached once safety_filter has already decided a car is
    # closing in fast from behind (see _BLOCK_TRIGGER_GAP there). Ease TOWARD
    # whichever side they're on — opposite direction from the overtake bias
    # above — so they have to go further round to complete a pass. Only fills
    # in when the map/overtake bias above are both neutral (never fights a
    # mapped corner entry or an overtake already in progress).
    if line_raw == 0.0 and strategy == BLOCK:
        if left_gap < right_gap - _BLOCK_ROOM_MARGIN:
            line_raw = _BLOCK_GAIN       # threat on the left → ease left to block
        elif right_gap < left_gap - _BLOCK_ROOM_MARGIN:
            line_raw = -_BLOCK_GAIN      # threat on the right → ease right to block
    # Slew toward the raw setpoint — side flips between alternating corners
    # become a ~1 s drift instead of a dart (the twisty-section weave fix).
    _line_lp += clamp(line_raw - _line_lp, -_LINE_SLEW, _LINE_SLEW)
    hold_gain = _LINE_GAIN if abs(_line_lp) > 0.05 else _HOLD_CENTRE
    fade    = max(0.0, 1.0 - abs(pursuit) / _PP_FREE)
    centre  = clamp((_line_lp - tpos) * hold_gain, -0.25, 0.25) * fade
    launching  = dist_raced < _START_CAUTION_DIST
    avoid_dist = _START_AVOID_DIST if launching else _AVOID_DIST
    avoid_gain = _START_AVOID_GAIN if launching else _AVOID_GAIN
    avoid = 0.0
    if left_gap < avoid_dist:
        avoid -= avoid_gain * (1.0 - left_gap / avoid_dist)
    if right_gap < avoid_dist:
        avoid += avoid_gain * (1.0 - right_gap / avoid_dist)
    # Convergence gate (see _SIDE_CLOSE_RATE_MIN above): scale avoid by
    # whether the binding side gap is actually shrinking, not just close.
    # With the floor at 0.0 this is now a real gate, not a softener, so it
    # must not fire on a guess: a rate reading only counts once we have an
    # actual prior sample to diff against AND it passes the sanity check.
    # Without either (opponent just entered the cone this tick, or a
    # cone-boundary jump made the reading meaningless) we don't know yet
    # whether it's converging — default to full authority rather than
    # silently assuming "stable", which is exactly the assumption that let
    # the car creep to the edge against a neighbour never actually confirmed
    # as non-converging.
    if avoid != 0.0:
        side_gap_now = min(left_gap, right_gap)
        rate_known = False
        if (_side_gap_prev is not None and _side_gap_prev < 200.0
                and side_gap_now < 200.0):
            candidate = (_side_gap_prev - side_gap_now) / _TICK_S
            if abs(candidate) <= _CLOSE_RATE_SANITY_MAX:
                _side_close_rate_lp += _CLOSE_RATE_ALPHA * (candidate - _side_close_rate_lp)
                rate_known = True
        _side_gap_prev = side_gap_now
        if rate_known:
            converge = _AVOID_CONVERGE_FLOOR + (1.0 - _AVOID_CONVERGE_FLOOR) * clamp(
                _side_close_rate_lp / _SIDE_CLOSE_RATE_MIN, 0.0, 1.0)
            avoid *= converge
    else:
        _side_gap_prev = min(left_gap, right_gap)
        if _side_gap_prev >= 200.0:
            _side_gap_prev = None
    # 2026-08-08: bt-inspired room taper. bt bounds its own side-avoidance
    # offset to roughly the middle third of the track width (driver.cpp,
    # Driver::filterSColl's myoffset clamp to ±trackwidth/WIDTHDIV) — it
    # structurally cannot push a car anywhere near the edge from avoidance
    # alone. We don't have an explicit position target to clamp (avoid is a
    # steering delta, not an offset), so this approximates the same idea:
    # once the car is already past _EDGE_FREE in the direction avoid itself
    # is pushing, taper avoid's authority toward zero over the remaining
    # margin. Verified live: a car sitting at a stable ~6-9 m side gap for
    # an extended stretch (never closing, never opening) let avoid and
    # barrier settle into a near-equilibrium AT the edge (tpos crept to
    # -0.30 -> -0.97 over ~150 ticks and stayed there) instead of either
    # resolving — avoid had no notion that it had already won all the room
    # it should get and kept pushing at ~constant strength while barrier
    # (deliberately gentle, see _EDGE_GAIN comment) tried to hold the line.
    # Only fires once genuinely near the edge IN AVOID'S OWN PUSH DIRECTION,
    # so it can't weaken a legitimate escape from a car still mid-track.
    if avoid != 0.0:
        edge_in_push_dir = tpos if avoid > 0.0 else -tpos
        room_taper = 1.0 - clamp(
            (edge_in_push_dir - _EDGE_FREE) / (1.0 - _EDGE_FREE), 0.0, 1.0)
        avoid *= room_taper
    # centre defers fully to cornering (fade); avoid never fully switches
    # off for THAT reason — collision risk doesn't stop mattering mid-corner
    # (see _AVOID_FADE_FLOOR above for the live incident this covers). The
    # room taper above is a separate, position-based reason it CAN fade —
    # once already at the edge it's pushing toward, continuing to push is
    # what caused the sustained rub, not a cornering deference question.
    avoid  *= max(fade, _AVOID_FADE_FLOOR)
    # Slew toward the raw value — a same-tick left/right flip becomes a fast
    # correction instead of an instant full-reversal dart (see _AVOID_SLEW).
    _avoid_lp += clamp(avoid - _avoid_lp, -_AVOID_SLEW, _AVOID_SLEW)
    avoid = _avoid_lp
    steer   = aim * _PP_GAIN + centre + barrier + avoid - speed_y * _STEER_DAMP
    steer  /= (1.0 + max(speed, 0.0) * _STEER_SPEED_K)
    # The alignment damper is deliberately OUTSIDE the speed attenuation: the
    # lateral loop loses damping as speed rises (ζ ~ 1/√v) — that is exactly
    # why the weave only ever appeared at high speed — so the one term that
    # damps it must not be softened with speed like the path-following terms.
    steer  += angle * params.steer_gain
    if abs(steer) < _STEER_DEADZONE:
        steer = 0.0
    steer = clamp(steer, -1.0, 1.0)

    # --- corner speed limit: sight distance + sharpness ---
    # Sight: the LONGEST of the ±10° beams.  The old ±5° median collapsed under
    # a tiny heading misalignment on a dead straight — the 0° beam grazes the
    # edge of the straight and reads ~100 m — which silently capped the car
    # ~100 km/h under its potential (the "never full throttle" bug).  A near-
    # forward beam aligned with the road keeps sight honest, while a real
    # corner still shortens every beam in the window.
    # Sharpness: how far off straight-ahead the open road is (|pursuit|), minus
    # the free band that is just the off-centre pull.  A 90° corner has a big
    # angle and must be taken far slower than a gentle bend with the same sight.
    fwd        = [track[i] for i in range(7, 12) if i < len(track)]
    sight      = max([d for d in fwd if d > 0.0], default=100.0)
    open_angle = abs(pursuit)
    if sight >= _STRAIGHT_CLEAR and open_angle < _STRAIGHT_ANGLE:
        # Clear AND straight ahead — run to the strategy's top speed.
        target_speed = params.max_speed
        _dbg["why"]  = "clear"
    else:
        sharp        = max(0.0, open_angle - _SHARP_FREE)
        floor        = params.max_speed * _CORNER_FLOOR
        dist_limit   = math.sqrt(floor * floor + max(sight, 1.0) * params.speed_factor)
        target_speed = min(params.max_speed,
                           dist_limit / (1.0 + _CORNER_SHARPNESS * sharp))
        _dbg["why"]  = "corner-sight"

    # --- side-avoidance throttle ease: pair the steer nudge with real separation ---
    if not launching:
        side_gap = min(left_gap, right_gap)
        if side_gap < _AVOID_DIST:
            _standoff_timer += _TICK_S
            if _standoff_timer >= _STANDOFF_TIME:
                # Standoff breaker (see _STANDOFF_TIME above): sustained
                # closeness, not just momentary — commit to a decisive yield.
                ease_gain = _STANDOFF_EASE_GAIN
                _dbg["why"] = "standoff-yield"
            else:
                ease_gain = _SIDE_EASE_GAIN
                _dbg["why"] = "side-close"
            target_speed *= 1.0 - ease_gain * (1.0 - side_gap / _AVOID_DIST)
        else:
            _standoff_timer = 0.0

    # --- front-opponent follow cap: brake ONLY when genuinely boxed in ---
    # A tight gap ahead is not by itself a reason to brake — if either side is
    # clear, easing aside (the overtake-line bias above) is strictly better
    # than shedding speed, so this only binds when there is nowhere to go.
    _dbg["ogap"]      = front_gap
    _dbg["lgap"]      = left_gap
    _dbg["rgap"]      = right_gap
    _dbg["opp_bound"] = 0.0
    boxed_in = left_gap < _FRONT_ESCAPE_M and right_gap < _FRONT_ESCAPE_M
    if front_gap < _FRONT_BRAKE_M and boxed_in:
        follow_cap = math.sqrt(_FRONT_FLOOR_KMH * _FRONT_FLOOR_KMH
                               + max(front_gap, 0.0) * _FRONT_FACTOR)
        if follow_cap < target_speed:
            target_speed = follow_cap
            _dbg["opp_bound"] = 1.0
            _dbg["why"] = "opp-boxed"

    # --- pre-race map lookahead: brake for corners the sensors can't see ---
    # min() with the reactive target, never a replacement: the map knows the
    # geometry but not traffic, damage, or whether we're even on the line.
    # map_bound feeds the per-lap "% of frames the map governed" statistic —
    # the calibration gauge: near 0% = pure backstop, high = map is taxing.
    _dbg["map_bound"] = 0.0
    _dbg["trust"]     = 0.0
    map_curve = False           # target comes from the map's braking curve
    if _track_model is not None and dist_from_start >= 0.0:
        map_limit = _track_model.limit_kmh(dist_from_start)
        _dbg["map"] = map_limit
        if map_limit < target_speed:
            _dbg["map_bound"] = 1.0
            target_speed = map_limit
            map_curve    = map_limit < params.max_speed - 1.0
            _dbg["why"]  = "map-corner"
        # --- TRUST mode: all five gates (see constants above) → the map may
        # RAISE the target: cancels false straight-line lifts, brakes later.
        elif (_track_model.real_lap is not None                    # gate 1
                and map_limit >= _TRUST_MIN_KMH                    # gate 5
                and abs(tpos) <= _TRUST_TPOS
                and abs(angle) <= _TRUST_ANGLE):                   # gate 2
            opps      = state.get("opponents", [])
            front_opp = min(opps[16:20]) if len(opps) >= 20 else 200.0
            nc        = _track_model.next_corner(dist_from_start)
            nc_dist   = nc["dist_m"] if nc else 800.0
            if (front_opp > _TRUST_OPP_CLEAR                       # gate 3
                    and sight >= min(nc_dist, 200.0) * _TRUST_SIGHT_FRAC):  # gate 4
                trusted = min(map_limit * _TRUST_MARGIN, params.max_speed)
                if trusted > target_speed:
                    target_speed  = trusted
                    _dbg["trust"] = 1.0
                    map_curve     = trusted < params.max_speed - 1.0
                    _dbg["why"]   = "map-trust"

    # Smooth the target: drops are instant (braking must never lag), rises are
    # rate-limited so a flickering straight/corner classification can't strobe
    # the pedals.
    if _target_lp is None or target_speed < _target_lp:
        _target_lp = target_speed
    else:
        if _target_lp + _TARGET_RISE < target_speed:
            _dbg["why"] = "rise-limited"   # raw target is higher; still climbing to it
        _target_lp = min(target_speed, _target_lp + _TARGET_RISE)
    target_speed = _target_lp
    _dbg.update(sight=sight, open_angle=open_angle, target=target_speed, mode="race")

    # --- accel / brake ---
    # Proportional throttle: full when well below the target, easing off inside
    # _ACCEL_BAND so cruising at the cap is a steady partial throttle — the old
    # bang-bang (full under / zero over) tapped the pedal all the way down a
    # straight once the car reached its top speed.
    # Brake only past a small overspeed deadband, so a slightly twitchy target
    # doesn't tap the brake on a clear straight and bleed off speed.
    if speed <= target_speed * (1.0 + _BRAKE_DEADBAND):
        if map_curve:
            # Brake-point mode: the map curve is a BRAKE trigger, not a
            # cruise setpoint — power to it flat out (racing-driver style;
            # the easing band made the car lift 180 m early and coast).
            # The small neutral gap below the curve prevents a sawtooth.
            accel = params.accel_limit if speed < target_speed * _MAP_THROTTLE_BAND else 0.0
        else:
            accel = params.accel_limit * clamp((target_speed - speed) / _ACCEL_BAND, 0.0, 1.0)
        brake = 0.0
    else:
        excess = (speed - target_speed) / max(target_speed, 1.0)
        accel  = 0.0
        brake  = clamp((excess - _BRAKE_DEADBAND) * params.brake_gain
                       * (_MAP_BRAKE_RESPONSE if map_curve else _BRAKE_RESPONSE),
                       0.0, 1.0)
        # Straight-line braking is strong; release it as the wheel turns so a
        # mid-corner target drop can't snap the rear loose (trail-brake guard).
        brake *= 1.0 - _BRAKE_STEER_CUT * min(abs(steer), 1.0)

    # Physics stopping-distance check (bt-inspired — see _brake_dist above).
    # _brake_dist(speed, target_speed, ...) is 0.0 whenever speed<=target, so
    # this can only ever matter in the "already braking" branch above — it
    # does NOT make braking start earlier (that's still entirely governed by
    # when target_speed itself drops below speed). What it catches: the
    # plain proportional gain (_BRAKE_RESPONSE=3.0) is known to be too gentle
    # for a genuine emergency stop — the _MAP_BRAKE_RESPONSE=5.0 branch above
    # exists for exactly this reason, but only when a trusted map curve is
    # available. This extends the same idea to plain sensor-sighted corners:
    # if the room actually available (sight) is less than the physically
    # exact distance needed to reach target_speed, the proportional gain's
    # output is upgraded to full brake regardless of how gentle the excess-
    # speed ratio alone would have made it. Only ever ADDS braking (max()),
    # never removes what the reactive block above already decided, and only
    # applies to genuine sensor-sighted corners (not the open straight, not
    # already covered by the map's own dedicated brake-point mode above).
    if not map_curve and sight < _STRAIGHT_CLEAR:
        needed = _brake_dist(speed, target_speed, _CAR_MASS_BASE + fuel)
        if needed >= sight:
            accel = 0.0
            brake = max(brake, 1.0)

    # 2026-08-09: front-collision hard brake, ported from bt's filterBColl
    # (driver.cpp) — same brakedist() physics as the sight-based check just
    # above, but checked against the car directly ahead (front_gap) instead
    # of track geometry: if the distance needed to shed speed down to THEIR
    # speed exceeds the real gap, brake now, regardless of whether either
    # side has room to swerve. The follow_cap above only reacts once BOTH
    # sides are blocked (_FRONT_ESCAPE_M) and is a smoothed target-speed cap,
    # not a hard instantaneous check — bt runs this unconditionally on any
    # laterally-aligned car ahead, the same division of labor as its
    # getOffset() (steer around) vs filterBColl (stop in time regardless).
    # SCR gives no opponent speed (bt reads it from full sim state); this
    # estimates it from the closing-rate signal computed above: opponent
    # speed ~= our speed - closing rate. Gated on close_rate_known — a car
    # that just appeared this tick has no rate history yet, and treating
    # "unknown" as "not closing" would silently skip the check on exactly
    # the case (a sudden close encounter) it exists to catch.
    if front_gap < _FRONT_BRAKE_M and _dbg.get("close_rate_known", False):
        opp_speed_est = max(0.0, speed - _close_rate_lp * 3.6)
        needed = _brake_dist(speed, opp_speed_est, _CAR_MASS_BASE + fuel)
        if needed >= front_gap:
            accel = 0.0
            brake = 1.0
            _dbg["why"] = "front-coll"

    # Start-of-race caution (see the _START_CAUTION_DIST comment): no throttle
    # cap any more (matches bt — see _START_ACCEL_CAP history), but the
    # launch clutch ramp below still applies during this window.
    if launching:
        accel = min(accel, _START_ACCEL_CAP)

    # Launch clutch ramp (see _CLUTCH_RAMP_TIME above): only while the launch
    # window is open AND the SIM has actually connected 1st gear (raw_gear,
    # not our own always-commands-1st override) does the timer run; anywhere
    # else clutch stays 0.0 (fully engaged), unchanged from before this.
    if launching and raw_gear == 1:
        _launch_clutch_timer += _TICK_S
        time_ceiling = clamp(1.0 - _launch_clutch_timer / _CLUTCH_RAMP_TIME, 0.0, 1.0)
        clutch = _launch_clutch(time_ceiling, speed, wheel_vels)
    else:
        _launch_clutch_timer = 0.0
        clutch = 0.0

    # ABS: prevent wheel lock-up under braking (snakeoil.py)
    brake = _apply_abs(brake, speed, wheel_vels)
    # Track-hold: cut throttle before running wide off the edge (bt-inspired,
    # see _apply_track_hold above) — checked before TCL, same order as bt's
    # filterTCL(filterTrk(...)) chain.
    accel = _apply_track_hold(accel, speed, tpos)
    # TCL: prevent rear-wheel spin on acceleration (snakeoil.py)
    accel = _apply_tcl(accel, speed, wheel_vels)

    return format_scr_control(accel=accel, brake=brake, gear=gear, steer=steer,
                              clutch=clutch)


# ---------------------------------------------------------------------------
# Step 5: Safety layer
# ---------------------------------------------------------------------------

# Thresholds — centralised here so they're easy to tune without touching logic.
_FUEL_PIT      = 10.0   # litres: force PIT regardless of Granite's choice
                         # TEMP (pit-system testing, 2026-08-09): raised from
                         # 5.0 so any fuel < 10 L schedules a pit stop
                         # immediately instead of waiting until nearly empty.
_FUEL_CAUTION  = 15.0   # litres: downgrade ATTACK → NORMAL (running low)
_DMG_NO_ATTACK = 8000   # damage points: disallow ATTACK (car degraded)
_DMG_DEFEND    = 9500   # damage points: force DEFEND even if Granite says NORMAL

# TEMP (pit-system testing, 2026-08-09): fuel caution no longer downgrades
# ATTACK → NORMAL. Combined with the proximity gate below, the car now
# commits to ATTACK everywhere except right at the pit lane. Flip back to
# True to restore the old "back off once fuel is merely low" behaviour.
_FUEL_CAUTION_ENABLED = False

# TEMP (pit-system testing, 2026-08-09): PIT is only committed to once this
# close to pit_entry — compute_control's own pit-lane docking (see
# _pit_control) only ever engages once the car has physically reached
# pit_entry anyway, so forcing PIT strategy a lap early just made the car
# crawl the whole lap at _PARAMS[PIT]'s 50 km/h cap for nothing. 150 m gives
# the car room to brake down from ATTACK's ~330 km/h target before reaching
# the pit-lane speed limit.
_PIT_APPROACH_DIST = 150.0   # metres

# 2026-08-10: how close to pit_entry _pit_control's own rigid docking law
# (track_pos pinned to 0, then eased onto box_tpos) is allowed to take over
# from ordinary pursuit-based driving. Deliberately NOT the same as
# _PIT_APPROACH_DIST above -- see the comment where this is used in
# compute_control for why forcing centre-line through real corners (as the
# old shared 150 m value did) doesn't work. 40 m clears forza's last real
# corner (curve 26 ends 30 m out) with a small margin, leaving the docking
# law entirely on straight/taper road, where holding track_pos steady is
# actually achievable.
_PIT_DOCK_LEAD_DIST = 40.0   # metres


def _near_pit_lane(state: dict[str, Any]) -> bool:
    """True once the car is within _PIT_APPROACH_DIST of crossing pit_entry,
    OR already inside the pit lane (entry..exit).

    2026-08-10 BUG FIX: the first version of this check computed "distance
    remaining until pit_entry" as (pit_entry - dist_from_start) % track_len.
    That reads near 0 while approaching, correctly gating PIT on — but the
    INSTANT the car crosses pit_entry, dist_from_start passes pit_entry and
    the same expression wraps all the way around to ~track_length (it starts
    measuring distance to *next lap's* entry instead). Verified live: strategy
    flipped PIT → NORMAL at the exact step the car crossed into the lane
    (pit_s went from ~5779 to ~38 on a 5850 m lap, strategy dropped in the
    same tick), which meant compute_control's docking dispatch (gated on
    `strategy == PIT` at the moment `in_lane` first becomes true) never even
    latched `_pit_docking` — the car sailed straight through. Reusing
    _pit_spline_coord (the same wrapped coordinate compute_control's own
    in_lane check uses) instead of a separate "distance to go" computation
    makes this agree with compute_control by construction: once inside
    [entry, exit] it's unconditionally "near" (s_now <= s_exit), and outside
    that it's "near" only within the approach window.
    """
    pit_entry       = state.get("pit_entry", -1.0)
    dist_from_start = state.get("dist_from_start", -1.0)
    track_len       = state.get("track_length", -1.0)
    if pit_entry < 0.0 or dist_from_start < 0.0 or track_len <= 0.0:
        return False
    s_now  = _pit_spline_coord(dist_from_start, pit_entry, track_len)
    s_exit = _pit_spline_coord(state.get("pit_exit", pit_entry), pit_entry, track_len)
    if s_now <= s_exit:
        return True   # already inside the lane (entry..exit)
    return (track_len - s_now) <= _PIT_APPROACH_DIST   # approaching from behind


def _rear_gap(opponents: list[float]) -> float:
    """Closest opponent distance in the rear cone (indices 0-3 + 32-35, the
    same 8 beams telemetry_common's compact_opponent_profile() uses for its
    rear_gap). Re-implemented locally so safety_filter works even in the
    _TELEMETRY_AVAILABLE=False fallback path, which has no rear_gap key."""
    if len(opponents) < 36:
        return 200.0
    rear = opponents[0:4] + opponents[32:36]
    return min(rear) if rear else 200.0


def safety_filter(strategy: str | None, state: dict[str, Any]) -> str:
    """Map a Granite-supplied strategy to a safe strategy using hard rules.

    Pure function — no I/O, no side effects.  Rules are checked in
    descending priority; the first match wins and short-circuits the rest.

    Args:
        strategy: Raw strategy name from Granite, or None on timeout/error.
        state:    Latest parsed SCR sensor dict from parse_scr_state().

    Returns:
        A strategy string guaranteed to be in _ALL_STRATEGIES.
    """
    fuel   = state.get("fuel",   50.0)
    damage = state.get("damage",  0.0)

    # Priority 1 — unknown / timed-out / Granite-forbidden strategy → safe
    # default.  Checked against _GRANITE_STRATEGIES, not _ALL_STRATEGIES —
    # BLOCK is system-only (see its definition) and must never be honoured
    # just because Granite happened to say the word.
    if strategy not in _GRANITE_STRATEGIES:
        return NORMAL

    # Priority 2 — almost out of fuel → pit, but only once actually close
    # enough to commit (_PIT_APPROACH_DIST) — see its comment above for why.
    # Two independent fuel-critical triggers, OR'd — this only ever fires
    # EARLIER than the old flat floor alone, never later (no regression):
    #   (a) absolute floor, unchanged from before;
    #   (b) bt's dynamic check (SimpleStrategy::needPitstop, strategy.cpp):
    #       not enough fuel for a 1.5-lap margin AND not enough to finish
    #       the race at the measured (or, before lap 1 completes,
    #       track-length-estimated) burn rate.  cmpfuel/laps_left come from
    #       the fuel-per-lap model updated once per tick in run_bot() —
    #       0.0/-1 here means "no data yet", which disables (b) and leaves
    #       (a) as the only guard, same as before this feature existed.
    fuel_critical = fuel < _FUEL_PIT
    if not fuel_critical:
        cmpfuel   = state.get("fuel_per_lap", 0.0) or state.get("expected_fuel_per_lap", 0.0)
        laps_left = state.get("laps_left", -1)
        if cmpfuel > 0.0 and laps_left >= 0:
            fuel_critical = fuel < 1.5 * cmpfuel and fuel < laps_left * cmpfuel
    near_pit = _near_pit_lane(state)
    if near_pit and (fuel_critical or strategy == PIT):
        return PIT
    if strategy == PIT:
        # Granite (or the fuel-critical check above) wants to pit, but the
        # car isn't close enough yet — keep racing instead of crawling a
        # lap early at _PARAMS[PIT]'s pace for no reason.
        strategy = ATTACK

    # Priority 3 — car is critically damaged → protect what's left
    if damage >= _DMG_DEFEND:
        return DEFEND

    # Priority 4 — car is damaged but still drivable → no attacking
    if damage >= _DMG_NO_ATTACK and strategy == ATTACK:
        return NORMAL

    # Priority 5 — fuel running low → conserve, don't attack
    if _FUEL_CAUTION_ENABLED and fuel < _FUEL_CAUTION and strategy == ATTACK:
        return NORMAL

    # Priority 6 — a car is closing in fast from directly behind and we're
    # otherwise healthy → hold the racing line against them instead of
    # yielding it for free.  Gated on the same health bar as the ATTACK
    # downgrades above so a damaged/low-fuel car always just gets home safe
    # instead of trying to defend a position.  Checked every frame here
    # (not left to Granite) because a 5 s poll is too slow for a car that is
    # already closing in.
    #
    # Also gated on NOT launching (dist_raced >= _START_CAUTION_DIST): live
    # on-track test (2026-08-07) showed BLOCK firing from step 1 — before the
    # green light, gear=0, speed=0 — because the standing two-row grid puts
    # every neighbour within _BLOCK_TRIGGER_GAP by construction. That is
    # exactly the merge window the start-of-race collision fix already
    # widens avoidance for (see _START_AVOID_DIST/_START_ACCEL_CAP); adding
    # an unrelated steering bias on top of it during the highest-risk phase
    # of the race would fight that fix, not complement it. dist_raced
    # defaults to 1e9 (never "launching") when the field is absent, matching
    # compute_control's own default for the same field.
    #
    # Recorded into _dbg unconditionally (not just when it fires) so the
    # per-step log line can show it — this is a DIFFERENT sensor cone than
    # the ogap/lgap/rgap printed from compute_control (those are the front/
    # diagonal _FRONT_CONE/_AVOID_LEFT/_AVOID_RIGHT beams); without this,
    # "why is strategy=BLOCK" was unanswerable from the log alone (2026-08-07).
    bgap = _rear_gap(state.get("opponents", []))
    _dbg["bgap"] = bgap
    if damage < _DMG_NO_ATTACK and fuel >= _FUEL_CAUTION:
        if state.get("dist_raced", 1e9) >= _START_CAUTION_DIST:
            if bgap < _BLOCK_TRIGGER_GAP:
                return BLOCK

    return strategy


# ---------------------------------------------------------------------------
# Step 6: Granite strategy caller
# ---------------------------------------------------------------------------

_STRATEGY_INTERVAL = 5.0    # seconds between Granite requests.  Measured
                             # round-trip through midware -> LM Studio is ~5.2s
                             # (granite-4.1-8b, reason capped at 8 words), so at
                             # 5s the model is essentially busy back-to-back and
                             # there is no idle margin.  That is deliberate: it
                             # buys the fastest strategy switching (_STRATEGY_CONFIRM
                             # = 1, so a strategy change lands on the next answer,
                             # ~5s after the state that triggered it).
                             # LatestTaskRunner keeps only the newest pending task,
                             # so overlapping ticks are dropped, never queued.
                             # Raise this if the machine has other load or a
                             # slower model — watch execution_s in midware's log.
_GRANITE_TIMEOUT   = 30.0   # seconds to wait for one strategy round-trip — must
                             # exceed midware's own 30s model timeout budget plus
                             # queue wait, or the bot gives up before the answer
                             # arrives and every call looks like a failure.
_STRATEGY_CONFIRM  = 1      # consecutive matching Granite answers required
                             # before switching the active strategy.  Was 2 (a
                             # debounce against a borderline reading flapping
                             # the car every ~5s poll) — dropped to 1 so a
                             # decision is visible/actionable on the very next
                             # answer instead of needing two in a row; a
                             # genuinely borderline state can now flap between
                             # strategies each poll, which is the accepted
                             # trade for responsiveness.
_GRANITE_MAX_TOK   = 80    # keep responses short and fast

_SYSTEM_PROMPT = """\
You are a race strategist for a TORCS simulation. \
Given live sensor data, choose one driving strategy and explain in one sentence why.

Respond with JSON only — no markdown, no extra text:
{"strategy": "<one of ATTACK|NORMAL|DEFEND""" + ("|SAVE_FUEL" if _SAVE_FUEL_ENABLED else "") + """|PIT>", "reason": "<one sentence>"}

Strategy guide (a separate safety system already downgrades ATTACK automatically
when damage gets risky, so you do not need to hedge — default to ATTACK
whenever nothing below rules it out):
- ATTACK:    default choice — push hard regardless of fuel level (a separate
             system takes over for the pit-lane approach automatically, you
             don't need to save fuel by picking anything else) as long as
             damage is below ~8000, even with other cars nearby; only avoid
             it when an opponent is close directly behind you (use DEFEND
             instead)
- NORMAL:    only pick this if ATTACK does not clearly apply and nothing forces
             DEFEND""" + ("/SAVE_FUEL" if _SAVE_FUEL_ENABLED else "") + """/PIT either
- DEFEND:    cautious, use when damaged or opponent close behind""" + ("""
- SAVE_FUEL: economical, use when fuel_L is only a few laps_left worth of
             fuel_per_lap_L (getting tight, but not yet PIT-critical) —
             laps_left/fuel_per_lap_L of -1/0 means no data yet, ignore them""" if _SAVE_FUEL_ENABLED else "") + """
- PIT:       fine to say once fuel < 10 L, but only takes effect once you're
             already near the pit lane — a separate hard safety rule forces
             it exactly when needed (both for low fuel and critical damage)
             and holds ATTACK otherwise, so you do not need to worry about
             getting the timing exactly right"""


def _build_strategy_prompt(state: dict[str, Any]) -> str:
    """Summarise the SCR state into a compact JSON payload for the prompt."""
    track  = state.get("track", [])
    opps   = state.get("opponents", [])

    track_summary = compact_track_profile(track)   if track else {}
    opp_summary   = compact_opponent_profile(opps) if opps  else {}

    payload = {
        "speed_kmh":   round(state.get("speed_x",      0.0), 1),
        "fuel_L":      round(state.get("fuel",         50.0), 1),
        "damage":      round(state.get("damage",        0.0), 0),
        "track_pos":   round(state.get("track_pos",    0.0), 3),
        "gear":              state.get("gear",            1),
        "race_pos":          state.get("race_pos",        1),
        "dist_raced_m":round(state.get("dist_raced",   0.0), 0),
        "fuel_per_lap_L": round(
            state.get("fuel_per_lap") or state.get("expected_fuel_per_lap", 0.0), 2),
        "laps_left":   state.get("laps_left", -1),
        "track":       track_summary,
        "opponents":   opp_summary,
    }
    import json as _json
    return _SYSTEM_PROMPT + "\n\nLive data:\n" + _json.dumps(payload, ensure_ascii=True)


def _parse_strategy_response(text: str) -> tuple[str, str]:
    """Extract (strategy, reason) from Granite's JSON reply.

    Returns (NORMAL, reason) if the strategy field is missing or invalid.
    """
    parsed = extract_json_object(text)
    if not parsed:
        return NORMAL, "parse error"
    raw_strategy = str(parsed.get("strategy", "")).strip().upper()
    reason       = str(parsed.get("reason", "")).strip()
    # _GRANITE_STRATEGIES, not _ALL_STRATEGIES — BLOCK is system-only and must
    # be rejected here even if Granite's text happens to say the word.
    strategy = raw_strategy if raw_strategy in _GRANITE_STRATEGIES else NORMAL
    return strategy, reason


def _next_debounced_strategy(
    active: str, candidate: str | None, candidate_count: int, proposed: str,
) -> tuple[str, str | None, int, bool]:
    """Pure transition function for GraniteStrategist's strategy debouncing.

    A new ``proposed`` strategy only becomes ``active`` once it has been
    proposed on ``_STRATEGY_CONFIRM`` consecutive calls; a proposal that
    doesn't match the running candidate resets the count to 1 rather than
    accumulating across different candidates.

    Returns (new_active, new_candidate, new_candidate_count, switched).
    No I/O, no side effects — safe to unit test without a Granite connection.
    """
    if proposed == active:
        return active, None, 0, False

    if proposed == candidate:
        candidate_count += 1
    else:
        candidate = proposed
        candidate_count = 1

    if candidate_count >= _STRATEGY_CONFIRM:
        return proposed, None, 0, True
    return active, candidate, candidate_count, False


class GraniteStrategist:
    """Async Granite strategy caller.

    Submits a new strategy request to Granite every ``interval`` seconds
    without blocking the main control loop.  The most recent completed
    result is cached and returned on each ``tick()`` call.

    Usage::

        g = GraniteStrategist(connection)
        # inside main loop:
        raw_strategy, reason = g.tick(state)
        safe_strategy = safety_filter(raw_strategy, state)
        ctrl = compute_control(state, safe_strategy)
    """

    def __init__(self, base_url: str = config.MIDWARE_BASE_URL, interval: float = _STRATEGY_INTERVAL) -> None:
        self._base_url = base_url.rstrip("/")
        self._interval   = interval
        self._runner     = LatestTaskRunner(self._call_granite, "granite-strategist")
        self._last_strategy: str = NORMAL
        self._last_reason:   str = "startup"
        self._last_submit:   float = -interval   # trigger immediately on first tick
        self._candidate_strategy: str | None = None
        self._candidate_count:    int = 0
        self.fallback = False
        self.last_error = ""

    # ------------------------------------------------------------------ #

    def tick(self, state: dict[str, Any]) -> tuple[str, str]:
        """Call once per main-loop iteration.

        Submits a new Granite request if the interval has elapsed, then
        returns the most recent completed (strategy, reason) pair.
        """
        now = time.monotonic()
        if now - self._last_submit >= self._interval:
            self._runner.submit({"state": state}, priority=0)
            self._last_submit = now

        result = self._runner.pop_completed()
        if result is not None:
            if result.error:
                self.fallback = True
                self.last_error = str(result.error)
                print(f"[ModelBroker] error: {result.error}; holding {self._last_strategy}")
            else:
                self.fallback = False
                self.last_error = ""
                strategy, reason = result.output
                self._debounce(strategy, reason)

        return self._last_strategy, self._last_reason

    def _debounce(self, strategy: str, reason: str) -> None:
        """Switch the active strategy once Granite proposes a new one on
        ``_STRATEGY_CONFIRM`` consecutive calls in a row.

        _STRATEGY_CONFIRM is currently 1, so in practice this switches on the
        very first differing answer — no smoothing, no delay.  The mechanism
        is kept generic (not hardcoded to "switch immediately") so a
        borderline state that turns out to flap distractingly can have the
        debounce turned back up without touching this method.  Note this only
        gates *Granite's* pick; safety_filter() still runs on every frame on
        top of whatever this returns, so a real emergency (low fuel, critical
        damage) is never delayed by it either way.
        """
        prev_active = self._last_strategy
        self._last_strategy, self._candidate_strategy, self._candidate_count, switched = (
            _next_debounced_strategy(
                self._last_strategy, self._candidate_strategy, self._candidate_count, strategy,
            )
        )
        if switched:
            self._last_reason = reason
            print(f"[Granite] {self._last_strategy}  — {reason}")
        elif strategy == prev_active:
            # Granite re-confirmed the strategy already in effect.
            self._last_reason = reason
        else:
            print(
                f"[Granite] candidate {strategy} "
                f"({self._candidate_count}/{_STRATEGY_CONFIRM}) — {reason}  "
                f"(holding {self._last_strategy})"
            )

    def last_strategy(self) -> str:
        return self._last_strategy

    # ------------------------------------------------------------------ #

    def _call_granite(self, task: dict[str, Any]) -> tuple[str, str]:
        """Worker: requests middleware Model Broker; never runs in control loop."""
        state  = task["state"]
        payload = json.dumps(
            {"bot_id": "default", "current_strategy": self._last_strategy, "sensor_state": state}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/bot/strategy",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_GRANITE_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        decision = result.get("decision") or {}
        return _parse_strategy_response(json.dumps(decision))


class BotStatusReporter:
    """Latest-only, non-blocking heartbeat reporter for the SCR loop."""

    def __init__(self, base_url: str = config.MIDWARE_BASE_URL, interval: float = 1.0) -> None:
        self._url = f"{base_url.rstrip('/')}/api/bot/status"
        self._interval = interval
        self._latest: dict[str, Any] = {"connected": False, "strategy": NORMAL}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_sent = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="bot-status-reporter")
        self._thread.start()

    def update(self, *, immediate: bool = False, **fields: Any) -> None:
        with self._lock:
            self._latest.update(fields)
        if immediate:
            self._wake.set()

    def tick(self, **fields: Any) -> None:
        self.update(**fields)
        if time.monotonic() - self._last_sent >= self._interval:
            self._wake.set()

    def close(self) -> None:
        self.update(connected=False, immediate=True)
        time.sleep(0.05)
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self._interval)
            self._wake.clear()
            with self._lock:
                payload = dict(self._latest)
            try:
                request = urllib.request.Request(
                    self._url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=0.4):
                    pass
            except Exception:
                pass
            self._last_sent = time.monotonic()


# ---------------------------------------------------------------------------
# Main drive loop
# ---------------------------------------------------------------------------

def run_bot(
    host:       str   = "localhost",
    port:       int   = config.SCR_UDP_PORT,
    strategy:   str   = NORMAL,
    *,
    use_granite: bool = False,
    track:      str | None = None,
    verbose:    bool  = True,
    client:     "ScrClient | None" = None,
) -> None:
    """Connect to TORCS and drive.

    With ``use_granite=True`` (Step 7), a GraniteStrategist is created and
    queried every few seconds to update the driving strategy dynamically.
    Without it, the fixed ``strategy`` argument is used throughout.

    ``track`` selects the pre-race map: a track name (``g-track-2``), a path
    to the track XML, or None to auto-detect from the TORCS raceman config.
    No map found → the bot drives on sensors alone, as before.

    ``client``, if given, is used instead of constructing a new
    ``ScrClient(host, port)`` — lets tests drive the loop with a fake/local
    client without touching anything below this line. ``host``/``port`` are
    then only used for the initial log line.
    """
    if strategy not in _ALL_STRATEGIES:
        print(f"Unknown strategy '{strategy}', falling back to NORMAL.")
        strategy = NORMAL

    # --- P1: study the circuit map before the race ---
    set_track_model(None)
    if track and track.lower() in ("off", "none", "no"):
        print("[map] disabled by --track off — sensors only (A/B baseline).")
    elif _TRACK_MODEL_AVAILABLE:
        _model = load_track_model(track or "auto", quiet=track is None)
        if _model is not None:
            set_track_model(_model)
            print(f"[map] {_model.summary()}")
        elif track:
            print(f"[warn] track map '{track}' not loaded — driving on sensors only.")
    elif track:
        print("[warn] --track given but track_model.py is missing — driving on sensors only.")

    # --- Step 7: optionally request strategy through middleware ---
    strategist: GraniteStrategist | None = None
    if use_granite:
        if not _TELEMETRY_AVAILABLE:
            print("[warn] telemetry_common not available — falling back to fixed strategy.")
        else:
            strategist = GraniteStrategist(config.MIDWARE_BASE_URL)

    print(f"Connecting to TORCS at {host}:{port}  strategy={strategy}  granite={strategist is not None}…")

    reporter = BotStatusReporter(config.MIDWARE_BASE_URL)
    atexit.register(reporter.close)
    with (client if client is not None else ScrClient(host, port)) as client:
        client.connect()
        reporter.update(connected=True, strategy=strategy, immediate=True)
        _reset_driver_state()   # fresh race — clear recovery / target-speed state
        print("Identified! Entering drive loop. Press Ctrl-C to stop.\n")

        step             = 0
        current_strategy = strategy               # updated by Granite each tick
        reported_strategy = current_strategy
        reported_fallback = False
        last_lap         = 0.0                    # lastLapTime seen so far
        lap_frames       = 0                      # frames driven this lap
        lap_bound        = 0                      # … of which the map governed
        lap_trust        = 0                      # … of which trust mode ruled
        prev_dist        = -1.0                   # distFromStart last frame
        close_hold       = 0                      # frames left of high-rate logging
                                                   # (see CLOSE_LOG_* below)

        try:
            while True:
                state = client.receive_state()

                if state is None:
                    print("Race ended — exiting loop.")
                    break

                if not state:
                    # Recv timed out — just keep waiting.  scr_server reuses
                    # the previous controls on its own (scr_server.cpp:602);
                    # re-sending here injects an EXTRA packet per timeout, the
                    # server's queue then runs permanently behind, and the car
                    # spends the first ~30 s of the race executing stale
                    # launch-phase commands (the 1st↔2nd gear ghost-flap).
                    continue

                # Practice-lap odometer calibration: the XML geometry can be
                # ~1% long (spiral-corner approximation) and the error is
                # CUMULATIVE — late-lap corners land tens of metres off, so
                # the map braked late into them and dragged every exit
                # (measured: +3.4 s/lap on Forza).  The car itself knows the
                # true lap length: it is where distFromStart wraps to 0.
                # Measure it at each start-line crossing and rescale.
                if _track_model is not None:
                    d_now = state.get("dist_from_start", -1.0)
                    if (prev_dist > _track_model.lap_length * 0.5
                            and 0.0 <= d_now < _track_model.lap_length * 0.3):
                        travel   = state.get("speed_x", 0.0) / 3.6 * 0.02
                        measured = prev_dist + max(0.0, travel - d_now)
                        if abs(measured - _track_model.lap_length) \
                                < _track_model.lap_length * 0.3:
                            if _track_model.real_lap is None:
                                print(f"[map] odometer calibrated: XML "
                                      f"{_track_model.lap_length:.0f} m → "
                                      f"measured {measured:.0f} m")
                            _track_model.calibrate(measured)
                    prev_dist = d_now

                # Map sanity: distFromStart beyond the map's lap length means
                # the loaded map is for a DIFFERENT track — a wrong braking
                # plan is worse than none, so drop it and fall back to sensors.
                if (_track_model is not None
                        and state.get("dist_from_start", 0.0)
                            > _track_model.lap_length * 1.02 + 10.0):
                    print(f"[map] WARNING: distFromStart "
                          f"{state['dist_from_start']:.0f} m exceeds map lap "
                          f"length {_track_model.lap_length:.0f} m — wrong "
                          f"track loaded?  Map disabled, sensors only.")
                    set_track_model(None)

                # --- fuel-per-lap model: feeds both Granite's SAVE_FUEL
                # judgment and safety_filter's PIT rule the same numbers,
                # computed once here so they can never disagree (see
                # _update_fuel_model / bt's SimpleStrategy.update()).
                _update_fuel_model(state)
                track_len = state.get("track_length", -1.0)
                state["fuel_per_lap"] = _fuel_per_lap_est
                state["expected_fuel_per_lap"] = (
                    track_len * _FUEL_PER_METER if track_len > 0.0 else 0.0
                )
                remaining = state.get("remaining_laps", -1)
                state["laps_left"] = (
                    max(remaining - state.get("laps_behind_leader", 0), 0)
                    if remaining >= 0 else -1
                )

                # --- Step 7: Granite strategy update (non-blocking) ---
                if strategist is not None:
                    raw_strategy, _reason = strategist.tick(state)
                    current_strategy = safety_filter(raw_strategy, state)
                else:
                    current_strategy = safety_filter(strategy, state)

                control = compute_control(state, current_strategy)
                client.send_control(control)
                fallback_active = bool(strategist and strategist.fallback)
                if current_strategy != reported_strategy or fallback_active != reported_fallback:
                    reporter.update(
                        strategy=current_strategy,
                        fallback=fallback_active,
                        error=strategist.last_error if strategist else "",
                        immediate=True,
                    )
                    reported_strategy = current_strategy
                    reported_fallback = fallback_active
                reporter.tick(
                    connected=True,
                    strategy=current_strategy,
                    speed_kmh=state.get("speed_x", 0.0),
                    gear=state.get("gear", 0),
                    last_control={"wire": control},
                    fallback=fallback_active,
                    error=strategist.last_error if strategist else "",
                )
                step += 1

                # --- per-lap A/B gauge: lap time + how often the map ruled ---
                lap_frames += 1
                lap_bound  += int(_dbg.get("map_bound", 0.0))
                lap_trust  += int(_dbg.get("trust", 0.0))
                llt = state.get("last_lap_time", 0.0)
                if llt > 0.0 and abs(llt - last_lap) > 1e-3:
                    pct_b = 100.0 * lap_bound / max(lap_frames, 1)
                    pct_t = 100.0 * lap_trust / max(lap_frames, 1)
                    print(f"[lap] {llt:7.2f} s   map-bound {pct_b:3.0f}%   "
                          f"trust {pct_t:3.0f}% of frames")
                    last_lap   = llt
                    lap_frames = lap_bound = lap_trust = 0

                # Close-encounter high-rate logging: the regular 100-step
                # (~2 s) cadence is too coarse to see what actually happens in
                # the 1-2 s before a side-contact crash — a live log showed a
                # car go from a 33 m gap to a full off-track hit in 4 s, but
                # only ONE sample (2 s in) fell inside that window, hiding
                # exactly the part that determines whether avoidance reacted
                # in time.  So: once either side gap drops under
                # _CLOSE_LOG_DIST, log every single step (not every 100) —
                # and keep doing so for _CLOSE_LOG_HOLD steps after the gap
                # opens back up, to also capture the immediate aftermath
                # (contact, damage, recovery mode) of a close pass.
                _CLOSE_LOG_DIST = 20.0   # m: either side gap under this triggers it
                _CLOSE_LOG_HOLD = 100    # steps (~2 s) of high-rate logging after
                                        # the gap last read closer than the above
                close_gap = min(_dbg.get("lgap", 200.0), _dbg.get("rgap", 200.0))
                if close_gap < _CLOSE_LOG_DIST:
                    close_hold = _CLOSE_LOG_HOLD
                elif close_hold > 0:
                    close_hold -= 1

                # 2026-08-07: same coarseness problem as the close-encounter case
                # above, but for stuck/recovery episodes with no opponent nearby —
                # a live incident sat in mode=stabilize for 46+ s (dist~3900-4200,
                # the known Forza blind-corner landmine) swinging tpos between -6
                # and +4 and taking damage 0->1826, but the 100-step cadence only
                # gave ~23 samples across the whole thing — not enough to tell
                # whether the car was genuinely spinning (angle crossing ±90°
                # repeatedly, as the gear +1/-1 flips implied) or wedged against
                # static geometry (sight pinned at 0.1 the entire time is the
                # actual anomaly, but 2 s resolution can't confirm it). Any
                # non-"race" mode now gets the same per-step logging as a close
                # pass, so the next occurrence is fully captured.
                stuck_mode = _dbg.get("mode", "race") != "race"

                if verbose and (step % 100 == 0 or close_hold > 0 or stuck_mode):
                    speed = state.get("speed_x", 0.0)
                    gear  = state.get("gear",    0)
                    fuel  = state.get("fuel",    0.0)
                    tpos  = state.get("track_pos", 0.0)
                    dmg   = state.get("damage",  0.0)
                    rpm   = state.get("rpm",     0.0)
                    print(
                        f"  step={step:6d}  {speed:6.1f} km/h  "
                        f"dist={_dbg.get('dist', -1.0):6.0f}  "
                        f"ps={_dbg.get('pit_s', -1.0):6.1f}  "        # TEMP DEBUG
                        f"ps0={_dbg.get('pit_s0', -1.0):6.1f}  "      # TEMP DEBUG
                        f"ps1={_dbg.get('pit_s1', -1.0):6.1f}  "      # TEMP DEBUG
                        f"pside={_dbg.get('pit_side', -1)}  "         # TEMP DEBUG
                        f"pinps={_dbg.get('pit_inps', 0)}  "          # TEMP DEBUG
                        f"pinlane={_dbg.get('pit_inlane', 0)}  "      # TEMP DEBUG
                        f"ptgt={_dbg.get('pit_target_tpos', 0.0):+.2f}  "   # TEMP DEBUG
                        f"perr={_dbg.get('pit_tpos_err', 0.0):+.2f}  "      # TEMP DEBUG
                        f"palign={_dbg.get('pit_aligned', -1)}  "          # TEMP DEBUG
                        f"pspdok={_dbg.get('pit_speed_ok', -1)}  "         # TEMP DEBUG
                        f"gear={gear}  fuel={fuel:.1f} L  tpos={tpos:+.2f}  "
                        f"angle={_dbg.get('angle', 0.0):+.3f}  "
                        f"steer={_dbg.get('cmd_steer', 0.0):+.3f}  "
                        f"strategy={current_strategy}  "
                        f"tgt={_dbg.get('target', 0.0):5.1f}  "
                        f"why={_dbg.get('why', '?'):12s}  "
                        f"map={_dbg.get('map', -1.0):5.0f}  "
                        f"tru={int(_dbg.get('trust', 0.0))}  "
                        f"sight={_dbg.get('sight', 0.0):5.1f}  "
                        f"open={_dbg.get('open_angle', 0.0):+.2f}  "
                        f"ogap={_dbg.get('ogap', 200.0):5.1f}  "
                        f"crate={_dbg.get('close_rate', 0.0):+5.1f}  "
                        f"lgap={_dbg.get('lgap', 200.0):5.1f}  "
                        f"rgap={_dbg.get('rgap', 200.0):5.1f}  "
                        f"bgap={_dbg.get('bgap', 200.0):5.1f}  "
                        f"obnd={int(_dbg.get('opp_bound', 0.0))}  "
                        f"rpm={rpm:5.0f}  dmg={dmg:5.0f}  "
                        f"acc={_dbg.get('cmd_accel', 0.0):.2f}  "
                        f"brk={_dbg.get('cmd_brake', 0.0):.2f}  "
                        f"clt={_dbg.get('cmd_clutch', 0.0):.2f}  "
                        f"cgear={_dbg.get('cmd_gear', 0)}  "
                        f"mode={_dbg.get('mode', '?')}"
                    )

        except KeyboardInterrupt:
            print(f"\nStopped after {step} steps.")

    reporter.close()
    atexit.unregister(reporter.close)
    if client.is_shutdown:
        print("Server sent ***shutdown***.")


# ---------------------------------------------------------------------------
# Entry points
#   python3 ai_bot.py                              → run unit tests
#   python3 ai_bot.py --bot                        → localhost:3001, NORMAL
#   python3 ai_bot.py --bot HOST PORT              → custom address
#   python3 ai_bot.py --bot HOST PORT STRATEGY     → e.g. ATTACK
#   --track NAME|XML   pre-race map (default: auto-detect from ~/.torcs)
#   --track off        disable the map — sensors-only A/B baseline
#   --granite          enable the Granite strategist
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    opponents = " ".join(["200.0"] * 36)
    track     = " ".join(["150.0"] * 9 + ["180.0"] + ["150.0"] * 9)
    wheels    = "12.5 12.5 13.0 13.0"
    focus_    = "-1.0 -1.0 -1.0 -1.0 -1.0"

    sample = (
        f"(angle 0.015)(curLapTime 42.3)(damage 0)(distFromStart 312.7)"
        f"(distRaced 312.7)(fuel 38.5)(gear 4)(lastLapTime 91.2)"
        f"(opponents {opponents})(racePos 2)(rpm 7800)"
        f"(speedX 148.3)(speedY -0.4)(speedZ 0.0)"
        f"(track {track})(trackPos 0.12)(wheelSpinVel {wheels})"
        f"(z 0.33)(focus {focus_})(x 241.0)(y 88.0)"
        f"(roll 0.0)(pitch 0.01)(yaw 1.57)"
        f"(speedGlobalX 120.1)(speedGlobalY 88.3)"
        f"(pitEntry 50.0)(pitStart 100.0)(pitEnd 150.0)(pitExit 200.0)"
        f"(pitSpeedLimit 60.0)(pitSide 1)(pitBoxOffset -19.0)(inPitStop 0)"
        f"(trackLength 2000.0)(remainingLaps 2)(lapsBehindLeader 0)(segWidth 11.0)"
    )

    # ---- parse_scr_state ------------------------------------------------
    state = parse_scr_state(sample)
    assert state is not None,                       "FAIL: returned None for valid packet"
    assert state["gear"] == 4,                      f"FAIL: gear={state['gear']}"
    assert state["race_pos"] == 2,                  f"FAIL: race_pos={state['race_pos']}"
    assert abs(state["speed_x"] - 148.3) < 1e-6,   f"FAIL: speed_x={state['speed_x']}"
    assert abs(state["fuel"] - 38.5) < 1e-6,        f"FAIL: fuel={state['fuel']}"
    assert len(state["opponents"]) == 36,           f"FAIL: opponents length={len(state['opponents'])}"
    assert len(state["track"]) == 19,               f"FAIL: track length={len(state['track'])}"
    assert len(state["wheel_spin_vel"]) == 4,       f"FAIL: wheel_spin_vel length={len(state['wheel_spin_vel'])}"
    assert len(state["focus"]) == 5,                f"FAIL: focus length={len(state['focus'])}"
    assert state["opponents"][0] == 200.0,          f"FAIL: opponents[0]={state['opponents'][0]}"
    assert state["focus"][0] == -1.0,               f"FAIL: focus[0]={state['focus'][0]}"
    assert state["pit_entry"] == 50.0,              f"FAIL: pit_entry={state['pit_entry']}"
    assert state["pit_start"] == 100.0,             f"FAIL: pit_start={state['pit_start']}"
    assert state["pit_end"] == 150.0,               f"FAIL: pit_end={state['pit_end']}"
    assert state["pit_exit"] == 200.0,              f"FAIL: pit_exit={state['pit_exit']}"
    assert state["pit_speed_limit"] == 60.0,        f"FAIL: pit_speed_limit={state['pit_speed_limit']}"
    assert state["pit_side"] == 1,                  f"FAIL: pit_side={state['pit_side']}"
    assert state["pit_box_offset"] == -19.0,        f"FAIL: pit_box_offset={state['pit_box_offset']}"
    assert state["in_pit_stop"] == 0,               f"FAIL: in_pit_stop={state['in_pit_stop']}"
    assert state["track_length"] == 2000.0,         f"FAIL: track_length={state['track_length']}"
    assert state["remaining_laps"] == 2,            f"FAIL: remaining_laps={state['remaining_laps']}"
    assert state["laps_behind_leader"] == 0,        f"FAIL: laps_behind_leader={state['laps_behind_leader']}"
    assert state["seg_width"] == 11.0,              f"FAIL: seg_width={state['seg_width']}"
    print("parse_scr_state  ... OK")

    assert parse_scr_state("") is None,             "FAIL: empty string should return None"
    assert parse_scr_state("(angle 0.1)") is None,  "FAIL: incomplete packet should return None"

    short_opp = " ".join(["50.0"] * 10)
    partial = (
        f"(angle 0)(curLapTime 0)(damage 0)(distFromStart 0)(distRaced 0)"
        f"(fuel 30)(gear 1)(lastLapTime 0)(opponents {short_opp})"
        f"(racePos 1)(rpm 0)(speedX 0)(speedY 0)(speedZ 0)"
        f"(track {track})(trackPos 0)(wheelSpinVel {wheels})(z 0)"
    )
    ps = parse_scr_state(partial)
    assert ps is not None,               "FAIL: partial packet returned None"
    assert len(ps["opponents"]) == 36,   "FAIL: short opponents not padded to 36"
    assert ps["opponents"][35] == 200.0, "FAIL: padding value wrong"
    # A packet from a scr_server build predating the pit-lane fields (or this
    # `partial` packet, which simply omits them) must default to "no pit
    # lane data" (-1), never a value that could be mistaken for a real
    # distance-from-start of 0.
    assert ps["pit_entry"] == -1.0,      f"FAIL: missing pit_entry should default to -1: {ps['pit_entry']}"
    assert ps["in_pit_stop"] == 0,       f"FAIL: missing in_pit_stop should default to 0: {ps['in_pit_stop']}"
    assert ps["remaining_laps"] == -1,   \
        f"FAIL: missing remaining_laps should default to -1, not 0 (0 would misread as 'no laps left'): {ps['remaining_laps']}"
    assert ps["pit_side"] == -1,         \
        f"FAIL: missing pit_side should default to -1, not a value that collides with TR_RGT(1)/TR_LFT(2): {ps['pit_side']}"
    assert ps["seg_width"] == -1.0,      \
        f"FAIL: missing seg_width should default to -1, not 0 (0 would divide-by-zero in the pit box conversion): {ps['seg_width']}"
    print("parse_scr_state  (edge cases) ... OK")

    # ---- format_scr_control --------------------------------------------
    ctrl = format_scr_control(accel=0.8, brake=0.0, gear=3, steer=-0.12)
    assert "(accel 0.800)" in ctrl
    assert "(brake 0.000)" in ctrl
    assert "(gear 3)"      in ctrl
    assert "(steer -0.120)" in ctrl
    assert "(clutch 0.000)" in ctrl
    assert "(focus 0)"     in ctrl
    assert "(meta 0)"      in ctrl
    assert "(pitRequest 0)" in ctrl
    print(f"format_scr_control ... OK  →  {ctrl}")

    over = format_scr_control(accel=2.0, brake=-1.0, steer=5.0, focus=200)
    assert "(accel 1.000)" in over
    assert "(brake 0.000)" in over
    assert "(steer 1.000)" in over
    assert "(focus 90)"    in over
    print("format_scr_control (clamping) ... OK")

    pit_ctrl = format_scr_control(accel=0.0, brake=1.0, pit_request=True)
    assert "(pitRequest 1)" in pit_ctrl, f"FAIL: pit_request=True should emit pitRequest 1: {pit_ctrl}"
    print("format_scr_control (pit_request) ... OK")

    # ---- _simple_autopilot --------------------------------------------
    track_vals = [150.0] * 9 + [180.0] + [150.0] * 9
    fake = {
        "speed_x": 80.0, "rpm": 5000.0, "gear": 3,
        "angle": 0.1, "track_pos": 0.2, "track": track_vals,
    }
    ap = _simple_autopilot(fake)
    assert "(accel 1.000)" in ap, f"FAIL: expected full throttle on clear track: {ap}"
    assert "(gear 3)"      in ap, f"FAIL: gear should stay 3 at 5000 rpm: {ap}"
    print(f"_simple_autopilot  ... OK  →  {ap}")

    # ---- ScrClient API (no TORCS) — just instantiation + close ----------
    c = ScrClient("localhost", 3001)
    assert c._addr == ("localhost", 3001)
    assert not c.is_shutdown
    c.close()   # no-op when never connected
    print("ScrClient          ... OK  (instantiation + close without connect)")

    # ---- compute_control ------------------------------------------------
    track_vals = [150.0] * 9 + [180.0] + [150.0] * 9   # clear straight
    cs = {"speed_x": 80.0, "rpm": 5000.0, "gear": 3,
          "angle": 0.0, "track_pos": 0.0, "track": track_vals}

    # clear straight at 80 km/h — each strategy should accelerate at its limit
    # (sight=180 ≥ _STRAIGHT_CLEAR and pursuit=0 → straight → strategy max_speed)
    cc_attack = compute_control(cs, ATTACK)
    assert "(accel 1.000)" in cc_attack,  f"FAIL ATTACK accel: {cc_attack}"
    assert "(brake 0.000)" in cc_attack,  f"FAIL ATTACK brake: {cc_attack}"
    print(f"compute_control ATTACK    ... OK  →  {cc_attack}")

    cc_normal = compute_control(cs, NORMAL)
    assert "(accel 1.000)" in cc_normal,  f"FAIL NORMAL accel (full throttle on a straight): {cc_normal}"
    print(f"compute_control NORMAL    ... OK  →  {cc_normal}")

    cc_save = compute_control(cs, SAVE_FUEL)
    assert "(accel 0.650)" in cc_save,    f"FAIL SAVE_FUEL accel: {cc_save}"
    print(f"compute_control SAVE_FUEL ... OK  →  {cc_save}")

    # NORMAL arriving hot at a corner (short sight all around) → should brake
    cs_fast = {**cs, "speed_x": 250.0, "track": [60.0] * 19}
    cc_over = compute_control(cs_fast, NORMAL)
    assert "(accel 0.000)" in cc_over, f"FAIL: over target should not accelerate: {cc_over}"
    assert "(brake 0.000)" not in cc_over, f"FAIL: over target should brake: {cc_over}"
    print(f"compute_control NORMAL over-speed ... OK  →  {cc_over}")

    # off-track + still carrying speed → shed speed, steer back at a SHALLOW angle
    # speed=80 > 55 cap: accel 0, brake 0.5; steer = clamp(angle − 0.5·tpos) = −0.75
    _reset_driver_state()
    cs_offt = {**cs, "track_pos": 1.5}   # speed_x=80 in cs → still rolling
    cc_offt = compute_control(cs_offt, ATTACK)
    assert "(accel 0.000)" in cc_offt, f"FAIL off-track accel: {cc_offt}"
    assert "(brake 0.500)" in cc_offt, f"FAIL off-track brake: {cc_offt}"
    assert "(steer -0.750)" in cc_offt, f"FAIL off-track steer (angle−0.5·tpos): {cc_offt}"
    print(f"compute_control off-track (moving)  ... OK  →  {cc_offt}")

    # hysteresis: back over the edge (|tpos|<1) but not yet well inside →
    # STILL in recovery mode (gentle pace), not full pursuit throttle
    cc_edge = compute_control({**cs, "track_pos": 0.95, "speed_x": 40.0}, ATTACK)
    assert "(accel 0.500)" in cc_edge, f"FAIL edge hysteresis (should stay in recovery): {cc_edge}"
    # well inside + aligned → recovery exits, normal driving resumes
    cc_back = compute_control({**cs, "track_pos": 0.2, "speed_x": 40.0}, ATTACK)
    assert "(accel 1.000)" in cc_back, f"FAIL recovery exit (should resume ATTACK): {cc_back}"
    print("compute_control re-entry hysteresis  ... OK")

    # apex kerb-ride (|tpos| just past 1) must NOT trigger recovery — the racing
    # line legitimately clips the kerb; a recovery grab here yanked the car to
    # the centre mid-corner and threw it off the outside (regression)
    _reset_driver_state()
    cc_apex = compute_control({**cs, "track_pos": 1.05, "speed_x": 120.0}, NORMAL)
    assert "(gear -1)"     not in cc_apex, f"FAIL apex kerb-ride must not reverse: {cc_apex}"
    assert "(accel 1.000)" in cc_apex, f"FAIL apex kerb-ride should keep racing: {cc_apex}"
    print(f"compute_control apex kerb-ride (regression) ... OK  →  {cc_apex}")

    # misaligned straight: the 0° beam grazes the edge of the straight (90 m)
    # but a ±5-10° beam still runs the length of the road → must stay FULL
    # throttle (regression: the ±5° median used to cap the car ~100 km/h low)
    graze = [150.0] * 9 + [90.0, 200.0, 200.0] + [150.0] * 7
    cc_graze = compute_control({**cs, "track": graze}, NORMAL)
    assert "(accel 1.000)" in cc_graze, f"FAIL grazing straight must keep full throttle: {cc_graze}"
    print(f"compute_control grazing straight (regression) ... OK  →  {cc_graze}")

    # cruising AT the cap must be a steady partial throttle — the old bang-bang
    # (full below target / zero above) tapped the pedal down the whole straight
    _reset_driver_state()
    cc_cruise = compute_control({**cs, "speed_x": 245.0, "gear": 6,
                                 "track": [200.0] * 19}, NORMAL)
    assert "(brake 0.000)" in cc_cruise, f"FAIL cruise must not brake: {cc_cruise}"
    assert "(accel 0.833)" in cc_cruise, f"FAIL cruise throttle should be proportional: {cc_cruise}"
    print(f"compute_control cruise at cap (regression) ... OK  →  {cc_cruise}")

    # 150 km/h with 100 m of sight on a straight-ish road must KEEP PUSHING —
    # the old zero-endpoint braking curve capped exactly this case at ~151 and
    # lifted half a straight early (regression for the "tops out at 150" bug)
    _reset_driver_state()
    cc_mid = compute_control({**cs, "speed_x": 150.0, "gear": 5,
                              "track": [100.0] * 19}, NORMAL)
    assert "(accel 1.000)" in cc_mid, f"FAIL: 150 km/h @ 100 m sight must keep pushing: {cc_mid}"
    assert "(brake 0.000)" in cc_mid, f"FAIL: 150 km/h @ 100 m sight must not brake: {cc_mid}"
    print(f"compute_control mid-straight pace (regression) ... OK  →  {cc_mid}")

    # weave damping: a heading offset on a straight must produce corrective
    # steer from the angle-alignment term — this is the damping that stops the
    # pendulum from building up (regression)
    _reset_driver_state()
    cc_align = compute_control({**cs, "angle": 0.10, "track": [200.0] * 19}, NORMAL)
    assert "(steer 0.000)" not in cc_align and "(steer -" not in cc_align, \
        f"FAIL: heading offset must produce positive corrective steer: {cc_align}"
    print(f"compute_control heading alignment (regression) ... OK  →  {cc_align}")

    # realistic ragged straight: front beam saturated, the ±5°/±10° beams read
    # unevenly (edge grazing) — the small wandering aim must be ignored, full
    # throttle held, wheel dead ahead (regression for the high-speed pendulum)
    _reset_driver_state()
    ragged = [15.0] * 7 + [90.0, 200.0, 200.0, 140.0, 52.0] + [15.0] * 7
    cc_rag = compute_control({**cs, "track": ragged, "speed_x": 180.0}, NORMAL)
    assert "(steer 0.000)" in cc_rag, f"FAIL: ragged straight must not pull sideways: {cc_rag}"
    assert "(accel 1.000)" in cc_rag, f"FAIL: ragged straight must hold full throttle: {cc_rag}"
    print(f"compute_control ragged straight (regression) ... OK  →  {cc_rag}")

    # off-centre on an open straight → gentle drift back toward the centre
    # line, so lateral offset carried out of corners no longer persists until
    # the car clips a kerb at speed (regression)
    _reset_driver_state()
    cc_kerb = compute_control({**cs, "track_pos": 0.7, "speed_x": 200.0,
                               "track": [200.0] * 19}, NORMAL)
    assert "(steer -0.040)" in cc_kerb, f"FAIL: kerb-side straight must drift centreward: {cc_kerb}"
    assert "(accel 1.000)" in cc_kerb, f"FAIL: recentring must not cost throttle: {cc_kerb}"
    print(f"compute_control hold-line recentring (regression) ... OK  →  {cc_kerb}")

    # rpm-first gear shifting (the speed table short-shifted and killed pickup)
    assert _gear_shift(3, 9000.0, 100.0) == 4, "FAIL: high rpm must upshift"
    assert _gear_shift(3, 3000.0,  60.0) == 2, "FAIL: low rpm at low speed must downshift"
    assert _gear_shift(3, 5000.0, 100.0) == 3, "FAIL: mid rpm holds gear"
    # speed guard: a stale low-rpm reading at road speed the lower gear cannot
    # carry must NOT downshift (the launch-strangling 1st↔2nd flap)
    assert _gear_shift(2, 3400.0,  51.0) == 2, "FAIL: 51 km/h must not drop to 1st"
    assert _gear_shift(3, 3000.0, 100.0) == 3, "FAIL: 100 km/h must not drop to 2nd"
    assert _gear_shift(6, 9500.0, 300.0) == 6, "FAIL: no upshift past top gear"
    assert _gear_shift(2,    0.0, 100.0) == 3, "FAIL: rpm missing → speed table"
    assert _gear_shift(1, 8600.0,  66.0) == 2, "FAIL: past _RPM_UP must upshift"
    assert _gear_shift(1, 8200.0,  66.0) == 1, "FAIL: below _RPM_UP must hold (no speed backstop)"
    # anti-hunting: right after a 1→2 upshift the revs land ~4000-4800 and sag
    # briefly — must HOLD 2nd, not bounce straight back down (this exact loop
    # once pinned the car at 56 km/h churning gears for a whole race)
    assert _gear_shift(2, 4045.0,  56.0) == 2, "FAIL: post-upshift revs must hold gear"
    assert _gear_shift(2, 3800.0,  56.0) == 2, "FAIL: shift sag must not re-downshift"
    print("_gear_shift (rpm-first + anti-hunt) ... OK")

    # ---- _brake_dist (bt-inspired physics stopping distance) --------------
    assert _brake_dist(100.0, 150.0, 1200.0) == 0.0, \
        "FAIL: v1<=v2 needs no braking distance"
    assert _brake_dist(100.0, 100.0, 1200.0) == 0.0, \
        "FAIL: equal speeds need no braking distance"
    # 200 -> 80 km/h at mass 1200 kg (car1-trb1 + 50 L fuel) should need
    # roughly 90 m at the assumed _BRAKE_MU=1.0 (hand-computed from the same
    # closed-form formula) — a sanity range check, not an exact literal, so
    # this doesn't pin down _BRAKE_MU if that constant gets tuned later.
    d = _brake_dist(200.0, 80.0, 1200.0)
    assert 80.0 < d < 100.0, f"FAIL: 200->80 km/h braking distance out of expected range: {d:.1f} m"
    # monotonic: a bigger speed drop needs more distance
    assert _brake_dist(200.0, 40.0, 1200.0) > _brake_dist(200.0, 80.0, 1200.0), \
        "FAIL: braking distance must grow with the speed drop"
    print("_brake_dist (bt-inspired physics) ... OK")

    # Flung far off track (track_pos way past a normal excursion) while still
    # carrying real speed → stabilize: brake straight to a stop, no steering,
    # taking priority over BOTH the stuck-jam burst and wrong-way turnaround
    # so they can't fight each other for control.
    _reset_driver_state()
    cs_flung = {**cs, "track_pos": 3.0, "speed_x": 50.0, "angle": 0.2}
    cc_flung = compute_control(cs_flung, ATTACK)
    assert "(accel 0.000)" in cc_flung, f"FAIL flung accel: {cc_flung}"
    assert "(brake 0.900)" in cc_flung, f"FAIL flung brake: {cc_flung}"
    assert "(steer 0.000)" in cc_flung, f"FAIL flung steer: {cc_flung}"
    # Once speed has bled off, the SAME extreme track_pos must switch to a
    # single steady creep back toward the centre line — not keep braking, and
    # not sit idle waiting for something else to happen (the gap this whole
    # fix closes: a car that crashed down to near-zero speed almost instantly
    # never left this state under the old speed-gated version).  Still facing
    # roughly the right way (angle 0.2, well inside _WRONG_WAY) → forward is
    # the faster way back, same steer formula as the plain re-entry branch.
    _reset_driver_state()
    cc_settled_fwd = compute_control({**cs_flung, "speed_x": 5.0}, ATTACK)
    assert "(gear -1)" not in cc_settled_fwd, \
        f"FAIL: facing roughly right must creep FORWARD, not reverse: {cc_settled_fwd}"
    assert "(brake 0.900)" not in cc_settled_fwd, \
        f"FAIL: settled car must not keep braking forever: {cc_settled_fwd}"
    # But facing badly wrong (angle > _WRONG_WAY) at the same extreme
    # track_pos and low speed → forward would only dig the hole deeper, so
    # this must still reverse.
    _reset_driver_state()
    cc_settled_rev = compute_control({**cs_flung, "speed_x": 5.0, "angle": 3.0}, ATTACK)
    assert "(gear -1)" in cc_settled_rev, \
        f"FAIL: facing badly wrong must still creep in reverse: {cc_settled_rev}"
    print(f"compute_control flung off-track → stabilize (regression) ... OK  →  {cc_flung}")
    _reset_driver_state()

    # 2026-08-09: stabilize's own no-progress watchdog. Simulate genuinely
    # wedged: extreme excursion triggers stabilize, then the car sits dead
    # stopped, badly wrong-way, with distFromStart frozen (not just slow
    # progress — literally unchanged tick to tick, the live symptom) for
    # longer than _NO_PROGRESS_FRAMES. Must hand off to the three-point-turn
    # escape (_turnaround) instead of repeating the same reverse forever.
    _reset_driver_state()
    compute_control({**cs, "track_pos": 3.0, "speed_x": 50.0, "angle": 0.2,
                      "dist_from_start": 4795.0}, ATTACK)
    cs_wedged = {**cs, "track_pos": 1.09, "speed_x": 0.0, "angle": 2.37,
                 "dist_from_start": 4795.0}
    cc_wedge = ""
    for _ in range(_NO_PROGRESS_FRAMES + 10):
        cc_wedge = compute_control(cs_wedged, ATTACK)
        if _dbg.get("mode") != "stabilize":
            break   # escaped the latch — no need to keep ticking
    assert _dbg.get("mode") != "stabilize", \
        f"FAIL: wedged stabilize should have handed off by now, still stuck in: {_dbg.get('mode')}"
    assert _turnaround, "FAIL: handoff must set _turnaround so the next ticks alternate legs"
    print(f"compute_control stabilize wedged → turnaround handoff (regression) ... OK  →  {cc_wedge}")
    _reset_driver_state()

    # off-track + slow + facing forward → forward crawl in 1st, shallow-angle steer
    _reset_driver_state()
    cs_crawl = {**cs, "track_pos": 1.5, "speed_x": 2.0, "angle": 0.0}
    cc_crawl = compute_control(cs_crawl, ATTACK)
    assert "(gear 1)"      in cc_crawl, f"FAIL crawl gear: {cc_crawl}"
    assert "(accel 0.500)" in cc_crawl, f"FAIL crawl accel: {cc_crawl}"
    assert "(brake 0.000)" in cc_crawl, f"FAIL crawl brake: {cc_crawl}"
    assert "(steer -0.750)" in cc_crawl, f"FAIL crawl steer (angle−0.5·tpos): {cc_crawl}"
    print(f"compute_control off-track (crawl fwd) ... OK  →  {cc_crawl}")

    # off-track + stopped + facing AWAY (angle 3.0 rad) → reverse-turn: back up
    # with inverted steer so the nose swings toward the track direction
    _reset_driver_state()
    cs_stuck = {**cs, "track_pos": 1.5, "speed_x": 0.0, "angle": 3.0}
    cc_stuck = compute_control(cs_stuck, ATTACK)
    assert "(gear -1)"     in cc_stuck, f"FAIL stuck gear: {cc_stuck}"
    assert "(accel 0.500)" in cc_stuck, f"FAIL stuck accel: {cc_stuck}"
    assert "(steer -1.000)" in cc_stuck, f"FAIL stuck steer (−angle·0.8+tpos·0.3): {cc_stuck}"
    print(f"compute_control off-track (turn-around) ... OK  →  {cc_stuck}")

    # WRONG WAY *inside* the track (spun car): must trigger the turn-around,
    # not fall through to normal driving — the old code drove off backwards here.
    _reset_driver_state()
    wrong = {**cs, "track_pos": 0.0, "speed_x": 0.0, "angle": 3.0,
             "track": [-1.0] * 19}   # sensors read -1 when facing backwards
    cc_wrong = compute_control(wrong, NORMAL)
    assert "(gear -1)" in cc_wrong, f"FAIL wrong-way on track must reverse-turn: {cc_wrong}"
    # same but still rolling forward fast → brake to a stop before manoeuvring
    cc_wf = compute_control({**wrong, "speed_x": 80.0}, NORMAL)
    assert "(brake 0.800)" in cc_wf, f"FAIL wrong-way at speed must brake: {cc_wf}"
    assert "(accel 0.000)" in cc_wf, f"FAIL wrong-way at speed must not accelerate: {cc_wf}"
    print(f"compute_control wrong-way (regression) ... OK  →  {cc_wrong}")
    _reset_driver_state()

    # Reverse leg must not run forever even when the car is NOT jammed (speed
    # stays above _TA_JAM_SPEED the whole time, so that counter never fires).
    # If the angle just isn't converging, _TA_REV_MAX_FRAMES must force a
    # forward leg after 120 frames regardless — guards against the car
    # backing up tens of metres in a single continuous reverse attempt
    # (logged live: one recovery reversed ~40 m before anything intervened).
    cs_freewheel = {**cs, "track_pos": 0.0, "speed_x": -10.0, "angle": 3.0,
                    "track": [-1.0] * 19}
    out = ""
    for _ in range(_TA_REV_MAX_FRAMES + 1):
        out = compute_control(cs_freewheel, NORMAL)
    assert "(gear 1)" in out and "(accel 0.400)" in out, \
        f"FAIL: reverse leg must cap out and force a forward leg: {out}"
    print("compute_control turnaround reverse-leg cap (regression) ... OK")
    _reset_driver_state()

    # No-progress watchdog: track_pos=2.3 is UNDER the 2.5 extreme-excursion
    # gate, so it alone never triggers stabilize — but dist_from_start pinned
    # at a fixed value (simulating a genuinely wedged car, whatever the
    # turn-rev/turn-fwd/burst cycling underneath is doing) must still escalate
    # after _NO_PROGRESS_FRAMES, regardless of how "not extreme" the position
    # looks (logged live: a car wedged at ~2.3 cycled for 46+ real seconds
    # before this fix existed).
    cs_wedged = {**cs, "track_pos": 2.3, "speed_x": 0.0, "angle": 3.0,
                "dist_from_start": 500.0}
    for _ in range(_NO_PROGRESS_FRAMES - 10):
        compute_control(cs_wedged, NORMAL)
    assert _dbg["mode"] != "stabilize", \
        f"FAIL: watchdog fired too early, before the full window: mode={_dbg['mode']}"
    for _ in range(20):
        out = compute_control(cs_wedged, NORMAL)
    assert _dbg["mode"] == "stabilize", \
        f"FAIL: no-progress watchdog must escalate to stabilize: mode={_dbg['mode']}  {out}"
    print("compute_control no-progress watchdog (regression) ... OK")
    _reset_driver_state()

    # on-track but ALL beams unusable (sensor glitch) → angle/centre fallback,
    # never floor it blind with steer 0
    cc_blind = compute_control({**cs, "track": [-1.0] * 19}, ATTACK)
    assert "(accel 1.000)" not in cc_blind, f"FAIL blind fallback must not floor it: {cc_blind}"
    print("compute_control blind-sensor fallback ... OK")

    # gear selector must handle TOP gear without IndexError (regression):
    # once the car got fast enough to reach 6th, _DOWN_SPEED[6] used to crash,
    # which killed the drive loop and sent the car straight off the track.
    assert _gear_from_speed(6, 250.0) == 6, "FAIL: top gear cruise"
    assert _gear_from_speed(6, 100.0) == 5, "FAIL: top gear downshift"
    assert _gear_from_speed(5, 200.0) == 6, "FAIL: upshift into top gear"
    assert _gear_from_speed(7, 250.0) == 7, "FAIL: out-of-range gear must not crash"
    # corner track: long beams to the left, short to the right → must turn in
    left_corner = [200.0] * 10 + [40.0] * 9
    cs_fast6 = {**cs, "speed_x": 250.0, "gear": 6, "track": left_corner}
    cc_fast6 = compute_control(cs_fast6, ATTACK)   # must not raise
    assert "(gear 6)" in cc_fast6, f"FAIL fast 6th: {cc_fast6}"
    assert "(steer 0.000)" not in cc_fast6, f"FAIL: pure pursuit should steer into a corner: {cc_fast6}"
    print(f"_gear_from_speed top gear (regression) ... OK  →  {cc_fast6}")

    # pure pursuit: symmetric track → aim straight ahead (~no steer); and a sharp
    # corner must drop the target speed well below a gentle bend of equal sight.
    assert "(steer 0.000)" in compute_control(
        {**cs, "speed_x": 200.0, "gear": 6, "track": [200.0] * 19, "track_pos": 0.0}, NORMAL
    ), "FAIL: pursuit should go straight on a symmetric track"
    print("compute_control pure-pursuit straight ... OK")

    # stuck → reverse recovery: a sustained crawl must trigger a reverse burst
    # even ON track (tpos < 1) — old code only knew how to reverse off-track.
    _reset_driver_state()
    wall = [150.0] * 9 + [2.0] + [150.0] * 9          # nose 2 m from a wall
    jammed = {**cs, "speed_x": 1.0, "gear": 1, "angle": 0.1, "track_pos": 0.2,
              "track": wall}
    out = ""
    for _ in range(_STUCK_FRAMES + 1):
        out = compute_control(jammed, NORMAL)
    assert "(gear -1)"     in out, f"FAIL: stuck car must reverse: {out}"
    assert "(accel 0.500)" in out, f"FAIL: reverse throttle: {out}"
    print(f"compute_control stuck → reverse (regression) ... OK  →  {out}")
    # a clear standing start (slow, but open road ahead) must NOT reverse
    _reset_driver_state()
    start = {**cs, "speed_x": 0.0, "gear": 1, "track_pos": 0.0}   # track[9]=180 clear
    out = ""
    for _ in range(_STUCK_FRAMES + 5):
        out = compute_control(start, NORMAL)
    assert "(gear -1)" not in out, f"FAIL: clear standing start wrongly reversed: {out}"
    print("compute_control clear start (no false reverse) ... OK")
    _reset_driver_state()

    # 2026-08-09: track-hold throttle cut (bt-inspired, see _apply_track_hold
    # above / bt's Driver::filterTrk) — cut the throttle BEFORE running off
    # the edge, not just after. Clear straight ahead each time (so the
    # reactive target-speed logic alone would always want full throttle),
    # varying only track_pos tick-to-tick to isolate the filter's own effect.
    clear_track = [200.0] * 19
    # (a) already past the apex-free band and still drifting further out →
    # cut to zero, even though the road ahead is clear.
    _reset_driver_state()
    compute_control({**cs, "speed_x": 100.0, "track_pos": 0.87, "track": clear_track}, NORMAL)
    out_drift = compute_control({**cs, "speed_x": 100.0, "track_pos": 0.92, "track": clear_track}, NORMAL)
    assert "(accel 0.000)" in out_drift, \
        f"FAIL: drifting further past the edge band must cut throttle: {out_drift}"
    # (b) same band, but curving back toward centre → left alone.
    _reset_driver_state()
    compute_control({**cs, "speed_x": 100.0, "track_pos": 0.92, "track": clear_track}, NORMAL)
    out_return = compute_control({**cs, "speed_x": 100.0, "track_pos": 0.87, "track": clear_track}, NORMAL)
    assert "(accel 0.000)" not in out_return, \
        f"FAIL: curving back toward centre must not be cut: {out_return}"
    # (c) inside the apex-free band (kerb-riding line) even while drifting →
    # left alone, same as bt not punishing a car already on the inside line.
    _reset_driver_state()
    compute_control({**cs, "speed_x": 100.0, "track_pos": 0.5, "track": clear_track}, NORMAL)
    out_apex = compute_control({**cs, "speed_x": 100.0, "track_pos": 0.6, "track": clear_track}, NORMAL)
    assert "(accel 0.000)" not in out_apex, \
        f"FAIL: apex-free band must not be cut by track-hold: {out_apex}"
    _reset_driver_state()
    print("compute_control track-hold throttle cut (bt-inspired) ... OK")

    # PIT strategy on a track with no pit-lane data (synthetic `cs` has none)
    # must never touch `meta` — meta=1 means RACE RESTART to scr_server
    # (CarControl::META_RESTART), not "pit please". The old code reused meta
    # as a fake pit signal and would have restarted the race the moment PIT
    # strategy slowed the car below 10 km/h; this is the regression test.
    _reset_driver_state()
    cs_pit = {**cs, "speed_x": 5.0, "rpm": 800.0, "gear": 1}
    cc_pit = compute_control(cs_pit, PIT)
    assert "(meta 1)" not in cc_pit, f"FAIL: PIT must never send meta=1 (race restart): {cc_pit}"
    print(f"compute_control PIT (no pit-lane data) ... OK  →  {cc_pit}")

    # ---- pit lane docking (bt parity — see pit.cpp) ----------------------
    _reset_driver_state()
    # Realistic scale (forza): 11 m base track, pit box ~19 m off centreline
    # -> target track_pos ~3.45, several widths past the normal +-1 range.
    # NOT a bug (see the 2026-08-09 comment on pit_box_offset in
    # _pit_control) -- the tests below are written against that real target.
    _TEST_PIT_TARGET = -19.0 / (11.0 / 2.0)
    # dist_from_start=125 -> s_now=75, safely inside (s_start=50, s_end=100)
    # away from either boundary.
    pit_cs = {
        **cs, "track_pos": 0.0, "angle": 0.0, "speed_x": 60.0,
        "dist_from_start": 125.0, "track_length": 2000.0,
        "pit_entry": 50.0, "pit_start": 100.0, "pit_end": 150.0,
        "pit_exit": 200.0, "pit_speed_limit": 60.0, "pit_side": 1,
        "pit_box_offset": -19.0, "seg_width": 11.0, "in_pit_stop": 0,
    }

    # 2026-08-10 (bt parity, pit.cpp Pit::setPitstop): docking can now only
    # be newly ARMED while the car is outside the raw [pit_entry, pit_exit]
    # range (see compute_control's `in_pit_range` gate) -- pit_cs's
    # dist_from_start=125 (s_now=75) sits inside that range, so tests that
    # want to exercise the stop zone need to arm the latch first via one
    # call from the lead-in, exactly like a real approach would. dist=20.0
    # is 30 m before pit_entry=50.0 (_pit_spline_coord wraps it to -30
    # through the 2000 m track length), comfortably inside
    # _PIT_DOCK_LEAD_DIST's 40 m lead-in window (NOT _PIT_APPROACH_DIST's
    # 150 m -- that constant now only gates when the STRATEGY layer commits
    # to PIT, not when this rigid docking law takes over from ordinary
    # driving; see the comment in compute_control).
    def _arm_pit_docking():
        compute_control({**pit_cs, "dist_from_start": 20.0, "speed_x": 90.0}, PIT)

    _reset_driver_state()
    _arm_pit_docking()
    cc_dock = compute_control(pit_cs, PIT)
    assert "(pitRequest 1)" in cc_dock, f"FAIL: docking must assert pitRequest: {cc_dock}"
    assert "(brake 0.000)" not in cc_dock, \
        f"FAIL: must brake toward a stop inside the box: {cc_dock}"
    print(f"compute_control pit docking (braking)  ... OK  →  {cc_dock}")

    # 2026-08-09: must already be braking toward creep speed DURING the
    # approach (pit_entry..pit_start), not just once inside the stop zone —
    # verified live: waiting until the stop zone left too little of it to
    # both bleed off speed AND swing tpos onto a target several
    # track-widths away, so the car blew straight through never converging.
    # dist_from_start=75 -> s_now=25, mid-way through the [0, s_start=50]
    # approach; at 90 km/h (well above the pit limit) it must already be
    # braking here, before ever reaching the stop zone. This is itself
    # inside the lead-in window relative to entry, so no separate arming
    # call is needed -- strategy==PIT arms it directly on this call.
    _reset_driver_state()
    cs_approach = {**pit_cs, "dist_from_start": 75.0, "speed_x": 90.0}
    cc_approach = compute_control(cs_approach, PIT)
    assert "(brake 0.000)" not in cc_approach, \
        f"FAIL: must already be bleeding speed during the approach, not just inside the stop zone: {cc_approach}"
    print(f"compute_control pit approach deceleration starts before the stop zone (regression) ... OK  →  {cc_approach}")
    _reset_driver_state()

    # Same distFromStart range, but strategy is NOT PIT: on most tracks the
    # pit lane runs alongside part of the main straight, so a normally
    # racing car passes through this same range every lap — must not dock.
    _reset_driver_state()
    cc_pass = compute_control({**pit_cs, "speed_x": 200.0}, NORMAL)
    assert "(pitRequest 1)" not in cc_pass, \
        f"FAIL: must not dock without PIT strategy: {cc_pass}"
    print("compute_control pit lane pass-through (no dock without PIT) ... OK")

    # 2026-08-10: strategy IS PIT, but the car is a fresh (unlatched) car
    # already sitting inside the raw pit-lane range with no prior lead-in --
    # e.g. a standing start whose grid position happens to read as already
    # past pit_start. Verified live: the old code armed docking from in-lane
    # presence alone and latched target_speed=0 before the car ever moved.
    # Must NOT dock here; only a genuine approach (via the lead-in) or an
    # already-latched visit may.
    _reset_driver_state()
    cc_stuck_start = compute_control(pit_cs, PIT)
    assert "(pitRequest 1)" not in cc_stuck_start, \
        f"FAIL: must not spuriously arm docking from a fresh car already inside the pit range: {cc_stuck_start}"
    assert _dbg.get("mode") != "pit", \
        f"FAIL: must not spuriously arm docking from a fresh car already inside the pit range: mode={_dbg.get('mode')}"
    print(f"compute_control pit no spurious arm from inside the range (regression) ... OK  →  {cc_stuck_start}")
    _reset_driver_state()

    # Once the engine reports inPitStop=1 (the stop was actually captured and
    # serviced), stop asking and resume toward pit_exit instead of
    # re-braking to a stop that is already done.
    _reset_driver_state()
    _arm_pit_docking()
    cc_arrive = compute_control({**pit_cs, "speed_x": 0.0, "in_pit_stop": 0}, PIT)
    assert "(pitRequest 1)" in cc_arrive, f"FAIL: must request pit stop on arrival: {cc_arrive}"
    cc_serviced = compute_control({**pit_cs, "speed_x": 0.0, "in_pit_stop": 1}, PIT)
    assert "(pitRequest 0)" in cc_serviced, \
        f"FAIL: must release pitRequest once serviced: {cc_serviced}"
    print(f"compute_control pit release after service ... OK  →  {cc_serviced}")
    _reset_driver_state()

    # 2026-08-10 (bt parity, driver.cpp filterBPit "Stop in the pit"): the
    # stop-zone target is unconditionally 0 now regardless of lateral/heading
    # alignment — bt never held a creep-speed floor here, it just
    # brake-distance-computed toward a full stop. A car above 0 target must
    # be braking whether or not it's aligned yet; the old version of this
    # test asserted the opposite (creep while misaligned), which is exactly
    # the behaviour that let a misaligned car cruise the whole stop zone at
    # 8 km/h and sail through without ever tripping the engine's own <1 m/s
    # capture gate — see _PIT_CREEP_KMH's comment above.
    _reset_driver_state()
    _arm_pit_docking()
    cs_misaligned = {**pit_cs, "speed_x": 4.0, "track_pos": _TEST_PIT_TARGET, "angle": 0.5}
    cc_misaligned = compute_control(cs_misaligned, PIT)
    assert "(accel 0.000)" in cc_misaligned, \
        f"FAIL: misaligned car above the stop target should still be braking, not creeping: {cc_misaligned}"
    print(f"compute_control pit hard stop while misaligned (regression) ... OK  →  {cc_misaligned}")
    _reset_driver_state()

    # Aligned case: same expectation — confirms alignment no longer changes
    # the speed target at all, only feeds the pit_aligned debug field.
    _reset_driver_state()
    _arm_pit_docking()
    cs_aligned = {**pit_cs, "speed_x": 4.0, "track_pos": _TEST_PIT_TARGET, "angle": 0.0}
    cc_aligned = compute_control(cs_aligned, PIT)
    assert "(accel 0.000)" in cc_aligned, \
        f"FAIL: aligned car above the true stop target should be braking, not accelerating: {cc_aligned}"
    print(f"compute_control pit genuine stop once aligned (regression) ... OK  →  {cc_aligned}")
    _reset_driver_state()

    # 2026-08-09: a car correctly stopped dead in the pit box (waiting on
    # inPitStop) must NOT be mistaken for a stuck/crashed car — verified
    # live: the no-progress watchdog and stuck-jam burst check both fired
    # after a few seconds of a legitimate pit stop, yanking control into
    # stabilize/burst and fighting the pit-lane code for it indefinitely.
    # Hold at dead stop in the box for well past _NO_PROGRESS_FRAMES.
    _reset_driver_state()
    _arm_pit_docking()
    cs_dock_wait = {**pit_cs, "speed_x": 0.0, "track_pos": _TEST_PIT_TARGET, "in_pit_stop": 0}
    cc_dock_wait = ""
    for _ in range(_NO_PROGRESS_FRAMES + 50):
        cc_dock_wait = compute_control(cs_dock_wait, PIT)
    assert "(pitRequest 1)" in cc_dock_wait, \
        f"FAIL: must keep requesting service the whole time it waits: {cc_dock_wait}"
    assert _dbg.get("mode") == "pit", \
        f"FAIL: no-progress watchdog must not hijack a car correctly stopped in the pit box: {_dbg.get('mode')}"
    print(f"compute_control pit-docking immune to no-progress watchdog (regression) ... OK  →  {cc_dock_wait}")
    _reset_driver_state()

    # _pit_target_tpos: ease in / hold / ease out shape sanity. s_lead=0.0
    # here reproduces the pre-2026-08-10 behaviour exactly (ease starting
    # right at pit_entry, no pre-entry lead-in).
    assert _pit_target_tpos(0.0, 0.0, 50.0, 100.0, 150.0, -0.6) == 0.0
    assert abs(_pit_target_tpos(25.0, 0.0, 50.0, 100.0, 150.0, -0.6) - (-0.3)) < 1e-9
    assert _pit_target_tpos(75.0, 0.0, 50.0, 100.0, 150.0, -0.6) == -0.6
    assert abs(_pit_target_tpos(125.0, 0.0, 50.0, 100.0, 150.0, -0.6) - (-0.3)) < 1e-9
    assert abs(_pit_target_tpos(150.0, 0.0, 50.0, 100.0, 150.0, -0.6)) < 1e-9
    print("_pit_target_tpos shape ... OK")

    # 2026-08-10: with a negative s_lead (pre-entry lead-in), the ease must
    # already be under way before s_now reaches 0 (pit_entry) -- this is the
    # whole point of the fix (more distance to converge onto the box offset
    # than [pit_entry, pit_start] alone provides).
    assert _pit_target_tpos(-150.0, -150.0, 50.0, 100.0, 150.0, -0.6) == 0.0, \
        "FAIL: at the start of the lead-in, target should still be the racing line"
    half = _pit_target_tpos(-100.0, -150.0, 50.0, 100.0, 150.0, -0.6)
    assert -0.6 < half < 0.0, \
        f"FAIL: partway through the lead-in, target should already be easing off the racing line: {half}"
    print("_pit_target_tpos lead-in (regression) ... OK")

    # ---- P1: pre-race map lookahead --------------------------------------
    if _TRACK_MODEL_AVAILABLE:
        from track_model import Segment, TrackModel
        tm = TrackModel([Segment("str", 600.0, 0.0, 0.0),
                         Segment("rgt", math.pi * 30.0, 30.0, 30.0),
                         Segment("str", 400.0, 0.0, 0.0)],
                        width=12.0, name="unit-map")
        set_track_model(tm)
        # sensors say clear straight, but the MAP knows a hairpin starts in
        # 20 m — the map cap must beat the reactive target and brake the car
        _reset_driver_state()
        cs_map = {**cs, "speed_x": 200.0, "gear": 6, "dist_from_start": 580.0,
                  "track": [200.0] * 19}
        out_map = compute_control(cs_map, NORMAL)
        assert "(accel 0.000)" in out_map, f"FAIL map cap must lift throttle: {out_map}"
        assert "(brake 0.000)" not in out_map, f"FAIL map cap must brake: {out_map}"
        # far from any corner the map must NOT interfere with a clear straight
        _reset_driver_state()
        out_far = compute_control({**cs_map, "speed_x": 80.0,
                                   "dist_from_start": 100.0}, NORMAL)
        assert "(accel 1.000)" in out_far, f"FAIL map must not bind on open straight: {out_far}"
        # no dist_from_start in the state → map silently skipped
        _reset_driver_state()
        out_nod = compute_control({**cs, "speed_x": 80.0}, NORMAL)
        assert "(accel 1.000)" in out_nod, f"FAIL missing dist must skip map: {out_nod}"
        # A+ entry line: right-hander 150 m ahead (beyond sensor braking need,
        # inside the entry zone) → drift LEFT (positive steer) to take the
        # entry from the outside.  The setpoint is slew-limited (weave fix),
        # so it needs ~0.8 s of frames to build up.
        _reset_driver_state()
        cs_line = {**cs, "speed_x": 80.0, "dist_from_start": 450.0,
                   "track": [200.0] * 19}
        out_line = ""
        for _ in range(40):
            out_line = compute_control(cs_line, NORMAL)
        m_steer = re.search(r"\(steer ([-0-9.]+)\)", out_line)
        assert m_steer and float(m_steer.group(1)) > 0.05, \
            f"FAIL entry bias must steer toward the outside: {out_line}"
        print("compute_control map lookahead + entry line (P1/A+) ... OK")

        # Brake-point mode: powering toward a mapped corner must hold FULL
        # throttle up to the braking curve (the old easing band lifted
        # ~15 km/h early and coasted the whole approach)…
        _reset_driver_state()
        out_bp = compute_control({**cs_line, "speed_x": 200.0, "gear": 5}, NORMAL)
        assert "(accel 1.000)" in out_bp, \
            f"FAIL: must power flat-out to the brake point: {out_bp}"
        # …and just below the curve sits a small neutral gap (no sawtooth).
        _reset_driver_state()
        out_coast = compute_control({**cs_line, "speed_x": 250.0, "gear": 5}, NORMAL)
        assert "(accel 0.000)" in out_coast and "(brake 0.000)" in out_coast, \
            f"FAIL: neutral gap just below the curve: {out_coast}"
        print("compute_control map brake-point mode ... OK")

        # TRUST mode: the sensors misread a grazed straight (sight 150 →
        # reactive lift at 200 km/h) but the map knows the next corner is
        # 500 m away.  Uncalibrated → gate 1 must veto; calibrated → the
        # false lift is cancelled and the car stays at full throttle.
        cs_trust = {**cs, "speed_x": 200.0, "gear": 5,
                    "dist_from_start": 100.0, "track": [150.0] * 19}
        _reset_driver_state()
        out_raw = compute_control(cs_trust, NORMAL)      # tm not calibrated yet
        assert _dbg["trust"] == 0.0, "FAIL gate 1: uncalibrated map trusted"
        assert "(accel 1.000)" not in out_raw, \
            f"FAIL: sensor lift expected without trust: {out_raw}"
        tm.calibrate(tm.lap_length)                      # practice lap done
        _reset_driver_state()
        out_tr = compute_control(cs_trust, NORMAL)
        assert _dbg["trust"] == 1.0, "FAIL: all gates pass → must trust"
        assert "(accel 1.000)" in out_tr, \
            f"FAIL: trust must cancel the false lift: {out_tr}"
        # gate 3: an opponent 50 m ahead vetoes trust (map can't see traffic)
        opps_ahead = [200.0] * 36
        opps_ahead[17] = 50.0
        _reset_driver_state()
        compute_control({**cs_trust, "opponents": opps_ahead}, NORMAL)
        assert _dbg["trust"] == 0.0, "FAIL gate 3: traffic must veto trust"
        # gate 5: slow corners are NEVER trusted — the map still binds min()
        _reset_driver_state()
        out_slow = compute_control({**cs_trust, "dist_from_start": 580.0,
                                    "track": [200.0] * 19}, NORMAL)
        assert _dbg["trust"] == 0.0, "FAIL gate 5: slow corner trusted"
        assert "(brake 0.000)" not in out_slow, \
            f"FAIL: map must still brake for the hairpin: {out_slow}"
        tm.real_lap = None
        set_track_model(None)
        _reset_driver_state()
        print("compute_control map trust mode (5 gates) ... OK")
    else:
        print("compute_control map lookahead (P1) ... SKIPPED (track_model.py not found)")

    # ---- physics stopping-distance override (bt-inspired, sensor-only) -----
    # Realistic (non-slipping) wheel speeds so ABS doesn't zero the brake out
    # from under this test — it isn't what's being tested here.
    def _wheel_vel(speed_kmh: float) -> list[float]:
        return [speed_kmh / 3.6 / _WHEEL_RADIUS] * 4

    # A tight-looking corner (sight=25 m, all of the range(7,12) beams short
    # so the max() sight calc can't be fooled by one long outlier) at 150
    # km/h: the plain proportional gain alone computes brake=0.827 (excess
    # ratio isn't enormous), but _brake_dist(150, ~115.8, 1200kg) needs
    # ~26.0 m — MORE than the 25 m actually visible — so the override must
    # step in and force full brake regardless of what the gentler
    # proportional number said.
    _reset_driver_state()
    tight = [40.0] * 7 + [25.0] * 5 + [40.0] * 7
    out_tight = compute_control({**cs, "speed_x": 150.0, "track": tight,
                                 "wheel_spin_vel": _wheel_vel(150.0)}, NORMAL)
    assert "(brake 1.000)" in out_tight, \
        f"FAIL: braking distance exceeding sight must force full brake: {out_tight}"
    _reset_driver_state()
    # Same shape corner, more sight (40 m) at 160 km/h: needed distance
    # (~23.8 m) comfortably fits in the 40 m visible, so this must NOT be
    # overridden — brake stays at whatever the plain proportional gain
    # alone computes (a genuine partial value, not saturated at 1.0),
    # proving the override is actually gated on distance and not just
    # always forcing full brake on any corner.
    open_corner = [40.0] * 19
    out_open = compute_control({**cs, "speed_x": 160.0, "track": open_corner,
                                "wheel_spin_vel": _wheel_vel(160.0)}, NORMAL)
    m = re.search(r"\(brake ([-0-9.]+)\)", out_open)
    assert m and 0.0 < float(m.group(1)) < 1.0, \
        f"FAIL: ample sight must leave the plain proportional (partial) brake alone: {out_open}"
    _reset_driver_state()
    print("compute_control physics stopping-distance override (bt-inspired) ... OK")

    # ---- side-traffic avoidance ---------------------------------------------
    # Straight road, dead ahead clear, opponent tight alongside on the right
    # (index 22, well inside _AVOID_RIGHT) → must steer LEFT (positive) to
    # open a gap, same convention as the map entry-line bias test above.
    _reset_driver_state()
    opps_right = [200.0] * 36
    opps_right[22] = 3.0
    out_avoid_r = compute_control({**cs, "speed_x": 80.0, "opponents": opps_right}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_avoid_r)
    assert m and float(m.group(1)) > 0.0, \
        f"FAIL: car tight on the right must steer left to clear it: {out_avoid_r}"

    # Mirror: opponent tight alongside on the left → steer RIGHT (negative).
    _reset_driver_state()
    opps_left = [200.0] * 36
    opps_left[13] = 3.0
    out_avoid_l = compute_control({**cs, "speed_x": 80.0, "opponents": opps_left}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_avoid_l)
    assert m and float(m.group(1)) < 0.0, \
        f"FAIL: car tight on the left must steer right to clear it: {out_avoid_l}"

    # Regression: dead-ahead (index 18, 0°) used to fall in the gap between
    # the two windows and read as clear on both sides — invisible.  It must
    # now register on whichever side owns the boundary (see the constants'
    # comment) and trigger the same nudge as any other close-front car.
    _reset_driver_state()
    opps_ahead2 = [200.0] * 36
    opps_ahead2[18] = 3.0
    out_dead_ahead = compute_control({**cs, "speed_x": 80.0, "opponents": opps_ahead2}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_dead_ahead)
    assert m and abs(float(m.group(1))) > 0.02, \
        f"FAIL: dead-ahead opponent must not fall into a blind gap: {out_dead_ahead}"
    _reset_driver_state()
    print("compute_control side-traffic avoidance ... OK")

    # 2026-08-08: convergence gate (bt-inspired, see _SIDE_CLOSE_RATE_MIN
    # above and bt's Driver::filterSColl diffangle*sideDist check). Same
    # final gap (8 m), compare avoid's converged magnitude when the gap got
    # there by actively closing vs when it was stable there the whole time
    # — a neighbour that isn't actually converging must not get the same
    # steady push as a genuine emergency.
    _reset_driver_state()
    opps_converging = [200.0] * 36
    for i in range(60):
        opps_converging[13] = 14.0 - i * 0.1   # closing 14m -> ~8m, steadily,
                                                # the whole 60 ticks (no early
                                                # plateau to let the rate decay)
        compute_control({**cs, "speed_x": 100.0, "track_pos": 0.0,
                         "track": [200.0] * 19, "opponents": opps_converging}, NORMAL)
    avoid_converging = _avoid_lp
    _reset_driver_state()
    opps_stable = [200.0] * 36
    opps_stable[13] = 8.0
    for _ in range(60):
        compute_control({**cs, "speed_x": 100.0, "track_pos": 0.0,
                         "track": [200.0] * 19, "opponents": opps_stable}, NORMAL)
    avoid_stable = _avoid_lp
    assert abs(avoid_converging) > 0.06, \
        f"FAIL: actively closing must keep avoid near full strength: {avoid_converging:.3f}"
    assert abs(avoid_stable) < abs(avoid_converging) * 0.6, \
        f"FAIL: a stable (non-closing) gap must be pulled toward the reduced floor: " \
        f"stable={avoid_stable:.3f} converging={avoid_converging:.3f}"
    _reset_driver_state()
    print("compute_control avoid convergence gate (bt-inspired) ... OK")

    # 2026-08-08: bt-inspired room taper on `avoid` (see the _EDGE_FREE-style
    # comment at the room_taper computation). Verified live: a persistent
    # ~6-9 m side gap that never closed or opened let avoid and barrier
    # settle into a near-equilibrium rub AT the track edge (tpos crept from
    # -0.30 to -0.97 over ~150 ticks and stayed there) instead of resolving.
    # 2026-08-09: the convergence gate above now zeroes `avoid` on its own
    # once a side gap is confirmed stable (floor dropped 0.4 -> 0.0), so a
    # permanently-static opponent like the old opps_room no longer reaches
    # meaningful avoid authority at ALL — there is nothing left for room
    # taper to visibly taper. Use an actively, steadily closing opponent
    # instead (same shape as the convergence-gate test above) so the
    # convergence gate stays satisfied (real closing rate the whole run) and
    # this test isolates room taper's own effect: same closing gap, compare
    # avoid's converged magnitude at track centre vs already almost at the
    # edge avoid itself pushes toward.
    _reset_driver_state()
    opps_room = [200.0] * 36
    for i in range(60):
        opps_room[13] = 14.0 - i * 0.1   # closing 14m -> ~8m, steadily
        compute_control({**cs, "speed_x": 100.0, "track_pos": 0.0,
                         "track": [200.0] * 19, "opponents": opps_room}, NORMAL)
    avoid_centre = _avoid_lp
    _reset_driver_state()
    opps_room = [200.0] * 36
    for i in range(60):
        opps_room[13] = 14.0 - i * 0.1
        compute_control({**cs, "speed_x": 100.0, "track_pos": -0.95,
                         "track": [200.0] * 19, "opponents": opps_room}, NORMAL)
    avoid_edge = _avoid_lp
    assert avoid_centre < -0.05, \
        f"FAIL: avoid should push meaningfully with room to spare: {avoid_centre:.3f}"
    assert abs(avoid_edge) < abs(avoid_centre) * 0.5, \
        f"FAIL: avoid must taper sharply once already near the edge it pushes toward: " \
        f"centre={avoid_centre:.3f} edge={avoid_edge:.3f}"
    _reset_driver_state()
    print("compute_control avoid room taper near the edge (bt-inspired) ... OK")

    # 2026-08-08: standoff breaker (bt-inspired, see _STANDOFF_TIME above and
    # bt's OPP_LETPASS). A side gap that stays inside _AVOID_DIST briefly
    # only gets the passive, graduated _SIDE_EASE_GAIN — but sustained
    # closeness (a neighbour that never resolves) must escalate to the much
    # stronger _STANDOFF_EASE_GAIN once _STANDOFF_TIME has elapsed, rather
    # than continuing the same passive nudge indefinitely (verified live: a
    # neighbour holding a stable ~6-9 m gap for an extended stretch never
    # let either car pull away — see conversation history).
    _reset_driver_state()
    opps_standoff = [200.0] * 36
    opps_standoff[13] = 8.0   # left_gap = 8 m, steady, inside _AVOID_DIST
    cs_standoff = {**cs, "speed_x": 150.0, "dist_raced": 1000.0,
                   "opponents": opps_standoff}
    # Just under the threshold: still the passive ease.
    for _ in range(int(_STANDOFF_TIME / _TICK_S) - 10):
        compute_control(cs_standoff, NORMAL)
    assert _dbg["why"] == "side-close", \
        f"FAIL: should still be the passive ease before the timer expires: {_dbg}"
    # Past the threshold: escalates to the decisive yield.
    for _ in range(20):
        compute_control(cs_standoff, NORMAL)
    assert _dbg["why"] == "standoff-yield", \
        f"FAIL: sustained closeness must escalate to the standoff breaker: {_dbg}"
    _reset_driver_state()
    print("compute_control standoff breaker (bt-inspired) ... OK")

    # ---- start-of-race caution ------------------------------------------
    # A 15 m gap is fine at racing speed (outside the normal 10 m
    # _AVOID_DIST) — but the whole grid launches together into one
    # narrowing line, closer together than any point in open racing, and a
    # real race logged exactly this: damage 0 → 1247 within the first
    # ~60 m raced.  While dist_raced is small the wider _START_AVOID_DIST
    # must catch a gap the normal-racing check would ignore.
    _reset_driver_state()
    opps_launch = [200.0] * 36
    opps_launch[22] = 15.0
    out_calm = compute_control({**cs, "speed_x": 80.0, "opponents": opps_launch,
                                "dist_raced": 1000.0}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_calm)
    assert m and float(m.group(1)) == 0.0, \
        f"FAIL: 15 m gap must not trigger avoidance once racing normally: {out_calm}"
    _reset_driver_state()
    out_launch = compute_control({**cs, "speed_x": 80.0, "opponents": opps_launch,
                                  "dist_raced": 50.0}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_launch)
    assert m and float(m.group(1)) > 0.0, \
        f"FAIL: same 15 m gap must trigger avoidance during the launch: {out_launch}"
    # 2026-08-08: throttle is NOT capped during the launch anymore — see
    # _START_ACCEL_CAP history. bt and the other built-in bots have no
    # launch throttle cap, so holding ours back just made it the slow car
    # in an otherwise flat-out field. The wider _START_AVOID_DIST/_GAIN
    # (asserted above) is the actual safety net and is unaffected.
    _reset_driver_state()
    out_launch_accel = compute_control({**cs, "speed_x": 80.0, "dist_raced": 50.0}, NORMAL)
    assert "(accel 1.000)" in out_launch_accel, \
        f"FAIL: launch throttle must not be capped (matches bt): {out_launch_accel}"
    _reset_driver_state()
    print("compute_control start-of-race caution ... OK")

    # 2026-08-08: launch clutch ramp (see _CLUTCH_RAMP_TIME) — borrowed from
    # bt's Driver::getClutch(). The moment the SIM reports 1st gear during
    # the launch window, clutch must start near full slip (not the instant
    # full-lock 0.0 that crashed rpm 9611->956 live) and decay to 0.0 over
    # _CLUTCH_RAMP_TIME as the car gets moving.
    _reset_driver_state()
    cs_launch1 = {**cs, "speed_x": 0.0, "gear": 1, "rpm": 9500.0, "dist_raced": 10.0}
    out_launch1 = compute_control(cs_launch1, NORMAL)
    m = re.search(r"\(clutch ([-0-9.]+)\)", out_launch1)
    assert m and float(m.group(1)) > 0.9, \
        f"FAIL: clutch must start near full slip the instant 1st gear connects: {out_launch1}"
    # After _CLUTCH_RAMP_TIME has elapsed it must be fully engaged again.
    out_launch_late = ""
    for _ in range(int(_CLUTCH_RAMP_TIME / _TICK_S) + 5):
        out_launch_late = compute_control(cs_launch1, NORMAL)
    assert "(clutch 0.000)" in out_launch_late, \
        f"FAIL: clutch must be fully engaged once the ramp finishes: {out_launch_late}"
    _reset_driver_state()
    # Already past 1st gear → no ramp needed, straight to fully engaged.
    out_launch_gear2 = compute_control({**cs, "speed_x": 40.0, "gear": 2,
                                        "dist_raced": 10.0}, NORMAL)
    assert "(clutch 0.000)" in out_launch_gear2, \
        f"FAIL: clutch ramp must not apply past 1st gear: {out_launch_gear2}"
    _reset_driver_state()
    # Outside the launch window → no ramp even in 1st gear (e.g. a mid-race
    # 1st-gear hairpin exit must not feather the clutch).
    out_midrace1 = compute_control({**cs, "speed_x": 0.0, "gear": 1,
                                    "dist_raced": 1000.0}, NORMAL)
    assert "(clutch 0.000)" in out_midrace1, \
        f"FAIL: clutch ramp must be scoped to the launch window: {out_midrace1}"
    _reset_driver_state()
    print("compute_control launch clutch ramp (bt-inspired) ... OK")

    # ---- front-opponent following/overtake -----------------------------------
    # Slower car dead ahead, actually being CAUGHT (gap closing ~12.5 m/s, well
    # above _OVERTAKE_CLOSE_RATE_MIN), inside the overtake trigger and outside
    # the tight brake window, with the right blocked and the left clear must
    # ease the car toward the open (left, tpos +) side.  Needs repeated ticks
    # with a genuinely shrinking gap: the closing-rate EMA needs a few ticks to
    # rise above the gate, then the line setpoint slews toward the bias exactly
    # like the map's own entry-line bias.
    _reset_driver_state()
    out_pass_l = ""
    for i in range(60):
        opps_pass_l = [200.0] * 36
        opps_pass_l[18] = max(25.0, 40.0 - i * 0.25)   # 40 m -> 25 m over 60 ticks
        opps_pass_l[22] = 15.0     # right side congested (not tight enough for _AVOID_DIST)
        out_pass_l = compute_control({**cs, "speed_x": 80.0, "opponents": opps_pass_l}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_pass_l)
    assert m and float(m.group(1)) > 0.05, \
        f"FAIL: closing on a slower car with the left open must ease left to pass: {out_pass_l}"

    # Mirror: left blocked, right open, closing → ease right (tpos −, negative steer).
    _reset_driver_state()
    out_pass_r = ""
    for i in range(60):
        opps_pass_r = [200.0] * 36
        opps_pass_r[18] = max(25.0, 40.0 - i * 0.25)
        opps_pass_r[13] = 15.0     # left side congested
        out_pass_r = compute_control({**cs, "speed_x": 80.0, "opponents": opps_pass_r}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_pass_r)
    assert m and float(m.group(1)) < -0.05, \
        f"FAIL: closing on a slower car with the right open must ease right to pass: {out_pass_r}"
    print("compute_control front-opponent overtake line bias ... OK")

    # 2026-08-08: matched-pace train — front_gap holds constant (not closing)
    # even though a side is open. Before the closing-rate gate this got the
    # exact same line-bias treatment as a car being genuinely caught, drifting
    # off the racing line for zero passing benefit — there is nothing to pass
    # at matched pace. Same setup as the passing case above, just a static gap
    # instead of a shrinking one.
    _reset_driver_state()
    opps_train = [200.0] * 36
    opps_train[18] = 25.0
    opps_train[22] = 15.0
    out_train = ""
    for _ in range(60):
        out_train = compute_control({**cs, "speed_x": 80.0, "opponents": opps_train}, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_train)
    assert m and abs(float(m.group(1))) < 0.05, \
        f"FAIL: a constant (non-closing) gap must not trigger the overtake line bias: {out_train}"
    _reset_driver_state()
    print("compute_control overtake bias requires an actually-closing gap ... OK")

    # 2026-08-08 (live capture): a car's bearing crossing the _FRONT_CONE
    # boundary makes front_gap itself jump discontinuously even though the
    # car's true distance barely changed (live log: ogap 23.7 -> 7.5 m in one
    # tick while right_gap, a different sensor window on the same car, only
    # moved 7.3 -> 7.5 m). The naive derivative read that as a ~970 m/s
    # closing spike. The sanity clamp must reject it, not smooth it in.
    _reset_driver_state()
    opps_conejump = [200.0] * 36
    opps_conejump[18] = 24.0    # first tick: nothing yet inside the front cone
    compute_control({**cs, "speed_x": 80.0, "opponents": opps_conejump}, NORMAL)
    opps_conejump[18] = 7.5     # next tick: same (nearby) car now inside it
    compute_control({**cs, "speed_x": 80.0, "opponents": opps_conejump}, NORMAL)
    assert abs(_dbg["close_rate"]) < 5.0, \
        f"FAIL: a cone-boundary jump must not read as a real closing spike: {_dbg['close_rate']}"
    _reset_driver_state()
    print("compute_control overtake closing-rate rejects cone-boundary jumps ... OK")

    # 2026-08-08: borrowed from TORCS's built-in "bt" robot (see
    # src/drivers/bt/driver.cpp Driver::getOffset()) — when a car dead ahead
    # is closing but neither side reads clearly roomier (left_gap == right_gap
    # here, both default 200), commit to the inside of the next known corner
    # instead of sitting neutral. Corner starts at 350 m — outside line_tpos's
    # own 250 m entry zone (so the map's out-in-out bias stays silent and
    # doesn't pre-empt this test) but inside the 400 m tiebreak horizon.
    if _TRACK_MODEL_AVAILABLE:
        from track_model import Segment, TrackModel
        tm_corner = TrackModel([Segment("str", 350.0, 0.0, 0.0),
                                 Segment("lft", 60.0, 40.0, 40.0),
                                 Segment("str", 400.0, 0.0, 0.0)],
                                width=12.0, name="tiebreak-map")
        set_track_model(tm_corner)
        _reset_driver_state()
        out_tiebreak = ""
        for i in range(60):
            opps_tiebreak = [200.0] * 36
            opps_tiebreak[18] = max(25.0, 40.0 - i * 0.25)   # closing, dead ahead
            out_tiebreak = compute_control(
                {**cs, "speed_x": 80.0, "dist_from_start": 0.0,
                 "opponents": opps_tiebreak}, NORMAL)
        m = re.search(r"\(steer ([-0-9.]+)\)", out_tiebreak)
        assert m and float(m.group(1)) > 0.05, \
            f"FAIL: ambiguous room + left-hander ahead must bias toward the inside (left): {out_tiebreak}"
        set_track_model(None)
        _reset_driver_state()
        print("compute_control overtake tiebreak uses next-corner direction (bt-inspired) ... OK")

    # ---- BLOCK: position-defence against a car closing in from behind -----
    # Threat on the left (index 13, within _AVOID_LEFT and outside the front
    # cone so it can't be mistaken for the overtake-ahead bias above) → ease
    # LEFT (tpos +, positive steer) to hold the line against them.
    # Gap deliberately kept OUTSIDE _AVOID_DIST (14 m): inside it, the
    # existing collision-avoidance nudge (`avoid`, opposite sign — steers
    # AWAY from a close side) fights and largely cancels this bias, which is
    # correct (safety wins at genuine contact range) but means this specific
    # assertion needs a gap where block is the only active term.
    _reset_driver_state()
    opps_block_l = [200.0] * 36
    opps_block_l[13] = 18.0
    cs_block_l = {**cs, "speed_x": 80.0, "opponents": opps_block_l}
    out_block_l = ""
    for _ in range(40):
        out_block_l = compute_control(cs_block_l, BLOCK)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_block_l)
    assert m and float(m.group(1)) > 0.02, \
        f"FAIL: threat on the left, must ease left to block: {out_block_l}"

    # Mirror: threat on the right (index 22) → ease right (tpos −, negative steer).
    _reset_driver_state()
    opps_block_r = [200.0] * 36
    opps_block_r[22] = 18.0
    cs_block_r = {**cs, "speed_x": 80.0, "opponents": opps_block_r}
    out_block_r = ""
    for _ in range(40):
        out_block_r = compute_control(cs_block_r, BLOCK)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_block_r)
    assert m and float(m.group(1)) < -0.02, \
        f"FAIL: threat on the right, must ease right to block: {out_block_r}"

    # Same threat, but strategy is NORMAL, not BLOCK → must NOT bias the
    # line — this behaviour only exists inside the dedicated strategy.
    _reset_driver_state()
    out_block_off = ""
    for _ in range(40):
        out_block_off = compute_control(cs_block_l, NORMAL)
    m = re.search(r"\(steer ([-0-9.]+)\)", out_block_off)
    assert m and abs(float(m.group(1))) < 0.05, \
        f"FAIL: BLOCK bias must not leak into other strategies: {out_block_off}"
    _reset_driver_state()
    print("compute_control BLOCK position-defence line bias ... OK")

    # Follow cap: opponent almost touching (5 m ahead) AND boxed in on both
    # sides (nowhere to ease to) must brake hard — genuinely nowhere else to
    # shed the closing gap but speed.
    _reset_driver_state()
    opps_boxed = [200.0] * 36
    opps_boxed[18] = 5.0    # tight gap dead ahead
    opps_boxed[13] = 8.0    # left also blocked
    opps_boxed[22] = 8.0    # right also blocked
    out_follow = compute_control({**cs, "speed_x": 200.0, "opponents": opps_boxed}, NORMAL)
    assert _dbg["opp_bound"] == 1.0, f"FAIL: boxed-in 5 m gap must bind the follow cap: {_dbg}"
    m = re.search(r"\(brake ([-0-9.]+)\)", out_follow)
    assert m and float(m.group(1)) > 0.5, \
        f"FAIL: must brake hard when boxed in with a car 5 m ahead: {out_follow}"

    # Same tight 5 m gap, but the right side is clear → must NOT brake.  This
    # is the regression that shipped first: braking here cost whole seconds a
    # lap in ordinary traffic and dropped the car from the podium to last,
    # because a side was almost always open and the cap fired anyway.
    _reset_driver_state()
    opps_open_side = [200.0] * 36
    opps_open_side[18] = 5.0
    out_escape = compute_control({**cs, "speed_x": 200.0, "opponents": opps_open_side}, NORMAL)
    assert _dbg["opp_bound"] == 0.0, \
        f"FAIL: an open side must be used instead of braking: {_dbg}"
    m = re.search(r"\(brake ([-0-9.]+)\)", out_escape)
    assert m and float(m.group(1)) < 0.1, \
        f"FAIL: must not brake when a side is clearly open: {out_escape}"

    # Outside the tight brake window the cap must not bind regardless of
    # sides — normal racing at a car-length or two shouldn't trip the brakes.
    _reset_driver_state()
    opps_far = [200.0] * 36
    opps_far[18] = 25.0
    compute_control({**cs, "speed_x": 200.0, "opponents": opps_far}, NORMAL)
    assert _dbg["opp_bound"] == 0.0, f"FAIL: 25 m gap should not trigger the brake cap: {_dbg}"
    _reset_driver_state()
    print("compute_control front-opponent follow cap (boxed-in only) ... OK")

    # ---- safety_filter ------------------------------------------------------
    base = {"fuel": 50.0, "damage": 0.0}

    # valid strategy + healthy car → pass through unchanged
    assert safety_filter(ATTACK,    base) == ATTACK,    "FAIL: healthy ATTACK should pass"
    assert safety_filter(NORMAL,    base) == NORMAL,    "FAIL: healthy NORMAL should pass"
    assert safety_filter(SAVE_FUEL, base) == SAVE_FUEL, "FAIL: healthy SAVE_FUEL should pass"
    print("safety_filter pass-through   ... OK")

    # unknown / None → NORMAL
    assert safety_filter(None,        base) == NORMAL, "FAIL: None → NORMAL"
    assert safety_filter("TURBO",     base) == NORMAL, "FAIL: unknown → NORMAL"
    assert safety_filter("",          base) == NORMAL, "FAIL: empty → NORMAL"
    print("safety_filter unknown/None   ... OK")

    # fuel < 5 → PIT (beats any strategy including ATTACK) — the absolute
    # floor, unchanged from before the bt-style dynamic check existed.
    low_fuel = {**base, "fuel": 3.0}
    assert safety_filter(ATTACK, low_fuel) == PIT, "FAIL: low fuel + ATTACK → PIT"
    assert safety_filter(NORMAL, low_fuel) == PIT, "FAIL: low fuel + NORMAL → PIT"
    print("safety_filter low fuel → PIT ... OK")

    # bt-style dynamic trigger (strategy.cpp: needPitstop) fires EARLIER than
    # the flat floor: 8 L is well above _FUEL_PIT=5, but at 6 L/lap with 2
    # laps left, both of bt's conditions hold — 8 < 1.5*6=9 (1.5-lap margin)
    # and 8 < 2*6=12 (won't finish the race) — so it must PIT anyway.
    dyn_fuel = {**base, "fuel": 8.0, "fuel_per_lap": 6.0, "laps_left": 2}
    assert safety_filter(ATTACK, dyn_fuel) == PIT, \
        f"FAIL: dynamic fuel check should PIT before the flat floor: {safety_filter(ATTACK, dyn_fuel)}"
    print("safety_filter dynamic fuel (bt-style) → PIT ... OK")

    # Comfortably more than a 1.5-lap margin (20 L at 6 L/lap, 5 laps left)
    # → neither of bt's conditions holds → must NOT pit.
    dyn_fuel_ok = {**base, "fuel": 20.0, "fuel_per_lap": 6.0, "laps_left": 5}
    assert safety_filter(ATTACK, dyn_fuel_ok) == ATTACK, \
        f"FAIL: enough fuel for the remaining laps must not PIT: {safety_filter(ATTACK, dyn_fuel_ok)}"
    print("safety_filter dynamic fuel — enough for the race ... OK")

    # No fuel_per_lap/laps_left data yet (defaults) → dynamic check disabled,
    # falls back to the flat floor only — same as before this feature existed.
    # fuel=30 (above _FUEL_CAUTION=15 too) isolates this from the unrelated
    # Priority 5 low-fuel-ATTACK-downgrade rule.
    assert safety_filter(ATTACK, {**base, "fuel": 30.0}) == ATTACK, \
        "FAIL: no lap-fuel data should not force PIT above the flat floor"
    print("safety_filter dynamic fuel — no data falls back to flat floor ... OK")

    # damage >= 9500 → DEFEND
    critical_dmg = {**base, "damage": 9600.0}
    assert safety_filter(ATTACK, critical_dmg) == DEFEND, "FAIL: critical damage → DEFEND"
    assert safety_filter(NORMAL, critical_dmg) == DEFEND, "FAIL: critical damage → DEFEND"
    print("safety_filter critical damage → DEFEND ... OK")

    # 8000 <= damage < 9500 → ATTACK blocked, others pass
    high_dmg = {**base, "damage": 8500.0}
    assert safety_filter(ATTACK, high_dmg) == NORMAL,  "FAIL: high damage + ATTACK → NORMAL"
    assert safety_filter(NORMAL, high_dmg) == NORMAL,  "FAIL: high damage + NORMAL passes"
    assert safety_filter(DEFEND, high_dmg) == DEFEND,  "FAIL: high damage + DEFEND should pass"
    print("safety_filter high damage     ... OK")

    # fuel < 15 → ATTACK blocked
    caution_fuel = {**base, "fuel": 12.0}
    assert safety_filter(ATTACK, caution_fuel) == NORMAL, "FAIL: caution fuel + ATTACK → NORMAL"
    assert safety_filter(NORMAL, caution_fuel) == NORMAL, "FAIL: caution fuel + NORMAL passes"
    print("safety_filter caution fuel    ... OK")

    # Granite can never self-select BLOCK, even healthy — it's system-only
    assert safety_filter(BLOCK, base) == NORMAL, "FAIL: Granite-picked BLOCK must be rejected"
    print("safety_filter BLOCK is Granite-forbidden ... OK")

    # rear car closing fast + healthy car → BLOCK, regardless of Granite's pick
    rear_close = {**base, "opponents": [200.0] * 36}
    rear_close["opponents"][1] = 15.0   # index 0-3/32-35 = rear cone, see _rear_gap
    assert safety_filter(ATTACK, rear_close) == BLOCK, "FAIL: close rear car + healthy → BLOCK"
    assert safety_filter(NORMAL, rear_close) == BLOCK, "FAIL: close rear car + healthy → BLOCK"
    print("safety_filter rear car close → BLOCK ... OK")

    # same rear threat, but car is damaged → must NOT try to block, just get
    # home safe (existing damage priority wins)
    rear_close_damaged = {**rear_close, "damage": 8500.0}
    assert safety_filter(ATTACK, rear_close_damaged) == NORMAL, \
        "FAIL: damaged car must not attempt BLOCK even with a rear threat"
    print("safety_filter rear car + damaged → no BLOCK ... OK")

    # distant rear car → no BLOCK
    rear_far = {**base, "opponents": [200.0] * 36}
    assert safety_filter(ATTACK, rear_far) == ATTACK, "FAIL: distant rear car must not trigger BLOCK"
    print("safety_filter rear car far → pass-through ... OK")

    # Regression (live on-track, 2026-08-07): a close rear gap during the
    # standing-grid launch (dist_raced small) must NOT trigger BLOCK — every
    # neighbour on a 2-row grid reads "close behind" by construction, and
    # this is exactly the merge window the start-of-race collision fix
    # already handles with its own widened avoidance.
    rear_close_launch = {**rear_close, "dist_raced": 20.0}
    assert safety_filter(ATTACK, rear_close_launch) == ATTACK, \
        "FAIL: close rear car during launch must not trigger BLOCK"
    rear_close_post_launch = {**rear_close, "dist_raced": 200.0}
    assert safety_filter(ATTACK, rear_close_post_launch) == BLOCK, \
        "FAIL: same rear gap, past the launch window, must still trigger BLOCK"
    print("safety_filter rear car close during launch → no BLOCK ... OK")

    # ---- Step 6: _parse_strategy_response ----------------------------------
    # valid JSON with known strategy
    s, r = _parse_strategy_response('{"strategy": "ATTACK", "reason": "clear track ahead"}')
    assert s == ATTACK, f"FAIL parse valid: {s}"
    assert r == "clear track ahead", f"FAIL reason: {r}"
    print(f"_parse_strategy_response valid   ... OK  ({s} / {r!r})")

    # strategy field in wrong case → should normalise
    s, r = _parse_strategy_response('{"strategy": "defend", "reason": "opponent close"}')
    assert s == DEFEND, f"FAIL parse lower-case: {s}"
    print(f"_parse_strategy_response lower   ... OK  ({s})")

    # unknown strategy name → NORMAL
    s, r = _parse_strategy_response('{"strategy": "TURBO", "reason": "go fast"}')
    assert s == NORMAL, f"FAIL parse unknown: {s}"
    print(f"_parse_strategy_response unknown → NORMAL ... OK")

    # garbage text → NORMAL
    s, r = _parse_strategy_response("Sorry, I cannot help with that.")
    assert s == NORMAL, f"FAIL parse garbage: {s}"
    print(f"_parse_strategy_response garbage → NORMAL ... OK")

    # BLOCK is deliberately not offered to Granite (system-only, see
    # safety_filter) — even if it says the word anyway, treat as invalid.
    s, r = _parse_strategy_response('{"strategy": "BLOCK", "reason": "defending"}')
    assert s == NORMAL, f"FAIL parse BLOCK (Granite-forbidden): {s}"
    print(f"_parse_strategy_response BLOCK forbidden → NORMAL ... OK")

    # missing reason field → empty string, strategy still valid
    s, r = _parse_strategy_response('{"strategy": "SAVE_FUEL"}')
    assert s == SAVE_FUEL, f"FAIL parse no-reason: {s}"
    assert r == "",         f"FAIL reason should be empty: {r!r}"
    print(f"_parse_strategy_response no-reason ... OK  ({s})")

    # ---- Step 7: _next_debounced_strategy (strategy switch, CONFIRM=1) -----
    # With _STRATEGY_CONFIRM == 1 a single differing proposal switches
    # immediately — no debounce wait, so a decision is actionable on the very
    # next Granite answer instead of needing two matching ones in a row.
    active, cand, cnt, switched = _next_debounced_strategy(NORMAL, None, 0, ATTACK)
    assert active == ATTACK and switched,      f"FAIL: single flip must switch immediately: {active}"
    assert cand is None and cnt == 0,          f"FAIL: candidate should reset after switch: {cand}/{cnt}"
    print("_next_debounced_strategy single flip      ... OK  (switched immediately to ATTACK)")

    # a second, different proposal switches again just as fast — no stale
    # state from the previous switch lingers.
    active, cand, cnt, switched = _next_debounced_strategy(active, cand, cnt, DEFEND)
    assert active == DEFEND and switched,      f"FAIL: back-to-back flip must also switch immediately: {active}"
    print("_next_debounced_strategy back-to-back flip ... OK  (switched immediately to DEFEND)")

    # proposal matching the ALREADY-active strategy → no-op, clears any candidate
    active, cand, cnt, switched = _next_debounced_strategy(NORMAL, DEFEND, 1, NORMAL)
    assert active == NORMAL and not switched,  "FAIL: re-confirm of active should be a no-op"
    assert cand is None and cnt == 0,          "FAIL: stale candidate should be cleared"
    print("_next_debounced_strategy re-confirm       ... OK  (no-op, candidate cleared)")

    # ---- Step 6: _build_strategy_prompt ------------------------------------
    sample_state = {
        "speed_x": 120.0, "fuel": 18.0, "damage": 500.0,
        "track_pos": 0.1, "gear": 4, "race_pos": 3,
        "dist_raced": 1200.0,
        "fuel_per_lap": 4.5, "laps_left": 3,
        "track":     [200.0] * 19,
        "opponents": [200.0] * 36,
    }
    prompt = _build_strategy_prompt(sample_state)
    assert "ATTACK" in prompt,     "FAIL: prompt missing strategy guide"
    assert "120.0"  in prompt,     "FAIL: prompt missing speed"
    assert "18.0"   in prompt,     "FAIL: prompt missing fuel"
    assert "strategy" in prompt,   "FAIL: prompt missing JSON schema hint"
    assert "fuel_per_lap_L" in prompt, "FAIL: prompt missing fuel_per_lap_L"
    assert "4.5"    in prompt,     "FAIL: prompt missing fuel_per_lap_L value"
    assert "laps_left" in prompt,  "FAIL: prompt missing laps_left"
    print("_build_strategy_prompt          ... OK  (prompt contains speed/fuel/strategy/fuel_per_lap/laps_left)")

    # ---- _update_fuel_model (bt-style measured fuel-per-lap) --------------
    _reset_driver_state()
    tick1 = {"fuel": 50.0, "last_lap_time": 0.0}
    _update_fuel_model(tick1)
    assert _fuel_per_lap_est == 0.0, \
        f"FAIL: no lap completed yet, estimate should stay 0: {_fuel_per_lap_est}"
    # Same lap (last_lap_time unchanged) — must not update.
    tick2 = {"fuel": 45.0, "last_lap_time": 0.0}
    _update_fuel_model(tick2)
    assert _fuel_per_lap_est == 0.0, \
        f"FAIL: last_lap_time unchanged should not update the estimate: {_fuel_per_lap_est}"
    # Lap boundary crossed (last_lap_time changes) — burned 50-42=8 L over it.
    tick3 = {"fuel": 42.0, "last_lap_time": 91.5}
    _update_fuel_model(tick3)
    assert abs(_fuel_per_lap_est - 8.0) < 1e-9, \
        f"FAIL: fuel_per_lap_est should be 50-42=8.0: {_fuel_per_lap_est}"
    # Second lap boundary — burned 42-35=7 L, estimate updates again.
    tick4 = {"fuel": 35.0, "last_lap_time": 88.0}
    _update_fuel_model(tick4)
    assert abs(_fuel_per_lap_est - 7.0) < 1e-9, \
        f"FAIL: fuel_per_lap_est should update to 42-35=7.0: {_fuel_per_lap_est}"
    _reset_driver_state()
    print("_update_fuel_model (bt-style measured fuel-per-lap) ... OK")

    print("\nAll tests passed.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--bot":
        _host, _port, _strategy = "localhost", 3001, NORMAL
        _granite = False
        _track: str | None = None
        positional: list[str] = []
        i = 1
        while i < len(args):
            if args[i] == "--strategy" and i + 1 < len(args):
                _strategy = args[i + 1].upper()
                i += 2
            elif args[i] == "--track" and i + 1 < len(args):
                _track = args[i + 1]
                i += 2
            elif args[i] == "--granite":
                _granite = True
                i += 1
            else:
                positional.append(args[i])
                i += 1
        if len(positional) > 0:
            _host = positional[0]
        if len(positional) > 1 and positional[1].isdigit():
            _port = int(positional[1])
        elif len(positional) > 1:
            _strategy = positional[1].upper()
        run_bot(_host, _port, _strategy, use_granite=_granite, track=_track)
    else:
        _run_tests()
