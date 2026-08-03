# Event-Driven AI Commentary Middleware for Real-Time Racing Simulation

Working paper concept and partial English draft, based on the current TORCS/F1 simulator work logs and implementation notes.

## Paper Spine

This paper solves the gap between high-frequency racing-simulator telemetry and human-facing race narration by introducing a low-intrusion, event-driven middleware that transforms structured vehicle state into bounded, real-time language output without interfering with the driving-control loop.

## Candidate Titles

1. Event-Driven AI Commentary Middleware for Real-Time Racing Simulation
2. From Telemetry to Narration: A Middleware Architecture for AI-Assisted Racing Commentary
3. Real-Time Language Interfaces for TORCS: Event Detection, Context Control, and Streaming Commentary

## Current Evidence Base

- TORCS 1.3.7 with SCR patch can be built and run under WSL2/Ubuntu.
- The simulator exposes vehicle state through two separate channels: the SCR control interface for external driving agents and the human-driver telemetry path for observation/commentary.
- The commentary middleware ingests UDP telemetry on port `3101`, stores recent frames in a sliding window, detects race events, and generates structured event payloads.
- The system supports event priorities, event-specific cooldowns, and hybrid commentary modes that combine interval-based updates with event-driven interruptions.
- The context manager converts telemetry and event payloads into natural-language prompts while keeping history inside a token budget.
- Generated commentary is streamed through a FastAPI/WebSocket service to a browser dashboard client.
- A second standalone service reuses telemetry history from the commentary service to provide rule-based dashboard feedback without opening another UDP listener.

## Contribution Chain

1. Problem formulation: real-time simulators produce rich telemetry, but raw state streams are not directly usable as live race commentary or driver-facing narrative feedback.
2. Interface design: a separate commentary middleware subscribes to telemetry without taking control authority from the driving loop.
3. Event abstraction: high-frequency frames are compressed into priority-ranked race events such as contact, position change, off-track moments, lap completion, battles, and pace surges.
4. Context-bounded generation: prompts are assembled from persona, recent event history, and current telemetry while respecting a configurable token budget.
5. Playback pipeline: generated tokens are streamed to a browser dashboard client, allowing commentary text and speech to be displayed during simulation.
6. Extension path: a standalone dashboard service demonstrates how the same telemetry substrate can support rule-based guidance and LLM-assisted explanation.

## Proposed Abstract

Racing simulators expose dense telemetry streams that are useful for control, analysis, and replay, but these streams are difficult to translate into timely human-facing commentary. This paper presents an event-driven AI commentary middleware for TORCS, a real-time racing simulator. The system listens to telemetry from a non-control UDP channel, maintains a sliding window of recent vehicle states, detects priority-ranked race events, and converts selected events into bounded prompts for an OpenAI-compatible language model. Generated commentary is streamed through a FastAPI and WebSocket layer to a browser dashboard interface, enabling live captions and optional speech playback. The design separates observation from control: the commentary layer does not issue driving commands and can therefore be developed alongside, rather than inside, the autonomous-driving loop. Current implementation evidence shows that the middleware can parse simulator telemetry, construct event payloads, manage context length, and support both interval-based and event-triggered commentary. The remaining evaluation will measure event-to-output latency, event detection quality, prompt grounding, interruption behaviour, and user-facing readability across controlled racing scenarios.

## Draft Introduction

Real-time racing simulators are commonly used as controlled environments for studying vehicle dynamics, autonomous driving agents, and human interaction with driving systems. They produce detailed state variables at high frequency, including vehicle speed, track position, lap progress, damage, opponent distance, and ranking information. These data streams are well suited to numerical control and offline analysis. However, they are much less accessible to a human observer during live simulation. A user watching or driving in the simulator may need concise explanations of what is happening, why a moment matters, and which state changes are worth attention.

Recent large language models make it possible to generate fluent descriptions from structured data, but a direct telemetry-to-language pipeline is risky in a real-time system. Raw simulator frames are too frequent for language generation, and unrestricted prompting can produce commentary that is delayed, repetitive, or weakly grounded in the actual race state. More importantly, a language-generation component should not be confused with the control interface of the simulator. In a racing system that may also contain an AI decision layer, the commentary module must remain observational: it should explain events without issuing driving commands or modifying vehicle behaviour.

This project addresses that gap through an event-driven commentary middleware for TORCS. The middleware listens to telemetry emitted by the simulator, stores recent frames in a sliding window, detects meaningful race events, and constructs compact event payloads for language generation. Instead of sending every frame to a language model, the system selects moments such as contact, position changes, off-track events, lap completion, close battles, and pace surges. Each event type has a priority and cooldown policy, allowing important incidents to interrupt routine updates while preventing repetitive narration.

The proposed architecture separates four responsibilities: telemetry ingestion, event detection, context-bounded prompt construction, and client playback. Telemetry is received over UDP and normalised into structured frames. Event detection compresses recent frame windows into semantic race events. The context manager combines a commentator persona, recent history, and the current event into a bounded prompt. Finally, a WebSocket layer streams generated tokens to a browser dashboard client. This separation makes the system easier to test, allows the commentary layer to evolve independently from the driving-control middleware, and provides a reusable telemetry substrate for additional features such as a rule-based dashboard.

The current paper reports the design and implementation of this middleware and outlines an evaluation plan for the next development phase. The central claim is not that the system improves driving performance. Rather, the claim is that a structured event layer can make high-frequency simulator telemetry usable for real-time, human-facing language output while keeping the language model inside an observational and bounded role.

## Draft Methods Section

### System Architecture

