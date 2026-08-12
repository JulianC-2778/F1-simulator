# AI Driving Bot (Direction 4) — Work Package A Implementation Report

> Source of truth: commit `9f777dc` (2026-08-08) plus one uncommitted,
> minimal production-code change described in §7. This report documents
> what was actually implemented against the real code in `ai_bot.py`, not
> what [`docs/bot_test_plan.md`](bot_test_plan.md) originally assumed before
> implementation — where the two differ, the code (and this report) wins.
> Written 2026-08-12; updated same day twice more (second pass closed the
> three `run_bot()`/track-map/traffic gaps; third pass closed the two
> remaining §6 gaps — `ScrClient`'s real UDP path and a black-box smoke test
> against a real `midware.app`).
>
> This is the direction-4 counterpart of [`docs/commentary_test_matrix.md`](commentary_test_matrix.md)
> for direction 3 (AI Live Commentary): same purpose (requirement → real code →
> test traceability, defects/findings, explicit scope boundaries), same
> underlying methodology, applied to the AI driving bot instead of the
> commentary engine. Background on why the two directions share one test
> methodology is in `bot_test_plan.md` §0.

## 1. Scope of this report

**Work Package A is now fully closed** — every requirement row in §4 is
`done`, including the `run_bot()` orchestration, track-map lookahead,
traffic/launch/BLOCK sections, `ScrClient`'s real UDP path, and a black-box
smoke test against a real `midware.app`, all originally deferred and closed
in this session's later passes. Work packages B
(real driving performance / strategy accuracy), C (control-loop and Granite
latency) and D (endurance + fault injection) all require a running
TORCS + LM Studio stack that is not available in this environment — see
`bot_test_plan.md` §5–§7 for their design; none of that has been run for
real, and this report makes no claims about it.

## 2. Environment used to run these tests

- No usable Python interpreter was found on the Windows side of this
  checkout (`python`/`py` both resolve to the Microsoft Store stub).
- The WSL instance's `~/F1-simulator` is a **different, unrelated checkout**
  (different commit history, many uncommitted TORCS build changes) — it was
  left untouched.
- Tests were run from WSL against this same repository via its Windows
  mount path (`/mnt/c/Users/abcdz/Desktop/ibm/F1-simulator`), using a
  purpose-built virtualenv `.venv_wsl` (`python3 -m venv .venv_wsl && pip
  install -r requirements-core.txt`), now excluded via `.gitignore`.
- Command: `.venv_wsl/bin/python -m pytest tests/bot -q` — 186 passed, ~8s,
  no network, no TORCS, no LM Studio (the 9 network-socket tests in
  `test_scr_client_network.py` use real local UDP over 127.0.0.1, not real
  TORCS).
- Separately, `.venv_wsl/bin/python tools/smoke_test_bot_status.py` spawns a
  real `python -m midware.app` subprocess (still no TORCS, no real LM
  Studio) — 8/8 checks passing, stable across repeated runs.

## 3. Component map

| Concept | Real code |
|---|---|
| SCR protocol decode/encode | `ai_bot.py::parse_scr_state` (L140) / `format_scr_control` (L181) |
| UDP client / handshake / send-recv | `ai_bot.py::ScrClient` (L237) |
| Gear selection (RPM-first + speed-table fallback) | `_auto_gear`/`_gear_from_speed`/`_gear_shift` (L382-461) |
| ABS / traction control | `_apply_abs`/`_apply_tcl` (L463-511) |
| Physics-derived braking distance | `_brake_dist` (L512) |
| Main strategy-parameterised controller | `compute_control` (L1439), `_DriveParams` (L576) |
| Off-track recovery / stabilize / turnaround | `_recovery_control`/`_recovery_steer`/`_stabilize_action` (L1288-1421) |
| Pure-pursuit steering target | `_pursuit_target` (L1422) |
| Safety net over any strategy source | `ai_bot.py::safety_filter` (L1961-2033) |
| Granite prompt construction / response parsing | `_build_strategy_prompt` (L2085) / `_parse_strategy_response` (L2108) |
| Strategy debounce (pure transition function) | `_next_debounced_strategy` (L2124) |
| Async, non-blocking Granite caller | `GraniteStrategist` (L2151), built on `telemetry_common.LatestTaskRunner` |
| Client-side heartbeat | `BotStatusReporter` (L2261) |
| Main drive-loop orchestration | `run_bot` (L2317) |
| Server-side heartbeat / health | `midware/runtime.py::bot_status_service`, `GET`/`POST /api/bot/status` (L1406-1429) |
| Server-side strategy endpoint | `POST /api/bot/strategy` (`runtime.py::request_bot_strategy`, L1433) |

