package com.zqr.localchat.ui.screen

import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.viewmodel.ChatViewModel
import java.text.SimpleDateFormat
import java.util.*

/**
 * 1:1 direct chat with a member. Messages are exchanged over a direct TCP
 * connection that was pulled up without confirmation; the list is seeded with
 * persisted history by the ViewModel. Supports text, file transfer (shared
 * [FileMessageBubble] UI) and video/audio calls (top-bar button).
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun DirectChatScreen(
    contactName: String,
    contactIp: String,
    connected: Boolean,
    messages: List<ChatMessage>,
    downloadStates: Map<String, ChatViewModel.DownloadState> = emptyMap(),
    onBack: () -> Unit,
    onSend: (String) -> Boolean,
    onDelete: (ChatMessage) -> Unit,
    onCopy: (String) -> Unit,
    onCall: () -> Unit = {},
    onPickFile: () -> Unit = {},
    onDownloadFile: (FileInfo) -> Unit = {}
) {
    val context = LocalContext.current
    var input by remember { mutableStateOf("") }
    var pendingDelete by remember { mutableStateOf<ChatMessage?>(null) }
    val tooLong = input.length > P2PManager.MAX_CONTENT_LENGTH
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()

    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) listState.scrollToItem(messages.size - 1)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(contactName, fontSize = 17.sp, fontWeight = FontWeight.Medium)
                        Text(
                            text = if (connected) "在线" else "未连接",
                            fontSize = 11.sp,
                            color = if (connected) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
                actions = {
                    IconButton(onClick = onCall) {
                        Icon(
                            Icons.Filled.Videocam,
                            contentDescription = "视频通话",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .consumeWindowInsets(padding)
        ) {
            if (!connected) {
                // clear disconnected-state feedback, mirroring the group chat
                // banner: sending still works — messages queue as pending and
                // deliver automatically once the peer comes online
                Text(
                    text = "对方未在线：消息将暂存，对方上线后自动发送",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 4.dp)
                )
            }
            if (messages.isEmpty()) {
                Box(
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = if (connected) "已连接，开始聊天吧" else "暂无消息，可先输入发送（对方上线后送达）",
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            } else {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    items(messages, key = { it.id }) { msg ->
                        if (msg.fileInfo != null) {
                            FileMessageBubble(
                                message = msg,
                                state = downloadStates[msg.id],
                                onDownload = { onDownloadFile(msg.fileInfo!!) },
                                onDelete = { pendingDelete = msg }
                            )
                        } else {
                            DirectMessageBubble(
                                msg = msg,
                                onCopy = {
                                    onCopy(msg.content)
                                    Toast.makeText(context, "已复制", Toast.LENGTH_SHORT).show()
                                },
                                onDelete = { pendingDelete = msg }
                            )
                        }
                    }
                }
            }

            HorizontalDivider()
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 8.dp)
                    .imePadding(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(onClick = onPickFile, enabled = connected) {
                    Icon(
                        Icons.Filled.AttachFile,
                        contentDescription = "发送文件",
                        tint = if (connected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Spacer(modifier = Modifier.width(4.dp))
                OutlinedTextField(
                    value = input,
                    onValueChange = { input = it },
                    placeholder = { Text("输入消息...") },
                    modifier = Modifier.weight(1f),
                    maxLines = 4,
                    isError = tooLong,
                    supportingText = if (tooLong) {
                        { Text("消息过长（最多 ${P2PManager.MAX_CONTENT_LENGTH} 字）") }
                    } else {
                        null
                    }
                )
                Spacer(modifier = Modifier.width(8.dp))
                FilledIconButton(
                    onClick = {
                        if (input.isNotBlank() && !tooLong) {
                            if (onSend(input)) {
                                input = ""
                            } else {
                                Toast.makeText(context, "发送失败", Toast.LENGTH_SHORT).show()
                            }
                        }
                    },
                    enabled = input.isNotBlank() && !tooLong
                ) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送")
                }
            }
        }
    }

    pendingDelete?.let { msg ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除消息") },
            text = { Text("删除后，这条消息会从双方的聊天记录中移除，且无法恢复。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        onDelete(msg)
                        pendingDelete = null
                    }
                ) {
                    Text("删除", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingDelete = null }) {
                    Text("取消")
                }
            }
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun DirectMessageBubble(
    msg: ChatMessage,
    onCopy: () -> Unit,
    onDelete: () -> Unit
) {
    val mine = msg.isFromMe
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .combinedClickable(onClick = onCopy, onLongClick = { if (mine) onDelete() }),
        horizontalAlignment = if (mine) Alignment.End else Alignment.Start
    ) {
        if (!mine) {
            Text(
                text = msg.senderName,
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 6.dp, bottom = 2.dp)
            )
        }
        Box(
            modifier = Modifier
                .background(
                    color = if (mine) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(
                        topStart = 14.dp,
                        topEnd = 14.dp,
                        bottomStart = if (mine) 14.dp else 4.dp,
                        bottomEnd = if (mine) 4.dp else 14.dp
                    )
                )
                .padding(horizontal = 12.dp, vertical = 8.dp)
        ) {
            Column {
                Text(
                    text = msg.content,
                    fontSize = 15.sp,
                    color = if (mine) MaterialTheme.colorScheme.onPrimary
                    else MaterialTheme.colorScheme.onSurface
                )
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.align(Alignment.End)
                ) {
                    if (mine && msg.pending) {
                        Text(
                            text = "待送达",
                            fontSize = 10.sp,
                            color = if (mine) MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.7f)
                            else MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(end = 6.dp)
                        )
                    }
                    Text(
                        text = timeText(msg.timestamp),
                        fontSize = 10.sp,
                        color = if (mine) MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.7f)
                        else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
    }
}

private fun timeText(timestamp: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(timestamp))
