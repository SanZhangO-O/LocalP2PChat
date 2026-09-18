package com.zqr.localchat

import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.widget.Toast
import com.zqr.localchat.viewmodel.ChatViewModel

/**
 * Quick-reply handler for message notifications: reads the text the user
 * typed into the notification's RemoteInput action and hands it to
 * [ChatViewModel.tryDeliverQuickReply], which routes it through the normal
 * send paths (group relay / direct session) and clears the conversation's
 * unread state. Runs on the main thread; the send itself only enqueues onto a
 * session sender queue, so no blocking network I/O happens here.
 */
class NotificationReplyReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ChatViewModel.ACTION_NOTIF_REPLY) return
        val conversationId =
            intent.getStringExtra(ChatViewModel.EXTRA_REPLY_CONVERSATION_ID) ?: return
        val text = RemoteInput.getResultsFromIntent(intent)
            ?.getCharSequence(ChatViewModel.REMOTE_INPUT_QUICK_REPLY)
            ?.toString()
            ?.trim()
            .orEmpty()
        if (text.isEmpty()) return
        if (!ChatViewModel.tryDeliverQuickReply(conversationId, text)) {
            Toast.makeText(context, "回复未发送：会话未连接", Toast.LENGTH_SHORT).show()
        }
    }
}
