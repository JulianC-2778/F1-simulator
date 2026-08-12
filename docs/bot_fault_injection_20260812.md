# AI Driving Bot — Real Fault Injection Report (2026-08-12)

> Status: **real trials**, not simulated/mocked. Run against a real TORCS
> instance (E-Track 3), real `midware.app`, and real LM Studio
> (`granite-4.1-8b`). This is a partial pass through
> [`docs/bot_test_plan.md`](bot_test_plan.md) §7.2's RB-01..RB-10 fault
> catalog — 6 of 10 fault IDs, 1 trial each (the full design asks for 5
> trials per fault) — chosen for the ones that specifically need a live
> stack to verify, rather than the ones already covered by mocked unit
> tests in `tests/bot/`.

## Summary

| ID | Fault | Result |
|---|---|---|
| RB-01 | Granite/midware model endpoint unreachable | ✅ Pass — controlled fallback, no crash |
| RB-02 | Granite restored | ✅ Pass — automatic recovery, no bot restart |
| RB-03 | `midware.app` killed entirely | ✅ Pass — bot completely unaffected |
| RB-04 | `midware.app` restarted | ✅ Pass — automatic reconnect, no bot restart |
| RB-10 | `bot` feature disabled | ✅ Pass — fails closed, keeps driving, no crash |
| RB-10 restore | `bot` feature re-enabled | ✅ Pass — automatic recovery |
| RB-05 | TORCS process hard-killed mid-session | ⚠️ **Real gap found** — see §2 |

## 1. RB-01/02 — Granite unavailable / restored

**Inject** (13:42:38): `POST /api/config/api` pointed `base_url` at
`http://127.0.0.1:1/v1` (nothing listens there) while the bot was driving
at real speed under Granite control.

