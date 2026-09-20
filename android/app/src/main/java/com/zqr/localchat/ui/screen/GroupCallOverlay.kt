package com.zqr.localchat.ui.screen

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material.icons.filled.Groups
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.call.GroupCallManager

/**
 * Full-screen overlay for a group voice conference (outgoing = waiting for
 * members, active = meeting live): participant list, mute toggle and hang-up.
 * Incoming invites are handled by the accept/reject dialog in MainActivity
 * (1:1 call parity: never stack two UIs).
 */
@Composable
fun GroupCallOverlay(
    state: GroupCallManager.GroupCallState,
    participants: List<GroupCallManager.Participant>,
    audioMuted: Boolean,
    onToggleAudio: () -> Unit,
    onHangup: () -> Unit
) {
    if (state !is GroupCallManager.GroupCallState.Outgoing &&
        state !is GroupCallManager.GroupCallState.Active
    ) {
        return
    }

    val subtitle = when (state) {
        is GroupCallManager.GroupCallState.Outgoing -> state.groupName
        is GroupCallManager.GroupCallState.Active -> state.title
        else -> ""
    }

    Surface(modifier = Modifier.fillMaxSize(), color = Color(0xFF10131A)) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    Icons.Filled.Groups,
                    contentDescription = null,
                    tint = Color.White.copy(alpha = 0.85f)
                )
                Spacer(modifier = Modifier.width(10.dp))
                Column {
                    Text(
                        text = if (state is GroupCallManager.GroupCallState.Active)
                            "语音会议中"
                        else
                            "正在邀请成员加入...",
                        color = Color.White,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Medium
                    )
                    Text(
                        text = subtitle,
                        color = Color.White.copy(alpha = 0.6f),
                        fontSize = 13.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
            }

            Spacer(modifier = Modifier.height(20.dp))
            Text(
                text = "参会成员（${participants.size}）",
                color = Color.White.copy(alpha = 0.55f),
                fontSize = 13.sp
            )
            Spacer(modifier = Modifier.height(8.dp))

            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                items(participants, key = { it.id }) { p ->
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color.White.copy(alpha = 0.08f), CircleShape)
                            .padding(horizontal = 14.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Surface(
                            modifier = Modifier.size(36.dp),
                            shape = CircleShape,
                            color = Color.White.copy(alpha = 0.15f)
                        ) {
                            Box(contentAlignment = Alignment.Center) {
                                Text(
                                    text = avatarChar(p.name.ifBlank { "?" }),
                                    fontSize = 15.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = Color.White
                                )
                            }
                        }
                        Spacer(modifier = Modifier.width(10.dp))
                        Text(
                            text = if (p.self) "${p.name}（我）" else p.name,
                            color = Color.White,
                            fontSize = 15.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.weight(1f)
                        )
                        if (state is GroupCallManager.GroupCallState.Outgoing) {
                            Text(
                                text = "邀请中",
                                color = Color.White.copy(alpha = 0.5f),
                                fontSize = 12.sp
                            )
                        }
                    }
                }
            }

            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 24.dp),
                horizontalArrangement = Arrangement.spacedBy(28.dp, Alignment.CenterHorizontally),
                verticalAlignment = Alignment.CenterVertically
            ) {
                FloatingActionButton(
                    onClick = onToggleAudio,
                    containerColor = if (audioMuted)
                        MaterialTheme.colorScheme.errorContainer
                    else
                        Color.White.copy(alpha = 0.2f),
                    shape = CircleShape
                ) {
                    Icon(
                        if (audioMuted) Icons.Filled.MicOff else Icons.Filled.Mic,
                        contentDescription = if (audioMuted) "取消静音" else "静音",
                        tint = if (audioMuted) MaterialTheme.colorScheme.onErrorContainer
                        else Color.White
                    )
                }
                FloatingActionButton(
                    onClick = onHangup,
                    containerColor = Color(0xFFD32F2F),
                    shape = CircleShape
                ) {
                    Icon(
                        Icons.Filled.CallEnd,
                        contentDescription = "挂断",
                        tint = Color.White
                    )
                }
            }
        }
    }
}
