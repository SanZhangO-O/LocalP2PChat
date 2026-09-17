package com.zqr.localchat

import com.zqr.localchat.network.DirectChatManager
import com.zqr.localchat.network.NetworkPacket
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression for the queued-send callbacks: a packet whose session dies at
 * enqueue time is failed immediately, while the sender thread's drain can race
 * for the SAME item — the delivery result must be reported exactly once, or
 * handlers like the file-offer rollback run twice (duplicate bubbles/toasts).
 */
class DirectOutboundTest {

    private fun item(calls: MutableList<String>) = DirectChatManager.Outbound(
        NetworkPacket(type = "chat"),
        onSent = { calls += "sent" },
        onFailed = { calls += "failed" }
    )

    @Test
    fun `failure wins when it settles first`() {
        val calls = mutableListOf<String>()
        val it = item(calls)

        it.settleFailed()
        it.settleSent()
        it.settleFailed()

        assertEquals(listOf("failed"), calls)
    }

    @Test
    fun `success wins when it settles first`() {
        val calls = mutableListOf<String>()
        val it = item(calls)

        it.settleSent()
        it.settleFailed()
        it.settleSent()

        assertEquals(listOf("sent"), calls)
    }

    @Test
    fun `a throwing callback is swallowed and still settles`() {
        val calls = mutableListOf<String>()
        val it = DirectChatManager.Outbound(
            NetworkPacket(type = "chat"),
            onSent = { throw IllegalStateException("boom") },
            onFailed = { calls += "failed" }
        )

        it.settleSent() // must not escape
        it.settleFailed() // already settled by the (throwing) success

        assertTrue("the throwing success must still consume the settle", calls.isEmpty())
    }

    @Test
    fun `no callbacks at all is harmless`() {
        val it = DirectChatManager.Outbound(NetworkPacket(type = "ping"))
        it.settleSent()
        it.settleFailed()
    }
}
