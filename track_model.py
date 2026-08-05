#!/usr/bin/env python3
"""
Feature 4 / P1: pre-race track map for the Granite AI bot.

Parses a TORCS track XML (the same file the simulator itself loads — public
data, no engine internals) into the ordered segment list, then precomputes a
speed-limit profile over the whole lap:

  1. corner limit   v = sqrt(A_LAT * R)                       (lateral grip)
  2. backward pass  v[i] = min(v[i], sqrt(v[i+1]^2 + 2*A_BRAKE*ds))
                    "be slow enough NOW for everything ahead"  (braking)

At runtime the bot queries ``limit_kmh(distFromStart)`` — an O(1) table
lookup — and takes the MINIMUM of this map target and its reactive
sensor-based target.  The map can therefore only ever slow the car down
(brake earlier for corners the sensors cannot see yet); if the map is
missing, misaligned or wrong, the car drives exactly as before on sensors.

Deliberately ignored (the reactive minimum covers the error):
  * elevation / banking — changes effective grip by a modest factor;
  * surface friction differences between tracks — A_LAT is one global
    calibration constant, tune it on track (too low = brakes early and
    crawls corners; too high = map never binds and only the sensors brake).
"""

from __future__ import annotations

import glob
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Physics calibration
#
# CALIBRATE OPTIMISTIC, not safe: compute_control takes min(sensors, map), so
# an optimistic map simply stops binding (the car drives on sensors, exactly
# as before P1), while a pessimistic map drags the whole lap down — braking
# early everywhere and pinning the mid-corner speed below what the tuned
# sensor controller would carry.  Verified on track: the first cut used a
# constant 1.0 g / 8 m/s^2 and made laps SLOWER than sensors alone.
# ---------------------------------------------------------------------------

_DS         = 2.0     # m: sample spacing of the precomputed profile
_A_LAT      = 16.5    # m/s^2: mechanical (low-speed) lateral grip, ~1.65 g.
                      # (10.0 → 12.0 → 13.5 → 15.0 → 16.5 → 17.5 → back to
                      # 16.5: 17.5/20.5 was tried and reverted after a single
                      # corner (dist≈3800-3950 on Forza, a blind spot where
                      # sight collapses to ~13-20 m at 200+ km/h entry) failed
                      # 3 separate on-track runs in a row at that tier — a
                      # damage-only hit, a wall-grinding stick, then a chaotic
                      # multi-second spin, three different symptoms from the
                      # same root cause (too much entry speed, not enough
                      # warning distance).  16.5/19.0 is the last tier that
                      # completed a full lap clean.  Do NOT re-raise this pair
                      # without either fixing that specific corner's map
                      # profile or re-verifying it stays clean multiple runs
                      # in a row, not just once.)
_K_AERO     = 0.002   # 1/m: downforce term — grip grows with v^2, so the
                      # cornering limit is v^2 = A_LAT*r / (1 - K_AERO*r).
                      # Radii past ~1/K_AERO are flat-out (no cap at all);
                      # hairpins are barely affected (downforce needs speed).
                      # Too slow in fast sweepers → raise; sliding wide in
                      # them → lower.
_A_BRAKE    = 19.0    # m/s^2: braking deceleration used by the backward
                      # pass.  Brakes visibly earlier than the sensors ever
                      # needed to → raise; arrives hot with the sensor brake
                      # scrambling → lower.  (10 → 11 → 13 → 15 → 17 → 19 →
                      # 20.5 → back to 19: see the _A_LAT comment — 20.5 is
                      # what let the car arrive at Forza's dist≈3800-3950
                      # blind corner hot enough to fail 3/3 runs in a row.
                      # 19.0 is the last tier verified clean over a full lap.)
_V_CAP_KMH  = 330.0   # km/h: profile ceiling (straights are "no limit")


def _corner_limit_ms(radius: float) -> float:
    """Cornering speed limit (m/s) for a radius, downforce included."""
    cap   = _V_CAP_KMH / 3.6
    denom = 1.0 - _K_AERO * radius
    if denom <= 0.05:            # huge radius: downforce wins, no limit
        return cap
    return min(cap, math.sqrt(_A_LAT * radius / denom))

