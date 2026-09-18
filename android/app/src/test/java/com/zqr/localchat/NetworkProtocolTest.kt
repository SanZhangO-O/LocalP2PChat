package com.zqr.localchat

import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.DeletedMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.Peer
import com.zqr.localchat.network.FileTransfer
import com.zqr.localchat.network.GroupMeshManager
import com.zqr.localchat.network.LineIn
import com.zqr.localchat.network.NetworkPacket
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.network.Wire
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.PrintWriter
import java.io.StringReader
import java.io.StringWriter
import java.net.Socket

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

    @Test
    fun `deletedIds omitted when null`() {
        // byte contract with the Windows peer (encodeDefaults=false): a
        // packet without tombstones must not carry the field at all
        val encoded = json.encodeToString(NetworkPacket(type = "join_ack", groupId = "g"))

        assertFalse("null deletedIds must not be serialized", encoded.contains("deletedIds"))
        assertNull(json.decodeFromString<NetworkPacket>(encoded).deletedIds)
    }

    @Test
    fun `deletedIds serializes and round-trips`() {
        val packet = NetworkPacket(type = "join_ack", groupId = "g", deletedIds = listOf("m-1", "m-2"))
        val encoded = json.encodeToString(packet)

        assertTrue(encoded.contains("\"deletedIds\":[\"m-1\",\"m-2\"]"))
        assertEquals(listOf("m-1", "m-2"), json.decodeFromString<NetworkPacket>(encoded).deletedIds)
    }

    @Test
    fun `history_reply tombstone packet parses without messages`() {
        // the tombstone sync rides a dedicated history_reply: no messages,
        // only the ids deleted while the returning member was offline
        val wire = """{"type":"history_reply","groupId":"g","deletedIds":["a","b"]}"""
        val decoded = json.decodeFromString<NetworkPacket>(wire)

        assertEquals("history_reply", decoded.type)
        assertEquals("g", decoded.groupId)
        assertNull(decoded.messages)
        assertEquals(listOf("a", "b"), decoded.deletedIds)
    }

    @Test
    fun `packets without deletedIds are tolerated (old peers)`() {
        // backward compatibility: an old peer never sends the field, and our
        // decoder must accept that (null default) — same for legacy senders
        // that receive it and ignore the unknown field via ignoreUnknownKeys
        val decoded = json.decodeFromString<NetworkPacket>("""{"type":"mesh_chat","groupId":"g"}""")

        assertNull(decoded.deletedIds)
    }

    @Test
    fun `mesh hello naming an unjoined group is rejected before any state is touched`() {
        // a handshake bound to group A must not be able to reach group B's
        // mesh state: a hello whose groupId names a group we are not in is
        // dropped and its socket closed (listener-side binding guard parity)
        val socket = Socket()
        val wire = Wire(LineIn { null }, PrintWriter(StringWriter(), true))
        try {
            GroupMeshManager.enterGroup(
                "g1",
                Peer("me", "Me", "192.168.1.2", 9999),
                emptyList(),
                emptyList(),
                "pw"
            )
            GroupMeshManager.handleMeshHello(
                socket,
                wire,
                NetworkPacket(
                    type = "mesh_hello",
                    groupId = "other-group",
                    peer = Peer("intruder", "A", "192.168.1.9", 9999)
                )
            )
            assertTrue("mismatched-group socket must be closed", socket.isClosed)
            assertFalse(GroupMeshManager.hasLinks("g1"))
        } finally {
            GroupMeshManager.leaveGroup("g1")
            runCatching { socket.close() }
        }
    }

    @Test
    fun `sanitizeFileName strips separators and dotdot`() {
        assertEquals("a_b.txt", P2PManager.sanitizeFileName("a/b.txt"))
        assertEquals("a_b.txt", P2PManager.sanitizeFileName("a\\b.txt"))
        assertEquals("a_b", P2PManager.sanitizeFileName("a..b"))
        assertEquals("__x", P2PManager.sanitizeFileName("../x"))
        assertEquals("__x", P2PManager.sanitizeFileName("..\\x"))
        assertEquals("报告.pdf", P2PManager.sanitizeFileName("报告.pdf"))
    }

    @Test
    fun `numericGroupIdOf fixed vector 测试😀群`() {
        // Cross-platform contract with the Windows client (Windows parity):
        // FNV-1a over the UTF-16 CODE UNITS of "groupName + \\u0000 +
        // fingerprint" (so 😀 contributes its surrogate PAIR, not a code
        // point), Long multiply wrapping mod 2^32, final value mod 1e8
        // zero-padded to 8 digits. Both sides must produce the identical id
        // for the same (name, fingerprint).
        assertEquals(
            "54153344",
            P2PManager.numericGroupIdOf("测试😀群", "0123456789abcdef")
        )
    }

    @Test
    fun `numericGroupIdOf is sensitive to name and fingerprint`() {
        val base = P2PManager.numericGroupIdOf("测试😀群", "0123456789abcdef")
        assertTrue(
            P2PManager.numericGroupIdOf("测试😀群2", "0123456789abcdef") != base
        )
        assertTrue(
            P2PManager.numericGroupIdOf("测试😀群", "fedcba9876543210") != base
        )
    }

    @Test
    fun `file_download offset serializes and is validated by the server`() {
        // the downloader always attaches the download token AND the resume
        // offset; both must round-trip
        val packet = NetworkPacket(
            type = "file_download", fileId = "f-1", token = "abc+/=", offset = 4096L
        )
        val encoded = json.encodeToString(packet)
        assertTrue(encoded.contains("\"token\":\"abc+/=\""))
        assertTrue(encoded.contains("\"offset\":4096"))
        val decoded = json.decodeFromString<NetworkPacket>(encoded)
        assertEquals("f-1", decoded.fileId)
        assertEquals("abc+/=", decoded.token)
        assertEquals(4096L, decoded.offset)

        // a request without the offset decodes (generic packet model) but has
        // no resume point: FileTransfer.serveFile refuses it before any bytes
        val missing = json.decodeFromString<NetworkPacket>(
            """{"type":"file_download","fileId":"f-1"}"""
        )
        assertNull(missing.offset)

        // the field is only emitted when set (a plain packet stays identical)
        assertFalse(json.encodeToString(NetworkPacket(type = "ping")).contains("offset"))
    }

    @Test
    fun `downloadToken fixed vector and determinism`() {
        // Windows contract: token = Base64(HMAC-SHA256(key=fileKey,
        // msg=ASCII("lc-file-dl-v1:" + fileId))). Independent vector computed
        // with .NET HMACSHA256: key = 0x01..0x20 (32 bytes).
        val key = ByteArray(32) { (it + 1).toByte() }
        assertEquals(
            "IHKQaivs7b+e+A5fQZ+f7o+6IPWES+VsYeiz4KuDU5Y=",
            FileTransfer.downloadToken("vector-file", key)
        )
        // deterministic: same key + id -> same token; different id -> differs
        assertEquals(
            FileTransfer.downloadToken("vector-file", key),
            FileTransfer.downloadToken("vector-file", key)
        )
        assertTrue(
            FileTransfer.downloadToken("other-file", key) !=
                FileTransfer.downloadToken("vector-file", key)
        )
    }

    @Test
    fun `sanitizeDeletedIds dedupes caps and drops junk`() {
        // the table keeps at most CAP ids per group, so accepting more from a
        // single wire packet only buys the sender unbounded work here
        assertEquals(DeletedMessage.CAP, P2PManager.MAX_DELETED_IDS)
        assertEquals(listOf("a", "b"), P2PManager.sanitizeDeletedIds(listOf("a", "", "b", "a")))
        assertEquals(emptyList<String>(), P2PManager.sanitizeDeletedIds(null))
        assertEquals(emptyList<String>(), P2PManager.sanitizeDeletedIds(listOf("  ")))
        val tooLong = "x".repeat(P2PManager.MAX_DELETED_ID_LEN + 1)
        assertEquals(emptyList<String>(), P2PManager.sanitizeDeletedIds(listOf(tooLong)))
        val many = (1..P2PManager.MAX_DELETED_IDS + 25).map { "m-$it" }
        val capped = P2PManager.sanitizeDeletedIds(many)
        assertEquals(P2PManager.MAX_DELETED_IDS, capped.size)
        assertEquals("m-1", capped.first())
        assertEquals("m-${P2PManager.MAX_DELETED_IDS}", capped.last())
    }
}
