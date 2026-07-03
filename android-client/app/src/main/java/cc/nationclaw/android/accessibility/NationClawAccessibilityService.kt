package cc.nationclaw.android.accessibility

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityService.GestureResultCallback
import android.accessibilityservice.GestureDescription
import android.graphics.Bitmap
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.Display
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class NationClawAccessibilityService : AccessibilityService() {
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    override fun onServiceConnected() {
        instance = this
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    fun widthHeight(): JSONObject {
        val metrics = resources.displayMetrics
        return JSONObject().put("width", metrics.widthPixels).put("height", metrics.heightPixels)
    }

    fun viewHierarchy(): JSONObject {
        val root = rootInActiveWindow
        val metrics = resources.displayMetrics
        val nodes = JSONArray()
        if (root != null) collectNode(root, nodes, -1)
        return JSONObject()
            .put("width", metrics.widthPixels)
            .put("height", metrics.heightPixels)
            .put("message", nodes.toString())
    }

    private fun collectNode(node: AccessibilityNodeInfo, nodes: JSONArray, parentId: Int): Int {
        val id = nodes.length()
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        val childIds = JSONArray()
        val obj = JSONObject()
            .put("temp_id", id)
            .put("parent", parentId)
            .put("class", node.className?.toString() ?: "")
            .put("text", node.text?.toString() ?: "")
            .put("content_desc", node.contentDescription?.toString() ?: "")
            .put("resource_id", if (Build.VERSION.SDK_INT >= 18) node.viewIdResourceName ?: "" else "")
            .put("clickable", node.isClickable)
            .put("long_clickable", node.isLongClickable)
            .put("enabled", node.isEnabled)
            .put("focused", node.isFocused)
            .put("visible", node.isVisibleToUser)
            .put("bounds", JSONArray().put(JSONArray().put(bounds.left).put(bounds.top)).put(JSONArray().put(bounds.right).put(bounds.bottom)))
            .put("children", childIds)
        nodes.put(obj)
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val childId = collectNode(child, nodes, id)
            childIds.put(childId)
            child.recycle()
        }
        return id
    }

    fun tap(x: Int, y: Int, durationMs: Long = 120): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        return dispatchBlocking(GestureDescription.Builder().addStroke(stroke).build())
    }

    fun drag(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long = 500): Boolean {
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs)
        return dispatchBlocking(GestureDescription.Builder().addStroke(stroke).build())
    }

    private fun dispatchBlocking(gesture: GestureDescription): Boolean {
        val latch = CountDownLatch(1)
        var result = false
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                result = true
                latch.countDown()
            }
            override fun onCancelled(gestureDescription: GestureDescription?) {
                result = false
                latch.countDown()
            }
        }, null)
        latch.await(5, TimeUnit.SECONDS)
        return result
    }

    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)
    fun home(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)
    fun expandNotifications(): Boolean = performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)

    fun setFocusedText(text: String): Boolean {
        val node = findFocusedNode() ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    fun getFocusedText(): String {
        return findFocusedNode()?.text?.toString() ?: ""
    }

    private fun findFocusedNode(): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null
        return root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: root.findFocus(AccessibilityNodeInfo.FOCUS_ACCESSIBILITY)
    }

    fun screenshotBase64(): String {
        if (Build.VERSION.SDK_INT < 30) {
            throw IllegalStateException("Accessibility screenshot requires Android 11/API 30 or newer")
        }
        val latch = CountDownLatch(1)
        var image: Bitmap? = null
        var error: Throwable? = null
        takeScreenshot(Display.DEFAULT_DISPLAY, Executors.newSingleThreadExecutor(), object : TakeScreenshotCallback {
            override fun onSuccess(screenshot: ScreenshotResult) {
                image = Bitmap.wrapHardwareBuffer(screenshot.hardwareBuffer, screenshot.colorSpace)?.copy(Bitmap.Config.ARGB_8888, false)
                screenshot.hardwareBuffer.close()
                latch.countDown()
            }
            override fun onFailure(errorCode: Int) {
                error = IllegalStateException("takeScreenshot failed with code $errorCode")
                latch.countDown()
            }
        })
        latch.await(10, TimeUnit.SECONDS)
        error?.let { throw it }
        val bitmap = image ?: throw IllegalStateException("No screenshot returned")
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
        return android.util.Base64.encodeToString(out.toByteArray(), android.util.Base64.NO_WRAP)
    }

    companion object {
        @Volatile var instance: NationClawAccessibilityService? = null
    }
}
