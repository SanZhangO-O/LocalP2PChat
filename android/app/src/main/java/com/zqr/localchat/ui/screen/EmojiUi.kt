package com.zqr.localchat.ui.screen

import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog

/** One emoji category: a display name plus its characters. */
data class EmojiCategory(val name: String, val emojis: List<String>)

/**
 * Plain Unicode emoji catalog (no image stickers). The first tab is the
 * recents list supplied by the caller; the rest are fixed categories.
 */
object EmojiCatalog {
    const val RECENT_TAB = "最近"

    /** 贴纸 tab: a curated set sent as plain text; pure-emoji short content
     *  renders LARGE (sticker style), so no image assets and no protocol
     *  change are needed — old peers just see a short emoji message. */
    val stickers: List<String> = listOf(
        "😂", "🥹", "😍", "🥺", "😤", "😱", "🤡", "💀",
        "🙏", "👍", "👎", "👏", "💪", "🤝", "✌️", "🫶",
        "❤️", "💔", "💯", "🔥", "✨", "🎉", "🎂", "🍺",
        "☕", "🌹", "🌈", "☀️", "🌙", "⚡", "🐱", "🐶",
        "🐼", "🦊", "🐷", "🐣", "🍀", "🎁", "🚀", "🏆"
    )

    val categories: List<EmojiCategory> = listOf(
        EmojiCategory(
            "贴纸",
            stickers
        ),
        EmojiCategory(
            "笑脸",
            listOf(
                "😀", "😃", "😄", "😁", "😆", "😅", "😂", "🤣",
                "😊", "😇", "🙂", "🙃", "😉", "😌", "😍", "🥰",
                "😘", "😗", "😙", "😚", "😋", "😛", "😝", "😜",
                "🤪", "🤨", "🧐", "🤓", "😎", "🥳", "😏", "😒",
                "😞", "😔", "😟", "😕", "🙁", "😣", "😖", "😫",
                "😩", "🥺", "😢", "😭", "😤", "😠", "😡", "🤬",
                "🤯", "😳", "🥵", "🥶", "😱", "😨", "😰", "😥",
                "😓", "🤗", "🤔", "🤭", "🤫", "🤥", "😶", "😐",
                "😑", "😬", "🙄", "😯", "😦", "😧", "😮", "😲",
                "🥱", "😴", "🤤", "😪", "🤐", "🥴", "🤢", "🤮",
                "🤧", "😷", "🤒", "🤕"
            )
        ),
        EmojiCategory(
            "手势",
            listOf(
                "👍", "👎", "👌", "✌️", "🤞", "🤟", "🤘", "🤙",
                "👈", "👉", "👆", "👇", "☝️", "✋", "🤚", "🖐️",
                "🖖", "👋", "🤝", "🙏", "💪", "🦾", "✍️", "💅",
                "🤳", "🦵", "🦶", "👂", "👃", "👀", "👁️", "👅",
                "👄"
            )
        ),
        EmojiCategory(
            "动物",
            listOf(
                "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼",
                "🐨", "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🙈",
                "🙉", "🙊", "🐔", "🐧", "🐦", "🐤", "🦆", "🦅",
                "🦉", "🦇", "🐺", "🐗", "🐴", "🦄", "🐝", "🐛",
                "🦋", "🐌", "🐞", "🐜", "🦗", "🕷️", "🦂", "🐢",
                "🐍", "🦎", "🐙", "🦑", "🦀", "🐠", "🐟", "🐬",
                "🐳", "🐋", "🦈"
            )
        ),
        EmojiCategory(
            "食物",
            listOf(
                "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓",
                "🍈", "🍒", "🍑", "🥭", "🍍", "🥥", "🥝", "🍅",
                "🥑", "🥦", "🥕", "🌽", "🌶️", "🥒", "🥬", "🧄",
                "🧅", "🍄", "🥜", "🍞", "🥐", "🥖", "🥨", "🧀",
                "🥚", "🍳", "🥞", "🧇", "🥓", "🍔", "🍟", "🍕",
                "🌭", "🥪", "🌮", "🌯"
            )
        ),
        EmojiCategory(
            "物品",
            listOf(
                "📱", "💻", "⌨️", "🖥️", "🖨️", "🖱️", "💽", "💾",
                "💿", "📀", "📷", "📸", "📹", "🎥", "📞", "☎️",
                "📟", "📠", "📺", "📻", "🎙️", "⏰", "⌚", "⏳",
                "🔋", "🔌", "💡", "🔦", "🕯️", "🧯", "💸", "💵",
                "💰", "💳", "💎", "⚖️", "🔧", "🔨", "⚒️", "🛠️"
            )
        ),
        EmojiCategory(
            "符号",
            listOf(
                "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍",
                "🤎", "💔", "❣️", "💕", "💞", "💓", "💗", "💖",
                "💘", "💝", "💟", "☮️", "✝️", "☪️", "🕉️", "☸️",
                "✡️", "🔯", "🕎", "☯️", "☦️", "🛐", "⛎", "♈",
                "♉", "♊", "♋", "♌", "♍", "♎", "♏", "♐",
                "♑", "♒", "♓"
            )
        )
    )
}

