package com.zqr.localchat

import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.Peer
import com.zqr.localchat.network.DeviceIdentity
import com.zqr.localchat.network.DirectChatManager
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Accepting a contact request must DIAL the accepted peer immediately
 * (convergence fix): previously the session waited for the peer's announce
 * or our 60s presence sweep — and never came up from our side at all when
 * the deterministic dialer rule picked the peer. The test runs a fake peer
 * listener and asserts an incoming TCP connection arrives right after
 * acceptContactRequest (the handshake itself fails against the dumb
 * listener, which is fine — the connection attempt is the point).
 */
class AcceptDialTest {

    @Before
    fun setUp() {
        DirectChatManager.resetForTest()
        // the dial performs the identity handshake; install an identity so
        // the failure (if any) happens after the TCP connect we assert on
        DeviceIdentity.install(Crypto.generateEcKeyPair())
    }

    @After
    fun tearDown() {
        DirectChatManager.resetForTest()
    }

    @Test
    fun `accepting a request dials the peer immediately`() {
        // fake peer: accepts TCP but speaks no protocol
        val fakePeer = ServerSocket(0)
        val connected = CountDownLatch(1)
        val acceptor = Thread {
            try {
                fakePeer.accept().close()
                connected.countDown()
            } catch (_: Exception) {
            }
        }
        acceptor.isDaemon = true
        acceptor.start()

        // high local id: the deterministic presence sweep would NEVER dial
        // the peer from our side — only the immediate accept dial can
        DirectChatManager.configure(
            myId = "zzzz-local-device",
            myName = "Me",
            myIp = "127.0.0.1",
            myPort = 1,
            savedContacts = emptyList()
        )
        DirectChatManager.recordContactRequest(
            Peer("aaaa-peer", "Peer", "127.0.0.1", fakePeer.localPort),
            fromRemoved = false
        )
        assertEquals(1, DirectChatManager.contactRequests.value.size)

        DirectChatManager.acceptContactRequest("aaaa-peer")

        assertTrue(
            "the accepted peer must be dialed immediately",
            connected.await(8, TimeUnit.SECONDS)
        )
        assertTrue(DirectChatManager.contactRequests.value.isEmpty())
        assertNotNull(DirectChatManager.contacts.value["aaaa-peer"])
        fakePeer.close()
    }
}
