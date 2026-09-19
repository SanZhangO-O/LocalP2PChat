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

    /** Apply an author edit to one stored message: replace the body and raise
     *  the edited flag. Returns the number of affected rows (0/1). */
    @Query("UPDATE saved_messages SET content = :content, edited = 1 WHERE groupId = :groupId AND id = :id")
    suspend fun updateMessageContent(groupId: String, id: String, content: String): Int

    /** Author-gated edit (defense in depth, Windows `update_message_content`
     *  parity): a forged edit naming another author must never rewrite stored
     *  history even if a caller skipped the network-layer authorization. */
    @Query(
        "UPDATE saved_messages SET content = :content, edited = 1 " +
            "WHERE groupId = :groupId AND id = :id AND senderId = :senderId"
    )
    suspend fun updateMessageContentFrom(
        groupId: String,
        id: String,
        content: String,
        senderId: String
    ): Int

    // ------------------------------------------------- reactions / pins / reads

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun addReaction(reaction: MessageReaction)

    @Query(
        "DELETE FROM message_reactions WHERE groupId = :groupId AND msgId = :msgId " +
            "AND emoji = :emoji AND actorId = :actorId"
    )
    suspend fun removeReaction(groupId: String, msgId: String, emoji: String, actorId: String)

    @Query("SELECT * FROM message_reactions WHERE groupId = :groupId ORDER BY rowid ASC")
    suspend fun getReactions(groupId: String): List<MessageReaction>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertPin(pin: PinnedMessage)

    @Query("DELETE FROM pinned_messages WHERE groupId = :groupId AND msgId = :msgId")
    suspend fun removePin(groupId: String, msgId: String)

    @Query("SELECT * FROM pinned_messages WHERE groupId = :groupId ORDER BY pinnedAt ASC")
    suspend fun getPins(groupId: String): List<PinnedMessage>

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun addGroupReads(reads: List<GroupRead>)

    @Query("SELECT * FROM group_reads WHERE groupId = :groupId")
    suspend fun getGroupReads(groupId: String): List<GroupRead>

    /**
     * Move every message row from one conversation key to another (used when
     * a manually added "ip:..." placeholder chat is revealed to be a real
     * device id by the handshake).
     *
     * NOT a bare `UPDATE OR REPLACE saved_messages SET groupId = ...`:
     *  - the FK children (message_reactions / pinned_messages / group_reads)
     *    have ON UPDATE NO ACTION, so re-keying a referenced parent aborts the
     *    statement, and
     *  - REPLACE is delete+insert, which would cascade the children away.
     * Order matters: (1) INSERT OR IGNORE + UPDATE the parents under the new
     * key (refresh, never delete — the target's child state survives),
     * (2) re-key the children (their new parent exists now), (3) delete the
     * source parents (nothing references them anymore, so the cascade is a
     * no-op).
     *
     * Deliberately NOT SQLite UPSERT (`ON CONFLICT ... DO UPDATE`): that
     * syntax needs SQLite >= 3.24 (Android 11 / API 30) while minSdk is 24,
     * and the failure was silently swallowed by the caller's runCatching —
     * history stayed under the placeholder key on Android 7-10. INSERT OR
     * IGNORE + a correlated UPDATE works on every supported API.
     */
    @Transaction
    suspend fun moveMessages(fromGroupId: String, toGroupId: String) {
        copyMessagesToGroup(fromGroupId, toGroupId)
        refreshMovedMessages(fromGroupId, toGroupId)
        moveMessageExtras(fromGroupId, toGroupId)
        deleteMessagesInGroup(fromGroupId)
    }

    @Query(
        "INSERT OR IGNORE INTO saved_messages " +
            "(id, groupId, content, timestamp, senderId, senderName, isFromMe, " +
            "fileSize, downloadHost, downloadPort, kind, folderId, folderName, " +
            "relativePath, folderTotal, pending, replyTo, replyPreview, replySender, " +
            "read, edited, mentions) " +
            "SELECT id, :toGroupId, content, timestamp, senderId, senderName, isFromMe, " +
            "fileSize, downloadHost, downloadPort, kind, folderId, folderName, " +
            "relativePath, folderTotal, pending, replyTo, replyPreview, replySender, " +
            "read, edited, mentions FROM saved_messages WHERE groupId = :fromGroupId"
    )
    suspend fun copyMessagesToGroup(fromGroupId: String, toGroupId: String)

    /** Refresh the payload of every moved row under the target key from its
     *  source copy (the INSERT above only created missing rows). Correlated
     *  subqueries keep this portable to SQLite 3.9 (API 24). */
    @Query(
        "UPDATE saved_messages SET " +
            "content = (SELECT s.content FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "timestamp = (SELECT s.timestamp FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "senderId = (SELECT s.senderId FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "senderName = (SELECT s.senderName FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "isFromMe = (SELECT s.isFromMe FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "fileSize = (SELECT s.fileSize FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "downloadHost = (SELECT s.downloadHost FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "downloadPort = (SELECT s.downloadPort FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "kind = (SELECT s.kind FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "folderId = (SELECT s.folderId FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "folderName = (SELECT s.folderName FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "relativePath = (SELECT s.relativePath FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "folderTotal = (SELECT s.folderTotal FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "pending = (SELECT s.pending FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "replyTo = (SELECT s.replyTo FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "replyPreview = (SELECT s.replyPreview FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "replySender = (SELECT s.replySender FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "read = (SELECT s.read FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "edited = (SELECT s.edited FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id), " +
            "mentions = (SELECT s.mentions FROM saved_messages s WHERE s.groupId = :fromGroupId AND s.id = saved_messages.id) " +
            "WHERE groupId = :toGroupId AND id IN " +
            "(SELECT s.id FROM saved_messages s WHERE s.groupId = :fromGroupId)"
    )
    suspend fun refreshMovedMessages(fromGroupId: String, toGroupId: String)

    /** Re-key the message-experience children of a moved conversation; a
     *  duplicate row already present under the target key collapses into one. */
    @Query("UPDATE OR REPLACE message_reactions SET groupId = :toGroupId WHERE groupId = :fromGroupId")
    suspend fun moveReactions(fromGroupId: String, toGroupId: String)

    @Query("UPDATE OR REPLACE pinned_messages SET groupId = :toGroupId WHERE groupId = :fromGroupId")
    suspend fun movePins(fromGroupId: String, toGroupId: String)

    @Query("UPDATE OR REPLACE group_reads SET groupId = :toGroupId WHERE groupId = :fromGroupId")
    suspend fun moveGroupReads(fromGroupId: String, toGroupId: String)

    @Transaction
    suspend fun moveMessageExtras(fromGroupId: String, toGroupId: String) {
        moveReactions(fromGroupId, toGroupId)
        movePins(fromGroupId, toGroupId)
        moveGroupReads(fromGroupId, toGroupId)
    }

    /** Delete one conversation's messages. Callers moving a conversation must
     *  run [moveMessageExtras] first: the FK cascade would otherwise drop the
     *  child rows still keyed to the source. */
    @Query("DELETE FROM saved_messages WHERE groupId = :fromGroupId")
    suspend fun deleteMessagesInGroup(fromGroupId: String)

    /** The timestamp of one stored message (null when absent): the cheap
     *  lookup behind a group read receipt, instead of decrypting the whole
     *  conversation. */
    @Query("SELECT timestamp FROM saved_messages WHERE groupId = :groupId AND id = :id LIMIT 1")
    suspend fun messageTimestamp(groupId: String, id: String): Long?

    /** Ids of own messages at or before [timestamp] — the receipt cut-off. */
    @Query(
        "SELECT id FROM saved_messages WHERE groupId = :groupId AND isFromMe = 1 " +
            "AND timestamp <= :timestamp"
    )
    suspend fun ownMessageIdsUpTo(groupId: String, timestamp: Long): List<String>

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

    /**
     * Search prefilter over the columns that are plaintext at rest (sender
     * name, file relative path, folder name), optionally restricted to one
     * conversation ([groupId] null = every conversation). [pattern] is a LIKE
     * pattern with `%`/`_`/`\` already escaped by
     * [com.zqr.localchat.data.MessageSearch].
     *
     * Message bodies are encrypted at rest (StoreCipher, "enc1:..."), so a
     * LIKE on `content` would silently match nothing — the body pass runs over
     * [searchScopeRows] with the decrypted text instead.
     */
    @Query(
        "SELECT * FROM saved_messages WHERE (:groupId IS NULL OR groupId = :groupId) AND " +
            "(senderName LIKE :pattern ESCAPE '\\' OR relativePath LIKE :pattern ESCAPE '\\' " +
            "OR folderName LIKE :pattern ESCAPE '\\') ORDER BY timestamp DESC LIMIT :limit"
    )
    suspend fun searchByNameColumns(
        groupId: String?,
        pattern: String,
        limit: Int
    ): List<SavedChatMessage>

    /**
     * Every row of one conversation (all conversations when [groupId] is null),
     * newest first: the body pass of the search decrypts and matches in code.
     */
    @Query(
        "SELECT * FROM saved_messages WHERE (:groupId IS NULL OR groupId = :groupId) " +
            "ORDER BY timestamp DESC"
    )
    suspend fun searchScopeRows(groupId: String?): List<SavedChatMessage>
}
