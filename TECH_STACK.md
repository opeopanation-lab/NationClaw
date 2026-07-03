# NationClaw Technical Stack

This document summarizes the recommended implementation technologies for NationClaw across the mobile client, AI runtime, automation layer, communication bridge, storage, plugin SDK, and optional dashboard.

## Component technology table

| Component | Typical Technology |
| --- | --- |
| Mobile App | Kotlin + Jetpack Compose (Android) |
| AI Runtime | Local/on-device LLM orchestration with optional external model support |
| Vision | OCR + screenshot analysis |
| Speech | Speech-to-Text + Text-to-Speech |
| Automation | Android Accessibility Service |
| Communication | WebSocket / REST |
| Storage | SQLite / local persistence |
| Plugin SDK | Modular tool interface |
| Dashboard | Web frontend (optional) |

## Recommended stack by layer

### Mobile App

Recommended technology:

```text
Kotlin + Jetpack Compose
```

Responsibilities:

- provide the Android user interface;
- show permission/setup status;
- start and stop NationClaw services;
- expose debugging and runtime status panels;
- provide user-facing controls for automation visibility and safety.

Related architecture document:

```text
ANDROID_COMPONENTS.md
```

### AI Runtime

Recommended technology:

```text
Local/on-device LLM orchestration with optional external model support
```

Responsibilities:

- coordinate task planning;
- call local or remote LLM/VLM providers;
- decide the next agent action;
- support fallback to external OpenAI-compatible APIs;
- support future on-device model runtimes.

Current Python-side modules:

```text
nationclaw/agent.py
nationclaw/fm/
nationclaw/intelligence/
```

### Vision

Recommended technology:

```text
OCR + screenshot analysis
```

Responsibilities:

- process screenshots from Android Media Projection or ADB;
- parse UI screenshots using OCR and/or GUI-VLM models;
- ground actions to screen coordinates;
- extract relevant visual context for the planning engine.

Related Android components:

- Media Projection
- Accessibility Service

Related runtime layer:

```text
Speech / OCR / Vision Parser
```

### Speech

Recommended technology:

```text
Speech-to-Text + Text-to-Speech
```

Responsibilities:

- convert user voice instructions to text;
- read agent responses aloud;
- enable hands-free interaction;
- optionally support voice-triggered workflows.

Related Android components:

- Speech Recognition
- Text To Speech

### Automation

Recommended technology:

```text
Android Accessibility Service
```

Responsibilities:

- inspect the UI hierarchy;
- perform tap, swipe, long press, back, home, and text entry actions;
- provide reliable GUI automation without app-specific integrations;
- serve as the primary mobile automation backend.

Related Python-side controller:

```text
nationclaw/device/phone/websocket_device.py
```

### Communication

Recommended technology:

```text
WebSocket / REST
```

Responsibilities:

- connect the Python runtime to the Android client;
- send commands to the Android device;
- return screenshots, UI tree data, and action results;
- support local ADB port forwarding;
- optionally expose REST endpoints for dashboard or external integrations.

Current expected bridge pattern:

```text
PC/Python Runtime → adb forward → Android WebSocket Service
```

### Storage

Recommended technology:

```text
SQLite / local persistence
```

Responsibilities:

- store local Android client state;
- persist permission status and runtime settings;
- cache device/app metadata;
- optionally store structured events and audit logs;
- keep sensitive data local-first.

Python-side memory currently uses markdown files:

```text
daily_memory/
working_memory/
long_term_memory.md
profile.md
```

SQLite is recommended mainly for the Android client and future structured local state.

### Plugin SDK

Recommended technology:

```text
Modular tool interface
```

Responsibilities:

- allow third-party tools and workflows;
- expose model, storage, network, device, UI, and workflow extensions;
- validate permissions;
- register tools into the agent runtime.

Related architecture document:

```text
PLUGIN_SDK.md
```

### Dashboard

Recommended technology:

```text
Web frontend (optional)
```

Responsibilities:

- display agent status;
- show active tasks and logs;
- show screenshots and device state;
- provide approval controls for sensitive actions;
- manage plugins and configuration.

Potential implementations:

- local web dashboard;
- Electron wrapper;
- hosted admin UI;
- embedded Android WebView for local diagnostics.

## Current vs target implementation

| Area | Current direction | Target direction |
| --- | --- | --- |
| Runtime | Python package and CLI | Python runtime with Android client bridge |
| Android client | Prebuilt APK included | Kotlin + Jetpack Compose source-managed client |
| AI models | OpenAI-compatible external APIs | External + local/on-device orchestration |
| Vision | Screenshot + VLM prompting | OCR + GUI-VLM + structured screen analysis |
| Speech | Planned architecture | Android STT/TTS components |
| Automation | ADB + WebSocket + Accessibility bridge | Accessibility-first Android automation |
| Communication | WebSocket bridge | WebSocket plus optional REST endpoints |
| Storage | Markdown memory files | Markdown memory + SQLite/local persistence |
| Plugins | Initial tool registry | Full Plugin SDK |
| Dashboard | Not core yet | Optional web frontend |

## Related documents

- `AGENT_RUNTIME_ARCHITECTURE.md` — end-to-end runtime flow.
- `ANDROID_COMPONENTS.md` — Android component architecture.
- `PLUGIN_SDK.md` — plugin architecture and extension interfaces.
