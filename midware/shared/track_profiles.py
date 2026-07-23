from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from track_model import load_track_model
except Exception:  # pragma: no cover - optional runtime integration
    load_track_model = None  # type: ignore[assignment]


ROOT_DIR = Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT_DIR / "data" / "track_profiles"
DEFAULT_TRACK_ID = os.getenv("TORCS_COACH_TRACK_PROFILE", "default-road").strip() or "default-road"


FALLBACK_PROFILE: dict[str, Any] = {
    "id": "default-road",
    "name": "Default road course",
    "length_m": 3600.0,
    "summary": "Generic road-course profile for early braking, line setup, and exit throttle planning.",
    "segments": [
        {
            "id": "s1",
            "name": "Launch straight",
            "type": "straight",
            "start_m": 0.0,
            "end_m": 420.0,
            "limit_kmh": 235.0,
            "line_hint": "center",
            "line_tpos": 0.0,
            "risk": "Low grip on cold tyres can make the first heavy brake zone easy to overshoot.",
            "advice": "Build speed cleanly and move to the outside before the first braking marker.",
        },
        {
            "id": "t1",
            "name": "T1 right hairpin",
            "type": "corner",
            "dir": "right",
            "start_m": 420.0,
            "end_m": 650.0,
            "limit_kmh": 92.0,
            "line_hint": "left outside",
            "line_tpos": -0.55,
            "brake_marker_m": 350.0,
            "risk": "Late braking pushes the car wide and delays throttle on exit.",
            "advice": "Brake in a straight line, rotate early, then wait for the wheel to open before full throttle.",
        },
        {
            "id": "s2",
            "name": "Short acceleration zone",
            "type": "straight",
            "start_m": 650.0,
            "end_m": 1080.0,
            "limit_kmh": 215.0,
            "line_hint": "center-right",
            "line_tpos": 0.15,
            "risk": "Over-correcting after T1 loses speed before the next braking phase.",
            "advice": "Use progressive throttle and settle the car before the next direction change.",
        },
        {
            "id": "t2",
            "name": "Medium left",
            "type": "corner",
            "dir": "left",
            "start_m": 1080.0,
            "end_m": 1340.0,
            "limit_kmh": 128.0,
            "line_hint": "right outside",
            "line_tpos": 0.5,
            "brake_marker_m": 1015.0,
            "risk": "Reactive steering near the edge can turn this into a track-limit warning.",
            "advice": "Commit to one arc and carry minimum speed instead of adding a late steering correction.",
        },
        {
            "id": "s3",
            "name": "Back straight",
            "type": "straight",
            "start_m": 1340.0,
            "end_m": 2100.0,
            "limit_kmh": 260.0,
            "line_hint": "center",
            "line_tpos": 0.0,
            "risk": "Exit hesitation compounds into a large speed deficit before the chicane.",
            "advice": "Prioritise early throttle and keep the wheel calm through the first half of the straight.",
        },
        {
            "id": "t3",
            "name": "Fast chicane entry",
            "type": "corner",
            "dir": "right-left",
            "start_m": 2100.0,
            "end_m": 2420.0,
            "limit_kmh": 150.0,
            "line_hint": "left setup",
            "line_tpos": -0.45,
            "brake_marker_m": 2000.0,
            "risk": "Too much entry speed forces a second steering input and hurts the left change of direction.",
            "advice": "Trim speed before turn-in and keep the first kerb touch light.",
        },
        {
            "id": "t4",
            "name": "Late apex left",
            "type": "corner",
            "dir": "left",
            "start_m": 2860.0,
            "end_m": 3180.0,
            "limit_kmh": 105.0,
            "line_hint": "right outside",
            "line_tpos": 0.55,
            "brake_marker_m": 2780.0,
            "risk": "Early throttle before the late apex creates understeer and a wide exit.",
            "advice": "Hold the outside entry, rotate patiently, and delay full throttle until after the apex.",
        },
    ],
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").replace("\r", " ").split()).strip()


def list_track_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    if load_track_model is not None:
        profiles.append(
            {
                "id": "auto",
                "name": "Auto-detected TORCS track model",
                "length_m": 0.0,
                "summary": "Use the existing physics-based track_model.py loader and the current TORCS raceman config.",
                "segments": 0,
            }
        )
        seen.add("auto")
    if PROFILE_DIR.exists():
        for path in sorted(PROFILE_DIR.glob("*.json")):
            try:
                profile = normalize_track_profile(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            profile_id = str(profile["id"])
            if profile_id in seen:
                continue
            seen.add(profile_id)
            profiles.append(_profile_summary(profile))

    fallback = normalize_track_profile(FALLBACK_PROFILE)
    if fallback["id"] not in seen:
        profiles.append(_profile_summary(fallback))
    return profiles


def coach_track_context(
    *,
    track_id: str | None = None,
    road_condition: str | None = None,
    profile_override: dict[str, Any] | None = None,
    dist_from_start: float = 0.0,
    speed_kmh: float = 0.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if profile_override:
        profile = load_track_profile(track_id, profile_override)
        return profile, build_track_context(
            profile,
            dist_from_start=dist_from_start,
            speed_kmh=speed_kmh,
            road_condition=road_condition or "dry",
        )

    profile_id = clean_text(track_id) or DEFAULT_TRACK_ID
    if profile_id == "auto" or (profile_id and not (PROFILE_DIR / f"{profile_id}.json").exists()):
        model_context = _track_model_context(
            profile_id,
            dist_from_start=dist_from_start,
            speed_kmh=speed_kmh,
            road_condition=road_condition or "dry",
        )
        if model_context is not None:
            return model_context

    profile = load_track_profile(profile_id)
    return profile, build_track_context(
        profile,
        dist_from_start=dist_from_start,
        speed_kmh=speed_kmh,
        road_condition=road_condition or "dry",
    )


def load_track_profile(track_id: str | None = None, override: dict[str, Any] | None = None) -> dict[str, Any]:
    if override:
        return normalize_track_profile(override)

    profile_id = clean_text(track_id) or DEFAULT_TRACK_ID
    candidates = [
        PROFILE_DIR / f"{profile_id}.json",
        PROFILE_DIR / f"{profile_id.lower()}.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            return normalize_track_profile(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            break
    return normalize_track_profile(FALLBACK_PROFILE)


def normalize_track_profile(profile: dict[str, Any]) -> dict[str, Any]:
    segments = [_normalize_segment(item) for item in profile.get("segments", []) if isinstance(item, dict)]
    segments = [item for item in segments if item["end_m"] > item["start_m"]]
    segments.sort(key=lambda item: item["start_m"])

    inferred_length = max((segment["end_m"] for segment in segments), default=0.0)
    length_m = safe_float(profile.get("length_m"), inferred_length or safe_float(FALLBACK_PROFILE["length_m"]))
    if length_m <= 0:
        length_m = safe_float(FALLBACK_PROFILE["length_m"])

    if not segments:
        segments = [_normalize_segment(item) for item in FALLBACK_PROFILE["segments"]]

    return {
        "id": clean_text(profile.get("id")) or "custom-track",
        "name": clean_text(profile.get("name")) or "Custom track",
        "length_m": round(length_m, 3),
        "summary": clean_text(profile.get("summary")) or "No track summary recorded.",
        "segments": segments,
    }


def build_track_context(
    profile: dict[str, Any],
    *,
    dist_from_start: float = 0.0,
    speed_kmh: float = 0.0,
    road_condition: str = "dry",
    lookahead_m: float = 1200.0,
) -> dict[str, Any]:
    normalized = normalize_track_profile(profile)
    length_m = safe_float(normalized.get("length_m"), 1.0) or 1.0
    dist = safe_float(dist_from_start) % length_m
    speed = safe_float(speed_kmh)
    segments = list(normalized["segments"])
    current_segment = _current_segment(segments, dist) or segments[0]
    upcoming = _upcoming_segments(segments, dist, length_m, lookahead_m)
    upcoming_corners = [item for item in upcoming if _is_corner(item)]
    next_corner = upcoming_corners[0] if upcoming_corners else None
    target = next_corner or current_segment
    limit = safe_float(target.get("limit_kmh"), safe_float(current_segment.get("limit_kmh"), 0.0))
    line_tpos = safe_float(target.get("line_tpos"), safe_float(current_segment.get("line_tpos"), 0.0))

    return {
        "available": True,
        "source": "track_profile",
        "profile_id": normalized["id"],
        "name": normalized["name"],
        "summary": normalized["summary"],
        "dist_from_start": round(dist, 3),
        "length_m": round(length_m, 3),
        "road_condition": clean_text(road_condition) or "dry",
        "limit_kmh": round(limit, 3),
        "speed_over_limit": round(speed - limit, 3) if limit else 0.0,
        "line_tpos": round(line_tpos, 3),
        "line_hint": clean_text(target.get("line_hint")) or "center",
        "next_corner": _corner_payload(next_corner, dist, length_m) if next_corner else None,
        "current_segment": _segment_payload(current_segment, dist, length_m),
        "upcoming_segments": [_segment_payload(item, dist, length_m) for item in upcoming[:5]],
        "upcoming_corners": [_corner_payload(item, dist, length_m) for item in upcoming_corners[:5]],
        "error": "",
    }


def _normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": clean_text(segment.get("id")) or clean_text(segment.get("name")) or "segment",
        "name": clean_text(segment.get("name")) or "Unnamed segment",
        "type": clean_text(segment.get("type")) or "straight",
        "dir": clean_text(segment.get("dir")),
        "start_m": safe_float(segment.get("start_m")),
        "end_m": safe_float(segment.get("end_m")),
        "limit_kmh": safe_float(segment.get("limit_kmh"), 140.0),
        "line_hint": clean_text(segment.get("line_hint")) or "center",
        "line_tpos": safe_float(segment.get("line_tpos")),
        "brake_marker_m": safe_float(segment.get("brake_marker_m"), safe_float(segment.get("start_m"))),
        "risk": clean_text(segment.get("risk")) or "No recorded risk.",
        "advice": clean_text(segment.get("advice")) or "Keep the car balanced and repeat the reference point.",
    }


def _profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile["id"],
        "name": profile["name"],
        "length_m": profile["length_m"],
        "summary": profile["summary"],
        "segments": len(profile["segments"]),
    }


def _is_corner(segment: dict[str, Any]) -> bool:
    segment_type = str(segment.get("type", "")).lower()
    return "corner" in segment_type or bool(clean_text(segment.get("dir")))


def _current_segment(segments: list[dict[str, Any]], dist: float) -> dict[str, Any] | None:
    for segment in segments:
        if safe_float(segment["start_m"]) <= dist < safe_float(segment["end_m"]):
            return segment
    return None


def _distance_ahead(target_dist: float, current_dist: float, length_m: float) -> float:
    delta = safe_float(target_dist) - safe_float(current_dist)
    if delta < 0:
        delta += length_m
    return delta


def _upcoming_segments(
    segments: list[dict[str, Any]],
    dist: float,
    length_m: float,
    lookahead_m: float,
) -> list[dict[str, Any]]:
    ranked = [
        (_distance_ahead(segment["start_m"], dist, length_m), segment)
        for segment in segments
    ]
    ranked.sort(key=lambda item: item[0])
    horizon = max(250.0, safe_float(lookahead_m, 1200.0))
    selected = [segment for delta, segment in ranked if delta <= horizon]
    if len(selected) < 3:
        selected = [segment for _delta, segment in ranked[:3]]
    return selected


def _segment_payload(segment: dict[str, Any], dist: float, length_m: float) -> dict[str, Any]:
    payload = dict(segment)
    payload["dist_m"] = round(_distance_ahead(segment["start_m"], dist, length_m), 3)
    payload["length_m"] = round(max(0.0, safe_float(segment["end_m"]) - safe_float(segment["start_m"])), 3)
    return payload


def _corner_payload(segment: dict[str, Any] | None, dist: float, length_m: float) -> dict[str, Any] | None:
    if not segment:
        return None
    payload = _segment_payload(segment, dist, length_m)
    payload["dir"] = clean_text(segment.get("dir")) or "corner"
    payload["brake_in_m"] = round(_distance_ahead(segment.get("brake_marker_m"), dist, length_m), 3)
    return payload


def _track_model_context(
    track_id: str,
    *,
    dist_from_start: float,
    speed_kmh: float,
    road_condition: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if load_track_model is None:
        return None
    spec = None if track_id == "auto" else track_id
    model = load_track_model(spec or "auto", quiet=True)
    if model is None:
        return None

    dist = safe_float(dist_from_start) % safe_float(model.real_lap or model.lap_length, model.lap_length)
    limit = safe_float(model.limit_kmh(dist), 0.0)
    line_tpos = safe_float(model.line_tpos(dist), 0.0)
    next_corner = model.next_corner(dist) or None
    line_hint = "center"
    if line_tpos > 0.05:
        line_hint = "left setup"
    elif line_tpos < -0.05:
        line_hint = "right setup"

    corner_payload = None
    if isinstance(next_corner, dict):
        corner_payload = {
            "id": "physics-next-corner",
            "name": f"{clean_text(next_corner.get('dir') or 'corner').title()} corner",
            "type": "corner",
            "dir": clean_text(next_corner.get("dir")) or "corner",
            "dist_m": safe_float(next_corner.get("dist_m")),
            "brake_in_m": max(0.0, safe_float(next_corner.get("dist_m")) - 80.0),
            "target_speed_kmh": safe_float(next_corner.get("limit_kmh"), limit),
            "limit_kmh": safe_float(next_corner.get("limit_kmh"), limit),
            "line_hint": line_hint,
            "line_tpos": round(line_tpos, 3),
            "risk": "Physics map predicts this as the next speed-binding corner.",
            "advice": "Use the map target as the first braking reference, then let live sensors refine the entry.",
        }

    profile = {
        "id": track_id or "auto",
        "name": clean_text(model.name) or "TORCS track model",
        "length_m": round(safe_float(model.real_lap or model.lap_length), 3),
        "summary": clean_text(model.summary()),
        "segments": [],
    }
    context = {
        "available": True,
        "source": "track_model.py",
        "profile_id": profile["id"],
        "name": profile["name"],
        "summary": profile["summary"],
        "dist_from_start": round(dist, 3),
        "length_m": profile["length_m"],
        "road_condition": clean_text(road_condition) or "dry",
        "limit_kmh": round(limit, 3),
        "speed_over_limit": round(safe_float(speed_kmh) - limit, 3) if limit else 0.0,
        "line_tpos": round(line_tpos, 3),
        "line_hint": line_hint,
        "next_corner": corner_payload,
        "current_segment": {
            "id": "physics-map-window",
            "name": "Physics map window",
            "type": "model",
            "dist_m": 0.0,
            "limit_kmh": round(limit, 3),
            "line_hint": line_hint,
            "line_tpos": round(line_tpos, 3),
            "risk": "Model limit is generated from TORCS XML geometry.",
            "advice": "Use live telemetry to confirm grip and exact braking feel.",
        },
        "upcoming_segments": [corner_payload] if corner_payload else [],
        "upcoming_corners": [corner_payload] if corner_payload else [],
        "error": "",
    }
    return profile, context
