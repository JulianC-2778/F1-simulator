# AI Driving Bot — Real Fault Injection Report (2026-08-14)

> Status: **real trials**, not simulated/mocked. Run against a real TORCS
> instance (Forza), real `midware.app`, and (for RB-08/RB-09) a purpose-built
> fake OpenAI-compatible backend standing in for LM Studio/Granite — real
> HTTP, real subprocess, real `ai_bot.py`, only the model's *content* is
> scripted, the same principle `tools/smoke_test_bot_status.py` already
> uses. This is a second pass through
> [`docs/bot_test_plan.md`](bot_test_plan.md) §7.2's RB-01..RB-10 catalog,
> covering the four fault IDs
> [`docs/bot_fault_injection_20260812.md`](bot_fault_injection_20260812.md)
> explicitly left untried (RB-06/07/08/09), plus a second real angle on
> RB-06 (short vs. long stall), plus 4 more RB-05 reps (§5) reaching the
> full design's 5 trials for that fault — the first RB ID in this project
> to get there.

## Summary

| ID | Fault | Result |
|---|---|---|
| RB-07 | Malformed SCR state packet | ✅ Pass — clean exit, no crash |
| RB-08 | Granite returns malformed JSON / invalid strategy name / says `BLOCK` directly | ✅ Pass — but caught one layer earlier than the plan assumed, see §2 |
| RB-09 | High-frequency strategy oscillation | ✅ Pass on the "no crash, no request backlog" invariant — but the plan's "won't flip every frame" expectation is now **stale**, see §3 |
| RB-06 (short stall) | `receive_state()` timeouts for 2s, TORCS still alive | ✅ Pass — no crash, no resend, auto-recovers, no restart needed |
| RB-06 (long stall) | `receive_state()` timeouts for 36s (unintentional, see §4) | ✅ Pass — correctly degenerates into the same clean-exit watchdog path RB-05 uses |
| RB-05 reps 2–5 | TORCS process hard-killed mid-drive, repeated 4x | ✅ Pass all 4 — see §5. Combined with the 1 trial in the 08-12 report, RB-05 now has **5/5**, the full design's count, for the first RB ID in this project |

## 0. Methodology note: a real SSE gotcha this session ran into first

RB-08/RB-09 both need to redirect midware's model backend to a script we
control, the same technique RB-01 used in the 08-12 report
(`POST /api/config/api`). The first attempt used a fake backend that replied
with a plain JSON body (`{"choices":[{"message":{"content": ...}}]}`,
`Content-Type: application/json`) — and got a uniform `502 Bad Gateway` for
*every* response, including syntactically valid `{"strategy":"ATTACK",...}`
ones. This looked at first like confirmation of RB-08's expected behavior,
but a positive-control check (feeding the same "valid" content through
RB-09's setup) also 502'd, which shouldn't happen — a sign something was
wrong with the harness, not the system under test.

Root cause: `ai_bot.py`'s `TORCS_BOT_PROMPT` env var only controls the
*client's* own polling interval/timeout (`_STRATEGY_INTERVAL`,
`_GRANITE_TIMEOUT`) — the actual HTTP request body `GraniteStrategist`
sends to `/api/bot/strategy` (`ai_bot.py` L3508-3510) never includes a
`prompt_mode` field at all. The prompt mode that actually matters —
whether `midware/runtime.py::request_bot_strategy` streams the model call
(`do_stream = mode != "legacy"`, L1789) — is resolved **server-side**, from
midware's own default, independent of anything `ai_bot.py`'s environment
says. This session's midware instance defaults to `reasoning`, which
streams: `model_gateway.py`'s `_chat_httpx` reads the response via
`response.aiter_lines()` and `extract_stream_token()`, which expects
`data: {...}` SSE lines with `choices[0].delta.content` — a plain
`Content-Type: application/json` body with `message.content` (the
*non-streaming* shape) makes `extract_stream_token` return `""` for every
line, so `full_text` stays empty, `extract_json_object("")` returns `None`,
and midware raises `"model returned no parsable JSON object: ''"` →
502 — regardless of whether the underlying content was ever malformed.

