package com.zqr.localchat

import com.zqr.localchat.viewmodel.ChatViewModel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Notification quick-reply routing: while a ViewModel is alive its sink
 * receives the reply text (the receiver runs outside the ViewModel scope);
 * blank input is rejected and, with no sink, a group reply reports failure
 * instead of being dropped silently.
 */
class QuickReplyRoutingTest {

    @Test
    fun `installed sink receives the reply`() {
        val received = mutableListOf<Pair<String, String>>()
        ChatViewModel.setQuickReplySink { conversationId, text ->
            received.add(conversationId to text)
        }
        try {
            assertTrue(ChatViewModel.tryDeliverQuickReply("group-1", "hello"))
            assertEquals(listOf("group-1" to "hello"), received)

            assertTrue(ChatViewModel.tryDeliverQuickReply("direct:peer-1", "hi"))
            assertEquals(2, received.size)

            assertFalse(ChatViewModel.tryDeliverQuickReply("group-1", "   "))
            assertFalse(ChatViewModel.tryDeliverQuickReply("", "hello"))
            assertFalse(ChatViewModel.tryDeliverQuickReply("group-1", ""))
            assertEquals(2, received.size)
        } finally {
            ChatViewModel.setQuickReplySink(null)
        }
    }

    @Test
    fun `without a sink a group reply is reported as undelivered`() {
        ChatViewModel.setQuickReplySink(null)
        assertFalse(ChatViewModel.tryDeliverQuickReply("group-1", "hello"))
    }

    @Test
    fun `reply intent constants are stable`() {
        // the manifest-declared receiver keys off these action names
        assertEquals("com.zqr.localchat.NOTIF_REPLY", ChatViewModel.ACTION_NOTIF_REPLY)
        assertEquals(
            "com.zqr.localchat.REPLY_CONVERSATION_ID",
            ChatViewModel.EXTRA_REPLY_CONVERSATION_ID
        )
        assertEquals(
            "com.zqr.localchat.QUICK_REPLY",
            ChatViewModel.REMOTE_INPUT_QUICK_REPLY
        )
    }
}