The simulator is organised around three major layers. The game-engine layer is TORCS, built from source and run under WSL2/Ubuntu. The driving-control path uses the SCR server interface, where an external client can receive vehicle state and return steering, throttle, brake, gear, clutch, focus, and meta commands. In parallel with this control path, the commentary path reads telemetry from the human-driver logging channel. This separation is central to the design: the commentary middleware observes vehicle state but does not send commands back to the simulator.

The commentary middleware is implemented as a FastAPI service. A background UDP listener receives telemetry packets on port `3101` and pushes parsed frames into a `TelemetryStore`. The store keeps a sliding time window of recent frames and ranking snapshots, making recent race context available to the event engine and API endpoints. This design supports live telemetry as well as injected demonstration data for testing without launching TORCS.

### Event Detection

The event engine periodically selects a recent telemetry window and summarises it into compact state features. Candidate events are ranked by priority. High-priority events include contact, position changes, and off-track moments. Medium-priority events include lap completion, close battles, and pace surges. Routine pace updates are assigned the lowest priority and are mainly used to keep commentary active during stable driving.

To prevent repeated or noisy commentary, the engine applies two cooldown mechanisms. A wall-clock cooldown limits the minimum time between any two emitted events. An event-signature cooldown prevents the same event type and reason from being emitted repeatedly within a short simulation interval. When multiple candidates are present, the highest-priority candidate is selected. This produces a compact event payload containing only the fields required for that event type.

### Context-Bounded Language Generation

The context manager converts telemetry and event payloads into natural-language prompts. It maintains a commentator persona, a history of previous messages, and a configurable context budget. Before each language-model call, the manager reserves space for the response, inserts the system persona, and trims history according to the selected strategy. This prevents unbounded prompt growth during long simulations.

The current implementation supports OpenAI-compatible local or remote model providers, including LM Studio. Responses are streamed token by token, which allows the client to display partial commentary immediately instead of waiting for the full response. The prompt explicitly frames the language model as a commentator rather than a controller, keeping generated text separate from vehicle actions.

### Client Playback

The middleware broadcasts commentary lifecycle messages over WebSocket. Clients receive `ai_start`, `token`, `ai_done`, and `error` messages. The browser dashboard displays streamed text as it arrives. When speech playback is enabled, completed commentary is split into sentences and played sequentially. A new high-priority event can cancel current generation and clear queued speech, allowing urgent commentary to replace routine narration.

## Results To Collect In The Next Two Weeks

| Question | Metric or evidence | Suggested test |
| --- | --- | --- |
| Can the middleware maintain live operation? | Uptime, dropped packets, WebSocket reconnect behaviour | 10-20 minute TORCS driving session |
| Is commentary fast enough? | Event-to-first-token latency, event-to-final-caption latency | Controlled events with timestamp logging |
| Are events detected correctly? | Precision/recall against manually labelled race moments | Scripted scenarios for off-track, contact, lap completion, position change |
| Is generation grounded? | Percentage of comments that match telemetry facts | Manual rubric over generated samples |
| Does priority interruption work? | Routine commentary cancelled by high-priority event | Trigger pace update followed by contact/off-track event |
| Does context control help? | Token count, trimmed history size, repetition rate | Compare small vs large context windows |
| Is the dashboard extension useful? | Rule output consistency, explanation faithfulness | Compare rule guidance with LLM explanation field |

## Two-Week Writing And Experiment Plan

### Week 1

- Finalise the paper title, research question, and claim boundary.
- Add timestamp instrumentation for event detection, model request start, first token, final token, and client display.
- Create 4-6 controlled TORCS scenarios: stable driving, off-track, contact, position change, lap completion, and close battle.
- Capture representative screenshots of the simulator and the browser dashboard UI.
- Start a small results table with latency, detected event type, generated output, and manual correctness notes.

### Week 2

- Run repeated trials for each scenario and summarise latency and event-detection behaviour.
- Audit generated commentary for telemetry grounding, repetition, and unsafe control-like wording.
- Write the Results section around transitions rather than raw logs: telemetry stream, event selection, prompt construction, streaming output, and failure cases.
- Complete the Discussion section with limitations: small scenario set, local model latency, manual evaluation, and TORCS-specific integration.
- Prepare figures: system architecture, event pipeline, WebSocket playback lifecycle, and example event-to-commentary trace.

## Figures And Tables To Prepare

1. Architecture figure: TORCS telemetry/control separation, middleware, model provider, browser dashboard UI.
2. Event pipeline figure: sliding window, event detection, priority/cooldown, payload construction, prompt generation.
3. Playback sequence figure: `ai_start`, streaming tokens, `ai_done`, speech queue, high-priority interruption.
4. Results table: controlled scenario, event type, latency, output correctness, notes.
5. Failure table: missed event, delayed generation, repeated narration, unsupported wording, likely cause.

## Claim Boundaries

- Do not claim that the system improves autonomous-driving performance unless driving metrics are measured.
- Do not claim general real-world motorsport validity; the current environment is TORCS-based simulation.
- Do not claim the language model understands race strategy; it formats and narrates structured telemetry evidence.
- Do not claim safety-critical reliability; the middleware is a user-facing commentary and dashboard layer.
- Keep the AI decision layer and commentary layer separate unless future experiments explicitly connect them.

## Missing Inputs

- Target format: conference paper, course report, dissertation chapter, or project portfolio paper.
- Required word count and citation style.
- Any supervisor rubric or marking criteria.
- Whether the final paper should use British English or American English.
- Final list of authors and project ownership statement.
- Experimental logs from the next two weeks.

