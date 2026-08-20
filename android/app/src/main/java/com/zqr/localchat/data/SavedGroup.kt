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
    val createdAt: Long = System.currentTimeMillis()
)
