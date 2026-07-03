package cc.nationclaw.android.clipboard

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context

class ClipboardController(private val context: Context) {
    private val clipboard = context.getSystemService(ClipboardManager::class.java)

    fun setText(text: String) {
        clipboard.setPrimaryClip(ClipData.newPlainText("NationClaw", text))
    }

    fun getText(): String {
        return clipboard.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(context)?.toString() ?: ""
    }
}
