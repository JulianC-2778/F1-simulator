"""
main.py — FastAPI 中间层

启动方式：
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

环境变量：
    TORCS_UDP_PORT    UDP 监听端口，默认 3101
    CACHE_WINDOW_SEC  滑动窗口秒数，默认 10
"""

import asyncio
import os
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from parser import parse_packet, CarFrame, RankingEntry
from cache import SlidingCache, RankingSnapshot

# ── 配置 ────────────────────────────────────────────────────────
UDP_PORT    = int(os.getenv("TORCS_UDP_PORT",    "3101"))
WINDOW_SEC  = float(os.getenv("CACHE_WINDOW_SEC", "10"))

# ── 全局缓存 ────────────────────────────────────────────────────
cache = SlidingCache(window_seconds=WINDOW_SEC)

# ── FastAPI ─────────────────────────────────────────────────────
app = FastAPI(title="TORCS Middleware", version="0.1.0")


# ── UDP 监听协议 ────────────────────────────────────────────────

class TorcsUdpProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr) -> None:
        packet = parse_packet(data)
        if packet.car:
            cache.push_car(packet.car)
        if packet.rankings:
            cache.push_rankings(
                sim_time=packet.rankings[0].sim_time,
                entries=packet.rankings,
            )

    def error_received(self, exc: Exception) -> None:
        print(f"[UDP] error: {exc}")


# ── 生命周期：启动时开启 UDP 监听 ───────────────────────────────

@app.on_event("startup")
async def start_udp():
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        TorcsUdpProtocol,
        local_addr=("0.0.0.0", UDP_PORT),
    )
    print(f"[UDP] listening on port {UDP_PORT}")


# ── REST 接口 ───────────────────────────────────────────────────

def _car_to_dict(frame: CarFrame) -> dict:
    return frame.raw


def _ranking_to_dict(entry: RankingEntry) -> dict:
    return {
        "car_index":       entry.car_index,
        "car_name":        entry.car_name,
        "race_pos":        entry.race_pos,
        "laps":            entry.laps,
        "dist_from_start": entry.dist_from_start,
    }


@app.get("/state", summary="最新一帧车辆数据")
async def get_state():
    """返回最近收到的一帧车辆传感器数据"""
    if cache.latest_car is None:
        return JSONResponse(status_code=503, content={"detail": "No data yet"})
    return _car_to_dict(cache.latest_car)


@app.get("/history", summary="滑动窗口内的历史车辆数据")
async def get_history(seconds: Optional[float] = Query(default=None, description="最近多少秒，默认全窗口")):
    """返回最近 N 秒的车辆帧列表"""
    frames = cache.get_car_history(seconds)
    return {"count": len(frames), "frames": [_car_to_dict(f) for f in frames]}


@app.get("/rankings", summary="最新排名")
async def get_rankings():
    """返回最近一次排名快照"""
    snap = cache.latest_ranking
    if snap is None:
        return JSONResponse(status_code=503, content={"detail": "No ranking data yet"})
    return {
        "sim_time": snap.sim_time,
        "rankings": [_ranking_to_dict(e) for e in snap.entries],
    }


@app.get("/rankings/history", summary="滑动窗口内的排名历史")
async def get_rankings_history(seconds: Optional[float] = Query(default=None)):
    snaps = cache.get_ranking_history(seconds)
    return {
        "count": len(snaps),
        "snapshots": [
            {"sim_time": s.sim_time, "rankings": [_ranking_to_dict(e) for e in s.entries]}
            for s in snaps
        ],
    }


@app.get("/health", summary="健康检查")
async def health():
    car = cache.latest_car
    return {
        "status": "ok",
        "udp_port": UDP_PORT,
        "window_seconds": WINDOW_SEC,
        "car_frames_cached": len(cache._car_frames),
        "ranking_snapshots_cached": len(cache._rankings),
        "latest_sim_time": car.sim_time if car else None,
    }
