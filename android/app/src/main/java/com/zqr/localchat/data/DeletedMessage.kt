package com.zqr.localchat.data

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index

/**
 * A tombstone for a deleted message, recorded every time a delete is applied
 * locally (own delete, a relayed or meshed delete_message, a peer's
 * deletedIds cleanup). Tombstones converge deletions to members that were
 * OFFLINE when it happened: the host's join_ack and a mesh history_reply
 * carry the group's tombstoned ids, so a returning member drops what it
 * missed. The primary key is (groupId, msgId) — message ids come off the
 * wire and are only unique per group; the FK cascade removes a group's
 * tombstones together with the group. Bounded per group (see [CAP]).
 */
@Entity(
    tableName = "deleted_messages",
    primaryKeys = ["groupId", "msgId"],
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
data class DeletedMessage(
    val groupId: String,
    val msgId: String,
    val deletedAt: Long
) {
    companion object {
        /** Keep only the newest tombstones per group (bounded growth). */
        const val CAP = 200
    }
}
