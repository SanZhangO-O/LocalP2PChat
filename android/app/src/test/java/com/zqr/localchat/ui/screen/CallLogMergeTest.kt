package com.zqr.localchat.ui.screen

import com.zqr.localchat.data.CallDirection
import com.zqr.localchat.data.CallLogEntity
import com.zqr.localchat.data.CallMedia
import com.zqr.localchat.data.CallResult
import com.zqr.localchat.data.ChatMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Local call-log rows are merged into the 1:1 conversation flow by time and
 * rendered as system lines (never chat bubbles). These are pure functions, so
 * they are unit-tested without a UI.
 */
class CallLogMergeTest {

    private fun msg(id: String, ts: Long) =
        ChatMessage(id = id, content = "m$id", timestamp = ts, senderId = "s", senderName = "S")

    private fun log(
        id: String,
        ts: Long,
        result: String,
        direction: String,
        media: String = CallMedia.VIDEO,
        duration: Long = 0L
    ) = CallLogEntity(
        id = id,
        conversationKey = "direct:peer-1",
        peerId = "peer-1",
        peerName = "\u5bf9\u65b9",
        direction = direction,
        result = result,
        media = media,
        startTime = ts,
        duration = duration
    )

    @Test
    fun `call logs interleave with messages by time`() {
        val items = buildDirectMessageItems(
            listOf(msg("m1", 1000), msg("m2", 3000)),
            listOf(log("c1", 2000, CallResult.MISSED, CallDirection.INCOMING))
        )
        assertEquals(3, items.size)
        assertTrue(items[0] is MessageItem.Msg)
        assertTrue(items[1] is MessageItem.Call)
        assertTrue(items[2] is MessageItem.Msg)
        assertEquals("call:c1", keyOf(items[1]))
    }

    @Test
    fun `no call logs leaves the message list unchanged`() {
        val messages = listOf(msg("m1", 1000), msg("m2", 2000))
        val items = buildDirectMessageItems(messages, emptyList())
        assertEquals(2, items.size)
        assertTrue(items.all { it is MessageItem.Msg })
    }

    @Test
    fun `message order is stable for equal timestamps`() {
        val items = buildDirectMessageItems(
            listOf(msg("m1", 1000), msg("m2", 1000)),
            listOf(log("c1", 1000, CallResult.ANSWERED, CallDirection.OUTGOING))
        )
        // stable sort: the two messages keep their relative order
        assertEquals("m1", (items[0] as MessageItem.Msg).message.id)
        assertEquals("m2", (items[1] as MessageItem.Msg).message.id)
        assertTrue(items[2] is MessageItem.Call)
    }

    @Test
    fun `missed incoming text and audio marker`() {
        assertEquals(
            "\u672a\u63a5\u6765\u7535",
            callLogText(log("c1", 0, CallResult.MISSED, CallDirection.INCOMING))
        )
        assertEquals(
            "\u672a\u63a5\u6765\u7535\uff08\u8bed\u97f3\uff09",
            callLogText(log("c2", 0, CallResult.MISSED, CallDirection.INCOMING, CallMedia.AUDIO))
        )
        assertEquals(
            "\u5bf9\u65b9\u672a\u63a5\u542c",
            callLogText(log("c3", 0, CallResult.MISSED, CallDirection.OUTGOING))
        )
    }

    @Test
    fun `answered text carries media kind and duration`() {
        assertEquals(
            "\u89c6\u9891\u901a\u8bdd 02:31",
            callLogText(log("c1", 0, CallResult.ANSWERED, CallDirection.OUTGOING, CallMedia.VIDEO, 151))
        )
        assertEquals(
            "\u8bed\u97f3\u901a\u8bdd 1:01:05",
            callLogText(log("c2", 0, CallResult.ANSWERED, CallDirection.OUTGOING, CallMedia.AUDIO, 3665))
        )
    }

    @Test
    fun `rejected cancelled and failed text`() {
        assertEquals(
            "\u5df2\u62d2\u7edd",
            callLogText(log("c1", 0, CallResult.REJECTED, CallDirection.INCOMING))
        )
        assertEquals(
            "\u5bf9\u65b9\u5df2\u62d2\u7edd",
            callLogText(log("c2", 0, CallResult.REJECTED, CallDirection.OUTGOING))
        )
        assertEquals(
            "\u5df2\u53d6\u6d88",
            callLogText(log("c3", 0, CallResult.CANCELLED, CallDirection.OUTGOING))
        )
        assertEquals(
            "\u901a\u8bdd\u672a\u63a5\u901a",
            callLogText(log("c4", 0, CallResult.FAILED, CallDirection.OUTGOING))
        )
    }

    private fun keyOf(item: MessageItem): String = when (item) {
        is MessageItem.Call -> "call:${item.log.id}"
        is MessageItem.Folder -> "folder:${item.group.folderId}"
        is MessageItem.Msg -> item.message.id
    }
}
