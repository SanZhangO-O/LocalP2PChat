package com.zqr.localchat

import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.Peer
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.network.BufferedReaderLineIn
import com.zqr.localchat.network.DeviceIdentity
import com.zqr.localchat.network.Handshake
import com.zqr.localchat.network.NetworkPacket
import com.zqr.localchat.network.Protocol
import com.zqr.localchat.network.Wire
import com.zqr.localchat.network.WireException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * End-to-end handshake tests over real localhost TCP sockets: the same code
 * path the app uses on the wire.
 */
class SecureWireTest {

    private fun newWire(socket: Socket): Wire = Wire(
        BufferedReaderLineIn(BufferedReader(InputStreamReader(socket.getInputStream()))),
        PrintWriter(socket.getOutputStream(), true)
    )

    /** Runs a full password-mode exchange and hands both wires to [body]
     *  (executed on the main thread; the server thread parks after its
     *  handshake finishes so it never touches the socket again). */
    private fun withPasswordHandshake(
        clientPassword: String,
        serverPassword: String,
        body: (client: Wire, server: Wire) -> Unit
    ) {
        val server = ServerSocket(0)
        val handshakeDone = CountDownLatch(1)
        val park = CountDownLatch(1)
        val serverWireRef = AtomicReference<Wire?>()
        val failure = AtomicReference<Throwable?>()
        val serverThread = Thread {
            try {
                val socket = server.accept()
                val wire = newWire(socket)
                val start = wire.recvRaw()!!
                val secured = Handshake.accept(wire, start) { _, _ -> serverPassword }
                if (secured == null) throw IllegalStateException("server rejected handshake")
                serverWireRef.set(wire)
            } catch (t: Throwable) {
                failure.set(t)
            } finally {
                handshakeDone.countDown()
            }
            park.await(60, TimeUnit.SECONDS)
        }
        serverThread.isDaemon = true
        serverThread.start()
        val clientSocket = Socket("127.0.0.1", server.localPort)
        val clientWire = newWire(clientSocket)
        try {
            Handshake.initiate(clientWire, Protocol.MODE_JOIN, "12345678", clientPassword)
            assertTrue("server handshake must finish", handshakeDone.await(30, TimeUnit.SECONDS))
            failure.get()?.let { throw it }
            val serverWire = serverWireRef.get()
            assertNotNull(serverWire)
            body(clientWire, serverWire!!)
        } finally {
            park.countDown()
            server.close()
            clientSocket.close()
        }
    }

    @Test
    fun `password handshake derives the same key and carries encrypted packets`() {
        withPasswordHandshake("correct-password", "correct-password") { client, server ->
            assertTrue(client.isSecure)
            val msg = ChatMessage("1", "hello", 1L, "a", "Alice")
            client.sendPacket(NetworkPacket(type = "chat", message = msg))
            val received = runBlocking { server.recvPacket() }
            assertNotNull(received)
            assertEquals("chat", received!!.type)
            assertEquals("hello", received.message!!.content)

            server.sendPacket(NetworkPacket(type = "join_ack", groupId = "g", members = listOf(Peer("a", "A", "10.0.0.1", 9999))))
            val reply = runBlocking { client.recvPacket() }
            assertEquals("join_ack", reply!!.type)
        }
    }

    @Test
    fun `wrong password is rejected with a clear message`() {
        val server = ServerSocket(0)
        val serverThread = Thread {
            try {
                val socket = server.accept()
                val wire = newWire(socket)
                val start = wire.recvRaw()!!
                Handshake.accept(wire, start) { _, _ -> "real-password" }
            } catch (_: Exception) {
            }
        }
        serverThread.start()
        val clientSocket = Socket("127.0.0.1", server.localPort)
        val client = newWire(clientSocket)
        val ex = assertThrows(WireException::class.java) {
            Handshake.initiate(client, Protocol.MODE_JOIN, "12345678", "wrong-password")
        }
        assertEquals("群组密码错误", ex.message)
        serverThread.join(10_000)
        server.close()
        clientSocket.close()
    }

