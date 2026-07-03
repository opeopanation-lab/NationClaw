# NationClaw Android Components

This document defines the Android-side component architecture for the NationClaw / MobileClaw client app. The Python runtime communicates with the Android client through a local WebSocket bridge and expects the Android app to expose device perception, device actions, app management, and system interaction capabilities.

## Component tree

```text
Application
│
├── Accessibility Service
├── Foreground Service
├── Overlay Service
├── Notification Listener
├── Media Projection
├── Speech Recognition
├── Text To Speech
├── Clipboard Monitor
├── Package Manager
└── Intent Dispatcher
```

## Runtime architecture

```text
NationClaw Android Client
│
├── NationClawApplication
│   ├── Permission Orchestrator
│   ├── Component Registry
│   └── Shared Event Bus
│
├── Bridge Layer
│   ├── Foreground Service
│   ├── WebSocket Server
│   ├── Command Router
│   └── Response Serializer
│
├── Perception Layer
│   ├── Accessibility Service
│   ├── Media Projection Screenshot Provider
│   ├── Notification Listener
│   └── Clipboard Monitor
│
├── Action Layer
│   ├── Accessibility Action Executor
│   ├── Intent Dispatcher
│   ├── Package Manager Adapter
│   └── Clipboard Controller
│
└── Interaction Layer
    ├── Overlay Service
    ├── Speech Recognition
    └── Text To Speech
```

## Components

### 1. Application

Suggested class:

```text
cc.nationclaw.android.NationClawApplication
```

Responsibilities:

- initialize logging and crash reporting;
- initialize the component registry;
- expose app-wide dependency providers;
- track permission readiness;
- coordinate startup of the foreground bridge service.

The `Application` should not perform long-running work directly. Long-running work belongs in the `Foreground Service`.

### 2. Foreground Service

Suggested class:

```text
cc.nationclaw.android.service.NationClawForegroundService
```

Responsibilities:

- keep the Android bridge alive;
- host the local WebSocket server, usually on port `6666` inside the phone;
- expose commands consumed by the Python controller after ADB forwarding, e.g. `adb forward tcp:51825 tcp:6666`;
- maintain a persistent notification explaining that device automation is active;
- route commands to the correct Android capability module;
- serialize responses as JSON.

This is the central runtime service for PC-to-phone communication.

### 3. Accessibility Service

Suggested class:

```text
cc.nationclaw.android.accessibility.NationClawAccessibilityService
```

Responsibilities:

- read the current UI hierarchy;
- expose bounds, text, content descriptions, clickability, focus state, enabled state, and visibility;
- execute UI actions:
  - click / tap;
  - long click;
  - swipe / drag;
  - back;
  - home;
  - text input when supported;
  - clear focused text field when supported;
- detect the active package and focused node;
- provide a fallback action path when ADB input is unavailable.

Expected bridge commands:

```text
view_hierarchy
click,x,y,duration
long_click,x,y,duration
back
home
input,text
clear
get_input_field_text
```

### 4. Overlay Service

Suggested classes:

```text
cc.nationclaw.android.overlay.NationClawOverlayService
cc.nationclaw.android.overlay.HighlightOverlayView
```

Responsibilities:

- show touch indicators;
- show highlight circles/boxes for debugging;
- show agent status bubbles if enabled;
- avoid obstructing user interactions whenever possible.

Expected bridge commands:

```text
show_highlight,x,y,radius
hide_highlight
```

### 5. Notification Listener

Suggested class:

```text
cc.nationclaw.android.notification.NationClawNotificationListenerService
```

Responsibilities:

- observe incoming notifications from messaging apps;
- normalize notification events into a common schema;
- optionally pass message events to the bridge;
- support chat-triggered automation workflows;
- avoid storing sensitive notification content unless explicitly configured.

Suggested event schema:

```json
{
  "type": "notification",
  "packageName": "com.example.app",
  "title": "Sender or app title",
  "text": "Notification text",
  "timestamp": 1730000000000
}
```

### 6. Media Projection

Suggested classes:

```text
cc.nationclaw.android.media.MediaProjectionController
cc.nationclaw.android.media.ScreenshotProvider
```

Responsibilities:

- capture the current screen for visual grounding;
- provide screenshots as PNG bytes or base64 strings;
- support privacy-safe failure modes when capture is blocked;
- optionally support screen recording.

Expected bridge commands:

```text
screenshot
start_screen_record
stop_screen_record
```

### 7. Speech Recognition

Suggested class:

```text
cc.nationclaw.android.speech.SpeechRecognitionController
```

Responsibilities:

- convert user speech to text;
- expose optional voice-command input;
- emit recognition events to the bridge or local event bus.

This component is optional for base GUI automation but useful for hands-free interaction.

### 8. Text To Speech

Suggested class:

```text
cc.nationclaw.android.speech.TextToSpeechController
```

Responsibilities:

- speak agent responses aloud;
- support stop/cancel speech;
- expose language and voice configuration where available.

Suggested bridge commands:

```text
speak,text
stop_speaking
```

