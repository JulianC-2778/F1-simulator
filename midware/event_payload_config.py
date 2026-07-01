"""
Event payload field configuration.

To customize what data is sent to the AI for each event type,
edit the EVENT_FIELDS dict below. Add or remove field names as needed.

Available fields
----------------
# Race state
race_pos          : int   — current race position
lap               : int   — current lap number
gear              : int   — current gear
track_pos         : float — lateral track position (0=centre, ±1=edge)
fuel_remaining    : float — remaining fuel in litres
total_damage      : float — cumulative damage value
last_lap_time     : float — last completed lap time in seconds

# Window summary (computed over the recent 6-second window)
damage_delta      : float — damage increase in this window

# Opponent distances (metres; 200 = no opponent detected)
front_gap         : float — gap to nearest car ahead
rear_gap          : float — gap to nearest car behind
nearest_gap       : float — gap to nearest car in any direction

# Event-specific computed fields
direction         : str   — "up" or "down" (position_change only)
new_pos           : int   — new race position (position_change only)
side              : str   — "left" or "right" (off_track only)
completed_lap     : int   — lap number just finished (lap_complete only)
collision_direction : str — direction of impact: front/rear/left/right (contact only)
collision_partner : dict  — inferred car involved: {car_name, race_pos} (contact only)

# Full ranking table
rankings          : list  — [{car_name, race_pos}, ...] for all cars
"""

EVENT_FIELDS: dict[str, list[str]] = {
    "contact": [
        "race_pos",
        "damage_delta",
        "total_damage",
        "collision_direction",
        "collision_partner",
    ],
    "position_change": [
        "direction",
        "new_pos",
        "lap",
        "rankings",
    ],
    "off_track": [
        "race_pos",
        "side",
        "track_pos",
        "damage_delta",
    ],
    "lap_complete": [
        "completed_lap",
        "last_lap_time",
        "race_pos",
        "fuel_remaining",
        "rankings",
    ],
    "battle": [
        "race_pos",
        "lap",
        "front_gap",
        "rankings",
    ],
    "pace_surge": [
        "race_pos",
        "lap",
        "gear",
        "front_gap",
        "rear_gap",
        "nearest_gap",
    ],
    "pace_update": [
        "race_pos",
        "lap",
        "fuel_remaining",
        "track_pos",
        "rankings",
    ],
}
