#!/usr/bin/env python3
"""
Feature 4: Granite-powered AI racing bot.

Steps implemented:
  1  parse_scr_state()    — decode TORCS SCR sensor string → Python dict
  2  format_scr_control() — encode control dict → TORCS SCR wire string
  3  ScrClient            — UDP handshake + main receive/send loop
     run_bot()            — connect to TORCS and drive
  4  compute_control()    — strategy-parameterized low-level controller
                            ATTACK / NORMAL / DEFEND / SAVE_FUEL / PIT
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
}

_ARRAY_FIELDS: frozenset[str] = frozenset({"opponents", "track", "wheelSpinVel", "focus"})
_INT_FIELDS:   frozenset[str] = frozenset({"gear", "racePos"})

_ARRAY_LENGTHS: dict[str, int] = {
    "opponents": 36, "track": 19, "wheelSpinVel": 4, "focus": 5,
}
_ARRAY_DEFAULTS: dict[str, float] = {
    "opponents": 200.0, "track": -1.0, "wheelSpinVel": 0.0, "focus": -1.0,
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
            state[py_name] = parse_int(raw_value, 0)
        else:
            state[py_name] = parse_float(raw_value, 0.0)

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
    # Single choke point for every control we emit — capture it for the drive
    # log so a stuck car can be diagnosed from what was actually COMMANDED.
    _dbg.update(cmd_accel=accel, cmd_brake=brake, cmd_gear=gear, cmd_steer=steer)
    return (
        f"(accel {accel:.3f})"
        f"(brake {brake:.3f})"
        f"(gear {gear})"
        f"(steer {steer:.3f})"
        f"(clutch {clutch:.3f})"
        f"(focus {focus})"
        f"(meta {meta})"
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
        try:
            self._sock.bind(("", 3100 + (self._addr[1] % 100)))
        except OSError as e:
            self._sock.close()
            self._sock = None
            raise ConnectionError(
                f"Another bot instance appears to be connected to this TORCS "
                f"slot (local port {3100 + (self._addr[1] % 100)} busy). "
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

_ALL_STRATEGIES: frozenset[str] = frozenset({ATTACK, NORMAL, DEFEND, SAVE_FUEL, PIT})


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
_PARAMS: dict[str, _DriveParams] = {
    ATTACK:    _DriveParams(300,    1.00,   1.20,    0.90,    0.20,  290),
    NORMAL:    _DriveParams(250,    1.00,   1.00,    0.85,    0.20,  230),
    DEFEND:    _DriveParams(180,    0.80,   0.90,    0.80,    0.25,  150),
    SAVE_FUEL: _DriveParams(150,    0.65,   0.80,    0.80,    0.20,   80),
    PIT:       _DriveParams( 50,    0.30,   1.50,    0.70,    0.30,   10),
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
_SHARP_FREE = 0.10

# Forward sight (m) at/above which the road is treated as an open straight
# and the corner-speed cap is lifted (track sensors saturate ~200 m).
_STRAIGHT_CLEAR = 180.0

# Throttle ease-off band: proportional throttle within this many km/h of the
# target instead of bang-bang.  Full-below/zero-above pulsed the pedal the
# whole way down a straight once the car touched its top speed — cruise is a
# steady partial throttle, not taps.
_ACCEL_BAND = 15.0

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
_REVERSE_FRAMES = 40      # how long to hold reverse once triggered
_stuck_frames   = 0       # module state: consecutive jammed frames seen
_reverse_frames = 0       # module state: reverse-burst frames remaining

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

_recovering = False   # module state: in off-track re-entry (with hysteresis)
_turnaround = False   # module state: executing a wrong-way turn-around
_ta_fwd     = 0       # module state: forward-leg frames remaining
_ta_jam     = 0       # module state: consecutive jammed frames while reversing


def _reset_driver_state() -> None:
    """Reset all module-level driving state (tests / new race)."""
    global _stuck_frames, _reverse_frames, _recovering, _turnaround, _ta_fwd, _ta_jam
    global _target_lp, _line_lp
    _stuck_frames = _reverse_frames = 0
    _recovering = _turnaround = False
    _ta_fwd = _ta_jam = 0
    _target_lp = None
    _line_lp = 0.0


def _recovery_steer(angle: float, tpos: float) -> float:
    """Steer command for backing out of a crash: de-rotate + drift to centre.
    In reverse the steering effect inverts, so the signs are flipped relative to
    the normal forward correction."""
    return clamp(-angle * 0.5 + tpos * 0.4, -0.6, 0.6)


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
    global _turnaround, _ta_fwd, _ta_jam

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
            _ta_fwd = _ta_jam = 0
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
                if _ta_jam >= _TA_JAM_FRAMES:    # blocked behind too → go forward
                    _ta_jam = 0
                    _ta_fwd = _TA_FWD_FRAMES
            else:
                _ta_jam = 0
            _dbg["mode"] = "turn-rev"
            return format_scr_control(accel=0.5, brake=0.0, gear=-1,
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

    global _stuck_frames, _reverse_frames, _recovering, _target_lp, _line_lp

    # --- stuck / crash recovery (works on OR off track, takes priority) ---
    # Once we've committed to a reverse burst, see it through; then resume normal
    # driving (which floors it forward again).  We trigger it after a sustained
    # crawl, which is the signature of having rammed a wall or another car.
    if _reverse_frames > 0:
        _reverse_frames -= 1
        _dbg["mode"] = "burst"
        return format_scr_control(accel=0.5, brake=0.0, gear=-1,
                                  steer=_recovery_steer(angle, tpos))
    # "jammed" = crawling AND something is right in front, or we're pinned at the
    # edge.  The front/edge gate is what prevents a false reverse on a clear
    # standing start or in the pit lane (slow, but open road ahead).
    front      = track[9] if len(track) > 9 else 200.0
    jammed_now = abs(speed) < _STUCK_SPEED and (front < _STUCK_WALL or abs(tpos) > 0.9)
    if jammed_now:
        _stuck_frames += 1
    else:
        _stuck_frames = 0
    if _stuck_frames >= _STUCK_FRAMES:
        _stuck_frames   = 0
        _reverse_frames = _REVERSE_FRAMES
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
    pursuit = _pursuit_target(track)
    if pursuit is None:
        # Nominally on track yet no usable beams — sensor glitch.  Fall back to
        # the angle/centre controller at a modest pace rather than flooring it
        # blind with steer 0.
        steer = clamp(angle - clamp(tpos, -2.0, 2.0) * 0.5 - speed_y * _STEER_DAMP, -1.0, 1.0)
        accel = 0.4 if speed < 60.0 else 0.0
        brake = 0.3 if speed > 80.0 else 0.0
        _dbg["mode"] = "blind"
        return format_scr_control(accel=accel, brake=brake, gear=gear, steer=steer)

    edge    = max(0.0, abs(tpos) - _EDGE_FREE)
    barrier = -math.copysign(edge * _EDGE_GAIN, tpos)
    aim     = math.copysign(max(0.0, abs(pursuit) - _PP_FREE), pursuit)
    # A+ racing line: hold-line setpoint.  0 (centre) on open road, but on
    # the approach to a mapped corner the map moves it to the OUTSIDE edge
    # (out-in-out entry).  Same fade as before: the term only acts while the
    # pursuit aim is quiet, so it positions the car on straights/braking
    # zones and never wrestles pursuit for the wheel mid-corner.
    line_raw = 0.0
    if _track_model is not None and dist_from_start >= 0.0:
        line_raw = _track_model.line_tpos(dist_from_start)
    # Slew toward the raw setpoint — side flips between alternating corners
    # become a ~1 s drift instead of a dart (the twisty-section weave fix).
    _line_lp += clamp(line_raw - _line_lp, -_LINE_SLEW, _LINE_SLEW)
    hold_gain = _LINE_GAIN if abs(_line_lp) > 0.05 else _HOLD_CENTRE
    fade    = max(0.0, 1.0 - abs(pursuit) / _PP_FREE)
    centre  = clamp((_line_lp - tpos) * hold_gain, -0.25, 0.25) * fade
    steer   = aim * _PP_GAIN + centre + barrier - speed_y * _STEER_DAMP
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
    else:
        sharp        = max(0.0, open_angle - _SHARP_FREE)
        floor        = params.max_speed * _CORNER_FLOOR
        dist_limit   = math.sqrt(floor * floor + max(sight, 1.0) * params.speed_factor)
        target_speed = min(params.max_speed,
                           dist_limit / (1.0 + _CORNER_SHARPNESS * sharp))

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

    # Smooth the target: drops are instant (braking must never lag), rises are
    # rate-limited so a flickering straight/corner classification can't strobe
    # the pedals.
    if _target_lp is None or target_speed < _target_lp:
        _target_lp = target_speed
    else:
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

    # ABS: prevent wheel lock-up under braking (snakeoil.py)
    brake = _apply_abs(brake, speed, wheel_vels)
    # TCL: prevent rear-wheel spin on acceleration (snakeoil.py)
    accel = _apply_tcl(accel, speed, wheel_vels)

    # PIT: once we've slowed to a crawl, ask TORCS for the pit stop
    meta = 1 if (strategy == PIT and speed < 10.0) else 0

    return format_scr_control(accel=accel, brake=brake, gear=gear, steer=steer, meta=meta)


# ---------------------------------------------------------------------------
# Step 5: Safety layer
# ---------------------------------------------------------------------------

# Thresholds — centralised here so they're easy to tune without touching logic.
_FUEL_PIT      = 5.0    # litres: force PIT regardless of Granite's choice
_FUEL_CAUTION  = 15.0   # litres: downgrade ATTACK → NORMAL (running low)
_DMG_NO_ATTACK = 8000   # damage points: disallow ATTACK (car degraded)
_DMG_DEFEND    = 9500   # damage points: force DEFEND even if Granite says NORMAL


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

    # Priority 1 — unknown / timed-out strategy → safe default
    if strategy not in _ALL_STRATEGIES:
        return NORMAL

    # Priority 2 — almost out of fuel → pit now, no argument
    if fuel < _FUEL_PIT:
        return PIT

    # Priority 3 — car is critically damaged → protect what's left
    if damage >= _DMG_DEFEND:
        return DEFEND

    # Priority 4 — car is damaged but still drivable → no attacking
    if damage >= _DMG_NO_ATTACK and strategy == ATTACK:
        return NORMAL

    # Priority 5 — fuel running low → conserve, don't attack
    if fuel < _FUEL_CAUTION and strategy == ATTACK:
        return NORMAL

    return strategy


# ---------------------------------------------------------------------------
# Step 6: Granite strategy caller
# ---------------------------------------------------------------------------

_STRATEGY_INTERVAL = 5.0    # seconds between Granite requests — measured local
                             # LM Studio round-trip is ~1.4-2.3s (granite-4.1-8b,
                             # 985-char prompt, 80 max_tokens); 5s keeps ~2x
                             # margin over the observed worst case while landing
                             # in the ~0.1-1Hz strategy-refresh target.
_GRANITE_TIMEOUT   = 10.0   # seconds to wait for a single LLM response — 4x+
                             # margin over the 2.3s observed worst case.
_STRATEGY_CONFIRM  = 2      # consecutive matching Granite answers required
                             # before switching the active strategy — stops a
                             # borderline reading from flapping the car
                             # between e.g. ATTACK/NORMAL every ~5s poll.
_GRANITE_MAX_TOK   = 80    # keep responses short and fast

_SYSTEM_PROMPT = """\
You are a race strategist for a TORCS simulation. \
Given live sensor data, choose one driving strategy and explain in one sentence why.

