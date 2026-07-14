#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import time


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Forward TORCS telemetry from WSL localhost to the Windows middleware listener."
    )
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=3101)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, default=3101)
    parser.add_argument("--log-every", type=int, default=250)
    args = parser.parse_args()

    listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind((args.listen_host, args.listen_port))

    forward = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.target_host, args.target_port)
    count = 0
    started = time.time()
    print(
        f"WSL UDP telemetry bridge: {args.listen_host}:{args.listen_port} -> "
        f"{args.target_host}:{args.target_port}",
        flush=True,
    )

    while True:
        data, peer = listen.recvfrom(65535)
        forward.sendto(data, target)
        count += 1
        if args.log_every > 0 and count % args.log_every == 0:
            elapsed = max(time.time() - started, 0.001)
            print(
                f"forwarded={count} rate={count / elapsed:.1f}/s "
                f"last_peer={peer[0]}:{peer[1]} bytes={len(data)}",
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
