package cc.nationclaw.android.media

import android.content.Context
import android.content.Intent
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread

class MediaProjectionController(private val context: Context) {
    private val manager = context.getSystemService(MediaProjectionManager::class.java)
    private var projection: MediaProjection? = null
    private var thread: HandlerThread? = null

    fun createCaptureIntent(): Intent = manager.createScreenCaptureIntent()

    fun setProjection(resultCode: Int, data: Intent) {
        projection = manager.getMediaProjection(resultCode, data)
    }

    fun stop() {
        projection?.stop()
        projection = null
        thread?.quitSafely()
        thread = null
    }

    fun createImageReader(width: Int, height: Int, densityDpi: Int): ImageReader {
        val activeProjection = projection ?: throw IllegalStateException("MediaProjection permission has not been granted")
        val imageReader = ImageReader.newInstance(width, height, android.graphics.PixelFormat.RGBA_8888, 2)
        val handlerThread = HandlerThread("NationClawMediaProjection").also { it.start() }
        thread = handlerThread
        activeProjection.createVirtualDisplay(
            "NationClawScreenCapture",
            width,
            height,
            densityDpi,
            0,
            imageReader.surface,
            null,
            Handler(handlerThread.looper)
        )
        return imageReader
    }
}
