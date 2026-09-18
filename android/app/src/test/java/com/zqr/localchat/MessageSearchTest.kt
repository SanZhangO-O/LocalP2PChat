package com.zqr.localchat

import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MessageSearch
import com.zqr.localchat.data.SavedChatMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure search helpers for the global message search: LIKE escaping, the
 * decrypted-body matcher (the stored column is ciphertext), the result
 * preview and the name-pass/body-pass merge.
 */
class MessageSearchTest {

    private fun text(
        id: String,
        groupId: String,
        content: String,
        timestamp: Long,
        sender: String = "u"
    ) = SavedChatMessage(
        id = id,
        groupId = groupId,
        content = content,
        timestamp = timestamp,
        senderId = sender,
        senderName = sender,
        isFromMe = false
    )

    @Test
    fun `like wildcards in the keyword are escaped`() {
        assertEquals("a\\%b\\_c\\\\d", MessageSearch.escapeLike("a%b_c\\d"))
        assertEquals("%a\\%b%", MessageSearch.likePattern("a%b"))
        assertEquals("%plain%", MessageSearch.likePattern("plain"))
    }

    @Test
    fun `matches body sender and file path`() {
        val row = text("m1", "g1", "hello world", 1, sender = "张三")
        assertTrue(MessageSearch.matches(row, "hello world", "world"))
        assertTrue(MessageSearch.matches(row, "hello world", "张三"))
        assertFalse(MessageSearch.matches(row, "hello world", "nope"))
        assertFalse(MessageSearch.matches(row, "hello world", ""))

        val fileRow = SavedChatMessage(
            id = "f1",
            groupId = "g1",
            content = "report_final.pdf",
            timestamp = 2,
            senderId = "u",
            senderName = "u",
            isFromMe = false,
            fileSize = 10L,
            relativePath = "docs/report_final.pdf",
            folderName = "bundle"
        )
        assertTrue(MessageSearch.matches(fileRow, "report_final.pdf", "docs/"))
        assertTrue(MessageSearch.matches(fileRow, "report_final.pdf", "bundle"))
        assertTrue(MessageSearch.matches(fileRow, "report_final.pdf", "final"))
    }

    @Test
    fun `preview prefixes file kinds and truncates`() {
        val plain = text("m1", "g1", "你好", 1)
        assertEquals("你好", MessageSearch.preview(plain, "你好"))

        val image = SavedChatMessage(
            id = "i1",
            groupId = "g1",
            content = "pic.png",
            timestamp = 2,
            senderId = "u",
            senderName = "u",
            isFromMe = false,
            fileSize = 5L,
            kind = FileKind.IMAGE,
            relativePath = "pic.png"
        )
        assertEquals("[图片] pic.png", MessageSearch.preview(image, "pic.png"))

        val long = text("m3", "g1", "x".repeat(200), 3)
        val preview = MessageSearch.preview(long, long.content)
        assertTrue(preview.endsWith("..."))
        assertEquals(MessageSearch.SNIPPET_MAX + 3, preview.length)
    }

    @Test
    fun `merge dedupes by conversation and caps`() {
        val a = text("1", "g1", "a", 10)
        val b = text("1", "g2", "a", 20) // same id, other conversation
        val c = text("2", "g1", "a", 30)

        val merged = MessageSearch.merge(listOf(a, b), listOf(b, c))
        assertEquals(3, merged.size)
        assertEquals(listOf("2", "1", "1"), merged.map { it.id })
        assertEquals(listOf(30L, 20L, 10L), merged.map { it.timestamp })
        assertEquals(2, MessageSearch.merge(listOf(a, b, c), emptyList(), max = 2).size)
    }
}
