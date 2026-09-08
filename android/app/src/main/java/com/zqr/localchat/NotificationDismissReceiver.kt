package com.zqr.localchat

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.zqr.localchat.viewmodel.ChatViewModel

/**
 * Swipe-away handler for message notifications: keeps the ViewModel's
 * notification log in sync with what the user actually dismissed, so the
 * group's next message does not resurrect dismissed bubbles and the grouped
 * summary never shows stale counts. Runs on the main thread; the work is a
 * map removal plus a notification reconcile, well within onReceive limits.
 */
class NotificationDismissReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ChatViewModel.ACTION_NOTIF_DISMISSED) return
        ChatViewModel.onNotificationsDismissed(
            context,
            intent.getStringExtra(ChatViewModel.EXTRA_DISMISSED_GROUP_ID)
        )
    }
}