### 9. Clipboard Monitor

Suggested class:

```text
cc.nationclaw.android.clipboard.ClipboardController
```

Responsibilities:

- set clipboard text for robust input;
- read clipboard text when permitted;
- support non-ASCII text input fallback;
- optionally monitor clipboard changes with user consent.

Expected bridge commands:

```text
set_clipboard,text
get_clipboard
```

Security requirement: clipboard monitoring should be disabled by default and should never persist clipboard content unless explicitly requested.

### 10. Package Manager

Suggested class:

```text
cc.nationclaw.android.packageinfo.PackageManagerAdapter
```

Responsibilities:

- list installed applications;
- resolve app display names to package names;
- return package metadata;
- resolve launcher activities;
- support launch and kill operations through package names when appropriate.

Expected bridge commands:

```text
open_app,appNameOrPackage
kill_app,appNameOrPackage
get_app_display_name,packageName
width_height
```

Suggested app info schema:

```json
{
  "name": "Chrome",
  "localName": "Chrome",
  "appPkg": "com.android.chrome",
  "appLauncher": "com.google.android.apps.chrome.Main"
}
```

### 11. Intent Dispatcher

Suggested class:

```text
cc.nationclaw.android.intent.IntentDispatcher
```

Responsibilities:

- open apps by package/activity;
- open URLs;
- open Android settings screens;
- dispatch share intents;
- open files with compatible apps;
- validate and restrict dangerous intents.

Suggested bridge commands:

```text
open_url,url
open_settings,settingsAction
send_intent,jsonPayload
```

## WebSocket command contract

The Python controller currently expects the Android bridge to return JSON with a `status` field.

Success response:

```json
{
  "status": "success",
  "message": "optional text result",
  "data": "optional base64 or structured payload"
}
```

Failure response:

```json
{
  "status": "error",
  "message": "human-readable error message"
}
```

Existing Python-side commands to support:

```text
width_height
screenshot
view_hierarchy
open_app,<app>
get_app_display_name,<package>
kill_app,<app>
back
home
click,<x>,<y>,<duration>
drag,<x1>,<y1>,<x2>,<y2>,<duration>
input,<text>
clear
get_input_field_text
set_clipboard,<text>
get_clipboard
expand_notification
start_screen_record
stop_screen_record
show_highlight,<x>,<y>,<radius>
hide_highlight
```

## Permission model

The Android app should present a setup checklist for permissions:

| Capability | Android permission / user setting | Required |
| --- | --- | --- |
| Accessibility actions and UI tree | Accessibility Service enablement | Yes |
| Long-running bridge | Foreground service notification | Yes |
| Screenshots | Media Projection consent | Yes for visual grounding |
| Overlay highlights | Draw over other apps | Optional |
| Notifications | Notification Listener access | Optional |
| Speech recognition | Microphone / speech recognizer | Optional |
| Text to speech | TTS engine availability | Optional |
| Clipboard | Clipboard APIs | Optional |
| Package queries | Package visibility / queries | Yes for app launch resolution |

## AndroidManifest skeleton

```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
    <uses-permission android:name="android.permission.SYSTEM_ALERT_WINDOW" />
    <uses-permission android:name="android.permission.RECORD_AUDIO" />
    <uses-permission android:name="android.permission.QUERY_ALL_PACKAGES" />

    <application
        android:name=".NationClawApplication"
        android:label="NationClaw">

        <service
            android:name=".service.NationClawForegroundService"
            android:exported="false"
            android:foregroundServiceType="mediaProjection" />

        <service
            android:name=".accessibility.NationClawAccessibilityService"
            android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"
            android:exported="true">
            <intent-filter>
                <action android:name="android.accessibilityservice.AccessibilityService" />
            </intent-filter>
            <meta-data
                android:name="android.accessibilityservice"
                android:resource="@xml/accessibility_service_config" />
        </service>

        <service
            android:name=".notification.NationClawNotificationListenerService"
            android:label="NationClaw Notification Listener"
            android:permission="android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"
            android:exported="true">
            <intent-filter>
                <action android:name="android.service.notification.NotificationListenerService" />
            </intent-filter>
        </service>

    </application>
</manifest>
```

## Security requirements

- The foreground notification must make automation visible to the user.
- The bridge should bind only to localhost unless explicitly configured otherwise.
- High-risk commands should be auditable.
- Clipboard and notification content should not be persisted by default.
- Intent dispatch should reject unsafe or unknown payloads by default.
- The app should provide a one-tap stop button for all automation services.
- The agent should be used on a spare device or separated app accounts, not a main personal device.

## Implementation priority

Recommended order:

1. `NationClawApplication`
2. `NationClawForegroundService` + WebSocket server
3. `NationClawAccessibilityService`
4. `MediaProjectionController`
5. `PackageManagerAdapter`
6. `ClipboardController`
7. `OverlayService`
8. `NotificationListenerService`
9. `IntentDispatcher`
10. Speech recognition and text-to-speech

This order delivers the minimum viable Android client first: command bridge, UI tree, screenshots, and basic GUI actions.
