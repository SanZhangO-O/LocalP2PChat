package com.zqr.localchat.ui.screen

import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Phone
import androidx.compose.material.icons.filled.Reply
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.CallLogEntity
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.replyPreviewText
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
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
    onRevealHandled: () -> Unit = {},
    // ---- message experience (reactions / pins / edit / voice)
    /** {msgId: [(emoji, actorId), …]} recorded reactions of this chat. */
    reactions: Map<String, List<Pair<String, String>>> = emptyMap(),
    /** Pinned rows, oldest first (banner shows the last). */
    pins: List<com.zqr.localchat.data.PinnedMessage> = emptyList(),
    myDeviceId: String = "",
    onToggleReaction: (String, String, Boolean) -> Unit = { _, _, _ -> },
    onTogglePin: (String, Boolean) -> Unit = { _, _ -> },
    /** Author-only text edit; false = not delivered (peer offline). */
    onEditMessage: (String, String) -> Boolean = { _, _ -> false },
    /** Send a recorded WAV file path as a voice message (file channel). */
    onSendVoice: (String) -> Unit = {}
) {
    val context = LocalContext.current
    var input by rememberSaveable(stateSaver = TextFieldValue.Saver) {
        mutableStateOf(TextFieldValue(""))
    }
    var pendingDelete by remember { mutableStateOf<ChatMessage?>(null) }
    var pendingFolderDelete by remember { mutableStateOf<FolderGroup?>(null) }    // Message being quoted by the next send (null = plain message).
    var replyTarget by remember { mutableStateOf<ChatMessage?>(null) }
    var searchActive by rememberSaveable { mutableStateOf(false) }
    var searchQuery by rememberSaveable { mutableStateOf("") }
    var highlightId by remember { mutableStateOf<String?>(null) }
    var revealConsumed by remember(revealMessageId) { mutableStateOf(false) }
    var emojiPickerOpen by remember { mutableStateOf(false) }
    var recentEmoji by remember { mutableStateOf(RecentEmoji.load(context)) }
    // Voice capture + playback (WAV 16 kHz mono, Windows parity).
    val voiceDir = remember { java.io.File(context.cacheDir, "voice") }
    val voiceRecorder = remember { com.zqr.localchat.ui.VoiceRecorder(voiceDir) }
    val voicePlayer = remember { com.zqr.localchat.ui.VoicePlayer() }
    var voiceRecording by remember { mutableStateOf(false) }
    var voiceSeconds by remember { mutableStateOf(0) }
    var playingVoiceId by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    // previous sessions' recordings can never be offered again (Windows
    // parity): prune them off the main thread
    LaunchedEffect(Unit) {
        withContext(Dispatchers.IO) {
            com.zqr.localchat.ui.VoiceNotes.pruneRecordings(voiceDir)
        }
    }
    /** Begin capture; the caller has already ensured the mic permission. */
    fun startVoiceRecording() {
        if (voiceRecording) return
        if (voiceRecorder.start()) {
            voiceRecording = true
            voiceSeconds = 0
        } else {
            Toast.makeText(
                context,
                "无法开始录音：请先授予麦克风权限",
                Toast.LENGTH_SHORT
            ).show()
        }
    }
    // RECORD_AUDIO is a runtime permission, requested by ChatInputBar.
    // Leaving the screen must not keep the microphone capturing or the
    // MediaPlayer playing (both are native resources). onFinished is bound in
    // an effect (not the composition body) and unbound on dispose.
    DisposableEffect(Unit) {
        voicePlayer.onFinished = { playingVoiceId = null }
        onDispose {
            voicePlayer.onFinished = null
            voicePlayer.stop()
            voiceRecorder.cancel()
        }
    }
    /** Stop capture off the main thread and offer the clip to the peer. */
    fun finishVoiceRecording() {
        scope.launch {
            // stop() joins the capture thread: never on the main thread (ANR)
            val path = withContext(Dispatchers.IO) { voiceRecorder.stop() }
            voiceRecording = false
            if (path.isNotEmpty() && connected) onSendVoice(path)
        }
    }
    LaunchedEffect(voiceRecording) {
        while (voiceRecording) {
            delay(250)
            voiceSeconds = voiceRecorder.elapsedSeconds()
            if (voiceSeconds >= 60) {
                // 1-minute cap like the Windows client: stop AND send the clip
                finishVoiceRecording()
                break
            }
        }
    }
    val tooLong = input.text.length > P2PManager.MAX_CONTENT_LENGTH
    val listState = rememberLazyListState()
    // Folder grouping + call-log merge is O(n log n): compute once per
    // message-list change instead of on every recomposition
    val messageItems = remember(messages, callLogs) { buildDirectMessageItems(messages, callLogs) }
    val pinnedIds = remember(pins) { pins.map { it.msgId }.toSet() }

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
            // Pinned-message banner: newest pin of this 1:1 chat (both sides
            // may pin; tap jumps to it, ✕ unpins). Without it a pin had no
            // visible effect at all.
            if (pins.isNotEmpty()) {
                val newest = pins.last()
                val pinnedPreview = remember(pins, messages) {
                    val raw = messages.firstOrNull { it.id == newest.msgId }
                        ?.let { m ->
                            (m.content.ifBlank { m.fileInfo?.fileName ?: "" })
                                .replace("\n", " ")
                        }
                        .orEmpty()
                    if (raw.isEmpty()) "（消息不在本地记录中）"
                    else com.zqr.localchat.data.takeCodePoints(raw, 40)
                }
                val pinnedBy = newest.pinnedBy.takeIf { it.isNotBlank() }?.let { " $it" } ?: ""
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 12.dp, vertical = 4.dp)
                        .clip(RoundedCornerShape(8.dp))
                        .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f))
                        .clickable { revealInList(newest.msgId) }
                        .padding(horizontal = 8.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = "📌$pinnedBy 置顶：$pinnedPreview",
                        fontSize = 12.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f)
                    )
                    TextButton(onClick = { onTogglePin(newest.msgId, false) }) {
                        Text("取消", fontSize = 12.sp)
                    }
                }
            }
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
                                val myReactions = reactions[msg.id]
                                    ?.filter { it.second == myDeviceId }
                                    ?.map { it.first }?.toSet() ?: emptySet()
                                val reactionList = reactions[msg.id]
                                    ?.fold(linkedMapOf<String, Int>()) { acc, (emoji, _) ->
                                        acc[emoji] = (acc[emoji] ?: 0) + 1
                                        acc
                                    }?.map { it.key to it.value } ?: emptyList()
                                if (fi != null && fi.kind == FileKind.AUDIO) {
                                    val saved =
                                        downloadStates[msg.id] as? ChatViewModel.DownloadState.Done
                                    VoiceMessageBubble(
                                        message = msg,
                                        state = downloadStates[msg.id],
                                        localPath = saved?.uri ?: resolveMedia(fi),
                                        playing = playingVoiceId == msg.id,
                                        isFromMe = msg.isFromMe,
                                        reactions = reactionList,
                                        myReactions = myReactions,
                                        pinned = msg.id in pinnedIds,
                                        onTogglePlay = {
                                            val path = saved?.uri ?: resolveMedia(fi)
                                            if (path != null) {
                                                if (playingVoiceId == msg.id) {
                                                    voicePlayer.stop()
                                                    playingVoiceId = null
                                                } else if (voicePlayer.play(path)) {
                                                    playingVoiceId = msg.id
                                                } else onOpenFile(path)
                                            } else onDownloadMedia(fi)
                                        },
                                        onReact = { emoji, active ->
                                            onToggleReaction(msg.id, emoji, active)
                                        },
                                        onPin = { active -> onTogglePin(msg.id, active) },
                                        onDelete = { pendingDelete = msg }
                                    )
                                } else if (fi != null && (fi.kind == FileKind.IMAGE || fi.kind == FileKind.VIDEO)) {
                                    val saved =
                                        downloadStates[msg.id] as? ChatViewModel.DownloadState.Done
                                    Column(horizontalAlignment = if (msg.isFromMe) Alignment.End else Alignment.Start) {
                                        MediaMessageBubble(
                                            message = msg,
                                            state = downloadStates[msg.id],
                                            localPath = saved?.uri ?: resolveMedia(fi),
                                            onDownload = { onDownloadMedia(fi) },
                                            onSaveAs = { onDownloadFile(fi) },
                                            onOpen = onOpenFile,
                                            onDelete = { pendingDelete = msg }
                                        )
                                        ReactionPills(
                                            message = msg,
                                            reactions = reactionList,
                                            myReactions = myReactions,
                                            onToggleReaction = onToggleReaction,
                                            readLabel = "",
                                            edited = msg.edited
                                        )
                                    }
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
                                        onDelete = { pendingDelete = msg },
                                        reactions = reactionList,
                                        myReactions = myReactions,
                                        pinned = msg.id in pinnedIds,
                                        onToggleReaction = onToggleReaction,
                                        onTogglePin = { active -> onTogglePin(msg.id, active) },
                                        onEdit = if (msg.isFromMe && !msg.pending) {
                                            { text -> onEditMessage(msg.id, text) }
                                        } else null
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
            ChatInputBar(
                value = input,
                onValueChange = {
                    input = it
                    if (it.text.isNotBlank()) onTyping()
                },
                canSend = input.text.isNotBlank() && !tooLong,
                onSend = {
                    if (input.text.isNotBlank() && !tooLong) {
                        if (onSend(input.text, replyTarget)) {
                            input = TextFieldValue("")
                            replyTarget = null
                        } else {
                            Toast.makeText(context, "发送失败", Toast.LENGTH_SHORT).show()
                        }
                    }
                },
                actionsEnabled = connected,
                onPickFile = onPickFile,
                onPickImage = onPickImage,
                onPickVideo = onPickVideo,
                onPickFolder = onPickFolder,
                onEmoji = { emojiPickerOpen = true },
                onVoiceStart = { startVoiceRecording() },
                onVoiceStop = { finishVoiceRecording() },
                voiceRecording = voiceRecording,
                voiceSeconds = voiceSeconds,
                maxLines = 4,
                imeAction = ImeAction.Default,
                isError = tooLong,
                supportingText = if (tooLong) {
                    { Text("消息过长（最多 ${P2PManager.MAX_CONTENT_LENGTH} 字）") }
                } else {
                    null
                },
                modifier = Modifier.imePadding()
            )
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
    onDelete: () -> Unit,
    reactions: List<Pair<String, Int>> = emptyList(),
    myReactions: Set<String> = emptySet(),
    pinned: Boolean = false,
    onToggleReaction: (String, String, Boolean) -> Unit = { _, _, _ -> },
    onTogglePin: (Boolean) -> Unit = {},
    onEdit: ((String) -> Boolean)? = null
) {
    val mine = msg.isFromMe
    var showMenu by remember { mutableStateOf(false) }
    var showReactionPicker by remember { mutableStateOf(false) }
    var showEditDialog by remember { mutableStateOf(false) }
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
                    if (msg.edited) {
                        Text(
                            text = "已编辑 · ",
                            fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.7f),
                            modifier = Modifier.padding(end = 0.dp)
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
                onDismissRequest = { showMenu = false; showReactionPicker = false }
            ) {
                if (showReactionPicker) {
                    Row(modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
                        REACTION_CHOICES.forEach { emoji ->
                            TextButton(onClick = {
                                onToggleReaction(msg.id, emoji, emoji !in myReactions)
                                showMenu = false
                                showReactionPicker = false
                            }) {
                                Text(emoji, fontSize = 20.sp)
                            }
                        }
                    }
                } else {
                    DropdownMenuItem(
                        text = { Text("回应") },
                        onClick = { showReactionPicker = true }
                    )
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
                    if (onEdit != null && mine && !msg.pending) {
                        DropdownMenuItem(
                            text = { Text("编辑") },
                            onClick = {
                                showMenu = false
                                showEditDialog = true
                            }
                        )
                    }
                    DropdownMenuItem(
                        text = { Text(if (pinned) "取消置顶" else "置顶") },
                        onClick = { onTogglePin(!pinned); showMenu = false }
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
        ReactionPills(
            message = msg,
            reactions = reactions,
            myReactions = myReactions,
            onToggleReaction = onToggleReaction,
            readLabel = "",
            edited = false
        )
    }
    if (showEditDialog) {
        var editDraft by remember { mutableStateOf(msg.content) }
        AlertDialog(
            onDismissRequest = { showEditDialog = false },
            title = { Text("编辑消息") },
            text = {
                OutlinedTextField(
                    value = editDraft,
                    onValueChange = { editDraft = it },
                    minLines = 2,
                    maxLines = 6
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    val text = editDraft.trim()
                    if (text.isNotEmpty() && onEdit?.invoke(text) == true) showEditDialog = false
                }) { Text("保存") }
            },
            dismissButton = {
                TextButton(onClick = { showEditDialog = false }) { Text("取消") }
            }
        )
    }
}

private fun timeText(timestamp: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(timestamp))
