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
    val kind: String = FileKind.FILE
)

@Serializable
data class ChatMessage(
    val id: String,
    val content: String,
    val timestamp: Long,
    val senderId: String,
    val senderName: String,
    val fileInfo: FileInfo? = null,
    @Transient val isFromMe: Boolean = false,
    /** Local-only delivery state (like [isFromMe], never sent over the
     *  wire): true while an offline-sent message still waits in the direct
     *  chat outbox for the peer to come online. */
    @Transient val pending: Boolean = false
)

/**
 * Metadata for a video/audio call.
 * Serialized with kotlinx defaults: fields equal to their default value
 * (mediaPort=0, accepted=true, audioEnabled=true) are omitted, matching the
 * Python side's output.
 */
@Serializable
data class CallInfo(
    val callId: String,
    val callerId: String,
    val callerName: String,
    val calleeId: String,
    val mediaPort: Int = 0,
    val accepted: Boolean = true,
    val audioEnabled: Boolean = true
)
