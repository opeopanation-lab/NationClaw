package cc.nationclaw.android.bridge

import android.content.Context
import cc.nationclaw.android.accessibility.NationClawAccessibilityService
import cc.nationclaw.android.clipboard.ClipboardController
import cc.nationclaw.android.intent.IntentDispatcher
import cc.nationclaw.android.notification.NationClawNotificationListenerService
import cc.nationclaw.android.overlay.OverlayController
import cc.nationclaw.android.packageinfo.PackageManagerAdapter
import cc.nationclaw.android.speech.TextToSpeechController
import cc.nationclaw.android.speech.SpeechRecognitionController
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.Base64
import java.util.concurrent.Executors
import kotlin.concurrent.thread

class AndroidBridgeServer(private val context: Context, private val port: Int) {
    @Volatile private var running = false
    private var serverSocket: ServerSocket? = null
    private val clients = Executors.newCachedThreadPool()
    private val clipboard = ClipboardController(context)
    private val packages = PackageManagerAdapter(context)
    private val intents = IntentDispatcher(context)
    private val speech = SpeechRecognitionController(context)

    fun start() {
        if (running) return
        running = true
        thread(name = "NationClawBridgeServer") {
            serverSocket = ServerSocket(port, 50, java.net.InetAddress.getByName("127.0.0.1"))
            while (running) {
                val socket = runCatching { serverSocket?.accept() }.getOrNull() ?: continue
                clients.submit { handleClient(socket) }
            }
        }
    }

    fun stop() {
        running = false
        runCatching { serverSocket?.close() }
        clients.shutdownNow()
    }

    private fun handleClient(socket: Socket) {
        socket.use {
            val input = BufferedInputStream(it.getInputStream())
            val output = it.getOutputStream()
            if (!handshake(input, output)) return
            while (running && !it.isClosed) {
                val message = readFrame(input) ?: break
                val response = runCatching { dispatch(message) }
                    .getOrElse { error(it.message ?: it.javaClass.simpleName) }
                writeFrame(output, response.toString())
            }
        }
    }

