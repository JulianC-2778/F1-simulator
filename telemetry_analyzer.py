#!/usr/bin/env python3
"""Deprecated Coach API debug client; use GET /api/coach/dashboard."""

import json
import time

from midware.client import get_json


def main() -> None:
    print("DEPRECATED: use GET /api/coach/dashboard; removal target: v2.")
    while True:
        try:
            payload = get_json("/api/coach/dashboard?window_seconds=6&history_seconds=16")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"[Coach API] {exc}")
        time.sleep(2.0)


if __name__ == "__main__":
    main()
