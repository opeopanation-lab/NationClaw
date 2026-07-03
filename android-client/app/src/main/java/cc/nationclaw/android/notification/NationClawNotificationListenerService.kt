package cc.nationclaw.android.notification

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONObject

class NationClawNotificationListenerService : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn ?: return
        latest = JSONObject()
            .put("packageName", sbn.packageName)
            .put("timestamp", sbn.postTime)
            .put("title", sbn.notification.extras.getCharSequence("android.title")?.toString() ?: "")
            .put("text", sbn.notification.extras.getCharSequence("android.text")?.toString() ?: "")
    }

    companion object {
        @Volatile var latest: JSONObject? = null
    }
}
