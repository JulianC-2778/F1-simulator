# Architecture refactor Phase 0 baseline

Recorded on 2026-07-20 before the Phase 0-3 implementation batch.

## Repository state

- Branch: `refactor/architecture`
- HEAD: `fc6cb1743742560953ee7d70cdbdb978224891ae`
- Worktree: clean
- Repository `AGENTS.md`: not present

## Shared ports

`config.json` and `config.py` resolve the following defaults, with environment
variables taking precedence:

- Middleware HTTP/WebSocket: `127.0.0.1:8880`
- Human telemetry UDP: `3101`
- SCR bot UDP: `3001`
- Legacy Feature 2 service: `8766`
- TTS: `8881`

## Existing entrypoints and interfaces

- Main backend: `python3 midware/commentary.py`
- Engineer debug clients: `python3 chat_engineer.py` and `python3 chat_engineer_gui.py`
- Coach legacy services: `python3 midware/feature2_service.py` and `python3 telemetry_analyzer.py`
- Bot: `python3 ai_bot.py --bot`
- REST routes are currently defined in `midware/commentary.py`, including config,
  feature status, health, commentary, telemetry, coach, engineer and bot status routes.
- WebSocket route: `/ws`; legacy outbound types include `connected`, `ai_start`,
  `token`, `ai_done`, `event_detected`, `telemetry_update`, `message`, `error` and `pong`.

The target `python3 -m midware.app` entrypoint is intentionally deferred to Phase 6,
when the app factory and router split are introduced.

## Baseline tests

- Project `py_compile` command: PASS
- `python3 ai_bot.py`: PASS (`All tests passed.`)
- `python3 test_a_module_latency.py`: PASS (`ALL TESTS PASSED`)

The default system Python environment did not include FastAPI/Pydantic, while the
checked-in local `midware/.venv-py314` environment contained FastAPI and Pydantic.
Pytest was not installed in that environment at baseline.
