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
    /** True while an own direct-chat message is still waiting for the peer
     *  to come online (pending send). Restored into the outbox at startup. */
    val pending: Boolean = false
)
