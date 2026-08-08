# Endurance run R02 notes

- Collection date: 2026-08-08
- Run ID: `R02`
- Monitor duration: 1802.0 seconds
- Model backend: DeepSeek API (`https://api.deepseek.com/v1`)
- Model: `deepseek-chat`
- Commentary mode: `hybrid`
- Playback policy: `interrupt`
- TTS: disabled
- Player skill level: `pro`
- Planned race length: 99 laps

## Operational notes

- This run was collected as a continuous 30-minute endurance run without
  repeating WebSocket, model, telemetry, or TTS fault injection. Fault
  recovery was covered separately by the completed 60-trial fault matrix.
- `reconnect_recovery_time_s` is intentionally blank because no reconnect
  was injected during this run.
- The player car was totalled during the window and the race was restarted
  inside TORCS. The middleware and endurance monitor were not restarted, so
  the same wall-clock monitoring window and output row were preserved.
- TORCS telemetry health retained a stale `last_progress_at` value across
  race-session resets, although direct telemetry samples confirmed that
  packets and `sim_time` continued advancing.

## Recorded result

- Events detected: 249
- Commentary requests: 249
- Successful outputs: 188 (75.50%)
- Model failures: 0
- Duplicate user-visible displays: 0
- Unhandled exceptions: 0
- Outputs over 45 words: 0
- CPU average / peak: 5.9% / 12.6%
- Memory initial / final / peak: 92.1 / 92.9 / 92.9 MB

The gap between requests and successful outputs occurred with zero recorded
model failures and is consistent with request preemption under explicit
`interrupt` mode; this is an interpretation, not a rewritten measurement.