Fixed by rewriting the fake backend to always answer in SSE shape
(`data: {"choices":[{"delta":{"content": ...}}]}\n\ndata: [DONE]\n\n`,
`Content-Type: text/event-stream`). A same-shaped positive-control request
through RB-09's setup then returned `ok:true` with the correct strategy and
a real (sub-50ms, local-loopback) `round_trip_s` — confirming the harness
itself was sound before trusting RB-08's results. Recorded here because
it's a real, non-obvious gotcha anyone else scripting a fake Granite
backend against this codebase will hit.

## 1. RB-07 — malformed SCR state packet

**Setup**: real TORCS can't be made to emit a malformed packet itself, so
this used a small standalone UDP script that performs the real SCR
handshake correctly (replies `***identified***` to `SCR(init ...)`) and
then sends one deliberately malformed packet: 6 non-UTF-8 bytes + literal
text `not-an-scr-packet-at-all{{{` + 2 more non-UTF-8 bytes — chosen to
exercise the real `recv()` → `decode(errors="replace")` → regex pipeline
over an actual socket, not a hand-built string passed straight into
`parse_scr_state()` the way the existing unit tests do. `ai_bot.py` itself
is the real, unmodified client process; only the "TORCS" on the other end
is a stand-in — see §0 of `docs/bot_real_experiment_20260814g.md`-adjacent
reasoning: this was the operator-approved way to test RB-07 at all, since
`ScrClient.connect()` calls `socket.connect(self._addr)` right after a
successful handshake (`ai_bot.py` L330), so the OS-level UDP socket only
accepts datagrams from the real peer's address afterward — no way to
inject a spoofed packet from outside without either IP spoofing or
replacing the peer.

**Observed**:
```
Identified! Entering drive loop. Press Ctrl-C to stop.

Race ended — exiting loop.
```
Exit code 0, no traceback. `parse_scr_state`'s regex (`_SCR_TOKEN`) finds no
`(key value)` tokens in the garbage, so `raw` stays empty →  returns `None`
(`ai_bot.py` L187-188) → `receive_state()` returns `None` → `run_bot()`
takes the same clean-exit path as a real race ending. This matches
`bot_test_plan.md`'s RB-07 requirement exactly as written ("`parse_scr_state`
返回 `None` 或安全丢弃，不让异常向上传播炸穿 `run_bot` 主循环") — `None` was
the outcome, and no exception ever propagated.

## 2. RB-08 — Granite returns malformed JSON / invalid strategy name / `BLOCK`

**Setup** (after the SSE fix in §0): fake backend cycles three bad replies —
plain non-JSON text, `{"strategy":"TURBOBOOST",...}` (a made-up name), and
`{"strategy":"BLOCK",...}` (the model directly claiming the system-only
strategy) — against real TORCS (Forza) and real `ai_bot.py 
--bot --granite`.

**Observed**: all three replies produced the identical outcome —
```json
{"fallback": true, "error": "HTTP Error 502: Bad Gateway", "strategy": "NORMAL"}
```
— while the car kept driving normally throughout (speeds 138–215 km/h
across repeated checks, `health: healthy`, no stall).

**Where the rejection actually happens — not where the plan assumed**:
`bot_test_plan.md`'s RB-08 expects `_parse_strategy_response` (the
`ai_bot.py`-side parser) to catch this and fall back to `NORMAL`. In this
trial it never got the chance to: `midware/runtime.py::request_bot_strategy`
does its own `extract_json_object(text)` + `Strategy(fields["strategy"])`
validation *before* ever returning a response to the client (L1806-1816);
`Strategy` (`midware/schemas/bot.py` L9-14) only defines
`ATTACK/NORMAL/DEFEND/SAVE_FUEL/PIT` — no `BLOCK` at all — so all three of
this trial's bad replies raise inside that `try` block and get converted to
a `502` before `ai_bot.py`'s own `_parse_strategy_response` logic is ever
exercised. From the client's perspective a malformed-content fault and a
Granite-totally-unreachable fault (RB-01) are indistinguishable — both
surface as an HTTP error → `GraniteStrategist.fallback=True`, hold last
confirmed strategy, keep driving.

