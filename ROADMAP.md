# NationClaw Roadmap

This roadmap turns the current NationClaw architecture into an implementation plan. The recommended strategy is to build the system in phases instead of trying to implement the Android app, communication gateway, Flutter UI, plugin SDK, dashboard, and full AI runtime all at once.

## Product direction

NationClaw should be organized around three major layers:

```text
1. Native Android Client
2. Python Agent Runtime
3. Optional Dashboard / Plugin Ecosystem
```

Recommended responsibility split:

```text
Native Android Client
├── Accessibility Service
├── Media Projection
├── Foreground Service
├── Overlay Service
├── Notification Listener
├── Clipboard Controller
├── Package Manager
└── Intent Dispatcher

Python Agent Runtime
├── Gateway
├── Planner
├── Executor
├── Memory
├── Model Providers
├── Tool Registry
└── Plugin SDK

Flutter / Web UI
├── Task UI
├── Device Status
├── Logs and Traces
├── Approval Prompts
├── Settings
└── Plugin Management
```

## Guiding principles

- Build the minimum reliable automation loop first.
- Keep native Android automation in Kotlin, not Flutter.
- Keep planning, memory, model orchestration, and plugins in the Python runtime.
- Put a Gateway between all user interfaces and the internal runtime.
- Use Flutter or Web for the dashboard/control surface, not for low-level Android services.
- Make safety, visibility, and interruption part of the core runtime.
- Treat Plugin SDK as an extension layer after the core loop is stable.

## MVP definition

The first useful NationClaw MVP should support this loop:

```text
User submits task
    ↓
Gateway receives task
    ↓
Planner decides next step
    ↓
Executor sends command to Android client
    ↓
Android Services perform action
    ↓
Observation returns to Executor
    ↓
Planner continues or finishes
    ↓
Result streams back to user
```

MVP capabilities:

1. Android app starts a foreground WebSocket service.
2. Android app exposes:
   - screenshot;
   - UI hierarchy;
   - tap/click;
   - long press;
   - swipe/drag;
   - text input;
   - back/home;
   - app launch.
3. Python Gateway accepts tasks over REST/WebSocket.
4. Planner uses an LLM to decide next steps.
5. Executor controls Android through the bridge.
6. Runtime streams progress and final result back to the user.
7. Runtime records logs and basic audit events.

## Phase 1 — Core runtime stabilization

Goal: make the existing Python runtime easier to package, test, and extend.

### Tasks

- Define stable internal interfaces:
  - `Planner`
  - `Executor`
  - `DeviceController`
  - `MemoryStore`
  - `ModelProvider`
  - `ToolRegistry`
- Reduce coupling inside `AutoAgent` where possible.
- Keep generated-action execution behind a clear executor boundary.
- Add better error handling around model calls and device calls.
- Add structured logging for task steps.
- Add test coverage for:
  - config parsing;
  - tool registry;
  - file interface;
  - model response parsing;
  - WebSocket device command formatting.
- Document current agent APIs used by generated code.

### Target modules

```text
nationclaw/agent.py
nationclaw/config.py
nationclaw/device/
nationclaw/file/
nationclaw/fm/
nationclaw/intelligence/
```

### Deliverables

- Cleaner internal interfaces.
- Basic unit tests.
- Runtime smoke test.
- Reduced import/package issues.

## Phase 2 — Native Android client

Goal: implement the Android automation client as a native Kotlin app.

### Recommended stack

```text
Kotlin + Jetpack Compose
Android Accessibility Service
Foreground Service
Media Projection
WebSocket Server
SQLite / local persistence
```

### Tasks

- Create Android project source tree.
- Implement setup UI with Jetpack Compose.
- Implement permission checklist:
  - Accessibility Service;
  - Media Projection;
  - Notification Listener;
  - Overlay;
  - foreground service notification;
  - microphone for optional speech.
- Implement foreground WebSocket service.
- Implement command router.
- Implement screenshot command.
- Implement UI hierarchy command.
- Implement accessibility actions:
  - click;
  - long click;
  - drag/swipe;
  - input text;
  - clear text;
  - back;
  - home.
- Implement app control:
  - list installed apps;
  - resolve display name to package;
  - launch app;
  - kill app where allowed;
  - dispatch intents.
- Implement overlay highlights.
- Implement clipboard get/set.
- Add local logs and diagnostics screen.

### Deliverables

- Native Android app source.
- Local WebSocket bridge compatible with Python runtime.
- Permission setup screen.
- Basic automation working on a spare Android device.

## Phase 3 — Gateway API

Goal: expose a stable UI-to-runtime protocol.

### Recommended stack

```text
FastAPI + WebSocket
```

### Tasks

- Implement Gateway service in Python.
- Add REST endpoints:
  - health check;
  - config summary;
  - device list;
  - current task state;
  - plugin list later.
- Add WebSocket endpoint for streaming:
  - task status;
  - logs;
  - screenshots;
  - approval prompts;
  - final results.
- Define event envelope:

```json
{
  "type": "task.status",
  "requestId": "req_001",
  "sessionId": "session_001",
  "timestamp": "2026-07-03T18:00:00+01:00",
  "payload": {}
}
```

- Add task cancellation tokens.
- Add pause/resume support.
- Add approval prompt protocol for high-risk actions.
- Add schema validation for inbound events.

### Deliverables

- `Gateway` module.
- REST/WebSocket API documentation.
- End-to-end task submission over WebSocket.

## Phase 4 — Planner and Executor separation

Goal: separate reasoning from action execution.

### Tasks

