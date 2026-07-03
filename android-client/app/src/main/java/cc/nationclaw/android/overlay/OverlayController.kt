package cc.nationclaw.android.overlay

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.os.Build
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager

object OverlayController {
    private lateinit var appContext: Context
    private var windowManager: WindowManager? = null
    private var view: HighlightView? = null

    fun initialize(context: Context) {
        appContext = context.applicationContext
        windowManager = appContext.getSystemService(WindowManager::class.java)
    }

    fun showHighlight(x: Int, y: Int, radius: Int): Boolean {
        if (!Settings.canDrawOverlays(appContext)) return false
        hideHighlight()
        val highlight = HighlightView(appContext, x, y, radius)
        val type = if (Build.VERSION.SDK_INT >= 26) WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY else WindowManager.LayoutParams.TYPE_PHONE
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            android.graphics.PixelFormat.TRANSLUCENT
        ).apply { gravity = Gravity.TOP or Gravity.START }
        windowManager?.addView(highlight, params)
        view = highlight
        return true
    }

    fun hideHighlight(): Boolean {
        view?.let { runCatching { windowManager?.removeView(it) } }
        view = null
        return true
    }
}

private class HighlightView(context: Context, private val x: Int, private val y: Int, private val radius: Int) : View(context) {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(180, 33, 150, 243)
        style = Paint.Style.STROKE
        strokeWidth = 8f
    }
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        canvas.drawCircle(x.toFloat(), y.toFloat(), radius.toFloat(), paint)
    }
}
