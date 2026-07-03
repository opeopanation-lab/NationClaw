# NationClaw Communication Stack

This document defines the communication path between the user-facing UI, the NationClaw planning runtime, the action executor, and Android device services.

## High-level stack

```text
Flutter UI
    │
    ▼
WebSocket
    │
    ▼
Gateway
    │
    ▼
Planner
    │
    ▼
Executor
    │
    ▼
Android Services
```

## Expanded architecture

```text
Flutter UI
│
├── Chat / Task Input
├── Voice Controls
├── Device Status
├── Approval Prompts
├── Logs / Traces
└── Plugin Panels
        │
        ▼
WebSocket
│
├── Persistent bidirectional connection
├── Task submission
├── Streaming status updates
├── Screenshot / UI-state events
├── Approval request / response events
└── Error and lifecycle events
        │
        ▼
Gateway
│
├── Authentication / session validation
├── Message normalization
├── Rate limiting
├── Permission checks
├── Event routing
├── Device routing
└── Protocol translation
        │
        ▼
Planner
│
├── Context loading
├── Memory retrieval
├── Task planning
├── Reasoning LLM calls
├── Workflow selection
└── Tool selection
        │
        ▼
Executor
│
├── Agent API executor
├── Tool executor
├── Device action executor
├── File / memory executor
├── Chat response executor
└── Audit logger
        │
        ▼
Android Services
│
├── Foreground Service
├── Accessibility Service
├── Media Projection
├── Overlay Service
├── Notification Listener
├── Clipboard Controller
├── Package Manager
└── Intent Dispatcher
```

## Layer responsibilities

### 1. Flutter UI

Purpose: provide a user-facing control surface for NationClaw.

Responsibilities:

- submit tasks to the agent;
- display task progress;
- show device status and screenshots;
- present approval prompts for sensitive actions;
- show logs, reasoning traces, and workflow status;
- expose plugin-provided UI panels;
- support mobile, desktop, or web builds where appropriate.

Typical UI events sent downstream:

```json
{
  "type": "task.submit",
  "requestId": "req_001",
  "payload": {
    "text": "Open WhatsApp and summarize the latest message",
    "attachments": []
  }
}
```

Typical UI events received upstream:

```json
{
  "type": "task.status",
  "requestId": "req_001",
  "payload": {
    "status": "running",
    "message": "Opening WhatsApp on phone1"
  }
}
```

### 2. WebSocket

Purpose: provide low-latency bidirectional communication between the UI and NationClaw runtime.

Responsibilities:

- maintain persistent client sessions;
- stream logs and intermediate task updates;
- send screenshots or screen-state references;
- deliver approval prompts;
- deliver cancellation and pause/resume commands;
- reconnect safely after network interruption.

Recommended event envelope:

```json
{
  "type": "event.type",
  "requestId": "req_001",
  "sessionId": "session_001",
  "timestamp": "2026-07-03T18:00:00+01:00",
  "payload": {}
}
```

Recommended core event types:

| Event type | Direction | Description |
| --- | --- | --- |
| `task.submit` | UI → Gateway | Submit a new user task |
| `task.cancel` | UI → Gateway | Cancel a running task |
| `task.pause` | UI → Gateway | Pause a running task |
| `task.resume` | UI → Gateway | Resume a paused task |
| `task.status` | Gateway → UI | Send task status update |
| `task.result` | Gateway → UI | Send final task result |
| `agent.log` | Gateway → UI | Stream runtime logs |
| `device.screenshot` | Gateway → UI | Send screenshot reference or payload |
| `approval.request` | Gateway → UI | Ask user to approve sensitive action |
| `approval.response` | UI → Gateway | User approves or denies action |
| `error` | Both | Report recoverable errors |

### 3. Gateway

Purpose: isolate external clients from the internal agent runtime.

Responsibilities:

- authenticate UI clients;
- normalize WebSocket messages;
- validate message schemas;
- enforce permissions and safety policy;
- route requests to the correct planner or device session;
- translate external protocol messages into internal agent calls;
- publish executor events back to connected clients.

The Gateway should be the only layer directly exposed to user-facing UI clients.

Suggested internal interface:

```python
class Gateway:
    async def handle_event(self, event: dict) -> None:
        ...

    async def publish(self, event: dict) -> None:
        ...
```