- Create a dedicated planner abstraction.
- Create a dedicated executor abstraction.
- Move tool/device/file execution behind executor methods.
- Support structured action objects instead of only generated Python snippets where possible.
- Preserve backward compatibility with existing `agent.*` generated-code flow during migration.
- Add task state tracking:
  - pending;
  - running;
  - waiting_for_approval;
  - paused;
  - finished;
  - failed;
  - infeasible;
  - cancelled.

### Deliverables

- Clear Planner → Executor boundary.
- Easier integration with Gateway and UI.
- Safer approval/cancellation flow.

## Phase 5 — Flutter or Web dashboard

Goal: provide a modern user-facing control surface.

### Recommendation

Use Flutter for cross-platform UI or a web frontend for faster iteration. Do not use Flutter for the low-level Android automation services.

### Tasks

- Implement task input screen.
- Implement device status screen.
- Show latest screenshot and UI state.
- Show task progress and logs.
- Implement approval prompts.
- Implement task cancellation.
- Implement settings screen.
- Later: add plugin management screen.

### Deliverables

- Dashboard connected to Gateway.
- Real-time task updates.
- Human approval UI.

## Phase 6 — Plugin SDK MVP

Goal: add extension support without destabilizing the core runtime.

### MVP scope

Start with Tool Plugins only.

### Tasks

- Implement plugin manifest parser.
- Validate plugin manifest schema.
- Load local plugins from a configured directory.
- Register Tool Plugins into `ToolRegistry`.
- Enforce declared permissions.
- Disable failed plugins safely.
- Add plugin list endpoint to Gateway.

### Initial plugin types

```text
Tool Plugin — first
AI Plugin — later
Storage Plugin — later
Device Plugin — later
Network Plugin — later
Workflow Plugin — later
UI Plugin — later
```

### Deliverables

- Local plugin loading.
- Tool Plugin support.
- Plugin permission model.

## Phase 7 — Storage and memory improvements

Goal: make memory more reliable while preserving markdown-based transparency.

### Tasks

- Keep markdown memory as the human-readable source of truth.
- Add optional SQLite index for structured metadata.
- Add search index for memory files.
- Add embedding provider abstraction.
- Add retention policy for temporary files.
- Add export/import tools.

### Deliverables

- Better memory search.
- Structured metadata index.
- Optional semantic retrieval.

## Phase 8 — Safety and observability

Goal: make automation visible, interruptible, and auditable.

### Tasks

- Add audit events for all high-risk actions.
- Add risk classification for actions:
  - safe;
  - low;
  - medium;
  - high;
  - blocked.
- Add approval policy.
- Add one-click stop on Android and dashboard.
- Add task replay logs.
- Add screenshot redaction options.
- Add clipboard privacy rules.
- Add notification privacy rules.

### Deliverables

- Audit log.
- Approval system.
- Safety policy config.
- User-visible automation status.

## Phase 9 — Voice, OCR, and multimodal input

Goal: expand beyond text chat input.

### Tasks

- Add speech-to-text input.
- Add text-to-speech output.
- Add OCR parser for image attachments and screenshots.
- Add camera/image task input.
- Add multimodal task normalization.

### Deliverables

- Voice command path.
- Spoken responses.
- OCR-enabled visual tasks.

## Phase 10 — Release engineering

Goal: prepare NationClaw for repeatable releases.

### Tasks

- Add CI for Python tests.
- Add Android build CI.
- Add linting and formatting.
- Add versioned releases.
- Add changelog.
- Add example configs.
- Add quickstart guide.
- Add security guide.

### Deliverables

- CI pipeline.
- Reproducible Android APK builds.
- Versioned Python package.
- Release notes.

## Suggested GitHub issues

Create issues from these milestones:

1. Define Planner and Executor interfaces.
2. Add unit tests for ToolRegistry.
3. Add FastAPI Gateway skeleton.
4. Define WebSocket event schema.
5. Implement Android Kotlin project skeleton.
6. Implement Android Foreground Service.
7. Implement Android Accessibility Service command router.
8. Implement screenshot command through Media Projection.
9. Implement UI hierarchy command.
10. Implement click/swipe/text/back/home commands.
11. Add dashboard task submission UI.
12. Add approval prompt protocol.
13. Add Tool Plugin manifest loader.
14. Add plugin permission validation.
15. Add audit logging for executor actions.
16. Add cancellation support for running tasks.
17. Add memory search improvements.
18. Add OCR parser.
19. Add speech-to-text and text-to-speech.
20. Add release CI.

## Current implementation status

The first Gateway implementation is now present in:

```text
nationclaw/gateway/
GATEWAY.md
```

It provides:

- FastAPI application factory;
- REST health, device, task, and cancellation endpoints;
- WebSocket event handling for `task.submit` and `task.cancel`;
- real `AutoAgent.execute_task` integration;
- cooperative task cancellation;
- live `agent.log` streaming over WebSocket;
- serial task execution to avoid conflicting device actions;
- `nationclaw-gateway` console entry point.

## Recommended immediate next step

The native Android client source is now present in:

```text
android-client/
ANDROID_CLIENT.md
```

It implements:

- Kotlin + Jetpack Compose setup UI;
- foreground WebSocket bridge service;
- accessibility service command execution;
- screenshot and UI hierarchy commands;
- overlay highlight control;
- notification listener;
- clipboard controller;
- package manager adapter;
- intent dispatcher;
- speech recognition;
- text-to-speech;
- MediaProjection controller.

The next concrete engineering step should be:

```text
Build, install, and test the Android client on a real spare Android phone, then align any command-response edge cases with Python WebsocketController.
```
