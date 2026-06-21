"""
cache.py — 滑动窗口缓存

保存最近 window_seconds 秒的车辆帧和排名数据。
线程安全（asyncio 单线程环境下直接操作 deque 即可）。
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from parser import CarFrame, RankingEntry


@dataclass
class RankingSnapshot:
    """某一时刻所有车辆的排名快照"""
    sim_time: float
    entries:  list[RankingEntry]


class SlidingCache:
    def __init__(self, window_seconds: float = 10.0):
        self.window_seconds = window_seconds

        # 车辆数据帧队列，元素为 CarFrame
        self._car_frames:   deque[CarFrame]        = deque()

        # 排名快照队列，元素为 RankingSnapshot
        self._rankings:     deque[RankingSnapshot] = deque()

        # 最新一帧（不受窗口限制）
        self.latest_car:    Optional[CarFrame]        = None
        self.latest_ranking: Optional[RankingSnapshot] = None

    # ── 写入 ────────────────────────────────────────────────────

    def push_car(self, frame: CarFrame) -> None:
        self._car_frames.append(frame)
        self.latest_car = frame
        self._evict_car(frame.sim_time)

    def push_rankings(self, sim_time: float, entries: list[RankingEntry]) -> None:
        if not entries:
            return
        snapshot = RankingSnapshot(sim_time=sim_time, entries=entries)
        self._rankings.append(snapshot)
        self.latest_ranking = snapshot
        self._evict_rankings(sim_time)

    # ── 读取 ────────────────────────────────────────────────────

    def get_car_history(self, seconds: Optional[float] = None) -> list[CarFrame]:
        """返回最近 seconds 秒的车辆帧；seconds=None 返回全部窗口内数据"""
        if not self._car_frames:
            return []
        cutoff = self._cutoff(self._car_frames[-1].sim_time, seconds)
        return [f for f in self._car_frames if f.sim_time >= cutoff]

    def get_ranking_history(self, seconds: Optional[float] = None) -> list[RankingSnapshot]:
        """返回最近 seconds 秒的排名快照"""
        if not self._rankings:
            return []
        cutoff = self._cutoff(self._rankings[-1].sim_time, seconds)
        return [r for r in self._rankings if r.sim_time >= cutoff]

    # ── 内部 ────────────────────────────────────────────────────

    def _cutoff(self, latest_time: float, seconds: Optional[float]) -> float:
        w = seconds if seconds is not None else self.window_seconds
        return latest_time - w

    def _evict_car(self, latest_time: float) -> None:
        cutoff = latest_time - self.window_seconds
        while self._car_frames and self._car_frames[0].sim_time < cutoff:
            self._car_frames.popleft()

    def _evict_rankings(self, latest_time: float) -> None:
        cutoff = latest_time - self.window_seconds
        while self._rankings and self._rankings[0].sim_time < cutoff:
            self._rankings.popleft()
