package cc.nationclaw.android.intent

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import org.json.JSONObject

class IntentDispatcher(private val context: Context) {
    fun openUrl(url: String): Boolean {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }

    fun openSettings(action: String?): Boolean {
        val resolved = action?.takeIf { it.isNotBlank() } ?: Settings.ACTION_SETTINGS
        context.startActivity(Intent(resolved).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return true
    }

    fun sendIntent(payload: JSONObject): Boolean {
        val action = payload.optString("action", Intent.ACTION_VIEW)
        val data = payload.optString("data", "").takeIf { it.isNotBlank() }?.let { Uri.parse(it) }
        val intent = Intent(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        data?.let { intent.data = it }
        payload.optString("package", "").takeIf { it.isNotBlank() }?.let { intent.setPackage(it) }
        context.startActivity(intent)
        return true
    }
}
