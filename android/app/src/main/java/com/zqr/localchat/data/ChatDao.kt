package com.zqr.localchat.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow

@Dao
interface ChatDao {
    /**
     * IGNORE (never REPLACE) for the group parent row: saved_messages has an
     * ON DELETE CASCADE foreign key, and REPLACE is delete+insert under the
     * hood — a concurrent re-insert of the same group could cascade-delete
     * messages that were just persisted. IGNORE makes concurrent first
     * inserts safe (the second is a no-op); updates go through [updateGroup].
     */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertGroup(group: SavedGroup)

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertMessages(messages: List<SavedChatMessage>)

    @Query("SELECT * FROM saved_groups ORDER BY createdAt DESC")
    fun getAllGroups(): Flow<List<SavedGroup>>

    @Query("SELECT * FROM saved_groups WHERE groupId = :groupId LIMIT 1")
    suspend fun getGroup(groupId: String): SavedGroup?

    @Query(
        "UPDATE saved_groups SET groupName = :groupName, isHost = :isHost, hostIp = :hostIp, " +
            "hostPort = :hostPort, myName = :myName, memberCount = :memberCount, " +
            "lastMessage = :lastMessage, lastMessageTime = :lastMessageTime WHERE groupId = :groupId"
    )
    suspend fun updateGroup(
        groupId: String,
        groupName: String,
        isHost: Boolean,
        hostIp: String,
        hostPort: Int,
        myName: String,
        memberCount: Int,
        lastMessage: String,
        lastMessageTime: Long
    )

    @Query("SELECT * FROM saved_messages WHERE groupId = :groupId ORDER BY timestamp ASC")
    fun getMessagesForGroup(groupId: String): Flow<List<SavedChatMessage>>

    /** Owner group_update: replace the display name and announcement without
     *  touching the reconnect metadata (updateGroup never writes these
     *  columns, so a metadata refresh cannot revert a rename). */
    @Query("UPDATE saved_groups SET groupName = :groupName, announcement = :announcement WHERE groupId = :groupId")
    suspend fun updateGroupAdminInfo(groupId: String, groupName: String, announcement: String)

    /** Mark a group this member was kicked out of: the row (and its history)
     *  stays, but the group is hidden from the list. */
    @Query("UPDATE saved_groups SET kickedAt = :kickedAt WHERE groupId = :groupId")
    suspend fun markGroupKicked(groupId: String, kickedAt: Long)

    /** Undelivered (pending-send) direct-chat messages across all chats;
     *  re-queued into the outbox at process start. */
    @Query("SELECT * FROM saved_messages WHERE groupId LIKE 'direct:%' AND pending = 1 ORDER BY timestamp ASC")
    suspend fun getPendingDirectMessages(): List<SavedChatMessage>

    /** Flip the persisted delivery state of one message (queued -> sent). */
    @Query("UPDATE saved_messages SET pending = :pending WHERE groupId = :groupId AND id = :id")
    suspend fun updateMessagePending(groupId: String, id: String, pending: Boolean)

    /** Flip the persisted read state of one own direct-chat message (the
     *  peer's read_receipt covered it). */
    @Query("UPDATE saved_messages SET read = :read WHERE groupId = :groupId AND id = :id")
    suspend fun updateMessageRead(groupId: String, id: String, read: Boolean)

    /** Move every message row from one conversation key to another (used when
     *  a manually added "ip:..." placeholder chat is revealed to be a real
     *  device id by the handshake). OR REPLACE: the target chat's observer
     *  may already have re-inserted some of these rows (IGNORE strategy), and
     *  a plain UPDATE would then abort on the composite-PK conflict — leaving
     *  the source group row behind (its CASCADE cleanup would be skipped).
     *  REPLACE makes the move idempotent regardless of insert/move ordering. */
    @Query("UPDATE OR REPLACE saved_messages SET groupId = :toGroupId WHERE groupId = :fromGroupId")
    suspend fun moveMessages(fromGroupId: String, toGroupId: String)

    /**
     * One transaction removes the group row; saved_messages rows cascade via
     * the FK. Do NOT delete messages first in a separate statement — the
     * two-step version left a "group without history" state if the process
     * died between the two deletes.
     */
    @Transaction
    suspend fun deleteGroupAndMessages(groupId: String) {
        deleteGroup(groupId)
    }

    @Query("DELETE FROM saved_groups WHERE groupId = :groupId")
    suspend fun deleteGroup(groupId: String)

    @Query("DELETE FROM saved_messages WHERE groupId = :groupId AND id = :messageId")
    suspend fun deleteMessage(groupId: String, messageId: String)

    /**
     * Tombstones of deleted messages per group (offline-member delete
     * convergence): REPLACE keeps the newest deletedAt on a re-apply, so
     * recording the same delete twice is idempotent.
     */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertDeletedMessages(messages: List<DeletedMessage>)

    @Query("SELECT * FROM deleted_messages WHERE groupId = :groupId")
    suspend fun getDeletedMessages(groupId: String): List<DeletedMessage>

    /** Keep only the newest [max] tombstones of a group (bounded growth). */
    @Query(
        "DELETE FROM deleted_messages WHERE groupId = :groupId AND msgId NOT IN " +
            "(SELECT msgId FROM deleted_messages WHERE groupId = :groupId " +
            "ORDER BY deletedAt DESC LIMIT :max)"
    )
    suspend fun trimDeletedMessages(groupId: String, max: Int)

    // -------------------------------------------------------------- call log
    // Local call history (never synced): one row per finished call, keyed to
    // the 1:1 conversation with the other participant.

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertCallLog(log: CallLogEntity)

    @Query("SELECT * FROM call_logs WHERE conversationKey = :key ORDER BY startTime ASC")
    fun callLogsFor(key: String): Flow<List<CallLogEntity>>

    /** Keep only the newest [max] call logs of a conversation. */
    @Query(
        "DELETE FROM call_logs WHERE conversationKey = :key AND id NOT IN " +
            "(SELECT id FROM call_logs WHERE conversationKey = :key " +
            "ORDER BY startTime DESC LIMIT :max)"
    )
    suspend fun trimCallLogs(key: String, max: Int)

    @Query("DELETE FROM call_logs WHERE conversationKey = :key")
    suspend fun deleteCallLogs(key: String)

    /** Re-key a conversation's call logs (placeholder id -> real device id).
     *  OR REPLACE: the target chat may already hold call logs (id collisions
     *  are impossible, but REPLACE keeps the move idempotent). */
    @Query("UPDATE OR REPLACE call_logs SET conversationKey = :toKey WHERE conversationKey = :fromKey")
    suspend fun moveCallLogs(fromKey: String, toKey: String)
}
