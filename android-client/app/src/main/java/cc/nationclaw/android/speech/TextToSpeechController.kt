package cc.nationclaw.android.speech

import android.content.Context
import android.speech.tts.TextToSpeech
import java.util.Locale

object TextToSpeechController : TextToSpeech.OnInitListener {
    private var tts: TextToSpeech? = null
    private var ready = false

    fun initialize(context: Context) {
        tts = TextToSpeech(context.applicationContext, this)
    }

    override fun onInit(status: Int) {
        ready = status == TextToSpeech.SUCCESS
        if (ready) tts?.language = Locale.getDefault()
    }

    fun speak(text: String): Boolean {
        if (!ready) return false
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "nationclaw_tts")
        return true
    }

    fun stop(): Boolean {
        tts?.stop()
        return true
    }
}