# next_corner() ignores radii above this — a 400 m sweeper is not a corner
# worth announcing, and barely dents the speed profile anyway.
_CORNER_MAX_RADIUS = 400.0


@dataclass(frozen=True)
class Segment:
    kind:       str    # "str" | "lft" | "rgt"
    length:     float  # metres along the centre line
    radius:     float  # start radius, m (0.0 for straights)
    end_radius: float  # end radius, m (== radius unless the turn is a spiral)


# ---------------------------------------------------------------------------
# XML helpers — TORCS params files carry a DOCTYPE with external entities
# (surfaces/objects) that ElementTree cannot resolve; strip them first.
# ---------------------------------------------------------------------------

def _sanitize_xml(text: str) -> str:
    i = text.find("<!DOCTYPE")
    if i != -1:
        bracket = text.find("[", i)
        close   = text.find(">", i)
        if bracket != -1 and (close == -1 or bracket < close):
            end   = text.find("]>", bracket)
            close = end + 1 if end != -1 else close
        if close != -1:
            text = text[:i] + text[close + 1:]
    # drop non-standard entity references (&default-surfaces; etc.)
    return re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)[\w.-]+;", "", text)


def _sections(el: ET.Element) -> list[ET.Element]:
    return [c for c in el if c.tag == "section"]


