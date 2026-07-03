# NationClaw Android Client

The Android client source lives in:

```text
android-client/
```

It is a native Android implementation for NationClaw device automation. It is designed to replace opaque APK-only development with auditable source code.

## Stack

```text
Kotlin
Jetpack Compose
Android Accessibility Service
Foreground Service
Raw WebSocket server on localhost:6666
Android Notification Listener
Overlay Window
Clipboard APIs
Package Manager APIs
Intent dispatch
Speech-to-Text
Text-to-Speech
MediaProjection controller
```

## Implemented components

| Component | Source |
| --- | --- |
| Mobile App | `android-client/app/src/main/java/cc/nationclaw/android/MainActivity.kt` |
| Application | `NationClawApplication.kt` |
| Foreground Service | `service/NationClawForegroundService.kt` |
| WebSocket Bridge | `bridge/AndroidBridgeServer.kt` |
| Accessibility Service | `accessibility/NationClawAccessibilityService.kt` |
| Overlay Service | `overlay/OverlayController.kt` |
| Notification Listener | `notification/NationClawNotificationListenerService.kt` |
| Media Projection | `media/MediaProjectionController.kt` |
| Speech Recognition | `speech/SpeechRecognitionController.kt` |
| Text To Speech | `speech/TextToSpeechController.kt` |
| Clipboard Monitor/Controller | `clipboard/ClipboardController.kt` |
| Package Manager | `packageinfo/PackageManagerAdapter.kt` |
| Intent Dispatcher | `intent/IntentDispatcher.kt` |

## Build

From the Android client directory:

```bash
cd android-client
./gradlew assembleDebug
```

If the Gradle wrapper is not present, use a system Gradle installation:

```bash
gradle assembleDebug
```

## Install

```bash
adb install android-client/app/build/outputs/apk/debug/app-debug.apk
```

## Setup on device

1. Open NationClaw app.
2. Enable Accessibility Service.
3. Enable Notification Listener if notification ingestion is needed.
4. Enable Overlay permission if highlight visualization is needed.
5. Start the NationClaw Bridge from the app.
6. Forward the bridge port from your computer:

```bash
adb forward tcp:51825 tcp:6666
```

Then configure Python with:

```yaml
phone_port_mappings:
  phone1: 51825
```

## Bridge commands

The Android bridge listens on phone-local port `6666`. The Python runtime reaches it through ADB forwarding.

Supported commands include:

```text
width_height
view_hierarchy
screenshot
click,x,y,duration
drag,x1,y1,x2,y2,duration
back
home
expand_notification
input,text
clear
get_input_field_text
set_clipboard,text
get_clipboard
open_app,appNameOrPackage
kill_app,appNameOrPackage
get_app_display_name,packageName
list_apps
show_highlight,x,y,radius
hide_highlight
open_url,url
open_settings,settingsAction
send_intent,jsonPayload
speak,text
stop_speaking
speech_recognize
latest_notification
```

## Response contract

Success:

```json
{
  "status": "success",
  "message": "optional result",
  "data": "optional base64 payload"
}
```

Error:

```json
{
  "status": "error",
  "message": "human-readable reason"
}
```

## Notes

- The WebSocket server binds to `127.0.0.1` inside the phone for safety.
- Screenshot uses Android Accessibility screenshot support on Android 11/API 30+.
- App killing uses `ActivityManager.killBackgroundProcesses`, which is limited by Android security policy.
- Speech recognition requires the Android speech recognizer service and microphone permission.
- MediaProjection support is included as a controller for screen-capture flows that require explicit user consent.
