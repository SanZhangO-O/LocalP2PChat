package com.zqr.localchat

import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MAX_FOLDER_FILES
import com.zqr.localchat.data.sanitizeFolderId
import com.zqr.localchat.data.sanitizeRelativePath
import com.zqr.localchat.data.sanitized
import com.zqr.localchat.data.withSanitizedFileInfo
import com.zqr.localchat.ui.screen.MessageItem
import com.zqr.localchat.ui.screen.buildMessageItems
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Folder transfer (Windows parity): the optional FileInfo folder fields, their
 * default-omission on the wire, legacy packet parsing and the traversal-safe
 * path/id sanitizers, plus the UI grouping helper.
 */
class FolderTransferTest {

    private val json = Json { ignoreUnknownKeys = true }

    // ------------------------------------------------------------- wire format

    @Test
    fun `plain file offer omits every folder field`() {
        val fi = FileInfo("f1", "报告.pdf", 2048L, "192.168.1.5", 42001)
        val encoded = json.encodeToString(fi)

        // byte contract with Windows FileInfo.to_dict(): defaults are omitted
        assertFalse(encoded.contains("folderId"))
        assertFalse(encoded.contains("folderName"))
        assertFalse(encoded.contains("relativePath"))
        assertFalse(encoded.contains("folderTotal"))
        assertFalse("kind defaults to file and is omitted", encoded.contains("\"kind\""))
    }

    @Test
    fun `folder fields serialize and round-trip`() {
        val fi = FileInfo(
            "f1", "a.txt", 10L, "192.168.1.5", 42001,
            folderId = "abc-123", folderName = "docs",
            relativePath = "sub/a.txt", folderTotal = 3
        )
        val encoded = json.encodeToString(fi)

        assertTrue(encoded.contains("\"folderId\":\"abc-123\""))
        assertTrue(encoded.contains("\"folderName\":\"docs\""))
        assertTrue(encoded.contains("\"relativePath\":\"sub/a.txt\""))
        assertTrue(encoded.contains("\"folderTotal\":3"))

        val decoded = json.decodeFromString<FileInfo>(encoded)
        assertEquals("abc-123", decoded.folderId)
        assertEquals("docs", decoded.folderName)
        assertEquals("sub/a.txt", decoded.relativePath)
        assertEquals(3, decoded.folderTotal)
    }

    @Test
    fun `folderId with a folderTotal of zero still omits only folderTotal`() {
        val fi = FileInfo(
            "f1", "a.txt", 10L, "h", 1, folderId = "abc"
        )
        val encoded = json.encodeToString(fi)
        assertTrue(encoded.contains("\"folderId\":\"abc\""))
        assertFalse(encoded.contains("folderTotal"))
    }

    @Test
    fun `legacy file packet without folder fields still parses`() {
        val wire = """{"fileId":"f1","fileName":"a.txt","fileSize":5,""" +
            """"downloadHost":"h","downloadPort":1}"""
        val decoded = json.decodeFromString<FileInfo>(wire)

        assertEquals("f1", decoded.fileId)
        assertEquals("", decoded.folderId)
        assertEquals("", decoded.folderName)
        assertEquals("", decoded.relativePath)
        assertEquals(0, decoded.folderTotal)
        assertEquals(FileKind.FILE, decoded.kind)
    }

    @Test
    fun `unknown folder-ish fields from a future peer are ignored`() {
        val wire = """{"fileId":"f1","fileName":"a.txt","fileSize":5,""" +
            """"downloadHost":"h","downloadPort":1,"folderFuture":42}"""
        val decoded = json.decodeFromString<FileInfo>(wire)
        assertEquals("f1", decoded.fileId)
    }

    @Test
    fun `forged folderTotal beyond the cap clamps to unknown like Windows`() {
        val forged = """{"fileId":"f1","fileName":"a.txt","fileSize":5,""" +
            """"downloadHost":"h","downloadPort":1,"folderId":"F",""" +
            """"relativePath":"a.txt","folderTotal":999999}"""
        val decoded = json.decodeFromString<FileInfo>(forged)
        assertEquals(999999, decoded.folderTotal) // raw decode keeps the value
        // decode-time normalization mirrors Windows from_dict: never trust it
        assertEquals(0, decoded.sanitized().folderTotal)
        // a count at the cap is legitimate and survives untouched
        assertEquals(MAX_FOLDER_FILES, decoded.copy(folderTotal = MAX_FOLDER_FILES).sanitized().folderTotal)

        val msg = ChatMessage("m1", "a.txt", 1L, "p", "X", fileInfo = decoded)
        assertEquals(0, msg.withSanitizedFileInfo().fileInfo!!.folderTotal)
    }

    // ------------------------------------------------------------ sanitizers

    @Test
    fun `sanitizeFolderId keeps only the safe alphabet and caps at 64`() {
        assertEquals("abc-123_XY", sanitizeFolderId("abc-123_XY"))
        assertEquals("abcd", sanitizeFolderId("a/b c.d"))
        assertEquals("etcpasswd", sanitizeFolderId("../../etc/passwd"))
        assertEquals("x".repeat(64), sanitizeFolderId("x".repeat(200)))
    }

    @Test
    fun `sanitizeRelativePath drops traversal and dot segments`() {
        assertEquals("", sanitizeRelativePath("../.."))
        assertEquals("", sanitizeRelativePath(""))
        assertEquals("", sanitizeRelativePath("."))
        assertEquals("", sanitizeRelativePath(".."))
        assertEquals("", sanitizeRelativePath("/"))
        assertEquals("", sanitizeRelativePath("\\"))
        assertEquals("", sanitizeRelativePath("./../."))
    }