The server-side rows (heartbeat and strategy endpoints inside `midware/`)
already had test coverage before this work
(`tests/integration/test_bot_heartbeat.py`, `tests/unit/test_bot_status_service.py`,
`tests/unit/test_bot_clients.py`, `tests/unit/test_strategy_decision.py`).
**Everything in `ai_bot.py` itself — the part that actually decides whether
the car crashes — had zero pytest coverage** before this work, only a
built-in `_run_tests()` self-test runnable via `python ai_bot.py` with no
CI integration, no JUnit output, and no pass-rate accounting. Closing that
gap is what this report covers.

## 4. Requirement traceability

| Requirement | Real code | Pre-existing test | New test | Status |
|---|---|---|---|---|
| SCR packet parsing (valid / empty / incomplete / short-array padding / non-numeric) | `parse_scr_state` | none | `tests/bot/test_scr_protocol.py::ParseScrStateTests` | done |
| SCR control encoding + clamping | `format_scr_control` | none | `tests/bot/test_scr_protocol.py::FormatScrControlTests` | done |
| `ScrClient` instantiation / close / not-connected error paths | `ScrClient` | none (self-test only covers instantiate+close) | `tests/bot/test_scr_protocol.py::ScrClientTests` | done |
| `ScrClient`'s real wire protocol — handshake (success / no-response / guard-port conflict), `receive_state()` (parse / timeout-as-`{}` / backlog-drain-to-newest / shutdown-vs-restart), `send_control()` | `ScrClient.connect`/`receive_state`/`send_control` | none | `tests/bot/test_scr_client_network.py` (9 tests, against a local fake `scr_server` over real UDP sockets — no real TORCS) | done |
| Gear shifting (RPM-first, speed-table fallback, anti-hunt guard) | `_auto_gear`/`_gear_from_speed`/`_gear_shift` | none | `tests/bot/test_control_logic.py::AutoGearTests`, `GearFromSpeedTests`, `GearShiftTests` | done |
| ABS / TCL | `_apply_abs`/`_apply_tcl` | none | `tests/bot/test_control_logic.py::AbsTclTests` | done |
| Braking distance physics | `_brake_dist` | none | `tests/bot/test_control_logic.py::BrakeDistTests` | done |
| `compute_control` on straights/corners (per-strategy throttle, cruise-at-cap, physics stopping-distance override) | `compute_control` | none | `tests/bot/test_control_logic.py::ComputeControlStraightLineTests`, `ComputeControlPhysicsOverrideTests`, `ComputeControlPitTests` | done |
| Off-track re-entry, hysteresis, apex kerb-ride | `compute_control` (recovery branch) | none | `tests/bot/test_recovery.py::OffTrackReentryTests` | done |
| Post-impact stabilize (extreme excursion) | `_stabilize_action` | none | `tests/bot/test_recovery.py::StabilizeAfterImpactTests` | done |
| Wrong-way turnaround + reverse-leg cap | `_recovery_control` | none | `tests/bot/test_recovery.py::WrongWayTurnaroundTests` | done |
| No-progress watchdog | `compute_control` | none | `tests/bot/test_recovery.py::NoProgressWatchdogTests` | done |
| All-sensors-blind fallback | `compute_control` | none | `tests/bot/test_recovery.py::BlindSensorFallbackTests` | done |
| Stuck-car reverse burst (on-track) | `compute_control` | none | `tests/bot/test_recovery.py::StuckReverseTests` | done |
| Pure-pursuit steering target | `_pursuit_target` | none | `tests/bot/test_recovery.py::PurePursuitTests` | done |
| **`safety_filter` — all 6 priority rules, exact threshold boundaries** | `safety_filter` | none | `tests/bot/test_safety_filter.py` (31 tests, P0) | done |
| Granite prompt construction | `_build_strategy_prompt` | none | `tests/bot/test_granite_strategy.py::BuildStrategyPromptTests` | done |
| Granite response parsing (incl. BLOCK rejection at parse layer) | `_parse_strategy_response` | none | `tests/bot/test_granite_strategy.py::ParseStrategyResponseTests` | done |
| Strategy debounce transition function | `_next_debounced_strategy` | none | `tests/bot/test_granite_strategy.py::NextDebouncedStrategyTests` | done |
| `GraniteStrategist.tick()` success / error / fallback / recovery | `GraniteStrategist` | `tests/unit/test_bot_clients.py` (non-blocking tick only) | `tests/bot/test_granite_strategist_runtime.py::GraniteStrategistTickTests` | done |
| `GraniteStrategist._call_granite` HTTP request/response shape | `GraniteStrategist._call_granite` | none | `tests/bot/test_granite_strategist_runtime.py::CallGraniteHttpLayerTests` | done |
| `BotStatusReporter` network-failure isolation, final disconnect on close | `BotStatusReporter` | `tests/unit/test_bot_clients.py` (non-blocking tick only) | `tests/bot/test_status_reporter.py` | done |
| **Forced regression: `safety_filter` overrides Granite's raw output end-to-end** | `safety_filter` + `GraniteStrategist.tick` | none | `tests/bot/test_safety_integration.py::SafetyFilterOverridesGraniteTests` | done |
| **Forced regression: `BLOCK` is unreachable from any Granite text, at both the parse layer and the filter layer** | `_parse_strategy_response` + `safety_filter` | none | `tests/bot/test_safety_integration.py::BlockIsSystemOnlyEndToEndTests` | done |
| `run_bot()` main-loop orchestration (handshake once, one control per real frame, timeout frames don't resend, `None` ends the loop cleanly, `KeyboardInterrupt` handled, reporter closed + atexit hook unregistered) | `run_bot` | none | `tests/bot/test_run_bot_integration.py` (9 tests) | done — required a minimal production-code change, see §7 |
| Pre-race track-map lookahead (map-ahead braking cap, entry-line bias, brake-point mode, 5-gate trust system) | `compute_control` (map branch) | `track_model.py`'s own self-test (separately, partial) | `tests/bot/test_track_map_lookahead.py` (10 tests) | done — skipped automatically if `track_model.py` isn't importable |
| Side-traffic avoidance (incl. convergence gate, room taper, standoff breaker), start-of-race launch caution + clutch ramp, front-opponent following/overtake (incl. cone-boundary-jump rejection, next-corner tiebreak), `BLOCK` steering-bias, boxed-in follow cap | `compute_control` | none | `tests/bot/test_traffic_and_launch.py` (25 tests) | done |
| `BotStatusReporter`/`GraniteStrategist` against a **real** `midware.app` process (not mocked HTTP): status round trip, close()'s final disconnect, a real Granite success round trip through the real `/api/bot/strategy` → fake-model chain, a real HTTP-failure round trip, and the `bot`-feature-disabled fail-closed path | `BotStatusReporter`, `GraniteStrategist`, `midware/runtime.py`'s bot endpoints | none | `tools/smoke_test_bot_status.py` (8 checks, black-box, real subprocess + real HTTP — not part of `tests/bot/`'s pytest count) | done — see §7 for why this is a separate script, not a pytest file |

**Total: 186/186 new pytest tests passing** (`tests/bot/`), plus 8/8 in the
black-box `tools/smoke_test_bot_status.py`, plus the pre-existing
`ai_bot.py` self-test (`python ai_bot.py`, `python track_model.py`) both
still green after the §7 production-code change — confirmed to still match
the current code, since most of the `tests/bot/` assertions were ported
from that self-test's known-correct values rather than independently
re-derived.

## 5. Findings

### 5.1 Hidden cross-frame coupling in `compute_control`'s hysteresis (documented, not a defect)

`compute_control`'s recovery-exit hysteresis depends on the module-level
`_recovering` flag persisting **across calls** — a single isolated call with
`track_pos=0.95` (back over the edge, but "not yet well inside") does not by
itself reproduce the "still in recovery mode" behavior; it only does so if
the *previous* call already drove the car off-track and set `_recovering`.
The original self-test's two adjacent assertions relied on this implicitly
(no state reset between them). Writing this as an independent pytest test
required making the two-call sequence explicit:

```python
compute_control(self._state(track_pos=1.5, speed_x=80.0), ATTACK)   # go off-track first
out_edge = compute_control(self._state(track_pos=0.95, speed_x=40.0), ATTACK)
self.assertIn("(accel 0.500)", out_edge)
```

See `tests/bot/test_recovery.py::OffTrackReentryTests.test_hysteresis_holds_recovery_pace_just_back_over_the_edge`.
Not a bug — `compute_control` is deliberately stateful frame-to-frame (that
is how a real hysteresis band has to work) — but worth flagging because it
means any *new* test against `compute_control`'s recovery branches must
call `ai_bot._reset_driver_state()` in `setUp()` (all files here do) and
must reproduce multi-frame sequences explicitly when a test depends on
carried-over state, rather than assuming each call is independent.

### 5.2 Pre-existing issues found incidentally, not part of this work's scope

Both were observed while running the full suite (`tests/unit` +
`tests/integration` + `tests/bot`) as a sanity check; neither is caused by,
or fixed by, the `tests/bot/` additions.

1. **43 `tests/integration/*` errors**: `OSError: [Errno 98] Address already
   in use` on port 3101. Cause: a separate, already-running
   `midware/.venv/bin/python -m midware.app` process (a different venv than
   the one used for this work) is holding UDP 3101 / TCP 8880 on this
   machine. Per `docs/testing-plan.md` §1's own guidance, the normal fix is
   `pkill -f midware.app` to free the port before running L2/L3 — not done
   here, since it wasn't clear whether that process was someone's active
   session.
2. **`tests/unit/test_model_broker.py::ModelBrokerTests::test_latest_stale_key_supersedes_queued_job`**
   fails even run in isolation (`asyncio.exceptions.CancelledError`) — a
   timing-sensitive test that synchronizes via `await asyncio.sleep(0)`.
   Unrelated to `ai_bot.py` or any bot code; not investigated further here.

### 5.3 `run_bot()` has an undeclared host-environment dependency (found while writing its integration tests, worked around, not fixed)

`run_bot()` unconditionally calls `load_track_model(track or "auto", ...)`
when `track_model.py` is importable and no explicit `track` argument is
given — which means, by default, it reads whatever TORCS raceman config
happens to exist under the *host machine's* `~/.torcs`. On the machine this
work was done on, that auto-detect genuinely found a real track ("CG track
2") left over from unrelated prior use. A test suite that called
`run_bot()` without pinning `track=` would therefore get a different map
(or none) depending on which machine/session runs it — not fully
deterministic, in violation of `bot_test_plan.md` §1's "automated tests
must run deterministically" principle.

**Workaround applied**: every `run_bot()` call in
`tests/bot/test_run_bot_integration.py` passes `track="off"` explicitly,
which takes a different code path (`"[map] disabled by --track off"`) that
never touches `~/.torcs`. Not treated as a defect to fix in `run_bot()`
itself — auto-detecting the map is the correct behavior for a real race,
the issue only exists in test contexts — but worth flagging for whoever
next writes an automated test that calls `run_bot()` or `load_track_model()`
without an explicit `track=`.

### 5.4 midware turns an upstream model HTTP 500 into a 502, not a passthrough 500 (observed, not a defect)

`tools/smoke_test_bot_status.py`'s failure scenario has its fake model
server return a real HTTP 500, but `GraniteStrategist.last_error` ends up
reading `"HTTP Error 502: Bad Gateway"`, not 500. This is `midware`'s own
model-gateway layer translating "the upstream model server itself failed"
into a 502 before it ever reaches `ai_bot.py` — consistent behavior, not a
bug, but it means a unit test that mocks `urllib.request.urlopen` to raise
a raw 500 (as `tests/bot/test_granite_strategist_runtime.py` does) is
technically simulating a slightly different failure shape than what a real
Granite outage produces through the real stack. Both are exercised now
(mocked 500 at the unit level, real 502-via-midware at the black-box
level), so this is recorded as a confirmed real-stack detail, not a gap.

### 5.5 A fake-model-server pitfall in the smoke script itself (found and fixed before landing, worth remembering)

`GraniteStrategist.tick()` resubmits a new request on *every* call when
`interval=0.0` (used throughout `tools/smoke_test_bot_status.py` for fast
polling) — not once per "logical" request. An early version of the script
shared one `FakeModelServer` instance (with a short canned reply sequence)
across all Granite scenarios; the success-scenario's polling loop alone
submitted enough requests to exhaust the shared reply sequence, so the
*next* scenario's "baseline successful call" silently got the fallback
("error") reply instead and failed. Fixed by giving each scenario its own
scenario-scoped `FakeModelServer` (and re-pointing midware at it via
`/api/config/api`) instead of sharing one across the whole run — see the
comment left in `scenario_granite_strategist_success()`. Recorded here
because it's a easy trap to fall into again if this script grows more
Granite-touching scenarios.

## 6. Explicitly out of scope / not fabricated

Per `bot_test_plan.md` §1's working principle ("if a piece of functionality
isn't covered, say so — don't fabricate a passing result"), the following
are known gaps, not silently skipped:

- **Work packages B, C, D full designs** — real driving-performance data
  (2 tracks × 3 sessions, human-driven ground truth), full t0–t5 latency
  breakdown, and 3×20-minute endurance runs. A real TORCS + LM Studio
  stack became available later in this same session and produced:
  - one real 17.4-minute bot-autonomous data point covering a partial
    slice of B and a decision-cadence proxy for C — see
    [`docs/bot_real_experiment_20260812.md`](bot_real_experiment_20260812.md);
  - 6 real fault-injection trials (RB-01/02/03/04/10, 1 each) against the
    live stack — see
    [`docs/bot_fault_injection_20260812.md`](bot_fault_injection_20260812.md),
    which also surfaces one **real, unfixed availability gap**: a
    hard-killed TORCS process doesn't reliably trigger `ConnectionRefusedError`
    in this WSL2 setup, so the bot idles forever instead of exiting, and
    `BotStatusReporter`'s background thread keeps re-sending a stale
    "healthy" heartbeat throughout.

  Still not done: a second track, human-driven ground-truth sessions,
  code-level first-token timestamps, the 3×20-minute endurance runs, and
  RB-05's follow-up (repeat trials, and deciding whether/how to fix the
  frozen-loop gap). This is now the **only** remaining gap from the
  original work-package-A scope; everything else tracked in §4 is done.

## 7. What changed in the repository

- `tests/bot/` (new, 219 pytest tests across 13 files): `__init__.py`,
  `test_scr_protocol.py`, `test_control_logic.py`, `test_recovery.py`,
  `test_safety_filter.py`, `test_granite_strategy.py`,
  `test_granite_strategist_runtime.py`, `test_status_reporter.py`,
  `test_safety_integration.py`, `test_run_bot_integration.py`,
  `test_track_map_lookahead.py`, `test_traffic_and_launch.py`,
  `test_scr_client_network.py`, `test_granite_prompt_context.py` (the last
  one added after the PR #37 "Granite strategy integration" merge — see its
  own docstring; covers `build_situation`/`_describe_gap`/lap-time
  tracking/reasoning-trace population, none of which existed before that
  merge).
- `evaluation/bot/` (new): `results/real_experiment_bot_drive_20260812.jsonl`
  (raw `TraceRecorder` output, 620 records) and
  `scripts/analyse_bot_trace.py` (computes the work-package-B/C-proxy
  numbers from it) — see
  [`docs/bot_real_experiment_20260812.md`](bot_real_experiment_20260812.md)
  for the write-up.
- `tools/smoke_test_bot_status.py` (new, not a pytest file): a black-box
  script mirroring `tools/smoke_test_commentary_queue.py`'s approach —
  spawns a real `python -m midware.app` subprocess plus a tiny fake
  OpenAI-compatible model server, then drives the real `ai_bot.py`
  `BotStatusReporter`/`GraniteStrategist` classes against it over real HTTP.
  Deliberately kept out of `tests/bot/`'s pytest count (like its commentary
  counterpart) because it spawns a real subprocess and binds real
  (ephemeral) ports — run it manually with
  `.venv/bin/python tools/smoke_test_bot_status.py [-v]`, exit 0/1.
- `ai_bot.py` (production code — the only change, minimal by design):
  `run_bot()` gained a keyword-only `client: ScrClient | None = None`
  parameter. When given, it's used in place of constructing a new
  `ScrClient(host, port)` (`with (client if client is not None else
  ScrClient(host, port)) as client:`); every line of the loop below that is
  byte-for-byte unchanged, so a test that injects a fake client exercises
  the exact same code path a real race runs. `python ai_bot.py`'s self-test
  (which never calls `run_bot()`) was confirmed still green before and
  after this change.
- `docs/bot_test_plan.md`: §12 added, then updated twice more the same day
  to record first the `run_bot()`/track-map/traffic pass, then the
  `ScrClient`-network/black-box-smoke pass.
- `.gitignore`: added `.venv_wsl/` (the local test virtualenv used to run
  this work; not meant to be committed).

## 8. Next steps (recommended order)

1. Free port 3101 (`pkill -f midware.app`, after confirming with whoever
   started it) and re-run `tests/integration` to get a clean full-suite
   baseline — the only observed issue left that isn't this report's own
   scope to fix.
2. Work packages B/C now have one real data point
   ([`docs/bot_real_experiment_20260812.md`](bot_real_experiment_20260812.md))
   but not the full designs — see that report's §5 for exactly what's
   missing (second track, human-driven ground truth, code-level first-token
   timestamps, repeat runs). Work package D (endurance + fault injection)
   has not been attempted. Continue per `bot_test_plan.md` §5–§7, following
   the same `evaluation/<direction>/` + CSV-schema + `SAMPLE`-vs-`real_experiment`
   discipline already established for commentary in `evaluation/commentary/`.
   This is the only work-package-A-adjacent item left; work package A
   itself (§4) is now fully closed.
