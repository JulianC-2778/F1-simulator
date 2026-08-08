# Endurance run R01 notes

- Collection date: 2026-08-08
- Run ID: `R01`
- Monitor duration: 1802.0 seconds
- Model backend: DeepSeek API (`https://api.deepseek.com/v1`)
- Model: `deepseek-chat`
- Commentary mode: `hybrid`
- Playback policy: `interrupt`
- TTS: enabled (Kokoro, local)
- Player skill level: `pro`
- Race format: Quick Race, 20 laps per race, repeated `Restart` whenever a
  race finished mid-window (lap-count field would not respond to keyboard
  input in this TORCS session, so laps were kept short and races were
  restarted in quick succession rather than configured to run the full
  30 minutes in one race)

## Operational notes

- The 4 required fault injections (WebSocket disconnect/reconnect, Granite
  unavailable/restored, telemetry/UDP interruption/restored, TTS
  failure/restored) were **not** injected during the 30-minute monitored
  window itself -- an operator error, caught only after the run finished.
  They were run immediately afterward:
  - WebSocket, Granite, and TTS faults were injected against the *same*
    `midware.app` process that had just completed the R01 window (already
    ~35 minutes uptime at that point), so they still exercise a long-lived,
    "aged" process, just not literally inside the stopwatch window.
  - The telemetry/UDP fault was injected on a **freshly restarted**
    `midware.app` process, because a real bug (below) was found in between
    and fixing it required a restart. This one was not tested against the
    aged R01 process.
  - `reconnect_recovery_time_s` = 15.9s, from the WebSocket fault -- this
    is the time until the *next naturally occurring* commentary event after
    reconnecting (no telemetry was force-pushed to trigger one immediately,
    unlike the isolated `RT-04` trial in the fault-injection matrix), so it
    is not directly comparable to `RT-04`'s figure.
- A real defect was found live during this run and is independently
  corroborated by R02's notes (same symptom, different device/session):
  `TelemetryStore._should_reset_session_locked()`'s reset branch
  (`midware/telemetry.py`) cleared `_frames`/`_latest_frame`/`_session_id`
  on a race restart but left `_last_progress_sim_time`/`_last_progress_at`
  pointing at the *previous* session's sim_time, so `/api/health` reported
  `is_stale=true` ("not advancing sim_time") indefinitely after a restart
  even though fresh telemetry was arriving and `sim_time` was genuinely
  advancing in the new session. **Fixed** (test-first): regression test in
  `tests/unit/test_telemetry_store.py`, fix resets both fields alongside
  the existing session-reset block. Verified against real TORCS telemetry
  after restarting midware -- `is_stale` correctly stayed `false` across
  several subsequent race restarts, and correctly flipped to `true` (with
  the *right* reason, "No telemetry packets received recently") during a
  genuine manual telemetry pause, then recovered.
- This bug is diagnostic-only: nothing in the codebase gates event
  detection or commentary generation on `is_stale`, so it did not affect
  R01's actual event/request/output counts below -- only the accuracy of
  `/api/health`'s reporting.

## Recorded result

- Events detected: 316
- Commentary requests: 316
- Successful outputs: 214 (67.72%)
- Model failures: 0
- Duplicate user-visible displays: 0
- Unhandled exceptions: 0
- Outputs over 45 words: 0
- CPU average / peak: 1.9% / 5.2%
- Memory initial / final / peak: 90.3 / 102.0 / 102.7 MB

As in R02: the gap between requests (316) and successful outputs (214)
occurred with zero recorded model failures and is consistent with request
preemption under explicit `interrupt` mode -- this run involved unusually
frequent race restarts (over 20 within the window), which plausibly raised
the preemption rate above R02's. This is an interpretation, not a rewritten
measurement.
