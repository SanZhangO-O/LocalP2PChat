package com.zqr.localchat.data

/**
 * Pure keyword-search helpers shared by the in-conversation search and the
 * global search screen.
 *
 * Message bodies are encrypted at rest ([com.zqr.localchat.crypto.StoreCipher],
 * "enc1:..."), so a SQL `LIKE` on `content` would silently match nothing: the
 * DAO prefilters the plaintext-at-rest columns ([escapeLike] feeds the
 * `ESCAPE '\'` pattern) and the caller decrypts the scope rows and matches the
 * body with [matches].
 */
object MessageSearch {

    /** Result cap for one global search. */
    const val MAX_RESULTS = 100

    /** How much of the matched body a result row shows. */
    const val SNIPPET_MAX = 60

    /** Escape SQL LIKE wildcards (and the escape character itself) so
     *  [keyword] is a literal substring: `a%b_c` -> `a\%b\_c`. Pair with
     *  `LIKE :pattern ESCAPE '\'`. */
    fun escapeLike(keyword: String): String =
        keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    /** The `%keyword%` LIKE pattern for [escapeLike]. */
    fun likePattern(keyword: String): String = "%" + escapeLike(keyword) + "%"

    /** True when one row matches [keyword]; [plainContent] is the DECRYPTED
     *  body (the stored column is ciphertext). */
    fun matches(row: SavedChatMessage, plainContent: String, keyword: String): Boolean {
        if (keyword.isEmpty()) return false
        return plainContent.contains(keyword) ||
            row.senderName.contains(keyword) ||
            row.relativePath.contains(keyword) ||
            row.folderName.contains(keyword)
    }

    /** One-line preview of a hit: media/file rows get a bracketed kind prefix,
     *  a folder entry shows its relative path, text shows its body. Mirrors the
     *  Windows `message_preview`. */
    fun preview(row: SavedChatMessage, plainContent: String): String {
        val text = plainContent.replace('\n', ' ').trim()
        val isFile = row.fileSize > 0L || row.relativePath.isNotEmpty() ||
            row.folderName.isNotEmpty() || row.folderId.isNotEmpty()
        if (!isFile) return truncate(text)
        val body = row.relativePath.ifEmpty { text }
        val prefix = when (row.kind) {
            FileKind.IMAGE -> "[图片] "
            FileKind.VIDEO -> "[视频] "
            else -> "[文件] "
        }
        return truncate(prefix + body)
    }

    /** Merge the name-column pass with the body pass, dedupe by (groupId, id),
     *  newest first, capped at [max]. */
    fun merge(
        nameHits: List<SavedChatMessage>,
        bodyHits: List<SavedChatMessage>,
        max: Int = MAX_RESULTS
    ): List<SavedChatMessage> {
        val seen = HashSet<Pair<String, String>>()
        val merged = ArrayList<SavedChatMessage>()
        for (row in nameHits + bodyHits) {
            if (seen.add(row.groupId to row.id)) merged.add(row)
            if (merged.size >= max) break
        }
        return merged.sortedByDescending { it.timestamp }
    }

    private fun truncate(text: String): String =
        if (text.length <= SNIPPET_MAX) text else text.take(SNIPPET_MAX) + "..."
}
