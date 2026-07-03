package cc.nationclaw.android

import android.app.Application
import cc.nationclaw.android.overlay.OverlayController
import cc.nationclaw.android.speech.TextToSpeechController

class NationClawApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        OverlayController.initialize(this)
        TextToSpeechController.initialize(this)
    }
}
