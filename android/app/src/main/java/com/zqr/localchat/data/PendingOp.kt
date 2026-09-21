package com.zqr.localchat.data

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index

/**
 * One staged offline message-experience op (edit) for a group, replayed in
 * staging order once the group becomes reachable again (Windows
 * `pending_ops` parity, LESSONS 2026-09-21). Idempotent by the primary key:
 * staging the same op again updates the payload in place. The FK cascade
 * removes a staged op when its target message is deleted locally — a replay
 * would find nothing to edit anyway.
 *
 * [content] holds conversation text and is encrypted at rest
 * (StoreCipher, "enc1:..."), exactly like message bodies.
 */
@Entity(
    tableName = "pending_ops",
    primaryKeys = ["groupId", "kind", "msgId", "emoji"],
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
data class PendingOp(
    val groupId: String,
    /** Op kind, "edit" today (reactions/pins stay advisory, no queue). */
    val kind: String,
    val msgId: String,
    /** Part of the idempotency key (Windows parity); empty for edits. */
    val emoji: String = "",
    /** Toggle payloads for future kinds; 0 for edits. */
    val active: Boolean = false,
    val content: String = "",
    val createdAt: Long
)
