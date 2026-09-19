package com.zqr.localchat.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.Transient

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
}

/** Image extensions recognized for media classification (lowercase, with
 *  dot); mirrors the Windows client's models.IMAGE_EXTENSIONS. */
val IMAGE_EXTENSIONS = setOf(".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic")

/** Video extensions recognized for media classification (lowercase, with
 *  dot); mirrors the Windows client's models.VIDEO_EXTENSIONS. */
val VIDEO_EXTENSIONS = setOf(".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp")

/** Classify a file by name for the send path: "image", "video" or "file". */
fun detectMediaKind(fileName: String): String {
    val ext = fileName.substringAfterLast('.', "").lowercase()
    if (ext.isEmpty()) return FileKind.FILE
    return when {
        ".$ext" in IMAGE_EXTENSIONS -> FileKind.IMAGE
        ".$ext" in VIDEO_EXTENSIONS -> FileKind.VIDEO
        else -> FileKind.FILE
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
 *  every wire value. */
fun FileInfo.sanitized(): FileInfo =
    if (folderTotal > MAX_FOLDER_FILES) copy(folderTotal = 0) else this

/** Clamp the advisory folder metadata carried by an inbound message (see
 *  [FileInfo.sanitized]); apply where a decoded file_message is accepted. */
fun ChatMessage.withSanitizedFileInfo(): ChatMessage =
    fileInfo?.let { copy(fileInfo = it.sanitized()) } ?: this

/**
 * Metadata for a video/audio call.
 * Serialized with kotlinx defaults: fields equal to their default value
 * (mediaPort=0, accepted=true, audioEnabled=true, media=null) are omitted,
 * matching the Python side's output.
 *
 * [media] is "audio" or "video"; null means video (omitted on the wire so a
 * plain video offer stays byte-identical to the pre-media-field format).
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
    val media: String? = null
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