    @Test
    fun `handshake lines never contain the password`() {
        val server = ServerSocket(0)
        val captured = ArrayList<String>()
        val serverThread = Thread {
            try {
                val socket = server.accept()
                val reader = BufferedReader(InputStreamReader(socket.getInputStream()))
                val writer = PrintWriter(socket.getOutputStream(), true)
                // hs_start
                synchronized(captured) { captured.add(com.zqr.localchat.network.P2PManager.readLineLimited(reader)!!) }
                // answer hs_ack with a real ephemeral key so the client
                // proceeds to send hs_confirm
                val dummyEph = Crypto.encodePub(Crypto.generateEcKeyPair().public)
                writer.println("""{"type":"hs_ack","eph":"$dummyEph"}""")
                // hs_confirm
                synchronized(captured) { captured.add(com.zqr.localchat.network.P2PManager.readLineLimited(reader)!!) }
                socket.close()
            } catch (_: Exception) {
            }
        }
        serverThread.isDaemon = true
        serverThread.start()
        val clientSocket = Socket("127.0.0.1", server.localPort)
        // the bogus ack key makes initiate() fail after hs_confirm was sent —
        // exactly the two lines a sniffer would record
        runCatching {
            val client = newWire(clientSocket)
            Handshake.initiate(client, Protocol.MODE_JOIN, "12345678", "s3cret-pw")
        }
        serverThread.join(10_000)
        server.close()
        clientSocket.close()
        val startLine = synchronized(captured) { captured.getOrNull(0) }
        val confirmLine = synchronized(captured) { captured.getOrNull(1) }
        assertTrue("hs_start must be captured", startLine != null)
        assertTrue("hs_confirm must be captured", confirmLine != null)
        // the sniffer-visible lines contain only base64 keys/macs — never the
        // password, so the exchange cannot be decrypted or verified offline
        // without brute-forcing PBKDF2
        assertFalse(startLine!!.contains("s3cret-pw"))
        assertFalse(confirmLine!!.contains("s3cret-pw"))
    }

    @Test
    fun `direct identity handshake authenticates both endpoints`() {
        val sharedIdentity = Crypto.generateEcKeyPair()
        DeviceIdentity.install(sharedIdentity)

        val server = ServerSocket(0)
        val serverResult = AtomicReference<Wire?>()
        val handshakeDone = CountDownLatch(1)
        val serverThread = Thread {
            try {
                val socket = server.accept()
                val wire = newWire(socket)
                val start = wire.recvRaw()!!
                val secured = Handshake.acceptDirect(wire, start, null)
                if (secured != null) serverResult.set(wire)
            } catch (_: Exception) {
            } finally {
                handshakeDone.countDown()
            }
        }
        serverThread.isDaemon = true
        serverThread.start()

        val clientSocket = Socket("127.0.0.1", server.localPort)
        val client = newWire(clientSocket)
        val secured = Handshake.initiateDirect(client, null)
        assertNotNull(secured)
        assertTrue(handshakeDone.await(30, TimeUnit.SECONDS))

        client.sendPacket(NetworkPacket(type = Protocol.DIRECT_HELLO, peer = Peer("b", "Bob", "10.0.0.2", 9999)))
        val serverWire = serverResult.get()
        assertTrue("server side must complete too", serverWire != null)
        val hello = runBlocking { serverWire!!.recvPacket() }
        assertEquals(Protocol.DIRECT_HELLO, hello!!.type)
        serverWire!!.sendPacket(NetworkPacket(type = Protocol.DIRECT_ACK, peer = Peer("a", "Alice", "10.0.0.1", 9999)))
        val ack = runBlocking { client.recvPacket() }
        assertEquals(Protocol.DIRECT_ACK, ack!!.type)
        server.close()
        clientSocket.close()
    }

    @Test
    fun `encrypted line is not plaintext json`() {
        // the wire encodes packet lines exactly this way: the JSON payload is
        // AES-GCM encrypted and Base64'd, so none of it is recognizable
        val key = Crypto.randomBytes(32)
        val json = """{"type":"chat","message":{"content":"秘密消息"}}"""
        val line = Crypto.toB64(Crypto.aesGcmEncrypt(key, json.toByteArray(Charsets.UTF_8)))
        assertFalse(line.contains("秘密消息"))
        assertFalse(line.contains("\"type\""))
    }
}
