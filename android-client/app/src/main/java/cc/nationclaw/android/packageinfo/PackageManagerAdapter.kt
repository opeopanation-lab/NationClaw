package cc.nationclaw.android.packageinfo

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import org.json.JSONArray
import org.json.JSONObject

class PackageManagerAdapter(private val context: Context) {
    private val pm = context.packageManager

    fun listLaunchableApps(): JSONArray {
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val apps = pm.queryIntentActivities(intent, 0)
        val array = JSONArray()
        for (info in apps) {
            val appInfo = info.activityInfo.applicationInfo
            array.put(JSONObject()
                .put("name", pm.getApplicationLabel(appInfo).toString())
                .put("localName", pm.getApplicationLabel(appInfo).toString())
                .put("appPkg", info.activityInfo.packageName)
                .put("appLauncher", info.activityInfo.name))
        }
        return array
    }

    fun launch(app: String): Boolean {
        val packageName = resolvePackage(app) ?: return false
        val intent = pm.getLaunchIntentForPackage(packageName) ?: return false
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }

    fun kill(app: String): Boolean {
        val packageName = resolvePackage(app) ?: return false
        context.getSystemService(ActivityManager::class.java).killBackgroundProcesses(packageName)
        return true
    }

    fun displayName(packageName: String): String {
        return try {
            val appInfo = pm.getApplicationInfo(packageName, 0)
            pm.getApplicationLabel(appInfo).toString()
        } catch (_: Exception) { "" }
    }

    fun resolvePackage(app: String): String? {
        if (app.contains(".")) {
            try {
                pm.getPackageInfo(app, 0)
                return app
            } catch (_: Exception) {}
        }
        val query = app.lowercase()
        val arr = listLaunchableApps()
        for (i in 0 until arr.length()) {
            val obj = arr.getJSONObject(i)
            val name = obj.optString("name").lowercase()
            val pkg = obj.optString("appPkg")
            if (name == query || name.contains(query) || pkg.lowercase().contains(query)) return pkg
        }
        return null
    }
}
