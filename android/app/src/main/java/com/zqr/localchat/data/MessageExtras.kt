package com.zqr.localchat.data

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index

/**
 * One emoji reaction of one actor on one message (edit-message-experience
 * sync). FK-cascades off the message's composite key, so deleting the message
 * (or its whole group) cleans its reactions up.
 */
@Entity(
    tableName = "message_reactions",
    primaryKeys = ["groupId", "msgId", "emoji", "actorId"],
    foreignKeys = [
        ForeignKey(
            entity = SavedChatMessage::class,
            parentColumns = ["groupId", "id"],
            childColumns = ["groupId", "msgId"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [Index("groupId")]
)
data class MessageReaction(
    val groupId: String,
    val msgId: String,
    val emoji: String,
    val actorId: String
)

/**
 * Pinned-message state per conversation ("置顶"). Any member may pin (the
 * group's trust model is its password); [pinnedBy] records who did. The
 * newest pin (max pinnedAt) feeds the chat's pinned banner.
 */
@Entity(
    tableName = "pinned_messages",
    primaryKeys = ["groupId", "msgId"],
    foreignKeys = [
        ForeignKey(
            entity = SavedChatMessage::class,
            parentColumns = ["groupId", "id"],
            childColumns = ["groupId", "msgId"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [Index("groupId")]
)
data class PinnedMessage(
    val groupId: String,
    val msgId: String,
    val pinnedAt: Long,
    val pinnedBy: String
)

/**
 * One group read receipt: [readerId] has read this OWN message up to its
 * timestamp. Feeds the "已读 n/m" label on own group messages.
 */
@Entity(
    tableName = "group_reads",
    primaryKeys = ["groupId", "msgId", "readerId"],
    foreignKeys = [
        ForeignKey(
            entity = SavedChatMessage::class,
            parentColumns = ["groupId", "id"],
            childColumns = ["groupId", "msgId"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [Index("groupId")]
)
data class GroupRead(
    val groupId: String,
    val msgId: String,
    val readerId: String
)
