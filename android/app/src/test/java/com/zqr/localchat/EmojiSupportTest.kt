package com.zqr.localchat

import com.zqr.localchat.ui.screen.EmojiCatalog
import com.zqr.localchat.ui.screen.EmojiText
import com.zqr.localchat.ui.screen.RecentEmoji
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Emoji panel support: the Unicode catalog shape, the pure recents
 * update/encode/parse logic (persisted between sessions) and the
 * cursor-insertion helper.
 */
class EmojiSupportTest {

    @Test
    fun `catalog has the expected categories with enough emoji`() {
        assertEquals(
            listOf("贴纸", "笑脸", "手势", "动物", "食物", "物品", "符号"),
            EmojiCatalog.categories.map { it.name }
        )
        EmojiCatalog.categories.forEach { category ->
            assertTrue(
                "${category.name} must hold a few dozen emoji",
                category.emojis.size >= 30
            )
            assertEquals(
                "no duplicates inside ${category.name}",
                category.emojis.size,
                category.emojis.toSet().size
            )
        }
        // every sticker renders BIG (isBigEmoji) so the sticker tab actually
        // produces the large sticker-style bubbles on both platforms
        EmojiCatalog.categories.first().emojis.forEach { sticker ->
            assertTrue(
                "sticker $sticker must be detected as big emoji",
                com.zqr.localchat.data.isBigEmoji(sticker)
            )
        }
    }

    @Test
    fun `recents update moves to the front dedupes and caps`() {
        var recent = emptyList<String>()
        for (i in 0 until RecentEmoji.CAP + 3) {
            recent = RecentEmoji.updated(recent, "e$i")
        }
        assertEquals(RecentEmoji.CAP, recent.size)
        assertEquals("e${RecentEmoji.CAP + 2}", recent.first())

        val oldest = recent.last()
        val again = RecentEmoji.updated(recent, oldest)
        assertEquals(oldest, again.first())
        assertEquals(RecentEmoji.CAP, again.size)
        assertEquals(again.size, again.toSet().size)
    }

    @Test
    fun `recents encode and parse round trip tolerates garbage`() {
        val list = listOf("\uD83D\uDE00", "\uD83D\uDC4D")
        assertEquals(list, RecentEmoji.parse(RecentEmoji.encode(list)))

        assertEquals(emptyList<String>(), RecentEmoji.parse(null))
        assertEquals(emptyList<String>(), RecentEmoji.parse(""))
        assertEquals(emptyList<String>(), RecentEmoji.parse("\u001f\u001f"))
        assertEquals(listOf("a"), RecentEmoji.parse("\u001fa\u001f"))

        val many = (0..40).joinToString("\u001f") { "x$it" }
        assertEquals(RecentEmoji.CAP, RecentEmoji.parse(many).size)
    }

    @Test
    fun `cursor insert replaces the selection and clamps`() {
        // UTF-16 caret indexes: an emoji is two code units long
        assertEquals("a\uD83D\uDE00b" to 3, EmojiText.insert("ab", 1, 1, "\uD83D\uDE00"))
        assertEquals("a\uD83D\uDE00" to 3, EmojiText.insert("ab", 1, 2, "\uD83D\uDE00"))
        assertEquals("\uD83D\uDE00ab" to 2, EmojiText.insert("ab", 0, 0, "\uD83D\uDE00"))
        assertEquals("ab\uD83D\uDE00" to 4, EmojiText.insert("ab", 2, 2, "\uD83D\uDE00"))
        assertEquals("ab" to 2, EmojiText.insert("ab", 5, 9, ""))
    }
}
