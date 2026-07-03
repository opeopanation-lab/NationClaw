package cc.nationclaw.android.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import cc.nationclaw.android.bridge.AndroidBridgeServer

class NationClawForegroundService : Service() {
    private var server: AndroidBridgeServer? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(1001, createNotification())
        server = AndroidBridgeServer(applicationContext, 6666).also { it.start() }
    }

    override fun onDestroy() {
        server?.stop()
        server = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotification(): Notification {
        val channelId = "nationclaw_bridge"
        if (Build.VERSION.SDK_INT >= 26) {
            val channel = NotificationChannel(
                channelId,
                "NationClaw Bridge",
                NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
        return if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, channelId)
                .setContentTitle("NationClaw is active")
                .setContentText("Automation bridge is listening on localhost:6666")
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setOngoing(true)
                .build()
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
                .setContentTitle("NationClaw is active")
                .setContentText("Automation bridge is listening on localhost:6666")
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setOngoing(true)
                .build()
        }
    }
}