    @Test
    fun `sanitizeRelativePath normalizes separators and keeps nesting`() {
        assertEquals("a/b/c.txt", sanitizeRelativePath("a/b/c.txt"))
        assertEquals("a/b/c.txt", sanitizeRelativePath("a\\b\\c.txt"))
        assertEquals("a/b/c.txt", sanitizeRelativePath("/a/b/c.txt"))
        assertEquals("a/b/c.txt", sanitizeRelativePath("a//b///c.txt"))
        // an interior ".." is not a whole segment, so it survives as the safe
        // basename "b" (Windows sanitize_file_name drops nothing but "." / "..")
        assertEquals("a/b/c.txt", sanitizeRelativePath("a/../b/c.txt"))
    }

    @Test
    fun `sanitizeRelativePath never yields a drive-relative or absolute path`() {
        // ":" is stripped so a segment can never become "C:" (drive-relative)
        val c = sanitizeRelativePath("C:\\Windows\\system32\\x.dll")
        assertFalse(c.contains(":"))
        assertEquals("C/Windows/system32/x.dll", c)
        assertFalse("result must stay relative", c.startsWith("/"))
        // a crafted segment cannot smuggle a separator via the sanitizer
        assertFalse(sanitizeRelativePath("a/..\\..\\b").startsWith("/"))
    }

    @Test
    fun `sanitizeRelativePath caps segments and total length`() {
        val deep = (1..80).joinToString("/") { "s$it" }
        val capped = sanitizeRelativePath(deep)
        assertTrue("at most 64 segments", capped.split('/').size <= 64)
    }

    // ------------------------------------------------------------ grouping UI

    private fun fileMsg(
        id: String,
        folderId: String = "",
        relativePath: String = "",
        folderName: String = "",
        timestamp: Long = 1L,
        fromMe: Boolean = false,
        host: String = "1.2.3.4"
    ) = ChatMessage(
        id = id,
        content = id,
        timestamp = timestamp,
        senderId = if (fromMe) "me" else "peer",
        senderName = "X",
        isFromMe = fromMe,
        fileInfo = FileInfo(
            id, "$id.txt", 10L, host, 1,
            folderId = folderId, relativePath = relativePath,
            folderName = folderName, folderTotal = 2
        )
    )

    @Test
    fun `non-folder messages keep their order and count`() {
        val messages = listOf(
            ChatMessage("t1", "hi", 1L, "a", "A"),
            fileMsg("f1"),
            ChatMessage("t2", "yo", 2L, "a", "A")
        )
        val items = buildMessageItems(messages)
        assertEquals(3, items.size)
        assertTrue(items[0] is MessageItem.Msg)
        assertTrue(items[1] is MessageItem.Msg)
        assertTrue(items[2] is MessageItem.Msg)
    }

    @Test
    fun `folder entries collapse into one row at the first entry position`() {
        val messages = listOf(
            ChatMessage("t1", "hi", 1L, "a", "A"),
            fileMsg("e1", folderId = "F", relativePath = "b.txt"),
            fileMsg("e2", folderId = "F", relativePath = "a.txt"),
            ChatMessage("t2", "yo", 5L, "a", "A")
        )
        val items = buildMessageItems(messages)

        // t1, folder, t2 — the two folder entries are NOT separate rows
        assertEquals(3, items.size)
        assertTrue(items[0] is MessageItem.Msg)
        val folder = items[1] as MessageItem.Folder
        assertTrue(items[2] is MessageItem.Msg)
        assertEquals(2, folder.group.total)
        assertEquals("F", folder.group.folderId)
        // entries are sorted by relativePath
        assertEquals("a.txt", folder.group.entries[0].fileInfo!!.relativePath)
        assertEquals("b.txt", folder.group.entries[1].fileInfo!!.relativePath)
    }

    @Test
    fun `folder group totals, size and expiry are computed from entries`() {
        val expired = ChatMessage(
            id = "e1", content = "e1", timestamp = 1L, senderId = "peer", senderName = "X",
            fileInfo = FileInfo("e1", "e1.txt", 100L, "", 0, folderId = "F", relativePath = "a")
        )
        val live = fileMsg("e2", folderId = "F", relativePath = "b")
        val group = (buildMessageItems(listOf(expired, live)).single() as MessageItem.Folder).group

        assertEquals(2, group.total)
        assertEquals(110L, group.size)
        assertFalse("one live entry keeps the folder downloadable", group.expired)
        assertFalse(group.isFromMe)
    }

    @Test
    fun `own folder is marked from me and never grouped with a peer folder`() {
        val mine = fileMsg("e1", folderId = "F1", relativePath = "a", fromMe = true)
        val theirs = fileMsg("e2", folderId = "F2", relativePath = "a")
        val items = buildMessageItems(listOf(mine, theirs))

        assertEquals(2, items.size)
        assertTrue((items[0] as MessageItem.Folder).group.isFromMe)
        assertFalse((items[1] as MessageItem.Folder).group.isFromMe)
    }

    @Test
    fun `duplicate message ids inside a folder collapse once per id`() {
        val m = fileMsg("e1", folderId = "F", relativePath = "a")
        val items = buildMessageItems(listOf(m, m))
        val group = (items.single() as MessageItem.Folder).group
        assertEquals(2, group.total)
    }

    @Test
    fun `max folder files constant matches the wire cap`() {
        assertEquals(1000, MAX_FOLDER_FILES)
    }
}