**Observed** (13:42:53, 15s later):
```json
{"strategy":"ATTACK","speed_kmh":241.199,"fallback":true,
 "error":"HTTP Error 502: Bad Gateway","health":"degraded"}
```
- `midware`'s own gateway layer turns the upstream failure into a 502
  before `ai_bot.py` ever sees it (same behavior confirmed earlier in
  `docs/bot_real_experiment_20260812.md`'s work-package-C section).
- The car **kept driving at real speed** (241 km/h) on the last confirmed
  strategy (`ATTACK`) — no stall, no crash.
- `safety_filter`'s own reflexes kept working independently of Granite: a
  few seconds later the log shows `strategy=BLOCK` firing on its own
  (`bgap=17.7` inside `_BLOCK_TRIGGER_GAP=20.0`) — proof the per-frame
  safety net doesn't depend on the 5s Granite poll being alive.
- `midware`'s own `/api/health` stayed `200` throughout.

**Restore** (13:43:55): `base_url` set back to the real LM Studio address.

**Observed** (13:44:09, 12s later):
```json
{"strategy":"ATTACK","fallback":false,"error":"","health":"healthy"}
```
Full real reasoning trace (`considered`/`rejected`) back within one poll
cycle. **No `ai_bot.py` restart was needed.**

## 2. RB-03/04 — `midware.app` killed / restarted

**Inject** (13:45:37): `kill <midware pid>` (SIGTERM), confirmed dead via
`ps -p` (no such process) and `ss -tln` (port 8880 no longer listening).

**Observed**: `curl` to `/api/health` returned connection failure
(`http_code=000`) as expected. Meanwhile the **bot's own log kept printing
new `step=` lines**, car still at real speed (202 km/h), `strategy=ATTACK`,
`mode=race`, undisturbed — `BotStatusReporter`'s and `GraniteStrategist`'s
network calls fail silently (both wrap their HTTP calls so failures don't
propagate into the drive loop), exactly per design.

**Restore** (13:46:48): `midware.app` restarted via
`setsid nohup midware/.venv/bin/python -m midware.app &` (plain `nohup ... &`
without `setsid` was tried first and did **not** survive past the spawning
shell's own exit — a real operational gotcha for whoever scripts this next:
`setsid` is required for a background midware/bot process to outlive the
shell that launched it in this environment).

**Observed** (13:47:00, ~12s after restart): `/api/health` returns `200`
again; `/api/bot/status` briefly showed `health:"disconnected"` right at
the reconnection boundary (a stale/empty snapshot from the freshly-restarted
midware, expected since bot status is in-memory and resets on restart), then
within another ~8s settled to `health:"healthy", heartbeat_age_s:0.6`,
`fallback:false`. **No `ai_bot.py` restart was needed** — its own retry
loops (heartbeat every ~1s, strategy poll every ~5s) found the revived
`midware` on their own.

## 3. RB-10 — `bot` feature disabled / re-enabled

**Inject** (13:49:15): `POST /api/features/enabled` with `bot` omitted from
the list.

**Observed** (15s later):
```json
{"strategy":"ATTACK","speed_kmh":103.271,"fallback":false,"error":"",
 "health":"disconnected","heartbeat_age_s":25.909}
```
Server-side `bot_status_service` correctly shows `disconnected` — every
`connected:true` heartbeat POST from the client is being rejected with 409
per `runtime.py`'s own check, so the server-side snapshot goes stale. The
**bot itself kept driving** the whole time (still incrementing steps, real
speed, `mode=race`, `strategy=BLOCK` at one point from its own safety
reflex) — it never crashes on a string of 409s, it just can't successfully
report status or fetch new strategy decisions while the feature is off
(`GraniteStrategist` would itself have gone to `fallback=True` on the 409s,
holding the last confirmed strategy — consistent with RB-01's behavior,
same code path).

**Restore** (13:50:21): `bot` re-added to `enabled`.

**Observed** (8s later): `health:"healthy"`, `heartbeat_age_s:0.469`,
`fallback:false` — immediate recovery, no restart.

## 4. RB-05 — TORCS hard-killed mid-session: a real gap

**Inject** (13:51:16): `kill -9 <torcs-bin pid>` while the bot was
mid-drive (`dmg=3759` from an earlier real collision, `strategy=DEFEND`,
164 km/h at the moment of the kill).

**Expected** (per `bot_test_plan.md`'s RB-05 design, and per
`tests/bot/test_scr_client_network.py`'s own unit coverage of this code
path): `ScrClient.receive_state()` eventually raises/observes
`ConnectionRefusedError` on the now-dead UDP peer, returns `None`,
`run_bot()` prints `"Race ended — exiting loop."` and exits cleanly.

**Actually observed**: **it did not exit.** 35+ seconds after the kill,
`pgrep` still showed the `ai_bot.py` process alive, no new `step=` lines
had been logged (the loop was stuck returning `{}` from `receive_state()` —
the "timeout, keep waiting" branch — not `None`), and:

```
check 1 (t+0s):  speed_kmh=147.451  heartbeat_age_s=0.479
check 2 (t+5s):  speed_kmh=147.451  heartbeat_age_s=0.769
```

**The `speed_kmh` value is byte-identical across both checks, while
`heartbeat_age_s` keeps refreshing to near-zero.** This is the real finding:
`BotStatusReporter` runs its own background thread
(`ai_bot.py::BotStatusReporter._run`) that independently re-POSTs whatever
is cached in `self._latest` every `interval` seconds *regardless of whether
the main drive loop is still calling `.tick()`* — so once the main loop
freezes (stuck in the `receive_state()` timeout branch because the expected
`ConnectionRefusedError` never arrived), the heartbeat mechanism **keeps
reporting `"health":"healthy","connected":true"` on a completely stale
snapshot**, indefinitely. Monitoring that only checks `heartbeat_age_s`
would never notice the car has stopped actually being controlled.

**Root cause, best understanding**: a UDP socket only learns its peer is
gone via an ICMP "port unreachable" message correlated back to the
`connect()`-ed socket. That mechanism is a real Linux kernel feature and is
exercised successfully by `tests/bot/test_scr_client_network.py`'s
`test_handshake_fails_cleanly_when_nothing_responds` test — but that test
never establishes a real peer first; it points at a port nothing was ever
listening on. RB-05 is a different shape: an *established* UDP session
whose peer process gets SIGKILL'd mid-session. Whether the ICMP
unreachable is generated and delivered back through WSL2's virtualized
network stack for that specific case is the open question — this trial
suggests it either isn't, or takes longer than the ~35s observed here.

**This is not a crash** (no unhandled exception, no corrupted state) but it
is a real availability gap: the bot neither keeps driving usefully nor
exits — it idles forever, and the one signal an operator would likely
check (`/api/bot/status`'s heartbeat) actively hides the problem. Filed
here as a finding, not fixed — deciding the right fix (e.g., a watchdog
timeout on "no real state received in N seconds" independent of the
heartbeat reporter, or having `BotStatusReporter` refuse to re-send an
unchanged snapshot past some age) is a product decision, not a testing one,
per this project's own testing principles.

## 5. What's still not covered from the RB-01..RB-10 catalog

RB-06 (repeated `receive_state()` timeouts without a full disconnect),
RB-07 (malformed SCR packet), RB-08 (Granite returns an invalid strategy
name), RB-09 (high-frequency strategy churn) already have deterministic
mocked-unit coverage in `tests/bot/` (see `docs/bot_test_matrix.md` §4) and
were not re-run against the live stack here. The 3×20-minute endurance
runs from `bot_test_plan.md` §7.1 were not attempted — today's real
driving time (see `docs/bot_real_experiment_20260812.md` and this report)
totals under 30 minutes across all real sessions combined, well short of
the endurance design's 60-minute minimum.
