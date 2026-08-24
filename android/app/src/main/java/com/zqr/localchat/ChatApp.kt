package com.zqr.localchat

import android.app.Application
import android.content.Context
import android.content.Intent
import android.provider.Settings
import androidx.core.content.ContextCompat
import com.zqr.localchat.network.Constants
import java.util.UUID

class ChatApp : Application() {

    override fun onCreate() {
        super.onCreate()
        instance = this
    }

    companion object {
        lateinit var instance: ChatApp
            private set

        private const val PREF_NAME = "localchat_prefs"
        private const val KEY_NICKNAME = "nickname"
        private const val KEY_BACKGROUND_RUNNING = "background_running"
        private const val KEY_PORT = "local_port"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_FINGERPRINT = "hardware_fingerprint"

        /**
         * Stable per-device fingerprint for group IDs. Uses ANDROID_ID when the
         * platform provides it; otherwise a generated value that is persisted
         * once and reused forever, so the fingerprint (and the numeric group ID
         * derived from it) never changes across restarts.
         */
        fun savedFingerprint(ctx: Context): String {
            val prefs = ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE)
            prefs.getString(KEY_FINGERPRINT, null)?.takeIf { it.isNotBlank() }?.let { return it }
            val fp = runCatching {
                Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ANDROID_ID)
            }.getOrNull()?.takeIf { it.isNotBlank() } ?: UUID.randomUUID().toString()
            prefs.edit().putString(KEY_FINGERPRINT, fp).apply()
            return fp
        }

        /**
         * Stable per-device identity, persisted once and reused by every
         * P2PManager. A reconnect (new P2PManager) then looks like the same
         * member to the host: the peer list, message attribution and delete
         * authorization all key off the peer id, so regenerating it per join
         * is what made reconnects feel broken.
         */
        fun savedDeviceId(ctx: Context): String {
            val prefs = ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE)
            var id = prefs.getString(KEY_DEVICE_ID, null)
            if (id.isNullOrBlank()) {
                id = UUID.randomUUID().toString()
                prefs.edit().putString(KEY_DEVICE_ID, id).apply()
            }
            return id
        }

        fun savedNickname(ctx: Context): String =
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE).getString(KEY_NICKNAME, "") ?: ""

        fun saveNickname(ctx: Context, name: String) {
            // defense in depth: every nickname entry point truncates to 20,
            // but a stray caller must not persist an unbounded name either
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE).edit()
                .putString(KEY_NICKNAME, name.take(20))
                .apply()
        }

        /** The single program-wide port used by every host group (default 9999). */
        fun savedPort(ctx: Context): Int =
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE)
                .getInt(KEY_PORT, Constants.TCP_PORT)

        fun savePort(ctx: Context, port: Int) {
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE)
                .edit()
                .putInt(KEY_PORT, port)
                .apply()
        }

        /** Whether the app should keep group connections alive while in the
         * background (via the foreground service). Defaults to true. */
        fun isBackgroundRunning(ctx: Context): Boolean =
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE).getBoolean(KEY_BACKGROUND_RUNNING, true)

        fun setBackgroundRunning(ctx: Context, enabled: Boolean) {
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE)
                .edit()
                .putBoolean(KEY_BACKGROUND_RUNNING, enabled)
                .apply()
        }

        private fun passwordKey(groupId: String) = "group_password_$groupId"

        fun savedGroupPassword(ctx: Context, groupId: String): String =
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE).getString(passwordKey(groupId), "") ?: ""

        fun saveGroupPassword(ctx: Context, groupId: String, password: String) {
            ctx.getSharedPreferences(PREF_NAME, MODE_PRIVATE)
                .edit()
                .putString(passwordKey(groupId), password)
                .apply()
        }

        fun startChatService(ctx: Context) {
            // The foreground service is only used to keep connections alive in
            // the background; when the user disabled background running it must
            // not be started at all.
            if (!isBackgroundRunning(ctx)) return
            ContextCompat.startForegroundService(ctx, Intent(ctx, ChatService::class.java))
        }

        fun stopChatService(ctx: Context) {
            ctx.stopService(Intent(ctx, ChatService::class.java))
        }

        fun refreshNotification(ctx: Context, groupCount: Int) {
            if (!isBackgroundRunning(ctx)) return
            val intent = Intent(ctx, ChatService::class.java)
                .setAction(ChatService.ACTION_REFRESH)
                .putExtra(ChatService.EXTRA_GROUP_COUNT, groupCount)
            runCatching { ContextCompat.startForegroundService(ctx, intent) }
        }
    }
}
