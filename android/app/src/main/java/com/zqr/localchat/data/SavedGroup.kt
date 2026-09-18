package com.zqr.localchat.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "saved_groups")
data class SavedGroup(
    @PrimaryKey val groupId: String,
    val groupName: String,
    val isHost: Boolean,
    val hostIp: String = "",
    val hostPort: Int = 0,
    val myName: String = "",
    val memberCount: Int = 1,
    val lastMessage: String = "",
    val lastMessageTime: Long = 0L,
    val createdAt: Long = System.currentTimeMillis(),
    /** Owner-published announcement (group_update), shown as a lobby banner
     *  and kept across restarts / while the host is offline. */
    val announcement: String = "",
    /** Non-zero when the owner kicked this device out: the row (and its chat
     *  history) is kept, but the group is hidden from the list and can never
     *  be rejoined. */
    val kickedAt: Long = 0L
)