/**
 * Recently used emoji, persisted in the app's shared preferences. The pure
 * list logic is separated from the Android storage so it stays unit-testable.
 */
object RecentEmoji {
    const val CAP = 24

    /** Unit separator: never appears inside an emoji. */
    private const val SEP = "\u001f"
    private const val PREFS = "localchat_prefs"
    private const val KEY = "recent_emoji"

    fun parse(raw: String?): List<String> =
        raw?.split(SEP)?.map { it.trim() }?.filter { it.isNotEmpty() }?.take(CAP)
            ?: emptyList()

    fun encode(recent: List<String>): String = recent.take(CAP).joinToString(SEP)

    /** Move [emoji] to the front, drop duplicates, cap at [CAP]. */
    fun updated(recent: List<String>, emoji: String): List<String> {
        if (emoji.isEmpty()) return recent.take(CAP)
        return (listOf(emoji) + recent.filter { it != emoji }).take(CAP)
    }

    fun load(context: Context): List<String> =
        parse(
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY, null)
        )

    /** Record a pick and persist the new list; returns it. */
    fun record(context: Context, emoji: String): List<String> {
        val next = updated(load(context), emoji)
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY, encode(next))
            .apply()
        return next
    }
}

/** Pure text-cursor insertion so it is unit-testable without Compose. */
object EmojiText {
    /**
     * Replace [selectionStart]..[selectionEnd] with [insert] and return the
     * new text plus the cursor position right after the inserted emoji.
     */
    fun insert(
        text: String,
        selectionStart: Int,
        selectionEnd: Int,
        insert: String
    ): Pair<String, Int> {
        val start = selectionStart.coerceIn(0, text.length)
        val end = selectionEnd.coerceIn(start, text.length)
        val next = text.substring(0, start) + insert + text.substring(end)
        return next to (start + insert.length)
    }
}

/**
 * Emoji picker: one tab per category, with a "最近" tab holding the most
 * recently used emoji. [onPick] fires for every tap; the caller inserts the
 * character at the input's cursor and persists the recents.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun EmojiPickerDialog(
    recent: List<String>,
    onPick: (String) -> Unit,
    onDismiss: () -> Unit
) {
    var selected by remember { mutableIntStateOf(0) }
    val tabs = remember { listOf(EmojiCatalog.RECENT_TAB) + EmojiCatalog.categories.map { it.name } }

    Dialog(onDismissRequest = onDismiss) {
        Surface(
            shape = RoundedCornerShape(20.dp),
            tonalElevation = 4.dp,
            modifier = Modifier
                .fillMaxWidth()
                .height(360.dp)
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                PrimaryScrollableTabRow(
                    selectedTabIndex = selected,
                    edgePadding = 4.dp
                ) {
                    tabs.forEachIndexed { index, name ->
                        Tab(
                            selected = selected == index,
                            onClick = { selected = index },
                            text = { Text(name, fontSize = 13.sp) }
                        )
                    }
                }
                Spacer(modifier = Modifier.height(8.dp))
                val items = if (selected == 0) {
                    recent
                } else {
                    EmojiCatalog.categories[selected - 1].emojis
                }
                if (items.isEmpty()) {
                    Box(
                        modifier = Modifier.fillMaxSize(),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            text = "还没有使用过的表情",
                            fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                } else {
                    LazyVerticalGrid(
                        columns = GridCells.Fixed(8),
                        modifier = Modifier.fillMaxSize()
                    ) {
                        items(items, key = { it }) { emoji ->
                            Box(
                                modifier = Modifier
                                    .size(40.dp)
                                    .clip(RoundedCornerShape(10.dp))
                                    .clickable { onPick(emoji) },
                                contentAlignment = Alignment.Center
                            ) {
                                Text(text = emoji, fontSize = 22.sp)
                            }
                        }
                    }
                }
            }
        }
    }
}
