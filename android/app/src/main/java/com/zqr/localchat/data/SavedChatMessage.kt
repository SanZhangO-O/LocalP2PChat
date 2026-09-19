package com.zqr.localchat.data

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index

/**
 * A persisted chat message. The primary key is (groupId, id) — NOT id alone:
 * message ids come off the wire (from other devices) and are not guaranteed
 * unique across sessions, so a bare id key would let one group's message
 * overwrite or delete another session's row.
 */
@Entity(
    tableName = "saved_messages",
    primaryKeys = ["groupId", "id"],
    foreignKeys = [
        ForeignKey(
            entity = SavedGroup::class,
            parentColumns = ["groupId"],
            childColumns = ["groupId"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [Index("groupId")]
)
data class SavedChatMessage(
    val id: String,
    val groupId: String,
    val content: String,
    val timestamp: Long,
    val senderId: String,
    val senderName: String,
    val isFromMe: Boolean,
    val fileSize: Long = 0L,
    val downloadHost: String = "",
    val downloadPort: Int = 0,
    /** File-message kind ("file" | "image" | "video"): media kinds render
     *  inline in the conversation, also after a restart. */
    val kind: String = "file",
    /** Folder transfer metadata (empty/0 for a plain file): survives restart so
     *  the grouped folder card still shows its name and entry list. Mirrors the
     *  Windows SavedMessage. */
    val folderId: String = "",
    val folderName: String = "",
    val relativePath: String = "",
    val folderTotal: Int = 0,
    /** True while an own direct-chat message is still waiting for the peer
     *  to come online (pending send). Restored into the outbox at startup. */
    val pending: Boolean = false,
    /** Reply/quote (empty when the message is not a reply): survives restart
     *  so the quoted header still renders. Mirrors the wire ChatMessage. */
    val replyTo: String = "",
    val replyPreview: String = "",
    val replySender: String = "",
    /** Own direct-chat message read by the peer (set by a read_receipt):
     *  survives restart so "已读" does not flip back after a relaunch. */
    val read: Boolean = false,
    /** Message edit: content replaced by its author (edit_message), survives
     *  restart so the 已编辑 marker stays. */
    val edited: Boolean = false,
    /** Mentioned peer ids (JSON list; "@" mentions), empty when none. */
    val mentions: String = ""
)
