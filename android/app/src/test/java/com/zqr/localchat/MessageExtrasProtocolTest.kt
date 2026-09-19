package com.zqr.localchat

import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MENTION_ALL
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.detectMediaKind
import com.zqr.localchat.data.isBigEmoji
import com.zqr.localchat.data.normalizeMediaKind
import com.zqr.localchat.data.resolveMentionIds
import com.zqr.localchat.data.sanitizeEmoji
import com.zqr.localchat.data.sanitizeMentions
import com.zqr.localchat.data.takeCodePoints
import com.zqr.localchat.data.withSanitizedExtras
import com.zqr.localchat.network.NetworkPacket
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Cross-platform contract for the message-experience packets (edit_message /
 * reaction / pin_message) and the extended ChatMessage (mentions / edited).
 * The expected JSON strings are exactly what the Windows peer (Python
 * json.dumps with compact separators) emits for the same values, so both ends
 * stay byte-compatible; the Windows-emitted literals must always parse.
 */
class MessageExtrasProtocolTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `edit message wire format matches the Windows peer`() {
        val packet = NetworkPacket(
            type = "edit_message",
            groupId = "g1",
            messageId = "m-1",
            senderId = "dev-A",
            newContent = "new body"
        )
        val wire = json.encodeToString(packet)
        assertEquals(
            "{\"type\":\"edit_message\",\"groupId\":\"g1\",\"messageId\":\"m-1\"," +
                "\"senderId\":\"dev-A\",\"newContent\":\"new body\"}",
            wire
        )
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertEquals("m-1", decoded.messageId)
        assertEquals("new body", decoded.newContent)
    }

    @Test
    fun `reaction wire format matches the Windows peer`() {
        val packet = NetworkPacket(
            type = "reaction",
            groupId = "g1",
            messageId = "m-1",
            senderId = "dev-B",
            emoji = "\uD83D\uDC4D",
            active = true
        )
        val wire = json.encodeToString(packet)
        assertEquals(
            "{\"type\":\"reaction\",\"groupId\":\"g1\",\"messageId\":\"m-1\"," +
                "\"senderId\":\"dev-B\",\"active\":true,\"emoji\":\"\uD83D\uDC4D\"}",
            wire
        )
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertTrue(decoded.active == true)
        assertEquals("\uD83D\uDC4D", decoded.emoji)
    }

    @Test
    fun `pin message wire format matches the Windows peer`() {
        val packet = NetworkPacket(
            type = "pin_message",
            groupId = "g1",
            messageId = "m-2",
            senderId = "dev-A",
            active = false
        )
        val wire = json.encodeToString(packet)
        assertEquals(
            "{\"type\":\"pin_message\",\"groupId\":\"g1\",\"messageId\":\"m-2\"," +
                "\"senderId\":\"dev-A\",\"active\":false}",
            wire
        )
    }

    @Test
    fun `packets without the new fields still parse`() {
        // an old peer (or a future field) must never break the decode: the
        // new fields are nullable defaults, omitted when unset
        val decoded = json.decodeFromString<NetworkPacket>(
            """{"type":"delete_message","messageId":"m","senderId":"a"}"""
        )
        assertNull(decoded.newContent)
        assertNull(decoded.emoji)
    }

    @Test
    fun `windows emitted extras packets parse`() {
        // literal bytes the Python side emits (compact separators,
        // ensure_ascii=False): the Chinese edit content must survive
        val edit = json.decodeFromString<NetworkPacket>(
            "{\"type\":\"edit_message\",\"groupId\":\"群@g1\",\"messageId\":\"m-1\"," +
                "\"senderId\":\"dev-A\",\"newContent\":\"修改后的内容\"}"
        )
        assertEquals("修改后的内容", edit.newContent)

        val react = json.decodeFromString<NetworkPacket>(
            "{\"type\":\"reaction\",\"groupId\":\"群@g1\",\"messageId\":\"m-1\"," +
                "\"senderId\":\"dev-B\",\"emoji\":\"🎉\",\"active\":true}"
        )
        assertEquals("🎉", react.emoji)

        val pin = json.decodeFromString<NetworkPacket>(
            "{\"type\":\"pin_message\",\"groupId\":\"群@g1\",\"messageId\":\"m-1\"," +
                "\"senderId\":\"dev-C\",\"active\":true}"
        )
        assertEquals(true, pin.active)

        val receipt = json.decodeFromString<NetworkPacket>(
            "{\"type\":\"read_receipt\",\"groupId\":\"群@g1\",\"upToId\":\"m-9\"," +
                "\"readerId\":\"dev-B\"}"
        )
        assertEquals("m-9", receipt.upToId)
        assertEquals("dev-B", receipt.readerId)
    }

    @Test
    fun `chat message mentions and edited round-trip`() {
        val msg = ChatMessage(
            id = "m-3",
            content = "hey",
            timestamp = 5L,
            senderId = "dev-A",
            senderName = "A",
            mentions = listOf("dev-B", MENTION_ALL),
            edited = true
        )
        val wire = json.encodeToString(msg)
        assertTrue(wire.contains("\"mentions\":[\"dev-B\",\"all\"]"))
        assertTrue(wire.contains("\"edited\":true"))
        val decoded = json.decodeFromString<ChatMessage>(wire)
        assertEquals(listOf("dev-B", MENTION_ALL), decoded.mentions)
        assertTrue(decoded.edited)
    }

    @Test
    fun `plain chat message stays byte-identical`() {
        // a message without the new fields must serialize exactly like the
        // pre-extras format (backward compatibility is a hard constraint)
        val msg = ChatMessage(
            id = "m-1",
            content = "hi",
            timestamp = 123L,
            senderId = "dev-A",
            senderName = "A"
        )
        val wire = json.encodeToString(msg)
        assertEquals(
            "{\"id\":\"m-1\",\"content\":\"hi\",\"timestamp\":123," +
                "\"senderId\":\"dev-A\",\"senderName\":\"A\"}",
            wire
        )
    }

    @Test
    fun `windows emitted mentioned message parses`() {
        val decoded = json.decodeFromString<ChatMessage>(
            "{\"id\":\"m-7\",\"content\":\"@所有人 开会\",\"timestamp\":9," +
                "\"senderId\":\"dev-A\",\"senderName\":\"A\",\"mentions\":[\"all\"]," +
                "\"edited\":true,\"unknownFutureField\":1}"
        )
        assertEquals(listOf(MENTION_ALL), decoded.mentions)
        assertTrue(decoded.edited)
    }

    @Test
    fun `mention and emoji sanitizers mirror the Windows client`() {
        assertEquals("\uD83D\uDE00", sanitizeEmoji(" \uD83D\uDE00\n"))
        assertEquals("abcd", sanitizeEmoji("ab\u0007cd"))
        assertEquals(16, sanitizeEmoji("x".repeat(99)).length)
        assertEquals("", sanitizeEmoji("   "))
        assertEquals(listOf("a", "b"), sanitizeMentions(listOf("a", "a", "", "b")))
        assertEquals(null, sanitizeMentions(listOf()))
        assertEquals(64, sanitizeMentions((0 until 999).map { it.toString() })?.size)
    }

    @Test
    fun `big emoji detection mirrors the Windows client`() {
        assertTrue(isBigEmoji("\uD83D\uDE00"))
        assertTrue(isBigEmoji("\uD83D\uDE00\uD83D\uDE01"))
        assertFalse(isBigEmoji("hello"))
        assertFalse(isBigEmoji("\uD83D\uDE00 text"))
        assertFalse(isBigEmoji("a".repeat(20)))
    }

    @Test
    fun `emoji sanitizer matches the Windows client on tricky sequences`() {
        // ZWJ is a FORMAT character: Python isprintable() is False, so both
        // platforms drop it (the family emoji degrades to its members)
        assertEquals(
            "\uD83D\uDC68\uD83D\uDC69\uD83D\uDC67",
            sanitizeEmoji("\uD83D\uDC68\u200D\uD83D\uDC69\u200D\uD83D\uDC67")
        )
        // keycap + copyright: all parts are printable marks/symbols on both
        assertEquals("#\uFE0F\u20E3", sanitizeEmoji("#\uFE0F\u20E3"))
        assertEquals("\u00A9\uFE0F", sanitizeEmoji("\u00A9\uFE0F"))
        // the 16-unit cap counts CODE POINTS (Windows parity): 20 astral
        // emoji -> 16, and a surrogate pair is never split
        val capped = sanitizeEmoji("\uD83D\uDE00".repeat(20))
        assertEquals(16, capped.codePointCount(0, capped.length))
        assertEquals(32, capped.length)
        // whitespace-only input leaves nothing usable
        assertEquals("", sanitizeEmoji(" \t\n"))
    }

    @Test
    fun `sanitized extras cap and dedupe inbound mentions`() {
        val wild = ChatMessage(
            id = "m",
            content = "x",
            timestamp = 1L,
            senderId = "a",
            senderName = "a",
            mentions = (0 until 999).map { it.toString() }
        )
        assertEquals(64, wild.withSanitizedExtras().mentions?.size)
        val dupes = wild.copy(mentions = listOf("a", "a", "", "b"))
        assertEquals(listOf("a", "b"), dupes.withSanitizedExtras().mentions)
    }

    @Test
    fun `lone surrogates are dropped like Python isprintable`() {
        // Python str.isprintable() is False for category Cs, so Windows drops
        // a lone surrogate; Android must too, or a crafted "\ud800" reaction
        // would be stored and re-broadcast with a different value
        assertEquals("", sanitizeEmoji("\uD800"))
        assertEquals("\uD83D\uDE00", sanitizeEmoji("\uD800\uD83D\uDE00"))
    }

    @Test
    fun `code point truncation never splits a surrogate pair`() {
        assertEquals("a\uD83D\uDE00", takeCodePoints("a\uD83D\uDE00b", 2))
        assertEquals("\uD83D\uDE00", takeCodePoints("\uD83D\uDE00", 1))
        assertEquals("", takeCodePoints("abc", 0))
        assertEquals("abc", takeCodePoints("abc", 9))
        // mentions cap by CODE POINTS like Windows item[:128]
        val mentions = sanitizeMentions(listOf("\uD83D\uDE00".repeat(200)))
        val only = mentions!![0]
        assertEquals(128, only.codePointCount(0, only.length))
    }

    @Test
    fun `mention resolution requires token boundaries`() {
        val members = listOf("dev-anna" to "Anna", "dev-ann" to "Ann")
        assertEquals(listOf("dev-anna"), resolveMentionIds("@Anna hi", members))
        // "@Ann" matches ONLY Ann (not Anna's prefix, which the old substring
        // rule also matched)
        assertEquals(listOf("dev-ann"), resolveMentionIds("@Ann hi", members))
        // mid-word and address-like occurrences never match
        assertEquals(null, resolveMentionIds("see user@Ann", members))
        assertEquals(null, resolveMentionIds("x@Anna", members))
        assertEquals(listOf(MENTION_ALL), resolveMentionIds("hi @所有人", emptyList()))
        assertEquals(
            listOf(MENTION_ALL, "dev-ann"),
            resolveMentionIds("@所有人 @Ann", members)
        )
        assertEquals(null, resolveMentionIds("no at sign", members))
    }

    @Test
    fun `audio files classify as audio and unknown kinds degrade`() {
        assertEquals(FileKind.AUDIO, detectMediaKind("voice.WAV"))
        assertEquals(FileKind.AUDIO, detectMediaKind("song.mp3"))
        assertEquals(FileKind.AUDIO, detectMediaKind("clip.m4a"))
        assertEquals(FileKind.FILE, detectMediaKind("paper.txt"))
        assertEquals(FileKind.AUDIO, normalizeMediaKind(FileKind.AUDIO))
        assertEquals(FileKind.FILE, normalizeMediaKind("hologram"))
    }
}