**Not a defect** — the end-to-end guarantee (no crash, keeps driving,
`safety_filter` remains the final gate regardless) holds either way, and
having *two* independent layers reject malformed model output (midware's
schema validation, and `ai_bot.py`'s own parser as a second line of
defense the client would still need if it ever talked to a raw model
endpoint directly) is arguably more robust than one. Recorded as a
correction to which layer this specific trial exercised, not a failure.

## 3. RB-09 — high-frequency strategy oscillation

**Setup**: fake backend alternates `ATTACK`/`DEFEND` on every request,
against real TORCS + real `ai_bot.py --bot --granite` (`reasoning` mode,
15s poll interval).

**Observed**, sampled across 6 consecutive real poll cycles:
```
request #0 ATTACK -> status.strategy: ATTACK  (fallback:false, round_trip_s:0.037)
request #1 DEFEND -> status.strategy: DEFEND
request #2 ATTACK -> status.strategy: ATTACK
request #3 DEFEND -> status.strategy: DEFEND
request #4 ATTACK -> status.strategy: ATTACK
request #5 DEFEND -> status.strategy: DEFEND
```
The active strategy flips in lockstep with every single poll — no
debounce delay observed.

**The plan's expected behavior is stale, not wrong-by-omission**:
`bot_test_plan.md`'s RB-09 row says "`_next_debounced_strategy` 按
`_STRATEGY_CONFIRM` 门槛工作，不会每帧都切换导致车辆行为抖动" — written when
`_STRATEGY_CONFIRM` was `2`. The current code (`ai_bot.py` L3124-3132) sets
`_STRATEGY_CONFIRM = 1`, with a code comment explicitly recording the
trade: *"Was 2 (a debounce against a borderline reading flapping the car
every ~5s poll) — dropped to 1 so a decision is visible/actionable on the
very next answer instead of needing two in a row; a genuinely borderline
state can now flap between strategies each poll, which is the accepted
trade for responsiveness."* This trial is a real, live confirmation that
the current, intended behavior is exactly what the comment says — not a
regression, but the plan's prose describing the old value is now
inaccurate and should not be read as today's expected behavior.

**What did hold**: the other half of RB-09's requirement —
`LatestTaskRunner` guarantees no backlog of unfinished Granite requests —
checked via `/api/health`'s scheduler stats throughout this trial:
`active: 0, queued: 0` at every sample, even under six rapid-fire
oscillating decisions. No pileup.

## 4. RB-06 — `receive_state()` timeouts while TORCS stays alive (server present, no data)

Two real trials this session, one by accident, both informative.

### 4.1 Short stall (2.0s, precisely timed) — recovers, no restart

**Inject**: `kill -STOP <torcs-bin pid>` then, in the *same* shell
invocation, `sleep 2` then `kill -CONT <torcs-bin pid>` — kept atomic
specifically to avoid the per-command overhead this environment's separate
tool calls otherwise add (see §4.2). Timestamps confirmed an exact 2.004s
gap. `ps -o stat` on the TORCS pid showed `T` (stopped) throughout the
pause, `R` immediately after resume — the pause was real, not just
inferred from the bot's side.

**Observed**: speed frozen at the pre-pause value (`155.472` unchanged
across two checks during the stall — real evidence of "waiting," not
"resending stale controls and drifting"), log line count static, process
alive throughout. After `CONT`, speed resumed changing on its own within
the next status poll (`220.398` then `235.386` across two follow-up
checks, no restart) — full automatic recovery, matching
`bot_test_plan.md`'s RB-06 requirement ("不重发上一次控制包...继续等待而不是
崩溃").

### 4.2 Long stall (36s, unintentional) — degenerates cleanly into the watchdog path

