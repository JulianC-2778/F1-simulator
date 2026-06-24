#!/usr/bin/env python3
"""
Quick smoke test for the local LM Studio connection used by TORCS middleware.
"""

from __future__ import annotations

import os
import sys

from telemetry_common import (
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_MODEL_NAME,
    chat_completion_text,
    connect_openai_compatible_model,
    print_connection_banner,
)


MODEL_BASE_URL = os.getenv("TORCS_AI_BASE_URL", DEFAULT_MODEL_BASE_URL)
MODEL_NAME = os.getenv("TORCS_AI_MODEL", DEFAULT_MODEL_NAME)
DEFAULT_PROMPT = "You are IBM Granite 4.1 in LM Studio. In one short sentence, confirm that the local connection works."


def main() -> None:
    prompt = " ".join(sys.argv[1:]).strip() or DEFAULT_PROMPT
    connection = connect_openai_compatible_model(
        base_url=MODEL_BASE_URL,
        requested_model=MODEL_NAME,
    )

    print_connection_banner(connection, "LM Studio Smoke Test")
    print(f"Prompt: {prompt}")

    text = chat_completion_text(
        connection,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=80,
        timeout=20.0,
    )

    print("\nModel response:")
    print(text)


if __name__ == "__main__":
    main()
