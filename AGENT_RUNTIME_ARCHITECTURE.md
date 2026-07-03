# NationClaw Agent Runtime Architecture

This document describes the end-to-end NationClaw agent runtime: user input enters through voice, chat, or camera; the system parses that input, enriches it with context and memory, plans the task, and executes actions on an Android device.

## High-level architecture

```text
USER
  │
  ├── Voice
  ├── Chat UI
  └── Camera
          │
          ▼
Input Processing Layer
          │
          ▼
Speech / OCR / Vision Parser
          │
          ▼
Context & Memory Engine
          │
          ▼
Agent Planning Engine
          │
          ├── Task Planner
          ├── Reasoning LLM
          └── Workflow Engine
          │
          ▼
Action Executor
          │
          ├── Accessibility
          ├── Shell / API
          ├── App Control
          └── File System
          │
          ▼
Android Device
```

## Layered view

```text
USER
│
├── Voice
├── Chat UI
└── Camera
        │
        ▼
Input Processing Layer
│
├── Speech Input Adapter
├── Chat Message Adapter
├── Camera / Image Adapter
└── Attachment Normalizer
        │
        ▼
Speech / OCR / Vision Parser
│
├── Speech-to-Text Parser
├── OCR Parser
├── Vision Parser
├── Intent Extractor
└── Input Safety Filter
        │
        ▼
Context & Memory Engine
│
├── Profile Loader
├── Daily Memory
├── Long-term Memory
├── Working Memory
├── File / Artifact Context
└── Conversation History
        │
        ▼
Agent Planning Engine
│
├── Task Planner
├── Reasoning LLM
├── Workflow Engine
├── Tool Registry
└── Policy / Permission Gate
        │
        ▼
Action Executor
│
├── Accessibility Executor
├── Shell / API Executor
├── App Control Executor
├── File System Executor
├── Chat Response Executor
└── Audit Logger
        │
        ▼
Android Device
```

## Layer responsibilities

### 1. User input channels

NationClaw should support multiple user input channels.

#### Voice

Voice input can come from the Android client, a desktop microphone, or a chat platform that sends audio files.

Responsibilities:

- capture spoken instructions;
- convert speech into text;
- pass normalized user intent to the input processing layer.

Related Android component:

- `Speech Recognition`

Related Plugin SDK type:

- `AI Plugin`
- `Tool Plugin`
- `UI Plugin`

#### Chat UI

Chat is currently the main interaction channel for the Python runtime.

Responsibilities:

- receive user messages;
- preserve sender and channel metadata;
- pass message history to the agent;
- return agent replies and task progress updates.

Existing integrations include:

- Telegram
- Lark / Feishu
- QQ
- Zulip
- Discord
- WhatsApp
- Slack
- Weixin

Related Python package:

```text
nationclaw/chat/
```

#### Camera

Camera input can mean either direct camera capture or user-provided images.

Responsibilities:

- capture or receive images;
- pass visual input to OCR or vision parsers;
- support image-based tasks such as reading signs, scanning documents, or interpreting screenshots.

Related Android component:

- `Media Projection`
- optional camera capture module

## 2. Input Processing Layer

The input processing layer normalizes raw user input into a consistent task request.

Responsibilities:

- normalize voice, chat, image, and file input;
- preserve metadata such as sender, channel, timestamp, and attachment paths;
- detect input type;
- prepare input for speech, OCR, or vision parsing;
- reject malformed or unsafe payloads.

Suggested normalized schema:

```json
{
  "input_id": "msg_001",
  "source": "chat",
  "channel": "telegram",
  "sender": "user123",
  "content": "Open WhatsApp and check the latest message",
  "attachments": [],
  "timestamp": "2026-07-03T18:00:00+01:00"
}
```

## 3. Speech / OCR / Vision Parser

This layer converts multimodal input into model-readable text, structured observations, or image references.

Responsibilities:

- speech-to-text conversion;
- OCR over screenshots, photos, or documents;
- visual understanding for images and screenshots;
- extracting entities, tasks, constraints, and relevant context;
- producing a canonical task description.

Possible outputs:

```json
{
  "task": "Check the latest WhatsApp message and summarize it",
  "entities": ["WhatsApp"],
  "constraints": ["Do not send any reply unless asked"],
  "input_modalities": ["chat"]
}
```

Related components:

- `Speech Recognition`
- `Media Projection`
- GUI-VLM model adapter
- OCR Tool Plugin

## 4. Context & Memory Engine

The context and memory engine prepares the agent's working context before planning.

Responsibilities:

- load agent profile;
- load daily memory;
- load long-term memory;
- retrieve task-specific working memory;
- inspect relevant files and artifacts;
- retrieve conversation history;
- include recent device state or screenshots when needed;
- store useful outcomes after task completion.

Existing Python package:

```text
nationclaw/file/
```

Existing memory layers:

```text
daily_memory/
working_memory/
long_term_memory.md
profile.md
skills/
```

Related Plugin SDK type:

- `Storage Plugin`
- `AI Plugin` for embeddings and semantic retrieval

## 5. Agent Planning Engine

The planning engine decides what the agent should do next.

It is composed of three main submodules:

```text
Task Planner    Reasoning LLM    Workflow Engine
```

### Task Planner

Responsibilities:

