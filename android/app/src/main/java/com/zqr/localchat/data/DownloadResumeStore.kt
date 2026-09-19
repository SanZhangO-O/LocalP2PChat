package com.zqr.localchat.data

import android.content.Context
import com.zqr.localchat.crypto.StoreCipher
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.util.concurrent.ConcurrentHashMap

/**
 * Persisted resume state for an interrupted file download (Windows parity).
 *
 * A download always streams into an app-private staging file first and is
 * finalized onto the user's document (or renamed for app media) only when it
 * completes. When it is cancelled or the connection breaks, the staging file
 * stays and this entry remembers where it is and how many bytes it holds, so
 * the card can show 「已暂停 xx%（点击续传）」 and the next attempt resumes from
 * the exact offset — including after the process was restarted, because the
 * address and per-file key (blanked on offers restored from the database) are
 * persisted here too.
 *
 * Values are Keystore-wrapped via [StoreCipher] ("enc1:..." at rest): the
 * per-file AES key never sits in the prefs file in the clear. Entries are
 * removed on success and pruned oldest-first beyond [MAX_ENTRIES].
 *
 * Decoded entries are memoized in [cache]: every [StoreCipher] round trip is
 * a Keystore + Cipher operation, and [put] runs [prune] → [all] on each
 * progress write, so re-decoding the whole store per put is prohibitively
 * expensive (the prefs file itself stays the source of truth; the cache is
 * lazily filled and re-synced against the pref keys on every [all]).
 */
@Serializable
data class DownloadResumeEntry(
    val fileId: String,
    /** Final destination: a SAF document URI string, or an app path for media. */
    val target: String = "",
    /** Absolute path of the app-private ".part" staging file. */
    val partPath: String = "",
    val received: Long = 0,
    val total: Long = 0,
    val host: String = "",
    val port: Int = 0,
    val fileKey: String = "",
    val fileName: String = "",
    val fileSize: Long = 0,
    val kind: String = "",
    /** Folder offers: the entry's folderId / entry count and the picked tree. */
    val folderId: String = "",
    val folderTotal: Int = 0,
    val destDir: String = "",
    val updatedAt: Long = 0
)

object DownloadResumeStore {

    private const val PREFS = "localchat_prefs"
    private const val PREFIX = "file_resume_"
    private const val MAX_ENTRIES = 200
    private val json = Json { ignoreUnknownKeys = true }

    private val cache = ConcurrentHashMap<String, DownloadResumeEntry>()

    private fun prefs(ctx: Context) =
        ctx.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun decode(fileId: String, raw: String): DownloadResumeEntry? {
        val entry = runCatching {
            json.decodeFromString<DownloadResumeEntry>(StoreCipher.unprotect(raw))
        }.getOrNull() ?: return null
        return if (entry.fileId == fileId) entry else null
    }

    fun get(ctx: Context, fileId: String): DownloadResumeEntry? {
        if (fileId.isEmpty()) return null
        cache[fileId]?.let { return it }
        val raw = prefs(ctx).getString(PREFIX + fileId, null) ?: return null
        val entry = decode(fileId, raw) ?: return null
        cache[fileId] = entry
        return entry
    }

    fun put(ctx: Context, entry: DownloadResumeEntry) {
        if (entry.fileId.isEmpty()) return
        val stored = entry.copy(updatedAt = System.currentTimeMillis())
        val raw = StoreCipher.protect(json.encodeToString(stored))
        prefs(ctx).edit().putString(PREFIX + entry.fileId, raw).apply()
        cache[entry.fileId] = stored
        prune(ctx)
    }

    fun remove(ctx: Context, fileId: String) {
        if (fileId.isEmpty()) return
        prefs(ctx).edit().remove(PREFIX + fileId).apply()
        cache.remove(fileId)
    }

    fun all(ctx: Context): List<DownloadResumeEntry> {
        val ids = prefs(ctx).all.keys
            .filter { it.startsWith(PREFIX) }
            .map { it.removePrefix(PREFIX) }
            .toSet()
        // drop evicted/removed ids, then decode only the misses (once per
        // process lifetime per entry)
        cache.keys.retainAll(ids)
        for (id in ids) {
            if (cache.containsKey(id)) continue
            val raw = prefs(ctx).getString(PREFIX + id, null) ?: continue
            decode(id, raw)?.let { cache[id] = it }
        }
        return cache.values.toList()
    }

    private fun prune(ctx: Context) {
        val entries = all(ctx)
        if (entries.size <= MAX_ENTRIES) return
        entries.sortedBy { it.updatedAt }
            .take(entries.size - MAX_ENTRIES)
            .forEach { remove(ctx, it.fileId) }
    }
}
