package com.zqr.localchat.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.Transient
import org.json.JSONArray

@Serializable
data class Peer(
    val id: String,
    val name: String,
    val ipAddress: String,
    val port: Int
)

/** File-message kind: a plain file card, or media that renders inline in the
 *  conversation once downloaded. Serialized only when not "file" (kotlinx
 *  omits defaults), so packets stay byte-compatible with older clients. */
object FileKind {
    const val FILE = "file"
    const val IMAGE = "image"
    const val VIDEO = "video"
    const val AUDIO = "audio"
}

/** Image extensions recognized for media classification (lowercase, with
 *  dot); mirrors the Windows client's models.IMAGE_EXTENSIONS. */
val IMAGE_EXTENSIONS = setOf(".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic")

/** Video extensions recognized for media classification (lowercase, with
 *  dot); mirrors the Windows client's models.VIDEO_EXTENSIONS. */
val VIDEO_EXTENSIONS = setOf(".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp")

/** Audio extensions recognized for media classification (lowercase, with
 *  dot); mirrors the Windows client's models.AUDIO_EXTENSIONS. Voice messages
 *  are plain 16 kHz mono WAV on both platforms, so no codec is involved. */
val AUDIO_EXTENSIONS = setOf(".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".mp3", ".flac")

/** Classify a file by name for the send path: "image", "video", "audio" or
 *  "file". Old peers that do not know "audio" degrade it to a plain file
 *  card that still downloads and plays externally. */
fun detectMediaKind(fileName: String): String {
    val ext = fileName.substringAfterLast('.', "").lowercase()
    if (ext.isEmpty()) return FileKind.FILE
    return when {
        ".$ext" in IMAGE_EXTENSIONS -> FileKind.IMAGE
        ".$ext" in VIDEO_EXTENSIONS -> FileKind.VIDEO
        ".$ext" in AUDIO_EXTENSIONS -> FileKind.AUDIO
        else -> FileKind.FILE
    }
}

/** File kinds a decode keeps ("audio" included on this platform; anything
 *  else degrades to a plain file, Windows normalize_media_kind parity). */
fun normalizeMediaKind(kind: String): String =
    if (kind == FileKind.IMAGE || kind == FileKind.VIDEO || kind == FileKind.AUDIO) kind
    else FileKind.FILE

/** Mentions (@提及): ChatMessage.mentions carries peer ids; the special id
 *  "all" means "mentioned everyone". Windows models parity. */
const val MENTION_ALL = "all"
const val MAX_MENTIONS = 64
const val MAX_MENTION_ID_LEN = 128

/** Serialize a mentions list into the persisted text column ("" = none). */
fun storedMentionsJson(mentions: List<String>?): String =
    if (mentions.isNullOrEmpty()) "" else JSONArray(mentions).toString()

/** Parse the persisted mentions column back into a list (null = none). */
fun parseStoredMentions(raw: String?): List<String>? {
    if (raw.isNullOrEmpty()) return null
    return runCatching {
        val arr = JSONArray(raw)
        (0 until arr.length()).map { arr.getString(it) }
    }.getOrNull()?.ifEmpty { null }
}

/** Python `str.isprintable() and not str.isspace()` parity for one code
 *  point: non-printable categories (Cc/Cf/Cs/Co/Cn/Zl/Zp/Zs) and every
 *  whitespace character are dropped, so both ends keep/drop exactly the same
 *  characters (ZWJ is Cf -> dropped; VS16/keycap are marks -> kept;
 *  SURROGATE is Cs -> dropped, matching Python, so a JSON lone surrogate can
 *  never reach the reaction table). */
private fun isPrintableNonSpaceCodePoint(cp: Int): Boolean {
    if (Character.isWhitespace(cp) || Character.isSpaceChar(cp)) return false
    return when (Character.getType(cp)) {
        Character.CONTROL.toInt(),
        Character.FORMAT.toInt(),
        Character.SURROGATE.toInt(),
        Character.PRIVATE_USE.toInt(),
        Character.UNASSIGNED.toInt(),
        Character.LINE_SEPARATOR.toInt(),
        Character.PARAGRAPH_SEPARATOR.toInt(),
        Character.SPACE_SEPARATOR.toInt() -> false
        else -> true
    }
}

/** A reaction emoji is a sender-supplied short string: drop non-printable and
 *  whitespace characters and cap at 16 CODE POINTS (never splitting a
 *  surrogate pair) — exactly the Windows `sanitize_emoji` rule, so the same
 *  wire value maps to the same stored string on both platforms. Returns ""
 *  when nothing usable remains. */
fun sanitizeEmoji(value: String?): String {
    val text = value ?: return ""
    val out = StringBuilder(text.length)
    var i = 0
    var count = 0
    while (i < text.length && count < 16) {
        val cp = text.codePointAt(i)
        i += Character.charCount(cp)
        if (isPrintableNonSpaceCodePoint(cp)) {
            out.appendCodePoint(cp)
            count++
        }
    }
    return out.toString()
}

/** Token-boundary match of "@name" (Windows `_matches_mention` parity): the
 *  token counts only at start-of-text/after whitespace AND end-of-text/before
 *  whitespace, so "@Anna" no longer mentions a member called "Ann" and
 *  "user@host" never matches. The longest name wins naturally: a shorter
 *  prefix leaves a letter (not a boundary) right after the token. */
fun matchesMention(text: String, name: String): Boolean {
    val token = "@$name"
    var start = 0
    while (true) {
        val i = text.indexOf(token, start)
        if (i < 0) return false
        val beforeOk = i == 0 || text[i - 1].isWhitespace()
        val end = i + token.length
        val afterOk = end == text.length || text[end].isWhitespace()
        if (beforeOk && afterOk) return true
        start = i + 1
    }
}

/** Ids mentioned in [text]: "@名字" tokens matched against [members] (plus
 *  "@所有人"); null when nobody was mentioned (no wire field). */
fun resolveMentionIds(text: String, members: List<Pair<String, String>>): List<String>? {
    if ('@' !in text) return null
    val ids = mutableListOf<String>()
    if (matchesMention(text, "所有人")) ids.add(MENTION_ALL)
    for ((id, name) in members) {
        if (name.isNotEmpty() && matchesMention(text, name) && id !in ids) ids.add(id)
    }
    return ids.ifEmpty { null }
}

/** Truncate [text] to at most [maxCodePoints] CODE POINTS, never splitting a
 *  surrogate pair. Windows slices strings by code points (`item[:128]`,
 *  `preview[:40]`); Kotlin String.length/take count UTF-16 units, so they must
 *  not be used for parity-sensitive limits (AGENTS.md section 6). */
fun takeCodePoints(text: String, maxCodePoints: Int): String {
    if (maxCodePoints <= 0) return ""
    var count = 0
    var i = 0
    while (i < text.length && count < maxCodePoints) {
        val cp = text.codePointAt(i)
        i += Character.charCount(cp)
        count++
    }
    return if (i >= text.length) text else text.substring(0, i)
}

/** Normalize an inbound mentions list: strings only, deduped in order, each
 *  capped, at most [MAX_MENTIONS] entries. A malformed shape is ignored. */
fun sanitizeMentions(raw: List<String>?): List<String>? {
    if (raw == null) return null
    val out = ArrayList<String>()
    for (item in raw) {
        val entry = takeCodePoints(item, MAX_MENTION_ID_LEN)
        if (entry.isNotEmpty() && entry !in out) out.add(entry)
        if (out.size >= MAX_MENTIONS) break
    }
    return out.ifEmpty { null }
}

private val EMOJI_MODIFIER_ORDS = setOf(0xFE0F, 0x200D, 0x20E3, 0x2764, 0xA9, 0xAE, 0x2122)

private fun isEmojiCodePoint(cp: Int): Boolean =
    cp >= 0x1F000 || cp in 0x2600..0x27BF || cp in 0x2B00..0x2BFF || cp in EMOJI_MODIFIER_ORDS

/** True when [content] is sticker-sized: only emoji (+ variation selectors,
 *  ZWJ, keycap caps), 1..16 CODE POINTS (a String iterates UTF-16 units, so
 *  the scan decodes surrogate pairs — Windows parity), and at least one real
 *  emoji character (not only modifiers). Local rendering hint only. */
fun isBigEmoji(content: String?): Boolean {
    val text = (content ?: "").trim()
    if (text.isEmpty()) return false
    val codePoints = ArrayList<Int>(text.length)
    var i = 0
    while (i < text.length) {
        val cp = text.codePointAt(i)
        codePoints.add(cp)
        i += Character.charCount(cp)
    }
    if (codePoints.size > 16) return false
    if (!codePoints.all { isEmojiCodePoint(it) }) return false
    return codePoints.any {
        it >= 0x1F000 || it in 0x2600..0x27BF || it in 0x2B00..0x2BFF ||
            it == 0x2764 || it == 0xA9 || it == 0xAE || it == 0x2122
    }
}

/**
 * Folder transfer: a folder is offered as one file_message per entry (so old
 * peers still see ordinary downloadable files), each carrying the same
 * folderId plus its folderName/relativePath/folderTotal. Receivers group the
 * entries by folderId and rebuild the tree under a directory the user picks.
 * Mirrors the Windows client's models.MAX_FOLDER_FILES / sanitize_relative_path
 * / sanitize_folder_id so both implementations agree on every wire value.
 */
const val MAX_FOLDER_FILES = 1000
const val MAX_RELATIVE_PATH_LENGTH = 1024
private const val MAX_RELATIVE_PATH_SEGMENTS = 64
private const val MAX_PATH_SEGMENT_LENGTH = 255
private const val FOLDER_ID_CHARS =
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

/** Folder ids are sender-generated opaque keys (uuid4). Keep only a safe
 *  alphabet so a crafted id can never do anything but group messages. Mirrors
 *  Windows sanitize_folder_id. */
fun sanitizeFolderId(value: String): String =
    value.filter { it in FOLDER_ID_CHARS }.take(64)

/** Windows sanitize_file_name for one path segment: normalize separators and
 *  keep only the basename, strip control characters, drop trailing dots/spaces
 *  (Windows cannot round-trip them) and cap the length. Returns "file" when
 *  nothing usable remains — exactly like the Windows helper. */
private fun sanitizePathSegment(segment: String): String {
    var name = segment.replace('\\', '/').substringAfterLast('/')
    name = name.filter { ch -> ch.code >= 32 && ch.code !in 0x7F..0xA0 }
    name = name.trimEnd(' ', '.')
    if (name.length > MAX_PATH_SEGMENT_LENGTH) {
        // truncation can re-expose a trailing dot/space
        name = name.take(MAX_PATH_SEGMENT_LENGTH).trimEnd(' ', '.')
    }
    return name.ifEmpty { "file" }
}

/**
 * Normalize a sender-provided folder-relative path to a safe relative
 * POSIX-style path. Backslashes become "/"; empty, "." and ".." segments are
 * dropped, and each remaining segment is sanitized (separators/control chars
 * stripped, trailing dots/spaces removed) with ":" removed so a segment can
 * never become a drive-relative path. The result can therefore never be
 * absolute or escape the receiver's chosen destination directory. Returns ""
 * when nothing usable remains. Mirrors Windows sanitize_relative_path.
 */
fun sanitizeRelativePath(value: String): String {
    val text = value.replace('\\', '/')
    val parts = ArrayList<String>()
    for (raw in text.split('/')) {
        if (raw.isEmpty() || raw == "." || raw == "..") continue
        val segment = sanitizePathSegment(raw).replace(":", "")
        if (segment.isNotEmpty()) parts.add(segment)
        if (parts.size >= MAX_RELATIVE_PATH_SEGMENTS) break
    }
    var out = parts.joinToString("/")
    if (out.length > MAX_RELATIVE_PATH_LENGTH) {
        out = out.take(MAX_RELATIVE_PATH_LENGTH).trimEnd('/', ' ', '.')
    }
    return out
}

/**
 * Metadata for a file offered in chat. The file bytes themselves are NOT sent
 * over the message stream: the sender opens a short-lived download server and
 * shares its address here; receivers connect back to fetch the file (see the
 * file_download/file_meta handshake in P2PManager). [fileKey] is a random
 * per-file AES key that travels INSIDE the (already encrypted) message
 * channel and protects the raw download stream: chunk framing and GCM
 * authentication are handled by FileTransfer.
 */
@Serializable
data class FileInfo(
    val fileId: String,
    val fileName: String,
    val fileSize: Long,
    val downloadHost: String,
    val downloadPort: Int,
    val fileKey: String = "",
    val kind: String = FileKind.FILE,
    /** Folder transfer (all optional, omitted on the wire at their defaults so
     *  a plain file offer stays byte-identical): one message per entry, the
     *  same folderId on each, relativePath locating the entry inside the
     *  folder, folderName the root folder name and folderTotal the entry count
     *  (0 = unknown). Old peers ignore these fields and just see files. */
    val folderId: String = "",
    val folderName: String = "",
    val relativePath: String = "",
    val folderTotal: Int = 0
)

@Serializable
data class ChatMessage(
    val id: String,
    val content: String,
    val timestamp: Long,
    val senderId: String,
    val senderName: String,
    val fileInfo: FileInfo? = null,
    /** Reply/quote (optional, omitted from the wire when unset so a plain
     *  message stays byte-identical, Windows parity): [replyTo] is the quoted
     *  message's id, [replyPreview] a short snippet of its content and
     *  [replySender] the original sender's display name. All three are
     *  self-contained so the receiver can render the quote even when the
     *  referenced message is not in its local history. */
    val replyTo: String? = null,
    val replyPreview: String? = null,
    val replySender: String? = null,
    /** @-mentioned peer ids ("all" = everyone). Optional, omitted from the
     *  wire when unset so a plain message stays byte-identical (Windows
     *  parity). */
    val mentions: List<String>? = null,
    /** True when the author replaced this message's content via edit_message
     *  (carried on the wire only when true). */
    val edited: Boolean = false,
    /** Group sender identity binding (TOFU, see network.GroupAuth): the
     *  author's long-term identity public key (Base64 SPKI, same key as the
     *  direct-mode handshake) plus the ECDSA signature over the group signing
     *  transcript. Optional, omitted from the wire when unset so a plain
     *  message stays byte-identical (Windows parity). Direct chats never set
     *  them. */
    val senderPubId: String? = null,
    val senderSig: String? = null,
    @Transient val isFromMe: Boolean = false,
    /** Local-only delivery state (like [isFromMe], never sent over the
     *  wire): true while an offline-sent message still waits in the direct
     *  chat outbox for the peer to come online. */
    @Transient val pending: Boolean = false,
    /** Local-only read state for OWN direct-chat messages: flipped when the
     *  peer's read_receipt covers this message. Never sent over the wire;
     *  group chats do not track per-reader receipts (Windows parity). */
    @Transient val read: Boolean = false
)

/** Reply/quote preview cap: the snippet copied into [ChatMessage.replyPreview]
 *  (and persisted) so one huge quoted message cannot bloat every reply
 *  packet. Mirrors Windows models.MAX_REPLY_PREVIEW. */
const val MAX_REPLY_PREVIEW = 120

/** The quote snippet for replying to this message: newlines flattened and
 *  capped at [MAX_REPLY_PREVIEW] (Windows parity); a file message with no
 *  text falls back to the file name (same as the reply bar shows). */
fun ChatMessage.replyPreviewText(): String {
    val base = content.replace('\n', ' ').trim()
    val preview = if (base.isNotEmpty() || fileInfo == null) base else fileInfo.fileName
    return preview.take(MAX_REPLY_PREVIEW)
}

/** Inbound advisory metadata must never be trusted: a forged folderTotal (a
 *  display-only entry count) decodes to 0 = unknown once it exceeds the cap —
 *  mirrors the Windows FileInfo.from_dict clamp so both platforms agree on
 *  every wire value. Unknown kinds degrade to a plain file (Windows
 *  normalize_media_kind parity). */
fun FileInfo.sanitized(): FileInfo {
    val capped = if (folderTotal > MAX_FOLDER_FILES) copy(folderTotal = 0) else this
    return if (normalizeMediaKind(capped.kind) != capped.kind) {
        capped.copy(kind = normalizeMediaKind(capped.kind))
    } else capped
}

/** Clamp every sender-controlled extra an inbound message carries — advisory
 *  folder metadata (see [FileInfo.sanitized]) AND the mentions list (dedupe,
 *  64 entries x 128 chars, Windows `sanitize_mentions` parity). Apply where a
 *  decoded message is accepted, so nothing downstream (memory, storage,
 *  render) ever sees an unbounded list. */
fun ChatMessage.withSanitizedExtras(): ChatMessage = copy(
    fileInfo = fileInfo?.sanitized(),
    mentions = sanitizeMentions(mentions)
)

/**
 * Metadata for a video/audio call.
 * Serialized with kotlinx defaults: fields equal to their default value
 * (mediaPort=0, accepted=true, audioEnabled=true, media=null, meetingId=null)
 * are omitted, matching the Python side's output.
 *
 * [media] is "audio" or "video"; null means video (omitted on the wire so a
 * plain video offer stays byte-identical to the pre-media-field format).
 *
 * [meetingId] binds group voice conference signaling (group_call_*) and the
 * conference media hello to one meeting; null for 1:1 calls (omitted on the
 * wire, so 1:1 call packets stay byte-identical).
 */
@Serializable
data class CallInfo(
    val callId: String,
    val callerId: String,
    val callerName: String,
    val calleeId: String,
    val mediaPort: Int = 0,
    val accepted: Boolean = true,
    val audioEnabled: Boolean = true,
    val media: String? = null,
    val meetingId: String? = null
)

/** Call media kinds carried by CallInfo.media. */
object CallMedia {
    const val AUDIO = "audio"
    const val VIDEO = "video"
}

/** Call-log direction/result vocabulary (local-only, never sent over the
 *  wire): mirrors the Windows client's models constants. */
object CallDirection {
    const val INCOMING = "incoming"
    const val OUTGOING = "outgoing"
}

object CallResult {
    const val ANSWERED = "answered"
    const val MISSED = "missed"
    const val REJECTED = "rejected"
    const val CANCELLED = "cancelled"
    const val FAILED = "failed"
}
