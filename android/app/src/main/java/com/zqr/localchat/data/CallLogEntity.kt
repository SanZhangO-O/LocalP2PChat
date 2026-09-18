package com.zqr.localchat.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * One local call-log entry (Room). Never sent over the wire and never synced
 * between devices — this is the user's own call history, keyed to the 1:1
 * conversation with the other participant ("direct:<peerId>"), exactly like
 * the Windows ChatStore.call_logs table.
 */
@Entity(
    tableName = "call_logs",
    indices = [Index("conversationKey")]
)
data class CallLogEntity(
    @PrimaryKey val id: String,
    val conversationKey: String,
    val peerId: String,
    val peerName: String,
    /** [CallDirection.INCOMING] | [CallDirection.OUTGOING] */
    val direction: String,
    /** [CallResult.ANSWERED] | MISSED | REJECTED | CANCELLED | FAILED */
    val result: String,
    /** [CallMedia.AUDIO] | [CallMedia.VIDEO] */
    val media: String = CallMedia.VIDEO,
    /** Epoch ms when the call was initiated/received. */
    val startTime: Long,
    /** Seconds actually connected (0 when the call was never answered). */
    val duration: Long = 0L
)