def _section(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if c.tag == "section" and c.get("name") == name:
            return c
    return None


def _attstr(el: ET.Element, name: str) -> str | None:
    for c in el:
        if c.tag == "attstr" and c.get("name") == name:
            return c.get("val")
    return None


def _attnum(el: ET.Element, name: str) -> tuple[float, str] | None:
    """Return (value, unit) of a direct <attnum> child, or None."""
    for c in el:
        if c.tag == "attnum" and c.get("name") == name:
            try:
                return float(c.get("val", "")), (c.get("unit") or "")
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Track model
# ---------------------------------------------------------------------------

class TrackModel:
    """Whole-lap speed-limit profile, indexed by distFromStart."""

    def __init__(self, segments: list[Segment], width: float = 10.0,
                 name: str = "", ds: float = _DS) -> None:
        if not segments:
            raise ValueError("empty segment list")
        self.name       = name
        self.width      = width
        self.ds         = ds
        self.segments   = list(segments)
        self.lap_length = sum(s.length for s in segments)

        # --- corner straightening: a driver uses the full track WIDTH ---
        # The XML radius is the CENTRE-LINE radius, but the racing line runs
        # out-in-out, which straightens shallow corners enormously: a 20°
        # kink on a 15 m wide track has an effective radius of several
        # hundred metres and is taken flat — the sensors always did; without
        # this term the map slammed the brakes for every little jink.
        # Effective radius gain (apex may move W/2 inward off the centre
        # arc):  R_eff = R + W_eff * cos(α/2) / (1 − cos(α/2)),  α = total
        # deflection.  Hairpins (α→180°) gain ~0 — physically right.
        # Consecutive same-direction segments are MERGED first: TORCS splits
        # big corners into several arcs, and rating each piece as a
        # "straightenable kink" would blow the limit of real corners wide
        # open.  Opposite-direction flicks (chicanes) stay separate.
        w_eff  = max(0.0, (width - 2.0) / 2.0)   # half the width beyond the car
        boosts = [0.0] * len(segments)
        b      = 0
        while b < len(segments):
            if segments[b].kind == "str":
                b += 1
                continue
            e, total_arc = b, 0.0
            while e < len(segments) and segments[e].kind == segments[b].kind:
                mean_r = (segments[e].radius + segments[e].end_radius) / 2.0
                if mean_r > 0.0:
                    total_arc += segments[e].length / mean_r
                e += 1
            sag   = 1.0 - math.cos(min(total_arc, math.pi) / 2.0)
            boost = (w_eff * (1.0 - sag) / sag) if sag > 1e-6 else 1e9
            for k in range(b, e):
                boosts[k] = boost
            b = e

        # --- sample corner radius every ds metres along the lap ---
        n = max(1, int(self.lap_length / ds))
        radius = [0.0] * n   # 0 = straight (true geometric radius, for reports)
        lim_r  = [0.0] * n   # radius + straightening boost (for speed limits)
        sign   = [0]   * n   # +1 left, -1 right
        si, seg_start = 0, 0.0
        for i in range(n):
            s_mid = (i + 0.5) * ds
            while (si < len(segments) - 1
                   and s_mid > seg_start + segments[si].length):
                seg_start += segments[si].length
                si += 1
            seg = segments[si]
            if seg.kind != "str" and seg.length > 0.0:
                t = min(1.0, max(0.0, (s_mid - seg_start) / seg.length))
                radius[i] = seg.radius + (seg.end_radius - seg.radius) * t
                lim_r[i]  = radius[i] + boosts[si]
                sign[i]   = 1 if seg.kind == "lft" else -1

        # --- corner limits, then backward braking pass (two laps for wrap) ---
        cap = _V_CAP_KMH / 3.6
        v = [_corner_limit_ms(r) if r > 0.0 else cap for r in lim_r]
        for i in range(2 * n - 1, -1, -1):
            j, nxt = i % n, (i + 1) % n
            v[j] = min(v[j], math.sqrt(v[nxt] ** 2 + 2.0 * _A_BRAKE * ds))

        self._radius     = radius
        self._sign       = sign
        self._limit_kmh  = [x * 3.6 for x in v]
        self.real_lap: float | None = None   # measured lap length (calibration)

    # ------------------------------------------------------------------ #

    def calibrate(self, measured_lap_m: float) -> None:
        """Set the MEASURED lap length (from a driven lap's distFromStart wrap).

        The XML geometry can be ~1% long on tracks with spiral corners, and
        the error is CUMULATIVE along the lap — late corners land tens of
        metres off, so the map brakes late into them and then drags the exit.
        After calibration every query is rescaled by lap_length/real_lap,
        which removes the cumulative drift in one shot (the practice lap).
        """
        if measured_lap_m > 0.0:
            self.real_lap = measured_lap_m

    def _to_model_dist(self, dist_from_start: float) -> float:
        if self.real_lap:
            return (dist_from_start % self.real_lap) * (self.lap_length / self.real_lap)
        return dist_from_start

    def limit_kmh(self, dist_from_start: float) -> float:
        """Max safe speed (km/h) at this distFromStart, braking included.

        Takes the min of the sample and its successor so a metre or two of
        start-line misalignment errs toward braking earlier, never later.
        """
        n = len(self._limit_kmh)
        d = self._to_model_dist(dist_from_start)
        i = int((d % self.lap_length) / self.ds)
        if i >= n:
            i = n - 1
        return min(self._limit_kmh[i], self._limit_kmh[(i + 1) % n])

    def next_corner(self, dist_from_start: float,
                    horizon: float = 800.0) -> dict | None:
        """First real corner ahead within ``horizon`` metres, or None.

        Returns {"dist_m", "dir", "radius_m", "limit_kmh"} — the input for
        Granite's track briefing (P2); unused by the control loop itself.
        """
        n     = len(self._radius)
        start = int((self._to_model_dist(dist_from_start) % self.lap_length) / self.ds)
        steps = min(n, int(horizon / self.ds))
        k     = 1
        while k < steps:
            i = (start + k) % n
            r = self._radius[i]
            # skip corners the racing line straightens out (limit ~ flat)
            if 0.0 < r < _CORNER_MAX_RADIUS and self._limit_kmh[i] < 280.0:
                entry_k      = k
                min_r, min_v = r, self._limit_kmh[i]
                direction    = "left" if self._sign[i] > 0 else "right"
                while k < n:                      # walk to the corner's end
                    i = (start + k) % n
                    if not (0.0 < self._radius[i] < _CORNER_MAX_RADIUS):
                        break
                    min_r = min(min_r, self._radius[i])
                    min_v = min(min_v, self._limit_kmh[i])
                    k += 1
                return {"dist_m":    round(entry_k * self.ds),
                        "dir":       direction,
                        "radius_m":  round(min_r),
                        "limit_kmh": round(min_v)}
            k += 1
        return None

    def line_tpos(self, dist_from_start: float,
                  entry_zone: float = 250.0) -> float:
        """Racing-line lateral setpoint (SCR trackPos units: +1 = left edge).

        Out-in-out entry positioning: while a real corner lies within
        ``entry_zone`` metres ahead, suggest the OUTSIDE of that corner
        (left side for a right-hander and vice versa); otherwise 0
        (centre).  Only the approach is placed from the map — mid-corner
        the pursuit controller owns the line, and the caller is expected
        to fade this setpoint out as soon as the corner is in sensor view.
        """
        nc = self.next_corner(dist_from_start, horizon=entry_zone)
        if nc is None:
            return 0.0
        # ±0.45, not deeper: the setpoint flips sides between alternating
        # corners, and a big amplitude turned that into a visible weave on
        # twisty sections (the caller also slew-limits the setpoint).
        return -0.45 if nc["dir"] == "left" else 0.45

    # ------------------------------------------------------------------ #

    def summary(self) -> str:
        n_corners = sum(1 for s in self.segments if s.kind != "str")
        return (f"{self.name or 'track'}: {self.lap_length:.0f} m, "
                f"{len(self.segments)} segments ({n_corners} turns), "
                f"width {self.width:.0f} m, "
                f"slowest point {min(self._limit_kmh):.0f} km/h")

    # ------------------------------------------------------------------ #

    @classmethod
    def from_xml(cls, path: str) -> "TrackModel":
        with open(path, encoding="utf-8", errors="replace") as f:
            root = ET.fromstring(_sanitize_xml(f.read()))

        header = _section(root, "Header")
        name   = (_attstr(header, "name") if header is not None else None) \
                 or os.path.splitext(os.path.basename(path))[0]

        main = _section(root, "Main Track")
        if main is None:
            raise ValueError(f"{path}: no 'Main Track' section")
        w     = _attnum(main, "width")
        width = w[0] if w else 10.0

        segs_el = _section(main, "Track Segments")
        if segs_el is None:
            raise ValueError(f"{path}: no 'Track Segments' section")

        segments: list[Segment] = []
        for s in _sections(segs_el):
            kind = (_attstr(s, "type") or "").strip()
            if kind == "str":
                lg = _attnum(s, "lg")
                if lg and lg[0] > 0.0:
                    segments.append(Segment("str", lg[0], 0.0, 0.0))
            elif kind in ("lft", "rgt"):
                rad = _attnum(s, "radius")
                arc = _attnum(s, "arc")
                if not rad or not arc or rad[0] <= 0.0:
                    continue
                er      = _attnum(s, "end radius")
                r0, r1  = rad[0], (er[0] if er else rad[0])
                a, unit = arc
                arc_rad = a if unit == "rad" else math.radians(a)
                # spiral (r0 != r1): centre-line length ≈ arc * mean radius
                length  = arc_rad * (r0 + r1) / 2.0
                if length > 0.0:
                    segments.append(Segment(kind, length, r0, r1))
        return cls(segments, width=width, name=name)


# ---------------------------------------------------------------------------
# Track file discovery
# ---------------------------------------------------------------------------

def _search_roots() -> list[str]:
    roots: list[str] = []
    env = os.environ.get("TORCS_DATA")
    if env:
        roots.append(env)
    roots.append(os.path.dirname(os.path.abspath(__file__)))
    roots.append(os.getcwd())
    roots += [
        os.path.expanduser("~/summer-project/F1-simulator"),
        "/usr/local/share/games/torcs",
        "/usr/share/games/torcs",
    ]
    return roots


def find_track_xml(name: str) -> str | None:
    """Locate <name>.xml under any known TORCS data directory."""
    for root in _search_roots():
        for pat in (f"{root}/data/tracks/*/{name}/{name}.xml",
                    f"{root}/tracks/*/{name}/{name}.xml"):
            hits = glob.glob(pat)
            if hits:
                return hits[0]
    return None


def autodetect_track_name() -> str | None:
    """Read the currently selected track from the newest TORCS raceman config.

    TORCS writes the chosen track into ~/.torcs/config/raceman/<mode>.xml
    (quickrace.xml, practice.xml, …) whenever the user configures a race, so
    the most recently modified file names the track about to be driven.
    """
    cfg = os.path.expanduser("~/.torcs/config/raceman")
    best: tuple[float, str] | None = None
    for path in glob.glob(cfg + "/*.xml"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                root = ET.fromstring(_sanitize_xml(f.read()))
        except (OSError, ET.ParseError):
            continue
        tracks = _section(root, "Tracks")
        first  = _section(tracks, "1") if tracks is not None else None
        name   = _attstr(first, "name") if first is not None else None
        if name:
            mtime = os.path.getmtime(path)
            if best is None or mtime > best[0]:
                best = (mtime, name)
    return best[1] if best else None


def load_track_model(spec: str | None, quiet: bool = False) -> TrackModel | None:
    """Build a TrackModel from a spec: an XML path, a track name, or 'auto'.

    Never raises — the bot must always be able to fall back to sensors-only
    driving, so every failure returns None (with a hint unless quiet).
    """
    try:
        if spec and spec != "auto" and os.path.isfile(spec):
            return TrackModel.from_xml(spec)
        name = spec if spec and spec != "auto" else autodetect_track_name()
        if not name:
            if not quiet:
                print("[map] could not auto-detect the current track "
                      "(no ~/.torcs raceman config) — pass --track <name>")
            return None
        path = find_track_xml(name)
        if path is None:
            if not quiet:
                print(f"[map] no track XML found for '{name}' "
                      f"(searched TORCS data dirs) — driving on sensors only")
            return None
        return TrackModel.from_xml(path)
    except Exception as e:                        # noqa: BLE001 — must not kill the bot
        if not quiet:
            print(f"[map] failed to load track model: {e}")
        return None


# ---------------------------------------------------------------------------
# Self-test / CLI
#   python3 track_model.py                 → unit tests (no files needed)
#   python3 track_model.py <track.xml>     → parse + print profile summary
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    # 600 m straight → 180° right hairpin R=30 → 400 m straight
    hairpin = math.pi * 30.0
    tm = TrackModel([Segment("str", 600.0, 0.0, 0.0),
                     Segment("rgt", hairpin, 30.0, 30.0),
                     Segment("str", 400.0, 0.0, 0.0)],
                    width=12.0, name="unit-test")

    total = 600.0 + hairpin + 400.0
    assert abs(tm.lap_length - total) < 1e-6, f"FAIL lap_length={tm.lap_length}"

    # mid-corner limit for an R=30 hairpin (downforce is negligible that slow)
    mid = tm.limit_kmh(600.0 + hairpin / 2.0)
    assert 65.0 < mid < 85.0, f"FAIL mid-corner limit={mid:.1f}"

    # far up the straight the braking curve is above the cap → 330
    assert tm.limit_kmh(0.0) == _V_CAP_KMH, f"FAIL open straight={tm.limit_kmh(0.0)}"

    # braking curve: 100 m before the corner, v = sqrt(v_c² + 2·A_BRAKE·100)
    v100 = math.sqrt(_corner_limit_ms(30.0) ** 2 + 2.0 * _A_BRAKE * 100.0) * 3.6
    got  = tm.limit_kmh(500.0)
    assert abs(got - v100) < 8.0, f"FAIL braking curve at 500 m: {got:.1f} vs {v100:.1f}"

    # downforce: a fast sweeper must be far less restricted than 1 g allows,
    # and a huge radius must not be capped at all
    assert _corner_limit_ms(200.0) > math.sqrt(_A_LAT * 200.0) * 1.15, "FAIL aero term missing"
    assert _corner_limit_ms(1000.0) == _V_CAP_KMH / 3.6, "FAIL huge radius must be flat-out"

    # corner straightening: a shallow 15° kink (R=80) on a 12 m wide track is
    # taken virtually flat by the racing line — the map must NOT brake for it
    kink_arc = 80.0 * math.radians(15.0)
    tk = TrackModel([Segment("str", 500.0, 0.0, 0.0),
                     Segment("rgt", kink_arc, 80.0, 80.0),
                     Segment("str", 500.0, 0.0, 0.0)],
                    width=12.0, name="kink")
    kink_lim = tk.limit_kmh(500.0 + kink_arc / 2.0)
    assert kink_lim > 250.0, f"FAIL kink must be near flat-out: {kink_lim:.0f} km/h"

    # split corners: TORCS often writes one 90° turn as two 45° arcs — they
    # must be MERGED before straightening, or a real corner rates as two
    # flat kinks.  Expect a sane 90°-corner limit, far below the kink's.
    half = 60.0 * math.radians(45.0)
    ts = TrackModel([Segment("str", 500.0, 0.0, 0.0),
                     Segment("rgt", half, 60.0, 60.0),
                     Segment("rgt", half, 60.0, 60.0),
                     Segment("str", 500.0, 0.0, 0.0)],
                    width=12.0, name="split-90")
    split_lim = ts.limit_kmh(500.0 + half)
    assert 80.0 < split_lim < 140.0, f"FAIL split 90° corner limit: {split_lim:.0f} km/h"
    assert split_lim < kink_lim - 80.0, "FAIL merge: split corner rated like a kink"

    # monotone: closer to the corner → lower limit
    assert tm.limit_kmh(560.0) < tm.limit_kmh(480.0) < tm.limit_kmh(300.0), "FAIL monotone braking"

    # wrap-around: just past the lap line ≡ lap start
    assert abs(tm.limit_kmh(total + 4.0) - tm.limit_kmh(4.0)) < 1e-9, "FAIL wrap"

    # negative distFromStart (SCR sends small negatives before the line)
    assert tm.limit_kmh(-2.0) > 0.0, "FAIL negative dist"

    # racing-line entry bias: a right-hander inside the entry zone → move to
    # the OUTSIDE (left, tpos +); far from any corner → hold the centre
    assert tm.line_tpos(450.0) > 0.35, f"FAIL entry bias: {tm.line_tpos(450.0)}"
    assert tm.line_tpos(100.0) == 0.0, f"FAIL no-corner bias: {tm.line_tpos(100.0)}"

    # odometer calibration: pretend the REAL lap is 1000 m (model says 1094).
    # The corner that sits at model-600 then really happens at ~548, so the
    # calibrated map must read slow there, where the raw map still read the
    # braking curve much higher.
    raw_550 = tm.limit_kmh(550.0)
    tm.calibrate(1000.0)
    cal_550 = tm.limit_kmh(550.0)
    assert cal_550 < 100.0 < raw_550, \
        f"FAIL calibration: raw {raw_550:.0f}, calibrated {cal_550:.0f}"
    tm.real_lap = None                      # restore for any later checks

    print("TrackModel unit tests ... OK")
    print("  " + tm.summary())

    # parse a real track if the repo data is next to us
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.join(here, "data", "tracks", "road", "g-track-2", "g-track-2.xml")
    if os.path.isfile(real):
        rm = TrackModel.from_xml(real)
        assert 1000.0 < rm.lap_length < 10000.0, f"FAIL real lap={rm.lap_length}"
        assert min(rm._limit_kmh) > 30.0, "FAIL real profile has absurdly slow point"
        print("TrackModel.from_xml(g-track-2) ... OK")
        print("  " + rm.summary())
    else:
        print("(g-track-2.xml not found next to script — real-file test skipped)")

    print("\nAll track_model tests passed.")


def _print_profile(path: str) -> None:
    tm = TrackModel.from_xml(path)
    print(tm.summary())
    print(f"{'dist(m)':>8}  {'limit(km/h)':>11}")
    n    = len(tm._limit_kmh)
    step = max(1, n // 40)                       # ~40 rows
    for i in range(0, n, step):
        bar = "#" * int(tm._limit_kmh[i] / 10.0)
        print(f"{i * tm.ds:8.0f}  {tm._limit_kmh[i]:11.0f}  {bar}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _print_profile(sys.argv[1])
    else:
        _run_tests()