- convert the user's request into executable subtasks;
- decide whether the task requires device control, file operations, model calls, or chat replies;
- select the next minimal action;
- avoid repeating failed actions.

Existing logic is primarily in:

```text
nationclaw/agent.py
nationclaw/fm/function_hub_local.py
```

### Reasoning LLM

Responsibilities:

- reason over current context;
- choose the next action;
- generate tool or agent API calls;
- interpret action results;
- decide when a task is finished, failed, or infeasible.

Existing model interface:

```text
nationclaw/fm/
```

### Workflow Engine

Responsibilities:

- run reusable multi-step procedures;
- persist workflow progress;
- resume interrupted workflows;
- coordinate tools, device actions, and file outputs.

Related Plugin SDK type:

- `Workflow Plugin`

## 6. Action Executor

The action executor maps the planning engine's decision to concrete operations.

```text
Action Executor
│
├── Accessibility
├── Shell / API
├── App Control
└── File System
```

### Accessibility

Responsibilities:

- execute tap, swipe, long press, back, home, and text entry actions;
- read UI hierarchy;
- inspect focused fields;
- coordinate with screenshots and GUI-VLM output.

Related Android component:

- `Accessibility Service`

Related Python files:

```text
nationclaw/device/phone/websocket_device.py
nationclaw/device/device_base.py
```

### Shell / API

Responsibilities:

- execute ADB-backed commands where appropriate;
- call Android bridge APIs;
- call external APIs through Network Plugins;
- use shell or API paths as fallbacks when GUI control is unreliable.

Related components:

- ADB
- WebSocket bridge
- `Network Plugin`

### App Control

Responsibilities:

- launch apps;
- resolve app package names;
- inspect installed apps;
- kill or restart apps;
- dispatch intents.

Related Android components:

- `Package Manager`
- `Intent Dispatcher`

### File System

Responsibilities:

- read and write agent memory files;
- store screenshots and artifacts;
- generate documents;
- maintain logs;
- parse input files.

Existing Python package:

```text
nationclaw/file/
```

Related Plugin SDK type:

- `Storage Plugin`

## 7. Android Device

The Android device is the execution target.

Expected Android-side components are defined in:

```text
ANDROID_COMPONENTS.md
```

The core Android runtime should provide:

- Accessibility Service;
- Foreground Service;
- Overlay Service;
- Notification Listener;
- Media Projection;
- Speech Recognition;
- Text To Speech;
- Clipboard Monitor;
- Package Manager;
- Intent Dispatcher.

## End-to-end execution flow

```text
1. User sends a request through voice, chat, or camera.
2. Input Processing Layer normalizes the request.
3. Speech / OCR / Vision Parser extracts text, visual observations, and intent.
4. Context & Memory Engine loads relevant profile, memory, files, and history.
5. Agent Planning Engine chooses the next action.
6. Action Executor performs the action through device, shell/API, app control, or file system.
7. The Android Device returns observations such as screenshots, UI tree, or command results.
8. Observations are added back to context.
9. The loop repeats until the task is finished, failed, or infeasible.
10. The agent stores useful memory and optionally replies to the user.
```

## Mapping to current NationClaw modules

| Architecture layer | Current module / document |
| --- | --- |
| Chat UI | `nationclaw/chat/` |
| Input processing | `AutoAgent.handle_message` in `nationclaw/agent.py` |
| Vision parser | `nationclaw/fm/function_hub_local.py` GUI-VLM calls |
| Context & memory | `nationclaw/file/` |
| Agent planning | `nationclaw/agent.py`, `nationclaw/fm/function_hub_local.py` |
| Task planner | `task_step` prompt/function flow |
| Reasoning LLM | `nationclaw/fm/` |
| Workflow engine | `PLUGIN_SDK.md` Workflow Plugin proposal |
| Action executor | `AutoAgent._create_agent_api_for_execution` |
| Accessibility | Android client + `websocket_device.py` |
| Shell/API | ADB and WebSocket command bridge |
| App control | Android Package Manager + Intent Dispatcher |
| File system | `nationclaw/file/file_interface.py` |
| Android components | `ANDROID_COMPONENTS.md` |

## Relationship to Android Components and Plugin SDK

This architecture is the top-level runtime flow.

Related documents:

- `ANDROID_COMPONENTS.md` defines the Android-side capability modules.
- `PLUGIN_SDK.md` defines how developers can extend this runtime.

Together:

```text
AGENT_RUNTIME_ARCHITECTURE.md  → end-to-end agent flow
ANDROID_COMPONENTS.md          → Android capability implementation
PLUGIN_SDK.md                  → extension mechanism
```

## MVP implementation scope

The minimum viable runtime should support:

1. Chat UI input.
2. Text-only input processing.
3. Context and memory loading from markdown files.
4. Reasoning LLM planning.
5. Android screenshots and accessibility actions.
6. App launch and basic app control.
7. File system memory updates.
8. Chat response back to the user.

Voice, camera, OCR, advanced workflows, and plugin-based extensions can be added incrementally.

## Safety requirements

- The user must be aware when Android automation is active.
- High-risk actions should be logged.
- Device-control actions should support interruption and shutdown.
- Sensitive data from screenshots, clipboard, notifications, and chat should not be persisted unless explicitly needed.
- The planning engine should prefer minimal, reversible actions.
- The agent should not control the user's main personal device unless the user fully understands the risk.
