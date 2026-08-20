package com.zqr.localchat

import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.network.NetworkPacket
import com.zqr.localchat.network.P2PManager
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.StringReader

class NetworkProtocolTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `isFromMe is not transmitted over the wire`() {
        val msg = ChatMessage("1", "hi", 100L, "sender-a", "Alice", isFromMe = true)
        val encoded = json.encodeToString(NetworkPacket(type = "chat", message = msg))

        assertFalse("isFromMe must not be serialized", encoded.contains("isFromMe"))

        val decoded = json.decodeFromString<NetworkPacket>(encoded)
        assertFalse(decoded.message!!.isFromMe)
    }

    @Test
    fun `pending delivery state is not transmitted over the wire`() {
        // a message still waiting in the direct-chat outbox carries
        // pending=true locally; the wire copy must never include it (the
        // receiver decodes it as a plain delivered message)
        val msg = ChatMessage("2", "offline send", 200L, "sender-a", "Alice", isFromMe = true, pending = true)
        val encoded = json.encodeToString(NetworkPacket(type = "chat", message = msg))

        assertFalse("pending must not be serialized", encoded.contains("pending"))

        val decoded = json.decodeFromString<NetworkPacket>(encoded)
        assertFalse(decoded.message!!.pending)
    }

    @Test
    fun `recipient marks messages by sender id`() {
        val incoming = ChatMessage("1", "hi", 100L, "alice", "Alice", isFromMe = false)

        assertTrue(P2PManager.markFromMe(incoming, "alice").isFromMe)
        assertFalse(P2PManager.markFromMe(incoming, "bob").isFromMe)
    }

    @Test
    fun `content length is limited`() {
        assertTrue(P2PManager.isValidContent("x".repeat(P2PManager.MAX_CONTENT_LENGTH)))
        assertFalse(P2PManager.isValidContent("x".repeat(P2PManager.MAX_CONTENT_LENGTH + 1)))
        assertFalse(P2PManager.isValidContent(""))
        assertFalse(P2PManager.isValidContent("   "))
    }

    @Test
    fun `unknown packet fields are tolerated and isFromMe defaults to false`() {
        val wire = """{"type":"chat","futureField":42,"message":{"id":"1","content":"hi","timestamp":1,"senderId":"a","senderName":"A"}}"""
        val packet = json.decodeFromString<NetworkPacket>(wire)

        assertEquals("chat", packet.type)
        assertFalse(packet.message!!.isFromMe)
    }

    @Test
    fun `join packet never carries a password field`() {
        // the password is proven in the handshake (PBKDF2-bound MACs) and
        // must not appear in any packet — legacy peers sending one anyway
        // are tolerated via ignoreUnknownKeys
        val wire = """{"type":"join","groupId":"g","password":"1234","peer":{"id":"1","name":"A","ipAddress":"10.0.0.1","port":9999}}"""
        val packet = json.decodeFromString<NetworkPacket>(wire)

        assertEquals("g", packet.groupId)
        assertEquals("1", packet.peer!!.id)
        val encoded = json.encodeToString(NetworkPacket(type = "join", groupId = "g"))
        assertFalse("password must not be serialized", encoded.contains("password"))
    }

    @Test
    fun `error packet carries error message`() {
        val packet = NetworkPacket(type = "error", errorMessage = "群组密码错误")
        val wire = json.encodeToString(packet)

        assertTrue(wire.contains("\"errorMessage\":\"群组密码错误\""))
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertEquals("群组密码错误", decoded.errorMessage)
    }

    @Test
    fun `delete packet carries sender id for authorization`() {
        val packet = NetworkPacket(type = "delete_message", messageId = "m-1", senderId = "user-a")
        val wire = json.encodeToString(packet)

        assertTrue(wire.contains("\"senderId\":\"user-a\""))
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertEquals("user-a", decoded.senderId)
    }

    @Test
    fun `heartbeat packets are compact and roundtrip`() {
        val ping = NetworkPacket(type = "ping")
        val pingWire = json.encodeToString(ping)
        assertEquals("{\"type\":\"ping\"}", pingWire)
        assertEquals("ping", json.decodeFromString<NetworkPacket>(pingWire).type)

        val pong = NetworkPacket(type = "pong")
        assertEquals("{\"type\":\"pong\"}", json.encodeToString(pong))
    }

    @Test
    fun `file message serializes with fileInfo and omits isFromMe`() {
        val msg = ChatMessage(
            id = "f1",
            content = "报告.pdf",
            timestamp = 100L,
            senderId = "a",
            senderName = "Alice",
            fileInfo = FileInfo("f1", "报告.pdf", 2048L, "192.168.1.5", 42001),
            isFromMe = true
        )
        val encoded = json.encodeToString(NetworkPacket(type = "file_message", message = msg))
        assertFalse("isFromMe must not be serialized", encoded.contains("isFromMe"))
        assertTrue(encoded.contains("\"fileInfo\""))
        assertTrue(encoded.contains("\"downloadPort\":42001"))

        val decoded = json.decodeFromString<NetworkPacket>(encoded)
        assertEquals("f1", decoded.message!!.fileInfo!!.fileId)
        assertEquals(2048L, decoded.message!!.fileInfo!!.fileSize)
        assertFalse(decoded.message!!.isFromMe)
    }

    @Test
    fun `readLineLimited keeps coalesced lines in order`() {
        // TCP does not preserve println() boundaries: two packets can arrive
        // in ONE read. The reader must return them line by line, never
        // discarding what follows the first '\n' in a chunk.
        val payload = "{\"type\":\"chat\",\"message\":{\"id\":\"1\"}}\n" +
            "{\"type\":\"delete_message\",\"messageId\":\"1\",\"senderId\":\"a\"}\n"
        val reader = BufferedReader(StringReader(payload))

        val first = P2PManager.readLineLimited(reader)
        val second = P2PManager.readLineLimited(reader)

        assertEquals("{\"type\":\"chat\",\"message\":{\"id\":\"1\"}}", first)
        assertEquals(
            "{\"type\":\"delete_message\",\"messageId\":\"1\",\"senderId\":\"a\"}",
            second
        )
    }

    @Test
    fun `readLineLimited strips CRLF and returns null at EOF`() {
        val reader = BufferedReader(StringReader("hello\r\nworld"))
        assertEquals("hello", P2PManager.readLineLimited(reader))
        assertEquals("world", P2PManager.readLineLimited(reader))
        assertNull(P2PManager.readLineLimited(reader))
    }

    @Test
    fun `readLineLimited is bounded and rejects overlong lines`() {
        val tooLong = "x".repeat(P2PManager.MAX_LINE_LENGTH + 1)
        val reader = BufferedReader(StringReader(tooLong + "\n"))
        assertNull(P2PManager.readLineLimited(reader))
    }

    @Test
    fun `hasInvalidPort flags out-of-range and overflow ports`() {
        assertFalse(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5"))
        assertFalse(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5:9999"))
        assertFalse(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5:1"))
        assertFalse(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5:65535"))
        assertTrue(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5:0"))
        assertTrue(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5:65536"))
        assertTrue(com.zqr.localchat.ui.screen.hasInvalidPort("192.168.1.5:99999999999"))
        assertFalse(com.zqr.localchat.ui.screen.hasInvalidPort("hostname"))
        assertFalse(com.zqr.localchat.ui.screen.hasInvalidPort("10.0.2.2:abc"))
    }

    @Test
    fun `parseHostPort defaults the port and keeps a custom one`() {
        val parsed = com.zqr.localchat.ui.screen.parseHostPort("192.168.1.5")
        assertEquals("192.168.1.5", parsed.host)
        assertEquals(9999, parsed.port)

        val withPort = com.zqr.localchat.ui.screen.parseHostPort("192.168.1.5:4242")
        assertEquals("192.168.1.5", withPort.host)
        assertEquals(4242, withPort.port)
    }
}
