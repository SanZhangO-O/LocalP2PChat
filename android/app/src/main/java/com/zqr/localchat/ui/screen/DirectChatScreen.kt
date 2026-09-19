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
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.EmojiEmotions
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Movie
import androidx.compose.material.icons.filled.Phone
import androidx.compose.material.icons.filled.Reply
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.CallLogEntity
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.replyPreviewText
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
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
    /** Local call history of this conversation, interleaved as system rows. */
    callLogs: List<CallLogEntity> = emptyList(),
    downloadStates: Map<String, ChatViewModel.DownloadState> = emptyMap(),
    /** True while the peer is typing ("对方正在输入…" in the header). */
    peerTyping: Boolean = false,
    onBack: () -> Unit,
    /** Send the draft, optionally quoting [replyTo] (null = plain message). */
    onSend: (String, ChatMessage?) -> Boolean,
    onDelete: (ChatMessage) -> Unit,
    onCopy: (String) -> Unit,
    onCall: () -> Unit = {},
    /** Start a voice-only call with this contact. */
    onCallAudio: () -> Unit = {},
    /** Clicking a call-log line redials with that entry's media kind. */
    onCallBack: (String) -> Unit = {},
    onPickFile: () -> Unit = {},
    onPickImage: () -> Unit = {},
    onPickVideo: () -> Unit = {},
    onPickFolder: () -> Unit = {},
    onDownloadFile: (FileInfo) -> Unit = {},
    onDownloadMedia: (FileInfo) -> Unit = {},
    folderDownloadStates: Map<String, ChatViewModel.FolderDownloadState> = emptyMap(),
    onDownloadFolder: (String) -> Unit = {},
    /** Long-press delete on a folder card: confirms with the caller, which
     *  removes every entry message of the folder (parity with Windows). */
    onDeleteFolder: (FolderGroup) -> Unit = {},
    resolveMedia: (FileInfo) -> String? = { null },
    /** Bumped by the ViewModel when an own sent image lands in the media
     *  dir: re-keys the local-path lookups so the sender's own bubble flips
     *  to the inline render without any other recomposition trigger. */
    mediaVersion: Int = 0,
    onOpenFile: (String) -> Unit = {},
    /** The local user typed in the draft: refresh our typing indicator
     *  (the ViewModel throttles and auto-stops it). */
    onTyping: () -> Unit = {},
    /** A search-result jump: scroll to this message and flash-highlight it,
     *  then call [onRevealHandled] (once). */
    revealMessageId: String? = null,
    onRevealHandled: () -> Unit = {}
) {
    val context = LocalContext.current
    var input by rememberSaveable(stateSaver = TextFieldValue.Saver) {
        mutableStateOf(TextFieldValue(""))
    }
    var pendingDelete by remember { mutableStateOf<ChatMessage?>(null) }
    var pendingFolderDelete by remember { mutableStateOf<FolderGroup?>(null) }
    // Message being quoted by the next send (null = plain message).
    var replyTarget by remember { mutableStateOf<ChatMessage?>(null) }
    var searchActive by rememberSaveable { mutableStateOf(false) }
    var searchQuery by rememberSaveable { mutableStateOf("") }
    var highlightId by remember { mutableStateOf<String?>(null) }
    var revealConsumed by remember(revealMessageId) { mutableStateOf(false) }
    var emojiPickerOpen by remember { mutableStateOf(false) }
    var recentEmoji by remember { mutableStateOf(RecentEmoji.load(context)) }
    val tooLong = input.text.length > P2PManager.MAX_CONTENT_LENGTH
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    // Folder grouping + call-log merge is O(n log n): compute once per
    // message-list change instead of on every recomposition
    val messageItems = remember(messages, callLogs) { buildDirectMessageItems(messages, callLogs) }

    // in-conversation search over the loaded list (newest first)
    val searchResults = remember(searchQuery, messageItems) {
        val kw = searchQuery.trim()
        if (kw.isEmpty()) emptyList()
        else messages
            .filter { it.content.contains(kw, ignoreCase = true) || it.senderName.contains(kw, ignoreCase = true) }
            .sortedByDescending { it.timestamp }
            .take(50)
    }

    /** Scroll to a message and flash-highlight it; false when it is not in
     *  the list (yet). */
    fun revealInList(messageId: String): Boolean {
        val index = messageItems.indexOfFirst { it.matchesMessageId(messageId) }
        if (index < 0) return false
        scope.launch { listState.animateScrollToItem(index) }
        highlightId = messageId
        return true
    }

    // external search-result jump (retried while the history is still loading)
    LaunchedEffect(revealMessageId, messageItems.size) {
        val target = revealMessageId ?: return@LaunchedEffect
        if (revealConsumed) return@LaunchedEffect
        if (revealInList(target)) {
            revealConsumed = true
            onRevealHandled()
        }
    }
    LaunchedEffect(revealMessageId) {
        // The target may never appear (deleted between the search hit and the
        // jump): drop the pending jump after a bounded wait so the autoscroll
        // in the block below resumes for new messages.
        if (revealMessageId == null || revealConsumed) return@LaunchedEffect
        delay(8000)
        if (!revealConsumed) {
            revealConsumed = true
            onRevealHandled()
        }
    }
    LaunchedEffect(highlightId) {
        if (highlightId != null) {
            delay(1800)
            highlightId = null
        }
    }

    LaunchedEffect(messageItems.size) {
        // a pending search jump owns the scroll position
        if (messageItems.isNotEmpty() && !(revealMessageId != null && !revealConsumed)) {
            listState.scrollToItem(messageItems.size - 1)
        }
    }

    // a different member never inherits the previous chat's quote target
    LaunchedEffect(contactName, contactIp) { replyTarget = null }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(contactName, fontSize = 17.sp, fontWeight = FontWeight.Medium)
                        Text(
                            text = when {
                                peerTyping -> "对方正在输入…"
                                connected -> "在线"
                                else -> "未连接"
                            },
                            fontSize = 11.sp,
                            color = if (connected || peerTyping) MaterialTheme.colorScheme.primary
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
                    IconButton(
                        onClick = {
                            searchActive = !searchActive
                            if (!searchActive) searchQuery = ""
                        }
                    ) {
                        Icon(Icons.Filled.Search, contentDescription = "搜索消息")
                    }
                    IconButton(onClick = onCall) {
                        Icon(
                            Icons.Filled.Videocam,
                            contentDescription = "视频通话",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
                    }
                    IconButton(onClick = onCallAudio) {
                        Icon(
                            Icons.Filled.Phone,
                            contentDescription = "语音通话",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
                    }
                }
            )
        }
    ) { padding ->
        Box(modifier = Modifier.fillMaxSize()) {
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
            if (messageItems.isEmpty()) {
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
                    items(
                        messageItems,
                        key = { item ->
                            when (item) {
                                is MessageItem.Folder -> "folder:${item.group.folderId}"
                                is MessageItem.Msg -> item.message.id
                                is MessageItem.Call -> "call:${item.log.id}"
                            }
                        }
                    ) { item ->
                        val highlighted = highlightId != null && item.matchesMessageId(highlightId!!)
                        HighlightWrapper(highlighted) {
                        when (item) {
                            is MessageItem.Call -> {
                                // local call log: system-style line, click = redial
                                CallLogLine(
                                    log = item.log,
                                    onClick = { onCallBack(item.log.media) }
                                )
                            }
                            is MessageItem.Folder -> {
                                val group = item.group
                                FolderMessageBubble(
                                    group = group,
                                    state = folderDownloadStates[group.folderId],
                                    onSave = { onDownloadFolder(group.folderId) },
                                    onCancel = { ChatViewModel.cancelDownload(group.folderId) },
                                    onLongPress = { pendingFolderDelete = group }
                                )
                            }
                            is MessageItem.Msg -> {
                                val msg = item.message
                                val fi = msg.fileInfo
                                if (fi != null && (fi.kind == FileKind.IMAGE || fi.kind == FileKind.VIDEO)) {
                                    val saved =
                                        downloadStates[msg.id] as? ChatViewModel.DownloadState.Done
                                    MediaMessageBubble(
                                        message = msg,
                                        state = downloadStates[msg.id],
                                        localPath = saved?.uri ?: resolveMedia(fi),
                                        onDownload = { onDownloadMedia(fi) },
                                        onSaveAs = { onDownloadFile(fi) },
                                        onOpen = onOpenFile,
                                        onDelete = { pendingDelete = msg }
                                    )
                                } else if (fi != null) {
                                    FileMessageBubble(
                                        message = msg,
                                        state = downloadStates[msg.id],
                                        onDownload = { onDownloadFile(fi) },
                                        onCancel = { ChatViewModel.cancelDownload(fi.fileId) },
                                        onDelete = { pendingDelete = msg }
                                    )
                                } else {
                                    DirectMessageBubble(
                                        msg = msg,
                                        onReply = { replyTarget = it },
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
                    }
                }
            }

            HorizontalDivider()
            replyTarget?.let { target ->
                ReplyComposeBar(target = target, onCancel = { replyTarget = null })
            }
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
                IconButton(onClick = onPickImage, enabled = connected) {
                    Icon(
                        Icons.Filled.Image,
                        contentDescription = "发送图片",
                        tint = if (connected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                IconButton(onClick = onPickVideo, enabled = connected) {
                    Icon(
                        Icons.Filled.Movie,
                        contentDescription = "发送视频",
                        tint = if (connected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                IconButton(onClick = onPickFolder, enabled = connected) {
                    Icon(
                        Icons.Filled.Folder,
                        contentDescription = "发送文件夹",
                        tint = if (connected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Spacer(modifier = Modifier.width(4.dp))
                IconButton(onClick = { emojiPickerOpen = true }) {
                    Icon(
                        Icons.Filled.EmojiEmotions,
                        contentDescription = "表情",
                        tint = MaterialTheme.colorScheme.primary
                    )
                }
                OutlinedTextField(
                    value = input,
                    onValueChange = {
                        input = it
                        if (it.text.isNotBlank()) onTyping()
                    },
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
                        if (input.text.isNotBlank() && !tooLong) {
                            if (onSend(input.text, replyTarget)) {
                                input = TextFieldValue("")
                                replyTarget = null
                            } else {
                                Toast.makeText(context, "发送失败", Toast.LENGTH_SHORT).show()
                            }
                        }
                    },
                    enabled = input.text.isNotBlank() && !tooLong
                ) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送")
                }
            }
        }
        if (searchActive) {
            ChatSearchOverlay(
                query = searchQuery,
                onQueryChange = { searchQuery = it },
                results = searchResults,
                onPick = { message ->
                    searchActive = false
                    searchQuery = ""
                    revealInList(message.id)
                },
                onClose = {
                    searchActive = false
                    searchQuery = ""
                },
                // the Box spans the whole scaffold body: keep the overlay
                // below the app bar
                modifier = Modifier.padding(top = padding.calculateTopPadding())
            )
        }
        }
    }

    if (emojiPickerOpen) {
        EmojiPickerDialog(
            recent = recentEmoji,
            onPick = { emoji ->
                val (text, caret) = EmojiText.insert(
                    input.text,
                    input.selection.start,
                    input.selection.end,
                    emoji
                )
                input = TextFieldValue(text, TextRange(caret))
                recentEmoji = RecentEmoji.record(context, emoji)
            },
            onDismiss = { emojiPickerOpen = false }
        )
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

    pendingFolderDelete?.let { group ->
        AlertDialog(
            onDismissRequest = { pendingFolderDelete = null },
            title = { Text("删除文件夹") },
            text = { Text("删除后，这个文件夹的所有消息会从双方的聊天记录中移除，且无法恢复。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        onDeleteFolder(group)
                        pendingFolderDelete = null
                    }
                ) {
                    Text("删除", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingFolderDelete = null }) {
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
    onReply: (ChatMessage) -> Unit,
    onCopy: () -> Unit,
    onDelete: () -> Unit
) {
    val mine = msg.isFromMe
    var showMenu by remember { mutableStateOf(false) }
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .combinedClickable(onClick = onCopy, onLongClick = { showMenu = true }),
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
                ReplyHeader(message = msg, mine = mine)
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
                            color = MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.7f),
                            modifier = Modifier.padding(end = 6.dp)
                        )
                    } else if (mine) {
                        // direct chats only: the peer's read_receipt flips this
                        // (group chats do not track per-reader receipts)
                        Text(
                            text = if (msg.read) "已读" else "未读",
                            fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.7f),
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
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false }
            ) {
                DropdownMenuItem(
                    text = { Text("回复") },
                    onClick = {
                        onReply(msg)
                        showMenu = false
                    },
                    leadingIcon = { Icon(Icons.Filled.Reply, contentDescription = null) }
                )
                DropdownMenuItem(
                    text = { Text("复制") },
                    onClick = {
                        onCopy()
                        showMenu = false
                    },
                    leadingIcon = {
                        Icon(Icons.Filled.ContentCopy, contentDescription = null)
                    }
                )
                if (mine) {
                    DropdownMenuItem(
                        text = { Text("删除") },
                        onClick = {
                            onDelete()
                            showMenu = false
                        },
                        leadingIcon = { Icon(Icons.Filled.Delete, contentDescription = null) }
                    )
                }
            }
        }
    }
}

private fun timeText(timestamp: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(timestamp))
