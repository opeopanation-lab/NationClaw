# NationClaw Gateway

The NationClaw Gateway is the real UI-to-runtime API layer. It exposes REST and WebSocket interfaces for submitting tasks, streaming progress, cancelling tasks, listing devices, and inspecting task state.

The Gateway wraps the existing `AutoAgent` runtime and executes real NationClaw tasks through `AutoAgent.execute_task`. It is not a demo service and it does not use mock task execution.

## Run

Install the package, then run:

```bash
nationclaw-gateway config.yaml --host 127.0.0.1 --port 11825
```

The default host is `127.0.0.1` for safety. Expose it on a network interface only when you understand the security implications.

```bash
nationclaw-gateway config.yaml --host 0.0.0.0 --port 11825
```

## REST API

### Health

```http
GET /health
```

Response:

```json
{
  "status": "ok",
  "service": "nationclaw-gateway",
  "timestamp": "2026-07-03T18:00:00+00:00"
}
```

### List devices

```http
GET /devices
```

Response:

```json
[
  {
    "name": "phone1",
    "description": "Smartphone"
  }
]
```

### Submit task

```http
POST /tasks
Content-Type: application/json
```

Request:

```json
{
  "task": "Open WhatsApp and summarize the latest unread message",
  "device": "phone1",
  "mode": "normal",
  "max_steps": 30,
  "metadata": {
    "source": "dashboard"
  }
}
```

Response:

```json
{
  "task_id": "task_...",
  "status": "queued"
}
```

### List tasks

```http
GET /tasks
```

### Get task

```http
GET /tasks/{task_id}
```

### Cancel task

```http
POST /tasks/{task_id}/cancel
```

Cancellation is cooperative. The Gateway sets a cancellation token that `AutoAgent.execute_task` checks between planning and execution steps. If the agent is inside a long model call or device call, cancellation completes after that call returns.

## WebSocket API

Connect to:

```text
ws://127.0.0.1:11825/ws
```

Optional session ID:

```text
ws://127.0.0.1:11825/ws?session_id=session_001
```

## Event envelope

All WebSocket messages use this envelope:

```json
{
  "type": "task.submit",
  "requestId": "req_001",
  "sessionId": "session_001",
  "timestamp": "2026-07-03T18:00:00+00:00",
  "payload": {}
}
```

## Submit task over WebSocket

```json
{
  "type": "task.submit",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "task": "Open Settings and tell me the Android version",
    "device": "phone1",
    "mode": "normal",
    "max_steps": 30
  }
}
```

The Gateway broadcasts task status, live `agent.log` events, and the final result event.

## Cancel task over WebSocket

```json
{
  "type": "task.cancel",
  "requestId": "req_002",
  "sessionId": "session_001",
  "payload": {
    "task_id": "task_..."
  }
}
```

## Emitted events

### `task.status`

```json
{
  "type": "task.status",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "task_id": "task_...",
    "status": "running",
    "message": "Task started",
    "cancel_requested": false
  }
}
```

### `agent.log`

```json
{
  "type": "agent.log",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "task_id": "task_...",
    "message": "🔵 Step 1 Thought: I need to inspect the phone screen."
  }
}
```

### `task.result`

```json
{
  "type": "task.result",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "task_id": "task_...",
    "status": "finished",
    "task": "Open Settings and tell me the Android version",
    "result": ["...agent step logs..."]
  }
}
```

### `error`

```json
{
  "type": "error",
  "requestId": "req_001",
  "sessionId": "session_001",
  "payload": {
    "message": "Unsupported event type: unknown"
  }
}
```

## Runtime behavior

- Tasks are executed serially through a single worker to avoid conflicting device actions.
- REST and WebSocket task submission both use the same `GatewayTaskManager`.
- The Gateway uses the configured `AutoAgent`; it does not start the infinite `agent.serve()` loop.
- The Gateway stops the agent cleanly on shutdown.
- A task can be cancelled cooperatively through REST or WebSocket.

## Flutter integration

Flutter should talk only to the Gateway, not directly to internal Python classes or Android services.

Recommended Flutter flow:

```text
Flutter UI → WebSocket → Gateway → AutoAgent Planner/Executor → Android Services
```

## Security notes

- Bind to `127.0.0.1` by default.
- Put authentication in front of the Gateway before exposing it beyond localhost.
- High-risk actions should be routed through the approval event flow as that capability is added.
- Do not expose screenshots, clipboard contents, notifications, or chat logs to untrusted clients.