### 4. Planner

Purpose: decide what the agent should do.

Responsibilities:

- load context and memory;
- call the reasoning LLM;
- decompose tasks into steps;
- select tools and workflows;
- decide which executor action should run next;
- request approval for high-risk actions when required.

Related current modules:

```text
nationclaw/agent.py
nationclaw/fm/function_hub_local.py
nationclaw/intelligence/
```

### 5. Executor

Purpose: perform the planner's selected action.

Responsibilities:

- execute device actions;
- call tools;
- read/write files;
- call app-control actions;
- send chat responses;
- record audit events;
- return observations back to the planner and gateway.

Related current modules:

```text
nationclaw/agent.py
nationclaw/device/
nationclaw/file/
nationclaw/intelligence/tool_registry.py
```

### 6. Android Services

Purpose: provide Android-side automation capabilities.

Responsibilities:

- expose screenshots and UI hierarchy;
- execute accessibility actions;
- launch apps and dispatch intents;
- monitor notifications when enabled;
- manage clipboard and overlays;
- keep the Android bridge alive through a foreground service.

Related document:

```text
ANDROID_COMPONENTS.md
```

## Communication paths

### Task submission

```text
Flutter UI → WebSocket → Gateway → Planner → Executor → Android Services
```

### Status streaming

```text
Executor → Gateway → WebSocket → Flutter UI
```

### Screenshot update

```text
Android Services → Executor → Gateway → WebSocket → Flutter UI
```

### Approval flow

```text
Planner / Executor → Gateway → WebSocket → Flutter UI
Flutter UI → WebSocket → Gateway → Planner / Executor
```

### Cancellation flow

```text
Flutter UI → WebSocket → Gateway → Executor cancellation token
```

## Message schema examples

### Submit task

```json
{
  "type": "task.submit",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "task": "Open WhatsApp and summarize the latest unread message",
    "device": "phone1",
    "mode": "normal"
  }
}
```

### Status update

```json
{
  "type": "task.status",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "status": "running",
    "step": 3,
    "message": "Reading current phone screen"
  }
}
```

### Approval request

```json
{
  "type": "approval.request",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "approvalId": "approval_001",
    "action": "send_message",
    "description": "Send a WhatsApp reply to Alice",
    "riskLevel": "medium"
  }
}
```

### Approval response

```json
{
  "type": "approval.response",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "approvalId": "approval_001",
    "approved": true
  }
}
```

### Final result

```json
{
  "type": "task.result",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "status": "finished",
    "summary": "The latest unread WhatsApp message is from Alice asking about tomorrow's meeting. No reply was sent."
  }
}
```

## Relationship to existing architecture

This communication stack sits between the user-facing interface and the agent runtime.

Related documents:

- `GATEWAY.md` — implemented REST/WebSocket Gateway API.
- `AGENT_RUNTIME_ARCHITECTURE.md` — end-to-end runtime flow.
- `ANDROID_COMPONENTS.md` — Android service capabilities.
- `PLUGIN_SDK.md` — extension model.
- `TECH_STACK.md` — recommended technologies.

```text
COMMUNICATION_STACK.md         → UI-to-runtime communication path
AGENT_RUNTIME_ARCHITECTURE.md  → complete agent reasoning and execution flow
ANDROID_COMPONENTS.md          → Android service implementation
PLUGIN_SDK.md                  → extension system
TECH_STACK.md                  → implementation technologies
```

## Implementation notes

- Flutter UI can be implemented as a separate app or dashboard client.
- The Gateway should not expose raw internal agent objects directly.
- WebSocket should be used for streaming logs, status, screenshots, and approval prompts.
- REST can be added for simple request/response APIs such as health checks, configuration, and plugin listing.
- Long-running tasks should have cancellation tokens.
- High-risk executor actions should support approval gates.
- Screenshots can be sent as references or base64 payloads depending on size and privacy policy.

## MVP scope

A minimum useful communication stack should support:

1. Flutter UI task submission.
2. WebSocket connection and reconnect.
3. Gateway event validation.
4. Planner task dispatch.
5. Executor status streaming.
6. Android screenshot/status events.
7. Task cancellation.
8. Final result delivery.

Approval prompts, plugin UI panels, and advanced dashboard features can be added after the core communication loop is stable.