Respond with JSON only — no markdown, no extra text:
{"strategy": "<one of ATTACK|NORMAL|DEFEND|SAVE_FUEL|PIT>", "reason": "<one sentence>"}

Strategy guide:
- ATTACK:    push hard, high risk, use when fuel ok and no damage and clear track
- NORMAL:    balanced pace, default choice
- DEFEND:    cautious, use when damaged or opponent close behind
- SAVE_FUEL: economical, use when fuel < 20 L and many laps remain
- PIT:       slow down for pit stop, use when fuel < 5 L or damage critical"""


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
    strategy = raw_strategy if raw_strategy in _ALL_STRATEGIES else NORMAL
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
        """Only switch the active strategy once Granite proposes the SAME new
        strategy on ``_STRATEGY_CONFIRM`` consecutive calls in a row.

        Without this, a state right at a threshold (e.g. speed/gap borderline
        between ATTACK and NORMAL) can flip the raw Granite answer back and
        forth on successive polls, and — since each poll is now only 5s apart
        — that flapped the car's strategy every few seconds. Note this only
        smooths *Granite's* pick; safety_filter() still runs on every frame
        on top of whatever this returns, so a real emergency (low fuel,
        critical damage) is never delayed by the confirmation wait.
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
) -> None:
    """Connect to TORCS and drive.

    With ``use_granite=True`` (Step 7), a GraniteStrategist is created and
    queried every few seconds to update the driving strategy dynamically.
    Without it, the fixed ``strategy`` argument is used throughout.

    ``track`` selects the pre-race map: a track name (``g-track-2``), a path
    to the track XML, or None to auto-detect from the TORCS raceman config.
    No map found → the bot drives on sensors alone, as before.
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
    with ScrClient(host, port) as client:
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

                if verbose and step % 100 == 0:
                    speed = state.get("speed_x", 0.0)
                    gear  = state.get("gear",    0)
                    fuel  = state.get("fuel",    0.0)
                    tpos  = state.get("track_pos", 0.0)
                    dmg   = state.get("damage",  0.0)
                    rpm   = state.get("rpm",     0.0)
                    print(
                        f"  step={step:6d}  {speed:6.1f} km/h  "
                        f"gear={gear}  fuel={fuel:.1f} L  tpos={tpos:+.2f}  "
                        f"strategy={current_strategy}  "
                        f"tgt={_dbg.get('target', 0.0):5.1f}  "
                        f"map={_dbg.get('map', -1.0):5.0f}  "
                        f"tru={int(_dbg.get('trust', 0.0))}  "
                        f"sight={_dbg.get('sight', 0.0):5.1f}  "
                        f"open={_dbg.get('open_angle', 0.0):+.2f}  "
                        f"rpm={rpm:5.0f}  dmg={dmg:5.0f}  "
                        f"acc={_dbg.get('cmd_accel', 0.0):.2f}  "
                        f"brk={_dbg.get('cmd_brake', 0.0):.2f}  "
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
    print(f"format_scr_control ... OK  →  {ctrl}")

    over = format_scr_control(accel=2.0, brake=-1.0, steer=5.0, focus=200)
    assert "(accel 1.000)" in over
    assert "(brake 0.000)" in over
    assert "(steer 1.000)" in over
    assert "(focus 90)"    in over
    print("format_scr_control (clamping) ... OK")

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
    assert "(accel 0.333)" in cc_cruise, f"FAIL cruise throttle should be proportional: {cc_cruise}"
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

    # PIT + speed < 10 → meta=1
    cs_pit = {**cs, "speed_x": 5.0, "rpm": 800.0, "gear": 1}
    cc_pit = compute_control(cs_pit, PIT)
    assert "(meta 1)" in cc_pit, f"FAIL PIT meta: {cc_pit}"
    print(f"compute_control PIT       ... OK  →  {cc_pit}")

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
        out_coast = compute_control({**cs_line, "speed_x": 211.0, "gear": 5}, NORMAL)
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

    # fuel < 5 → PIT (beats any strategy including ATTACK)
    low_fuel = {**base, "fuel": 3.0}
    assert safety_filter(ATTACK, low_fuel) == PIT, "FAIL: low fuel + ATTACK → PIT"
    assert safety_filter(NORMAL, low_fuel) == PIT, "FAIL: low fuel + NORMAL → PIT"
    print("safety_filter low fuel → PIT ... OK")

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

    # missing reason field → empty string, strategy still valid
    s, r = _parse_strategy_response('{"strategy": "SAVE_FUEL"}')
    assert s == SAVE_FUEL, f"FAIL parse no-reason: {s}"
    assert r == "",         f"FAIL reason should be empty: {r!r}"
    print(f"_parse_strategy_response no-reason ... OK  ({s})")

    # ---- Step 7: _next_debounced_strategy (strategy switch debouncing) -----
    # single differing proposal → held as a candidate, active unchanged
    active, cand, cnt, switched = _next_debounced_strategy(NORMAL, None, 0, ATTACK)
    assert active == NORMAL and not switched, "FAIL: single flip should not switch"
    assert cand == ATTACK and cnt == 1,        f"FAIL candidate tracking: {cand}/{cnt}"
    print("_next_debounced_strategy single flip     ... OK  (held NORMAL, candidate ATTACK 1/2)")

    # same proposal again → confirms and switches
    active, cand, cnt, switched = _next_debounced_strategy(NORMAL, cand, cnt, ATTACK)
    assert active == ATTACK and switched,      f"FAIL: 2nd confirm should switch: {active}"
    assert cand is None and cnt == 0,          "FAIL: candidate should reset after switch"
    print("_next_debounced_strategy confirmed flip  ... OK  (switched to ATTACK)")

    # proposal matching the ALREADY-active strategy → no-op, clears any candidate
    active, cand, cnt, switched = _next_debounced_strategy(NORMAL, DEFEND, 1, NORMAL)
    assert active == NORMAL and not switched,  "FAIL: re-confirm of active should be a no-op"
    assert cand is None and cnt == 0,          "FAIL: stale candidate should be cleared"
    print("_next_debounced_strategy re-confirm      ... OK  (no-op, candidate cleared)")

    # candidate changes mid-flight → count resets to 1 for the new candidate
    active, cand, cnt, switched = _next_debounced_strategy(NORMAL, ATTACK, 1, DEFEND)
    assert active == NORMAL and not switched,  "FAIL: switched candidate should not switch yet"
    assert cand == DEFEND and cnt == 1,        f"FAIL: candidate should reset to new value: {cand}/{cnt}"
    print("_next_debounced_strategy candidate swap  ... OK  (restarted count for DEFEND)")

    # ---- Step 6: _build_strategy_prompt ------------------------------------
    sample_state = {
        "speed_x": 120.0, "fuel": 18.0, "damage": 500.0,
        "track_pos": 0.1, "gear": 4, "race_pos": 3,
        "dist_raced": 1200.0,
        "track":     [200.0] * 19,
        "opponents": [200.0] * 36,
    }
    prompt = _build_strategy_prompt(sample_state)
    assert "ATTACK" in prompt,     "FAIL: prompt missing strategy guide"
    assert "120.0"  in prompt,     "FAIL: prompt missing speed"
    assert "18.0"   in prompt,     "FAIL: prompt missing fuel"
    assert "strategy" in prompt,   "FAIL: prompt missing JSON schema hint"
    print("_build_strategy_prompt          ... OK  (prompt contains speed/fuel/strategy)")

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
