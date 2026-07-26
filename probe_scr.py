#!/usr/bin/env python3
"""Minimal SCR probe — bypasses ai_bot.py entirely.

Sends constant FULL THROTTLE with a trivial rpm gearbox and prints raw
physics from the server, plus the two numbers that matter for diagnosing a
car that will not accelerate despite accel=1.0:

  simdt : sim-time advance (s) per 50 received packets — should be ~1.00
          (50 ticks x 20 ms).  If it is bigger, we are MISSING ticks and the
          server is driving with stale/default actions in between.
  wall  : wall-clock time (s) for those 50 packets — should also be ~1.00.
          Much smaller means TORCS is running faster than real time.
  t/o   : recv timeouts seen so far.

Usage: start the TORCS quickrace, then  python3 probe_scr.py [host [port]]
"""

import re
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 3001

_TOKEN = re.compile(r"\((\w+)\s+([^)]*)\)")


def parse(msg: str) -> dict:
    out = {}
    for k, v in _TOKEN.findall(msg):
        parts = v.split()
        out[k] = [float(p) for p in parts] if len(parts) > 1 else float(parts[0])
    return out


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5.0)
    init = "SCR(init " + " ".join(str(a) for a in (
        -90, -75, -60, -45, -30, -20, -15, -10, -5, 0,
        5, 10, 15, 20, 30, 45, 60, 75, 90)) + ")"
    for _ in range(5):
        sock.sendto(init.encode(), (HOST, PORT))
        try:
            data, _addr = sock.recvfrom(1000)
        except socket.timeout:
            print("handshake timeout, retrying…")
            continue
        if b"identified" in data:
            break
    else:
        print("Could not identify with TORCS — is the race running?")
        return

    sock.settimeout(1.0)
    print("identified — full throttle probe running (Ctrl-C to stop)\n")

    n = timeouts = 0
    gear = 1
    first_raw_shown = False
    last_wall = time.monotonic()
    last_sim = None

    while True:
        try:
            data, _addr = sock.recvfrom(1000)
        except socket.timeout:
            timeouts += 1
            continue
        text = data.rstrip(b"\x00").decode(errors="replace")
        if text.startswith("***"):
            print(f"\nserver message: {text}  (after {n} packets, {timeouts} timeouts)")
            break

        s = parse(text)
        if not first_raw_shown:
            first_raw_shown = True
            print("FIRST RAW PACKET:\n" + text + "\n")

        rpm   = s.get("rpm", 0.0)
        speed = s.get("speedX", 0.0)
        # trivial gearbox
        if gear < 6 and rpm > 8500:
            gear += 1
        elif gear > 1 and rpm < 3500:
            gear -= 1
        angle = s.get("angle", 0.0)
        tpos  = s.get("trackPos", 0.0)
        steer = angle * 10.0 / 3.14159 - tpos * 0.5
        sock.sendto(
            f"(accel 1.0)(brake 0.0)(gear {gear})(steer {steer:.3f})"
            f"(clutch 0.0)(focus 0)(meta 0)".encode(), (HOST, PORT))

        n += 1
        if n % 50 == 0:
            now  = time.monotonic()
            simt = s.get("curLapTime", 0.0)
            simdt = (simt - last_sim) if last_sim is not None else 0.0
            print(
                f"n={n:5d}  {speed:6.1f} km/h  rpm={rpm:5.0f}  gear={gear}  "
                f"dist={s.get('distRaced', 0.0):7.1f}  tpos={tpos:+.2f}  "
                f"angle={angle:+.2f}  z={s.get('z', 0.0):.2f}  "
                f"dmg={s.get('damage', 0.0):4.0f}  "
                f"simdt={simdt:5.2f}s  wall={now - last_wall:5.2f}s  t/o={timeouts}"
            )
            last_wall, last_sim = now, simt


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
