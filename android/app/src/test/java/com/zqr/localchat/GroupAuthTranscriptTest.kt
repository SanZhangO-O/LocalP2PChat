package com.zqr.localchat

import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.network.GroupAuth
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Group signing transcript byte-parity with the Windows side
 * (localchat/groupauth.py). The expected digests were produced by the Python
 * implementation; a mismatch here would silently break every group signature
 * across platforms, so this test is the cheapest early warning.
 */
class GroupAuthTranscriptTest {

    @Test
    fun `message transcript digest matches the Windows codec`() {
        val msg = ChatMessage(
            id = "m-1",
            content = "hello",
            timestamp = 1600000000000L,
            senderId = "alice",
            senderName = "Alice"
        )
        val parts = GroupAuth.messageParts("12345678", msg)
        assertEquals(
            listOf(
                "msg", "12345678", "alice", "m-1", "1600000000000",
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            ),
            parts
        )
        assertEquals(
            "54b567bee407fb430f4c4a25b61404fdabfbd23b7c1708c43ed755ab7779f443",
            com.zqr.localchat.crypto.Crypto.hex(GroupAuth.transcriptHash(parts))
        )
    }

    @Test
    fun `delete transcript digest matches the Windows codec`() {
        val parts = GroupAuth.deleteParts("12345678", "alice", "m-1")
        assertEquals(
            "6fe153e9d278a066e9f0cbcc477ae4d2f540c4d139775529c2e8c8ff9fb25698",
            com.zqr.localchat.crypto.Crypto.hex(GroupAuth.transcriptHash(parts))
        )
    }

    @Test
    fun `edit transcript digest matches the Windows codec`() {
        val parts = GroupAuth.editParts("12345678", "alice", "m-1", "new text")
        assertEquals(
            listOf(
                "edit", "12345678", "alice", "m-1",
                "cb0208b0b1fa06bc59f85c8b2be1e45ff2ef6ddbf0cef02e9f276b8208ea48ab"
            ),
            parts
        )
        assertEquals(
            "4bc9de1f64ca8db95090d6b24f067438d60b885ec635a878e6c34fd2255dbe33",
            com.zqr.localchat.crypto.Crypto.hex(GroupAuth.transcriptHash(parts))
        )
    }

    @Test
    fun `length prefixing keeps pipes inside ids from splitting parts`() {
        // "a|b" as ONE part must not produce the same digest as two parts a, b
        val withPipe = GroupAuth.transcriptHash(listOf("a|b"))
        val twoParts = GroupAuth.transcriptHash(listOf("a", "b"))
        assertEquals(false, withPipe.contentEquals(twoParts))
    }

    @Test
    fun `content digest is sha256 hex of the utf8 bytes`() {
        assertEquals(
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            GroupAuth.contentDigest("hello")
        )
    }
}
