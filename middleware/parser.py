"""
parser.py — UDP 包拆解

每个 UDP 包包含多行：
  第一行：车辆数据（以数字序号开头）
  后续行：排名数据（以 R, 开头）
"""

from dataclasses import dataclass, field
from typing import Optional

# ── 车辆数据列名（按 player_logger.cpp 顺序）────────────────────
_CAR_FIXED_COLS = [
    "seq", "sim_time", "player", "lap",
    "x", "y", "yaw", "accel_x", "accel_y",
    "steer", "throttle", "brake", "clutch",
    "angle", "curLapTime", "damage",
    "distFromStart", "distRaced", "fuel", "gear",
    "lastLapTime", "racePos", "rpm",
    "speedX", "speedY", "speedZ", "trackPos", "z",
]
_OPP_COUNT        = 36
_TRACK_COUNT      = 19
_WHEEL_COUNT      = 4
_FOCUS_COUNT      = 5

_CAR_COLS = (
    _CAR_FIXED_COLS
    + [f"opponent_{i}"    for i in range(_OPP_COUNT)]
    + [f"track_{i}"       for i in range(_TRACK_COUNT)]
    + [f"wheelSpinVel_{i}" for i in range(_WHEEL_COUNT)]
    + [f"focus_{i}"       for i in range(_FOCUS_COUNT)]
)

# ── 数据类 ──────────────────────────────────────────────────────

@dataclass
class CarFrame:
    """一帧车辆传感器数据"""
    raw: dict = field(repr=False)   # 完整字段字典

    # 常用字段直接提升为属性，方便访问
    @property
    def sim_time(self)    -> float: return self.raw["sim_time"]
    @property
    def lap(self)         -> int:   return int(self.raw["lap"])
    @property
    def speed_x(self)     -> float: return self.raw["speedX"]
    @property
    def throttle(self)    -> float: return self.raw["throttle"]
    @property
    def brake(self)       -> float: return self.raw["brake"]
    @property
    def steer(self)       -> float: return self.raw["steer"]
    @property
    def gear(self)        -> int:   return int(self.raw["gear"])
    @property
    def track_pos(self)   -> float: return self.raw["trackPos"]
    @property
    def rpm(self)         -> float: return self.raw["rpm"]
    @property
    def damage(self)      -> int:   return int(self.raw["damage"])
    @property
    def race_pos(self)    -> int:   return int(self.raw["racePos"])
    @property
    def last_lap_time(self) -> float: return self.raw["lastLapTime"]
    @property
    def cur_lap_time(self)  -> float: return self.raw["curLapTime"]


@dataclass
class RankingEntry:
    """一辆车的排名信息"""
    sim_time:        float
    car_index:       int
    car_name:        str
    race_pos:        int
    laps:            int
    dist_from_start: float


@dataclass
class Packet:
    """一次完整 UDP 包解析结果"""
    car:      Optional[CarFrame]       = None
    rankings: list[RankingEntry]       = field(default_factory=list)


# ── 解析函数 ────────────────────────────────────────────────────

def _parse_car_line(line: str) -> Optional[CarFrame]:
    parts = line.split(",")
    if len(parts) < len(_CAR_COLS):
        return None
    try:
        raw = {}
        for i, col in enumerate(_CAR_COLS):
            raw[col] = float(parts[i])
        return CarFrame(raw=raw)
    except ValueError:
        return None


def _parse_ranking_line(line: str) -> Optional[RankingEntry]:
    # 格式: R,sim_time,car_index,car_name,race_pos,laps,dist_from_start
    parts = line.split(",", 6)
    if len(parts) < 7:
        return None
    try:
        return RankingEntry(
            sim_time        = float(parts[1]),
            car_index       = int(parts[2]),
            car_name        = parts[3],
            race_pos        = int(parts[4]),
            laps            = int(parts[5]),
            dist_from_start = float(parts[6]),
        )
    except ValueError:
        return None


def parse_packet(data: bytes) -> Packet:
    """将原始 UDP 字节解析为 Packet"""
    packet   = Packet()
    text     = data.decode("utf-8", errors="replace")
    lines    = [l.strip() for l in text.splitlines() if l.strip()]

    for line in lines:
        if line.startswith("R,"):
            entry = _parse_ranking_line(line)
            if entry:
                packet.rankings.append(entry)
        else:
            if packet.car is None:
                packet.car = _parse_car_line(line)

    return packet