The *first* attempt at this trial was meant to be a few seconds but ran to
36 real seconds — each shell command in this environment (spawning
`wsl.exe`, a fresh bash session, `curl`, etc.) carries several seconds of
overhead on its own, and chaining `SIGSTOP` → separate `sleep` calls →
`SIGCONT` across multiple tool invocations let that overhead accumulate far
past the intended pause. (§4.1's fix was doing the whole stop/sleep/resume
sequence inside one shell invocation instead.)

**Observed**: `ai_bot.py` printed `"No data from TORCS for 5.0s — assuming
the connection is dead. Exiting loop."` and exited cleanly (not in `ps`
afterward, no traceback) once the pause crossed the
`_CONNECTION_LOST_FRAMES` threshold (~5s) — the same watchdog RB-05's
08-12 trial found missing and got fixed the same day. This is a second,
independent real confirmation of that fix, reached via a different fault
mechanism (a paused-but-alive TORCS process, not a killed one) — the
watchdog correctly treats "no data for 5s" the same way regardless of
*why* no data is arriving, which is the right generalization.

**Reading both halves together**: RB-06's real boundary is exactly where
`bot_test_plan.md`'s two related fault IDs meet — a short stall recovers
silently (this section, §4.1), a long one degenerates into RB-05's clean
exit (§4.2). Both are real, both are correct, and knowing where the line is
(~5s, `_CONNECTION_LOST_FRAMES`) required hitting it by accident once.

## 5. RB-05 — 4 more reps, completing the full design's 5 trials

**Setup, each rep identical**: real TORCS (Forza), real `ai_bot.py --bot
--granite`, let it drive until real speed is confirmed via
`/api/bot/status`, then `kill -9` the `torcs-bin` process, wait past the
5s watchdog, confirm clean exit, then relaunch TORCS
(`torcs_launcher.sh`) and re-enter Quick Race for the next rep. Unlike
every other fault in this report, RB-05 needs the *whole* TORCS
application killed and relaunched per rep — a Quick Race menu re-entry
alone isn't enough, since the process itself is the thing being killed.

| Rep | Speed at kill | Exit | Decision source (this rep) |
|---|---:|---|---|
| 2 (08-12's was #1) | 193.3 km/h | clean, `No data from TORCS for 5.0s...` | 83.1% granite / 16.9% rule_block |
| 3 | 198.2 km/h | clean, same message | 100% granite |
| 4 | 199.4 km/h | clean, same message | 100% granite |
| 5 (final) | 204.9 km/h | clean, same message | 100% granite |

All 4 passed identically to the original 08-12 trial and to each other —
no crash, no traceback, no lingering process, `_CONNECTION_LOST_FRAMES`
watchdog fired every time. **RB-05 is now the first fault ID in this
project's RB-01..RB-10 catalog to reach the full design's 5 real trials**
(1 + 4), and every one of the 5 passed.

**Operational side-note, not a bot-code finding**: `torcs_launcher.sh`
itself was noticeably less reliable under this rapid kill/relaunch cycle
than in earlier sessions — several relaunches needed a retry (one log
showed a spawned `torcs-bin` immediately `Killed` before a second attempt
in the same script run stuck), and at one point two `torcs-bin` processes
ended up running simultaneously from overlapping launcher invocations
(cleaned up with `pkill -9 -f torcs-bin` before continuing). This is
WSLg/launcher-script behavior under repeated rapid restarts, not anything
in `ai_bot.py` or `midware` — worth knowing if this exact rep-in-a-row
pattern is repeated for another fault, but out of scope for this report to
fix.

## 6. What's still not covered from the RB-01..RB-10 catalog

- RB-01/02/03/04/06/07/08/09/10 each still have only 1 real trial; RB-05 is
  the only one at the full design's 5 so far (§5). Repeating the rest 4x
  each would need the same kind of session this report and the 08-12 one
  together represent, just more of it.
- Endurance runs and human-driven ground truth remain descoped per the
  operator's 2026-08-14 decisions (`bot_test_plan.md` §5.1, §7.1).