    private fun handshake(input: BufferedInputStream, output: OutputStream): Boolean {
        var key: String? = null
        while (true) {
            val line = readHttpLine(input) ?: return false
            if (line.isEmpty()) break
            val idx = line.indexOf(':')
            if (idx > 0 && line.substring(0, idx).trim().equals("Sec-WebSocket-Key", ignoreCase = true)) {
                key = line.substring(idx + 1).trim()
            }
        }
        val wsKey = key ?: return false
        val accept = Base64.getEncoder().encodeToString(
            MessageDigest.getInstance("SHA-1").digest((wsKey + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").toByteArray(Charsets.ISO_8859_1))
        )
        val response = "HTTP/1.1 101 Switching Protocols\r\n" +
            "Upgrade: websocket\r\n" +
            "Connection: Upgrade\r\n" +
            "Sec-WebSocket-Accept: $accept\r\n\r\n"
        output.write(response.toByteArray(Charsets.ISO_8859_1))
        output.flush()
        return true
    }

    private fun readHttpLine(input: BufferedInputStream): String? {
        val bytes = ArrayList<Byte>()
        while (true) {
            val b = input.read()
            if (b < 0) return null
            if (b == '\r'.code) {
                val next = input.read()
                if (next == '\n'.code) break
                bytes.add(b.toByte())
                if (next >= 0) bytes.add(next.toByte())
            } else if (b == '\n'.code) {
                break
            } else {
                bytes.add(b.toByte())
            }
        }
        return bytes.toByteArray().toString(Charsets.ISO_8859_1)
    }

    private fun readFrame(input: BufferedInputStream): String? {
        val b1 = input.read()
        if (b1 < 0) return null
        val opcode = b1 and 0x0F
        if (opcode == 0x8) return null
        val b2 = input.read()
        if (b2 < 0) return null
        val masked = (b2 and 0x80) != 0
        var length = (b2 and 0x7F).toLong()
        if (length == 126L) length = ((input.read() shl 8) or input.read()).toLong()
        if (length == 127L) {
            length = 0
            repeat(8) { length = (length shl 8) or input.read().toLong() }
        }
        val mask = ByteArray(4)
        if (masked) input.read(mask)
        val payload = ByteArray(length.toInt())
        var read = 0
        while (read < payload.size) {
            val n = input.read(payload, read, payload.size - read)
            if (n < 0) return null
            read += n
        }
        if (masked) for (i in payload.indices) payload[i] = (payload[i].toInt() xor mask[i % 4].toInt()).toByte()
        return payload.toString(Charsets.UTF_8)
    }

    private fun writeFrame(output: OutputStream, text: String) {
        val payload = text.toByteArray(Charsets.UTF_8)
        output.write(0x81)
        when {
            payload.size < 126 -> output.write(payload.size)
            payload.size <= 65535 -> {
                output.write(126)
                output.write((payload.size shr 8) and 0xFF)
                output.write(payload.size and 0xFF)
            }
            else -> {
                output.write(127)
                val length = payload.size.toLong()
                for (shift in 56 downTo 0 step 8) output.write(((length shr shift) and 0xFF).toInt())
            }
        }
        output.write(payload)
        output.flush()
    }

    private fun dispatch(command: String): JSONObject {
        val parts = command.split(",")
        val name = parts.firstOrNull()?.trim() ?: return error("empty command")
        val acc = NationClawAccessibilityService.instance
        return when (name) {
            "width_height" -> success(acc?.widthHeight() ?: defaultWidthHeight())
            "view_hierarchy" -> if (acc != null) success(acc.viewHierarchy()) else error("Accessibility service is not enabled")
            "screenshot" -> if (acc != null) success(JSONObject().put("data", acc.screenshotBase64())) else error("Accessibility service is not enabled")
            "click" -> requireAcc(acc) { service -> service.tap(parts.getInt(1), parts.getInt(2), parts.getLongOrNull(3) ?: 120L) }
            "drag" -> requireAcc(acc) { service -> service.drag(parts.getInt(1), parts.getInt(2), parts.getInt(3), parts.getInt(4), parts.getLongOrNull(5) ?: 500L) }
            "back" -> requireAcc(acc) { it.back() }
            "home" -> requireAcc(acc) { it.home() }
            "expand_notification" -> requireAcc(acc) { it.expandNotifications() }
            "input" -> requireAcc(acc) { it.setFocusedText(command.substringAfter(',', "")) }
            "clear" -> requireAcc(acc) { it.setFocusedText("") }
            "get_input_field_text" -> if (acc != null) success(JSONObject().put("message", acc.getFocusedText())) else error("Accessibility service is not enabled")
            "set_clipboard" -> { clipboard.setText(command.substringAfter(',', "")); success() }
            "get_clipboard" -> success(JSONObject().put("message", clipboard.getText()))
            "open_app" -> bool(packages.launch(command.substringAfter(',', "")), "app not found or cannot be launched")
            "kill_app" -> bool(packages.kill(command.substringAfter(',', "")), "app not found or cannot be killed")
            "get_app_display_name" -> success(JSONObject().put("message", packages.displayName(command.substringAfter(',', ""))))
            "list_apps" -> success(JSONObject().put("message", packages.listLaunchableApps()))
            "show_highlight" -> bool(OverlayController.showHighlight(parts.getInt(1), parts.getInt(2), parts.getInt(3)), "overlay permission is not granted")
            "hide_highlight" -> bool(OverlayController.hideHighlight(), "failed to hide highlight")
            "open_url" -> bool(intents.openUrl(command.substringAfter(',', "")), "failed to open url")
            "open_settings" -> bool(intents.openSettings(parts.getOrNull(1)), "failed to open settings")
            "send_intent" -> bool(intents.sendIntent(JSONObject(command.substringAfter(',', "{}"))), "failed to send intent")
            "speak" -> bool(TextToSpeechController.speak(command.substringAfter(',', "")), "text-to-speech is not ready")
            "stop_speaking" -> bool(TextToSpeechController.stop(), "failed to stop speech")
            "speech_recognize" -> success(JSONObject().put("message", speech.recognizeOnce()))
            "latest_notification" -> success(JSONObject().put("message", NationClawNotificationListenerService.latest ?: JSONObject()))
            else -> error("unknown command: $name")
        }
    }

    private fun defaultWidthHeight(): JSONObject {
        val metrics = context.resources.displayMetrics
        return JSONObject().put("width", metrics.widthPixels).put("height", metrics.heightPixels)
    }

    private fun requireAcc(acc: NationClawAccessibilityService?, action: (NationClawAccessibilityService) -> Boolean): JSONObject {
        return if (acc == null) error("Accessibility service is not enabled") else bool(action(acc), "accessibility action failed")
    }

    private fun bool(ok: Boolean, message: String): JSONObject = if (ok) success() else error(message)
    private fun success(extra: JSONObject = JSONObject()): JSONObject = JSONObject(extra.toString()).put("status", "success")
    private fun error(message: String): JSONObject = JSONObject().put("status", "error").put("message", message)

    private fun List<String>.getInt(index: Int): Int = getOrNull(index)?.toIntOrNull() ?: throw IllegalArgumentException("missing integer argument $index")
    private fun List<String>.getLongOrNull(index: Int): Long? = getOrNull(index)?.toLongOrNull()
}
