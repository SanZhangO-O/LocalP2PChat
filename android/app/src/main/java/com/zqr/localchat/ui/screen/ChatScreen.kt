package com.zqr.localchat.ui.screen

import android.content.ClipData
import android.graphics.BitmapFactory
import android.media.ThumbnailUtils
import android.provider.MediaStore
import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Forward
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Movie
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.PushPin
import androidx.compose.material.icons.filled.Reply
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.AddReaction
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.CallDirection
import com.zqr.localchat.data.CallLogEntity
import com.zqr.localchat.data.CallMedia
import com.zqr.localchat.data.CallResult
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.resolveMentionIds
import com.zqr.localchat.data.replyPreviewText
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Calendar

/**
 * Synthetic conversation row: one folder card for the file messages that share
 * a folderId (a folder offer). Entries are ordered by relativePath and include
 * the sender's own offers (rendered as 已发送). Mirrors the Windows FolderGroup.
 */
// public: appears in ChatScreen/DirectChatScreen callback signatures
class FolderGroup(
    val folderId: String,
    val folderName: String,
    val entries: List<ChatMessage>
) {
    val total: Int get() = entries.size
    val size: Long get() = entries.sumOf { it.fileInfo?.fileSize ?: 0L }
    val isFromMe: Boolean get() = entries.isNotEmpty() && entries.all { it.isFromMe }
    /** An offer without a download address expired with its sender's previous
     *  session (the short-lived download server is gone). */
    val expired: Boolean get() = entries.all { it.fileInfo?.downloadHost.isNullOrBlank() }
    val timestamp: Long get() = entries.firstOrNull()?.timestamp ?: 0L
}

/** One rendered conversation row after folder grouping. */
internal sealed class MessageItem {
    abstract val timestamp: Long
    data class Msg(val message: ChatMessage) : MessageItem() {
        override val timestamp: Long get() = message.timestamp
    }
    data class Folder(val group: FolderGroup) : MessageItem() {
        override val timestamp: Long get() = group.timestamp
    }
    /** A local call-log line (system style, never a bubble, never deletable). */
    data class Call(val log: CallLogEntity) : MessageItem() {
        override val timestamp: Long get() = log.startTime
    }
}

/**
 * Expand a flat message list into conversation rows, collapsing file messages
 * that share a folderId into ONE folder row at the position of the first
 * entry (Android parity with the Windows iter_message_rows). Only the message
 * that opens a folder plus its plain neighbors survive as individual rows:
 * every later folder entry is folded into the same group and never rendered on
 * its own. Old behavior for non-folder messages is unchanged.
 */
internal fun buildMessageItems(messages: List<ChatMessage>): List<MessageItem> {
    val items = ArrayList<MessageItem>()
    val folders = LinkedHashMap<String, MutableList<ChatMessage>>()
    for (msg in messages) {
        val fi = msg.fileInfo
        if (fi != null && fi.folderId.isNotEmpty()) {
            val bucket = folders.getOrPut(fi.folderId) { mutableListOf() }
            if (bucket.isEmpty()) {
                // placeholder slot keeps the first entry's position; filled
                // after the pass once all entries are known
                bucket.add(msg)
                items.add(MessageItem.Folder(FolderGroup(fi.folderId, fi.folderName, bucket)))
            } else {
                bucket.add(msg)
            }
        } else {
            items.add(MessageItem.Msg(msg))
        }
    }
    for ((folderId, entries) in folders) {
        if (entries.isEmpty()) continue
        val sorted = entries.sortedBy {
            it.fileInfo?.relativePath?.ifEmpty { it.fileInfo?.fileName ?: "" } ?: ""
        }
        val index = items.indexOfFirst {
            it is MessageItem.Folder && it.group.folderId == folderId
        }
        if (index >= 0) {
            val name = entries.firstNotNullOfOrNull {
                it.fileInfo?.folderName?.ifBlank { null }
            } ?: "文件夹"
            items[index] = MessageItem.Folder(FolderGroup(folderId, name, sorted))
        }
    }
    return items
}

/** Display text of one call-log line ("未接来电", "视频通话 02:31", ...).
 *  Audio is called out explicitly; video is the implicit default. Mirrors the
 *  Windows call_log_text. */
internal fun callLogText(log: CallLogEntity): String {
    val base = when (log.result) {
        CallResult.ANSWERED -> {
            val seconds = log.duration.coerceAtLeast(0L)
            val mm = seconds / 60
            val ss = seconds % 60
            val hh = mm / 60
            val dur = if (hh > 0) "%d:%02d:%02d".format(hh, mm % 60, ss)
            else "%02d:%02d".format(mm, ss)
            "${if (log.media == CallMedia.AUDIO) "语音" else "视频"}通话 $dur"
        }
        CallResult.MISSED -> if (log.direction == CallDirection.INCOMING) "未接来电" else "对方未接听"
        CallResult.REJECTED -> if (log.direction == CallDirection.INCOMING) "已拒绝" else "对方已拒绝"
        CallResult.CANCELLED -> "已取消"
        else -> "通话未接通"
    }
    return if (log.media == CallMedia.AUDIO && log.result != CallResult.ANSWERED) "$base（语音）" else base
}

/**
 * Conversation rows of a direct chat: the message flow with the local
 * call-log lines interleaved by time (stable sort keeps message order for
 * equal timestamps). Call logs are local-only and never part of the message
 * protocol, so they are merged at render time.
 */
internal fun buildDirectMessageItems(
    messages: List<ChatMessage>,
    callLogs: List<CallLogEntity>
): List<MessageItem> {
    val items = buildMessageItems(messages)
    if (callLogs.isEmpty()) return items
    return (items + callLogs.map { MessageItem.Call(it) }).sortedBy { it.timestamp }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    groupId: String,
    groupName: String,
    messages: List<ChatMessage>,
    groups: List<ChatViewModel.GroupMeta>,
    connectionLost: Boolean,
    downloadStates: Map<String, ChatViewModel.DownloadState> = emptyMap(),
    /** Display names of members currently typing ("xx 正在输入…"). */
    typingNames: List<String> = emptyList(),
    /** Send the draft, optionally quoting [replyTo] (null = plain message)
     *  and mentioning [mentions] (null = nobody). */
    onSendMessage: (String, ChatMessage?, List<String>?) -> Boolean,
    onForward: (groupId: String, content: String) -> Boolean,
    onDelete: (String) -> Unit,
    onPickFile: () -> Unit = {},
    onPickImage: () -> Unit = {},
    onPickVideo: () -> Unit = {},
    onDownloadFile: (FileInfo) -> Unit = {},
    onDownloadMedia: (FileInfo) -> Unit = {},
    onPickFolder: () -> Unit = {},
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
    /** The open conversation shows the newest messages (screen opened or a
     *  batch arrived): report the group read receipt (the ViewModel dedups
     *  until a newer message arrives). */
    onVisible: () -> Unit = {},
    /** A search-result jump: scroll to this message and flash-highlight it,
     *  then call [onRevealHandled] (once). */
    revealMessageId: String? = null,
    onRevealHandled: () -> Unit = {},
    // ---- message experience (edit / reactions / pins / group receipts / mentions)
    /** {msgId: [(emoji, actorId), …]} recorded reactions of this group. */
    reactions: Map<String, List<Pair<String, String>>> = emptyMap(),
    /** Pinned rows, oldest first (banner shows the last). */
    pins: List<com.zqr.localchat.data.PinnedMessage> = emptyList(),
    /** {msgId: [readerId, …]} recorded group read receipts (own messages). */
    groupReaders: Map<String, List<String>> = emptyMap(),
    /** Other-member count for the 已读 n/m label. */
    memberCount: Int = 1,
    /** This device's id (reaction ownership / mention highlight). */
    myDeviceId: String = "",
    /** (id, name) of the group's members for the @-picker. */
    members: List<Pair<String, String>> = emptyList(),
    onToggleReaction: (String, String, Boolean) -> Unit = { _, _, _ -> },
    onTogglePin: (String, Boolean) -> Unit = { _, _ -> },
    /** Author-only text edit; false = not delivered (disconnected). */
    onEditMessage: (String, String) -> Boolean = { _, _ -> false },
    /** Send a recorded WAV file path as a voice message (file channel). */
    onSendVoice: (String) -> Unit = {},
    onBack: () -> Unit
) {
    val context = LocalContext.current
    var inputText by rememberSaveable(stateSaver = TextFieldValue.Saver) {
        mutableStateOf(TextFieldValue(""))
    }
    var pendingForward by remember { mutableStateOf<String?>(null) }
    var pendingDelete by remember { mutableStateOf<String?>(null) }
    var pendingFolderDelete by remember { mutableStateOf<FolderGroup?>(null) }
    // Message being quoted by the next send (null = plain message).
    var replyTarget by remember { mutableStateOf<ChatMessage?>(null) }
    var searchActive by rememberSaveable { mutableStateOf(false) }
    var searchQuery by rememberSaveable { mutableStateOf("") }
    var highlightId by remember { mutableStateOf<String?>(null) }
    var revealConsumed by remember(revealMessageId) { mutableStateOf(false) }
    var emojiPickerOpen by remember { mutableStateOf(false) }
    var recentEmoji by remember { mutableStateOf(RecentEmoji.load(context)) }
    // @-picker open while the draft ends with "@"
    var mentionPickerOpen by remember { mutableStateOf(false) }
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
    /** Stop capture off the main thread and offer the clip to the group. */
    fun finishVoiceRecording() {
        scope.launch {
            // stop() joins the capture thread: never on the main thread (ANR)
            val path = withContext(Dispatchers.IO) { voiceRecorder.stop() }
            voiceRecording = false
            if (path.isNotEmpty() && !connectionLost) onSendVoice(path)
        }
    }
    LaunchedEffect(voiceRecording) {
        while (voiceRecording) {
            delay(250)
            voiceSeconds = voiceRecorder.elapsedSeconds()
            if (voiceSeconds >= 60) {
                // 1-minute cap like the Windows client: stop AND send the clip
                // (breaking alone would let the mic record forever)
                finishVoiceRecording()
                break
            }
        }
    }
    val listState = rememberLazyListState()
    var shouldAutoScroll by remember(groupName) { mutableStateOf(true) }
    val snackbarHostState = remember { SnackbarHostState() }
    // Folder grouping + sorting is O(n log n): compute once per message-list
    // change instead of on every recomposition (and inside every scroll event)
    val messageItems = remember(messages) { buildMessageItems(messages) }

    val contentTooLong = inputText.text.length > P2PManager.MAX_CONTENT_LENGTH

    // 已读 n/m for OWN group messages from the recorded receipts (cut-off rule
    // matches the direct chat and Windows: timestamp <= the receipt's message).
    // One ascending pass per message-list/read change — a per-row scan would
    // make every recomposition O(rows x messages).
    val readLabels = remember(messages, groupReaders, memberCount) {
        val out = HashMap<String, String>()
        if (groupReaders.isNotEmpty() && memberCount > 0) {
            var covered = HashSet<String>()
            messages.asSequence()
                .filter { it.isFromMe }
                .sortedBy { it.timestamp }
                .forEach { m ->
                    covered.addAll(groupReaders[m.id].orEmpty())
                    val n = covered.size
                    if (n > 0) {
                        out[m.id] = if (n >= memberCount) "已读" else "已读 $n/$memberCount"
                    }
                }
        }
        out
    }

    fun readLabelFor(message: ChatMessage): String =
        if (message.isFromMe) readLabels[message.id] ?: "" else ""

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
        shouldAutoScroll = false
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
    LaunchedEffect(highlightId) {
        if (highlightId != null) {
            delay(1800)
            highlightId = null
        }
    }

    fun sendInput() {
        // The IME send action still fires while the send button is disabled:
        // never truncate silently — the error is shown, the user shortens it.
        if (contentTooLong) return
        val text = inputText.text
        if (text.isNotBlank()) {
            val mentions = resolveMentionIds(text, members)
            if (onSendMessage(text, replyTarget, mentions)) {
                inputText = TextFieldValue("")
                replyTarget = null
                mentionPickerOpen = false
            } else {
                scope.launch {
                    snackbarHostState.showSnackbar("消息未发送：已断开连接")
                }
            }
        }
    }

    LaunchedEffect(listState, messages.size) {
        snapshotFlow { listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1 }
            .collect { lastVisibleIndex ->
                val last = lastItemIndex(messageItems)
                shouldAutoScroll = last < 0 || lastVisibleIndex == -1 || lastVisibleIndex >= last - 2
            }
    }

    LaunchedEffect(messages.size, shouldAutoScroll) {
        val last = lastItemIndex(messageItems)
        if (shouldAutoScroll && last >= 0) {
            val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            if (last - lastVisible > 20) {
                listState.scrollToItem(last)
            } else {
                listState.animateScrollToItem(last)
            }
        }
    }

    // a different group never inherits the previous chat's quote target
    LaunchedEffect(groupId) { replyTarget = null }

    // the open chat shows the newest message: report a group read receipt
    // (the ViewModel dedups until a newer message arrives)
    LaunchedEffect(groupId, messages.size) { onVisible() }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(groupName)
                        if (typingNames.isNotEmpty()) {
                            Text(
                                text = typingNames.joinToString("、") + " 正在输入…",
                                fontSize = 11.sp,
                                color = MaterialTheme.colorScheme.primary,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = "返回",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
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
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    titleContentColor = MaterialTheme.colorScheme.onSurface
                )
            )
        },
        bottomBar = {
            Surface(
                tonalElevation = 3.dp,
                shadowElevation = 8.dp
            ) {
                Column(modifier = Modifier.fillMaxWidth().navigationBarsPadding().imePadding()) {
                    if (connectionLost) {
                        Text(
                            text = "与群组的连接已断开，消息无法发送",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 4.dp)
                        )
                    }
                    replyTarget?.let { target ->
                        ReplyComposeBar(target = target, onCancel = { replyTarget = null })
                    }
                    ChatInputBar(
                        value = inputText,
                        onValueChange = {
                            inputText = it
                            if (it.text.isNotBlank()) onTyping()
                            mentionPickerOpen = it.text.endsWith("@") && members.isNotEmpty()
                        },
                        canSend = inputText.text.isNotBlank() && !contentTooLong && !connectionLost,
                        onSend = { sendInput() },
                        actionsEnabled = !connectionLost,
                        onPickFile = onPickFile,
                        onPickImage = onPickImage,
                        onPickVideo = onPickVideo,
                        onPickFolder = onPickFolder,
                        onEmoji = { emojiPickerOpen = true },
                        onVoiceStart = { startVoiceRecording() },
                        onVoiceStop = { finishVoiceRecording() },
                        voiceRecording = voiceRecording,
                        voiceSeconds = voiceSeconds,
                        maxLines = 5,
                        isError = contentTooLong,
                        supportingText = if (contentTooLong) {
                            { Text("消息过长（最多 ${P2PManager.MAX_CONTENT_LENGTH} 字）") }
                        } else if (inputText.text.length > P2PManager.MAX_CONTENT_LENGTH - 200) {
                            { Text("${inputText.text.length}/${P2PManager.MAX_CONTENT_LENGTH}") }
                        } else {
                            null
                        }
                    )
            }
        }
    }
    ) { padding ->
      Box(modifier = Modifier.fillMaxSize()) {
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            // Pinned-message banner: the newest pin; tap jumps to it, ✕ unpins.
            if (pins.isNotEmpty()) {
                val newest = pins.last()
                // remembered: a per-recomposition firstOrNull() over the whole
                // list is exactly the O(rows x messages) pattern LESSONS #5
                // warns about; the cap also counts CODE POINTS (Windows
                // preview[:40]), not UTF-16 units
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
                        .clickable {
                            revealInList(newest.msgId)
                        }
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
            Box(modifier = Modifier.weight(1f)) {
                if (messages.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        text = "还没有消息",
                        fontSize = 15.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "打个招呼吧",
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
                    )
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(horizontal = 12.dp),
                state = listState,
                contentPadding = PaddingValues(vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                val items = messageItems
                itemsIndexed(items, key = { index, item ->
                    when (item) {
                        is MessageItem.Folder -> "folder:${item.group.folderId}"
                        is MessageItem.Msg -> item.message.id
                        is MessageItem.Call -> "call:${item.log.id}"
                    }
                }) { index, item ->
                    val prev = items.getOrNull(index - 1)
                    if (prev == null || !isSameDay(prev.timestamp, item.timestamp)) {
                        DateHeader(timestamp = item.timestamp)
                    }
                    val highlighted = highlightId != null && item.matchesMessageId(highlightId!!)
                    HighlightWrapper(highlighted) {
                    when (item) {
                        is MessageItem.Call -> {
                            // local call log: a system-style line, never a bubble
                            CallLogLine(log = item.log, onClick = {})
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
                            val message = item.message
                            val fi = message.fileInfo
                            val myReactions = reactions[message.id]
                                ?.filter { it.second == myDeviceId }
                                ?.map { it.first }
                                ?.toSet()
                                ?: emptySet()
                            val reactionList = reactions[message.id]
                                ?.fold(linkedMapOf<String, Int>()) { acc, (emoji, _) ->
                                    acc[emoji] = (acc[emoji] ?: 0) + 1
                                    acc
                                }
                                ?.map { it.key to it.value }
                                ?: emptyList()
                            if (fi != null && fi.kind == FileKind.AUDIO) {
                                val saved =
                                    downloadStates[message.id] as? ChatViewModel.DownloadState.Done
                                VoiceMessageBubble(
                                    message = message,
                                    state = downloadStates[message.id],
                                    localPath = saved?.uri ?: resolveMedia(fi),
                                    playing = playingVoiceId == message.id,
                                    isFromMe = message.isFromMe,
                                    reactions = reactionList,
                                    myReactions = myReactions,
                                    pinned = pins.any { it.msgId == message.id },
                                    onTogglePlay = {
                                        val path = saved?.uri ?: resolveMedia(fi)
                                        if (path != null) {
                                            if (playingVoiceId == message.id) {
                                                voicePlayer.stop()
                                                playingVoiceId = null
                                            } else if (voicePlayer.play(path)) {
                                                playingVoiceId = message.id
                                            } else {
                                                onOpenFile(path)
                                            }
                                        } else {
                                            onDownloadMedia(fi)
                                        }
                                    },
                                    onReact = { emoji, active ->
                                        onToggleReaction(message.id, emoji, active)
                                    },
                                    onPin = { active -> onTogglePin(message.id, active) },
                                    onDelete = { pendingDelete = message.id }
                                )
                            } else if (fi != null) {
                                if (fi.kind == FileKind.IMAGE || fi.kind == FileKind.VIDEO) {
                                    val saved =
                                        downloadStates[message.id] as? ChatViewModel.DownloadState.Done
                                    Column(horizontalAlignment = if (message.isFromMe) Alignment.End else Alignment.Start) {
                                        MediaMessageBubble(
                                            message = message,
                                            state = downloadStates[message.id],
                                            localPath = saved?.uri ?: resolveMedia(fi),
                                            onDownload = { onDownloadMedia(fi) },
                                            onSaveAs = { onDownloadFile(fi) },
                                            onOpen = onOpenFile,
                                            onDelete = { pendingDelete = message.id }
                                        )
                                        ReactionPills(
                                            message = message,
                                            reactions = reactionList,
                                            myReactions = myReactions,
                                            onToggleReaction = onToggleReaction,
                                            readLabel = readLabelFor(message),
                                            edited = message.edited
                                        )
                                    }
                                } else {
                                    val saved =
                                        downloadStates[message.id] as? ChatViewModel.DownloadState.Done
                                    Column(horizontalAlignment = if (message.isFromMe) Alignment.End else Alignment.Start) {
                                        FileMessageBubble(
                                            message = message,
                                            state = downloadStates[message.id],
                                            onDownload = { onDownloadFile(fi) },
                                            onOpen = saved?.let { done -> { onOpenFile(done.uri) } },
                                            onCancel = { ChatViewModel.cancelDownload(message.id) },
                                            onDelete = { pendingDelete = message.id }
                                        )
                                        ReactionPills(
                                            message = message,
                                            reactions = reactionList,
                                            myReactions = myReactions,
                                            onToggleReaction = onToggleReaction,
                                            readLabel = readLabelFor(message),
                                            edited = message.edited
                                        )
                                    }
                                }
                            } else {
                                MessageBubble(
                                    message = message,
                                    onReply = { replyTarget = it },
                                    onForward = { pendingForward = it },
                                    onDelete = { pendingDelete = it },
                                    reactions = reactionList,
                                    myReactions = myReactions,
                                    pinned = pins.any { it.msgId == message.id },
                                    mentionHighlighted = !message.isFromMe && message.mentions?.let {
                                        it.contains(myDeviceId) || it.contains(com.zqr.localchat.data.MENTION_ALL)
                                    } == true,
                                    readLabel = readLabelFor(message),
                                    onToggleReaction = onToggleReaction,
                                    onTogglePin = { active -> onTogglePin(message.id, active) },
                                    onEdit = { onEditMessage(message.id, it) }
                                )
                            }
                        }
                    }
                    }
                }
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
    }

    if (emojiPickerOpen) {
        EmojiPickerDialog(
            recent = recentEmoji,
            onPick = { emoji ->
                val (text, caret) = EmojiText.insert(
                    inputText.text,
                    inputText.selection.start,
                    inputText.selection.end,
                    emoji
                )
                inputText = TextFieldValue(text, TextRange(caret))
                recentEmoji = RecentEmoji.record(context, emoji)
            },
            onDismiss = { emojiPickerOpen = false }
        )
    }

    if (mentionPickerOpen) {
        AlertDialog(
            onDismissRequest = { mentionPickerOpen = false },
            title = { Text("@ 提及成员") },
            text = {
                Column {
                    Text(
                        text = "@所有人",
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable {
                                val t = inputText.text.removeSuffix("@") + "@所有人 "
                                inputText = TextFieldValue(t, TextRange(t.length))
                                mentionPickerOpen = false
                            }
                            .padding(vertical = 8.dp)
                    )
                    members.forEach { (id, name) ->
                        Text(
                            text = "@$name",
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable {
                                    val t = inputText.text.removeSuffix("@") + "@$name "
                                    inputText = TextFieldValue(t, TextRange(t.length))
                                    mentionPickerOpen = false
                                }
                                .padding(vertical = 8.dp)
                        )
                    }
                }
            },
            confirmButton = {},
            dismissButton = {
                TextButton(onClick = { mentionPickerOpen = false }) { Text("取消") }
            }
        )
    }

    pendingForward?.let { content ->
        ForwardDialog(
            content = content,
            currentGroupId = groupId,
            groups = groups,
            onForward = { targetId ->
                if (onForward(targetId, content)) {
                    pendingForward = null
                } else {
                    scope.launch {
                        snackbarHostState.showSnackbar("转发失败：目标群组已断开连接")
                    }
                }
            },
            onDismiss = { pendingForward = null }
        )
    }

    pendingFolderDelete?.let { group ->
        AlertDialog(
            onDismissRequest = { pendingFolderDelete = null },
            title = { Text("删除文件夹") },
            text = { Text("删除后，这个文件夹的所有消息会从群内聊天记录中移除，且无法恢复。") },
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

    pendingDelete?.let { messageId ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除消息") },
            text = { Text("删除后，这条消息会从群内所有成员的聊天记录中移除，且无法恢复。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        onDelete(messageId)
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

@Composable
private fun ForwardDialog(
    content: String,
    currentGroupId: String,
    groups: List<ChatViewModel.GroupMeta>,
    onForward: (String) -> Unit,
    onDismiss: () -> Unit
) {
    val targets = groups.filter { it.groupId != currentGroupId && it.connected }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("转发消息") },
        text = {
            Column {
                Text(
                    text = "\"${content.take(20)}${if (content.length > 20) "..." else ""}\"",
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 2
                )
                Spacer(modifier = Modifier.height(12.dp))
                if (targets.isEmpty()) {
                    Text(
                        text = "没有其他可转发的群组（未连接的群组无法转发）",
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                } else {
                    Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                        targets.forEach { group ->
                            TextButton(
                                onClick = { onForward(group.groupId) },
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text(
                                    text = group.groupName,
                                    textAlign = TextAlign.Start,
                                    maxLines = 1
                                )
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {},
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消")
            }
        }
    )
}

@Composable
private fun DateHeader(timestamp: Long) {
    val text = when {
        isSameDay(timestamp, System.currentTimeMillis()) -> "今天"
        isSameDay(timestamp, System.currentTimeMillis() - 24 * 60 * 60 * 1000L) -> "昨天"
        else -> {
            val cal = Calendar.getInstance().apply { timeInMillis = timestamp }
            val now = Calendar.getInstance()
            if (cal.get(Calendar.YEAR) == now.get(Calendar.YEAR)) {
                "${cal.get(Calendar.MONTH) + 1}月${cal.get(Calendar.DAY_OF_MONTH)}日"
            } else {
                "${cal.get(Calendar.YEAR)}年${cal.get(Calendar.MONTH) + 1}月${cal.get(Calendar.DAY_OF_MONTH)}日"
            }
        }
    }
    Box(modifier = Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        Text(
            text = text,
            fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
        )
    }
}

/** Index of the last item in the LazyColumn, accounting for inserted DateHeader items. */
private fun lastItemIndex(items: List<MessageItem>): Int {
    if (items.isEmpty()) return -1
    var headers = 0
    var prev: Long? = null
    for (m in items) {
        if (prev == null || !isSameDay(prev, m.timestamp)) headers++
        prev = m.timestamp
    }
    return items.size + headers - 1
}

private fun isSameDay(a: Long, b: Long): Boolean {
    val ca = Calendar.getInstance().apply { timeInMillis = a }
    val cb = Calendar.getInstance().apply { timeInMillis = b }
    return ca.get(Calendar.YEAR) == cb.get(Calendar.YEAR) &&
        ca.get(Calendar.DAY_OF_YEAR) == cb.get(Calendar.DAY_OF_YEAR)
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun MessageBubble(
    message: ChatMessage,
    onReply: (ChatMessage) -> Unit,
    onForward: (String) -> Unit,
    onDelete: (String) -> Unit,
    reactions: List<Pair<String, Int>> = emptyList(),
    myReactions: Set<String> = emptySet(),
    pinned: Boolean = false,
    mentionHighlighted: Boolean = false,
    readLabel: String = "",
    onToggleReaction: (String, String, Boolean) -> Unit = { _, _, _ -> },
    onTogglePin: (Boolean) -> Unit = {},
    onEdit: ((String) -> Boolean)? = null
) {
    val isFromMe = message.isFromMe
    val bigEmoji = com.zqr.localchat.data.isBigEmoji(message.content)
    val alignment = if (isFromMe) Alignment.End else Alignment.Start
    val bgColor = if (isFromMe)
        MaterialTheme.colorScheme.primary
    else
        MaterialTheme.colorScheme.surfaceVariant
    val textColor = if (isFromMe)
        MaterialTheme.colorScheme.onPrimary
    else
        MaterialTheme.colorScheme.onSurfaceVariant

    var showMenu by remember { mutableStateOf(false) }
    var showReactionPicker by remember { mutableStateOf(false) }
    var showEditDialog by remember { mutableStateOf(false) }
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    val maxBubbleWidth = (LocalConfiguration.current.screenWidthDp * 0.72f).dp

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = alignment
    ) {
        val shape = RoundedCornerShape(
            topStart = 16.dp,
            topEnd = 16.dp,
            bottomStart = if (isFromMe) 16.dp else 4.dp,
            bottomEnd = if (isFromMe) 4.dp else 16.dp
        )
        Box(
            modifier = Modifier
                .widthIn(max = maxBubbleWidth)
                .then(if (mentionHighlighted) Modifier.border(2.dp, Color(0xFFE8A13D), shape) else Modifier)
                .clip(shape)
                .background(if (bigEmoji) Color.Transparent else bgColor)
                .combinedClickable(
                    // both taps open the actions menu: the bubble is clearly
                    // interactive, so an empty onClick was misleading dead UI
                    onClick = { showMenu = true },
                    onLongClick = { showMenu = true }
                )
                .padding(horizontal = if (bigEmoji) 4.dp else 14.dp, vertical = if (bigEmoji) 2.dp else 10.dp)
        ) {
            Column(horizontalAlignment = Alignment.End) {
                if (!isFromMe && !bigEmoji) {
                    Text(
                        text = message.senderName,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Medium,
                        color = textColor.copy(alpha = 0.7f),
                        modifier = Modifier.align(Alignment.Start)
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                }
                ReplyHeader(message = message, mine = isFromMe)
                Text(
                    text = message.content,
                    color = textColor,
                    fontSize = if (bigEmoji) 40.sp else 15.sp
                )
                // Stickers keep a compact status line too: without it a big
                // emoji lost 待送达/已编辑/已读/时间 entirely (Windows keeps
                // the timestamp for stickers as well)
                val status = buildString {
                    if (message.pending) append("待送达 · ")
                    else if (readLabel.isNotEmpty()) append(readLabel + " · ")
                    if (message.edited) append("已编辑 · ")
                    append(formatTime(message.timestamp))
                }
                Text(
                    text = status,
                    color = textColor.copy(alpha = 0.6f),
                    fontSize = 11.sp,
                    modifier = Modifier.align(Alignment.End)
                )
            }
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false; showReactionPicker = false }
            ) {
                if (showReactionPicker) {
                    Row(modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
                        REACTION_CHOICES.forEach { emoji ->
                            TextButton(onClick = {
                                val active = emoji !in myReactions
                                onToggleReaction(message.id, emoji, active)
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
                        onClick = { showReactionPicker = true },
                        leadingIcon = { Icon(Icons.Default.AddReaction, contentDescription = null) }
                    )
                    DropdownMenuItem(
                        text = { Text("回复") },
                        onClick = {
                            onReply(message)
                            showMenu = false
                        },
                        leadingIcon = { Icon(Icons.Filled.Reply, contentDescription = null) }
                    )
                    DropdownMenuItem(
                        text = { Text("复制") },
                        onClick = {
                            scope.launch {
                                clipboard.setClipEntry(ClipEntry(ClipData.newPlainText("LocalChat", message.content)))
                            }
                            Toast.makeText(context, "已复制", Toast.LENGTH_SHORT).show()
                            showMenu = false
                        },
                        leadingIcon = { Icon(Icons.Default.ContentCopy, contentDescription = null) }
                    )
                    DropdownMenuItem(
                        text = { Text("转发") },
                        onClick = {
                            onForward(message.content)
                            showMenu = false
                        },
                        leadingIcon = { Icon(Icons.AutoMirrored.Filled.Forward, contentDescription = null) }
                    )
                    if (onEdit != null && isFromMe && !message.pending) {
                        DropdownMenuItem(
                            text = { Text("编辑") },
                            onClick = {
                                showMenu = false
                                showEditDialog = true
                            },
                            leadingIcon = { Icon(Icons.Default.Edit, contentDescription = null) }
                        )
                    }
                    DropdownMenuItem(
                        text = { Text(if (pinned) "取消置顶" else "置顶") },
                        onClick = {
                            onTogglePin(!pinned)
                            showMenu = false
                        },
                        leadingIcon = { Icon(Icons.Default.PushPin, contentDescription = null) }
                    )
                    if (isFromMe) {
                        DropdownMenuItem(
                            text = { Text("删除") },
                            onClick = {
                                onDelete(message.id)
                                showMenu = false
                            },
                            leadingIcon = { Icon(Icons.Default.Delete, contentDescription = null) }
                        )
                    }
                }
            }
        }
        ReactionPills(
            message = message,
            reactions = reactions,
            myReactions = myReactions,
            onToggleReaction = onToggleReaction,
            readLabel = "",
            edited = message.edited
        )
    }
    if (showEditDialog) {
        val initial = message.content
        var editDraft by remember { mutableStateOf(initial) }
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
                TextButton(
                    onClick = {
                        val text = editDraft.trim()
                        if (text.isNotEmpty() && onEdit?.invoke(text) == true) {
                            showEditDialog = false
                        }
                    }
                ) {
                    Text("保存")
                }
            },
            dismissButton = {
                TextButton(onClick = { showEditDialog = false }) {
                    Text("取消")
                }
            }
        )
    }
}

/** Quick reaction set offered in the message menu (Windows parity). */
internal val REACTION_CHOICES = listOf("👍", "❤️", "😂", "😮", "😢", "🎉")

/** Reaction pill row under a bubble: grouped emoji with counts; tapping a
 *  pill of mine toggles it off. File/media cards have no status row of their
 *  own, so this also carries the own-message status label (read state /
 *  已编辑) for them. */
@Composable
internal fun ReactionPills(
    message: ChatMessage,
    reactions: List<Pair<String, Int>>,
    myReactions: Set<String>,
    onToggleReaction: (String, String, Boolean) -> Unit,
    readLabel: String,
    edited: Boolean
) {
    val showLabel = readLabel.isNotEmpty() && message.isFromMe
    if (reactions.isEmpty() && !showLabel && !edited) return
    val align = if (message.isFromMe) Alignment.End else Alignment.Start
    Column(
        modifier = Modifier.padding(top = 2.dp),
        horizontalAlignment = align
    ) {
        if (showLabel || edited) {
            Text(
                text = listOfNotNull(
                    readLabel.takeIf { showLabel },
                    "已编辑".takeIf { edited }
                ).joinToString(" · "),
                fontSize = 10.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        if (reactions.isNotEmpty()) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                reactions.forEach { (emoji, count) ->
                    val mine = emoji in myReactions
                    Surface(
                        shape = RoundedCornerShape(50),
                        color = if (mine)
                            MaterialTheme.colorScheme.primary.copy(alpha = 0.18f)
                        else
                            MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.7f),
                        border = if (mine) {
                            androidx.compose.foundation.BorderStroke(
                                1.dp, MaterialTheme.colorScheme.primary
                            )
                        } else null,
                        onClick = { onToggleReaction(message.id, emoji, !mine) }
                    ) {
                        Text(
                            text = if (count > 1) "$emoji ×$count" else emoji,
                            fontSize = 12.sp,
                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                        )
                    }
                }
            }
        }
    }
}

/** Voice-message bubble: play/stop + duration, rendered like a compact card.
 *  Click plays the local WAV copy (auto-download first when not fetched);
 *  long-press offers the same menu vocabulary as the other bubbles. */
@Composable
internal fun VoiceMessageBubble(
    message: ChatMessage,
    state: ChatViewModel.DownloadState?,
    localPath: String?,
    playing: Boolean,
    isFromMe: Boolean,
    reactions: List<Pair<String, Int>>,
    myReactions: Set<String> = emptySet(),
    pinned: Boolean,
    onTogglePlay: () -> Unit,
    onReact: (String, Boolean) -> Unit,
    onPin: (Boolean) -> Unit,
    onDelete: () -> Unit
) {
    val fi = message.fileInfo
    val duration = remember(localPath) {
        localPath?.let { com.zqr.localchat.ui.VoiceNotes.wavDurationSeconds(it) } ?: 0
    }
    val align = if (isFromMe) Alignment.End else Alignment.Start
    var showMenu by remember { mutableStateOf(false) }
    var showReactionPicker by remember { mutableStateOf(false) }
    val bg = if (isFromMe) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant
    val textColor = if (isFromMe) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurfaceVariant
    Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = align) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = bg,
            modifier = Modifier
                .widthIn(max = 220.dp)
                .combinedClickable(onClick = onTogglePlay, onLongClick = { showMenu = true })
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = if (playing) "⏸" else "▶",
                    color = textColor,
                    fontSize = 16.sp
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = when {
                        // formatDuration, not a hand-rolled "0:${...}": a 75 s
                        // clip used to render as "0:75"
                        playing -> "播放中 " + com.zqr.localchat.ui.VoiceNotes.formatDuration(duration)
                        localPath != null -> com.zqr.localchat.ui.VoiceNotes.formatDuration(duration)
                        state is ChatViewModel.DownloadState.Downloading -> "接收中…"
                        else -> "语音（点击接收）"
                    },
                    color = textColor,
                    fontSize = 14.sp
                )
            }
            DropdownMenu(expanded = showMenu, onDismissRequest = { showMenu = false; showReactionPicker = false }) {
                if (showReactionPicker) {
                    Row(modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
                        REACTION_CHOICES.forEach { emoji ->
                            TextButton(onClick = {
                                // toggle, exactly like the text/file bubbles: a
                                // reaction I already placed is removed again
                                onReact(emoji, emoji !in myReactions)
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
                        text = { Text(if (pinned) "取消置顶" else "置顶") },
                        onClick = { onPin(!pinned); showMenu = false }
                    )
                    if (isFromMe) {
                        DropdownMenuItem(
                            text = { Text("删除") },
                            onClick = { onDelete(); showMenu = false }
                        )
                    }
                }
            }
        }
        ReactionPills(
            message = message,
            reactions = reactions,
            myReactions = myReactions,
            onToggleReaction = { _, emoji, active -> onReact(emoji, active) },
            readLabel = "",
            edited = message.edited
        )
    }
}

/**
 * Quoted-message header inside a reply bubble: an accent bar plus the
 * original sender's name and an elided one-line preview. Both come from the
 * self-contained reply wire fields, so the quote renders even when the
 * referenced message is not in local history (Windows parity).
 */
@Composable
internal fun ReplyHeader(message: ChatMessage, mine: Boolean) {
    if (message.replyTo == null) return
    val base = if (mine) MaterialTheme.colorScheme.onPrimary
    else MaterialTheme.colorScheme.onSurfaceVariant
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(bottom = 6.dp)
            .background(base.copy(alpha = 0.10f), RoundedCornerShape(6.dp))
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier
                .width(3.dp)
                .height(26.dp)
                .background(base.copy(alpha = 0.7f), RoundedCornerShape(2.dp))
        )
        Spacer(modifier = Modifier.width(6.dp))
        Column(modifier = Modifier.weight(1f, fill = false)) {
            Text(
                text = message.replySender?.ifBlank { null } ?: "回复",
                fontSize = 11.sp,
                fontWeight = FontWeight.Medium,
                color = base.copy(alpha = 0.85f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                text = message.replyPreview.orEmpty().replace('\n', ' '),
                fontSize = 11.sp,
                color = base.copy(alpha = 0.7f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
        }
    }
}

/** Compose bar shown above the input while replying: what will be quoted and
 *  a cancel button (shared by the group and direct chat screens). */
@Composable
internal fun ReplyComposeBar(target: ChatMessage, onCancel: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .background(
                MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f),
                RoundedCornerShape(8.dp)
            )
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            modifier = Modifier
                .width(3.dp)
                .height(32.dp)
                .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(2.dp))
        )
        Spacer(modifier = Modifier.width(8.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = "回复 ${target.senderName}",
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.primary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                text = target.replyPreviewText(),
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
        }
        IconButton(onClick = onCancel) {
            Icon(
                Icons.Filled.Close,
                contentDescription = "取消回复",
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

private fun formatTime(timestamp: Long): String {
    val sdf = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
    return sdf.format(java.util.Date(timestamp))
}

/** Compact file-size string, e.g. "1.5 MB"; 0 means unknown. */
private fun formatFileSize(size: Long): String {
    if (size <= 0) return "大小未知"
    if (size < 1024) return "$size B"
    var value = size.toDouble()
    for (unit in arrayOf("KB", "MB", "GB", "TB")) {
        value /= 1024.0
        if (value < 1024 || unit == "TB") return String.format("%.1f %s", value, unit)
    }
    return "$size B"
}

/** Live transfer detail line of a downloading card: 已传/总大小 · 速度 ·
 *  剩余时间 (Windows parity: format_transfer_detail). */
private fun formatTransferDetail(state: ChatViewModel.DownloadState.Downloading): String {
    val text = StringBuilder(formatFileSize(state.received))
    if (state.total > 0) {
        text.append("/").append(formatFileSize(state.total))
    }
    if (state.speedBps > 0) {
        text.append(" · ").append(formatFileSize(state.speedBps)).append("/s")
    }
    if (state.etaSeconds >= 0) {
        val seconds = state.etaSeconds
        text.append(" · 剩余 ")
        if (seconds < 60) {
            text.append(maxOf(1L, seconds)).append(" 秒")
        } else {
            text.append(seconds / 60).append(" 分 ").append(seconds % 60).append(" 秒")
        }
    }
    return text.toString()
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun FileMessageBubble(
    message: ChatMessage,
    state: ChatViewModel.DownloadState?,
    onDownload: () -> Unit,
    onOpen: (() -> Unit)? = null,
    onCancel: (() -> Unit)? = null,
    onDelete: () -> Unit
) {
    val fileInfo = message.fileInfo ?: return
    val isFromMe = message.isFromMe
    val alignment = if (isFromMe) Alignment.End else Alignment.Start
    val bgColor = if (isFromMe)
        MaterialTheme.colorScheme.primary
    else
        MaterialTheme.colorScheme.surfaceVariant
    val textColor = if (isFromMe)
        MaterialTheme.colorScheme.onPrimary
    else
        MaterialTheme.colorScheme.onSurfaceVariant

    var showMenu by remember { mutableStateOf(false) }
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()

    // an offer without a download address expired with its sender's previous
    // session (the short-lived download server is gone); a paused download is
    // different — its address/key live in the persisted resume entry
    val paused = state is ChatViewModel.DownloadState.Paused
    val expired = fileInfo.downloadHost.isBlank() && !paused
    val downloading = state is ChatViewModel.DownloadState.Downloading && !isFromMe
    val statusText = when (state) {
        is ChatViewModel.DownloadState.Downloading -> "下载中 ${state.percent}%"
        is ChatViewModel.DownloadState.Paused -> "已暂停 ${state.percent}%（点击续传）"
        is ChatViewModel.DownloadState.Done -> "已保存"
        is ChatViewModel.DownloadState.Failed -> state.message
        else -> when {
            isFromMe -> "已发送"
            expired -> "已过期"
            else -> "点击下载"
        }
    }
    // While a download is running the bubble tap CANCELS it (when the screen
    // wired a cancel handler); otherwise the tap (re)starts or resumes it.
    val clickable = !expired && !isFromMe && when {
        downloading -> onCancel != null
        else -> state == null || state is ChatViewModel.DownloadState.Failed ||
            state is ChatViewModel.DownloadState.Paused
    }

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = alignment
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 280.dp)
                .clip(RoundedCornerShape(16.dp))
                .background(bgColor)
                .combinedClickable(
                    onClick = {
                        if (!clickable) return@combinedClickable
                        if (downloading) onCancel?.invoke() else onDownload()
                    },
                    onLongClick = { showMenu = true }
                )
                .padding(horizontal = 14.dp, vertical = 12.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Surface(
                    modifier = Modifier.size(40.dp),
                    shape = RoundedCornerShape(8.dp),
                    color = if (isFromMe)
                        MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.15f)
                    else
                        MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)
                ) {
                    Box(contentAlignment = Alignment.Center) {
                        Text("📄", fontSize = 20.sp)
                    }
                }
                Spacer(modifier = Modifier.width(10.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = fileInfo.fileName,
                        color = textColor,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Medium,
                        maxLines = 2,
                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        // live 已传/总大小 · 速度 · 剩余时间 while downloading
                        // (falls back to the plain size otherwise)
                        text = if (state is ChatViewModel.DownloadState.Downloading) {
                            formatTransferDetail(state)
                        } else {
                            formatFileSize(fileInfo.fileSize)
                        },
                        color = textColor.copy(alpha = 0.7f),
                        fontSize = 11.sp
                    )
                }
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = statusText,
                    color = textColor.copy(alpha = 0.8f),
                    fontSize = 11.sp
                )
            }
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false }
            ) {
                if (!isFromMe && !expired) {
                    DropdownMenuItem(
                        text = { Text(if (paused) "续传" else "下载") },
                        onClick = {
                            showMenu = false
                            onDownload()
                        }
                    )
                }
                if (downloading && onCancel != null) {
                    DropdownMenuItem(
                        text = { Text("取消下载") },
                        onClick = {
                            showMenu = false
                            onCancel()
                        },
                        leadingIcon = { Icon(Icons.Default.Close, contentDescription = null) }
                    )
                }
                if (onOpen != null) {
                    DropdownMenuItem(
                        text = { Text("打开文件") },
                        onClick = {
                            showMenu = false
                            onOpen()
                        },
                        leadingIcon = { Icon(Icons.AutoMirrored.Filled.OpenInNew, contentDescription = null) }
                    )
                }
                DropdownMenuItem(
                    text = { Text("复制文件名") },
                    onClick = {
                        scope.launch {
                            clipboard.setClipEntry(
                                ClipEntry(ClipData.newPlainText("LocalChat", fileInfo.fileName))
                            )
                        }
                        showMenu = false
                    }
                )
                if (isFromMe) {
                    DropdownMenuItem(
                        text = { Text("删除") },
                        onClick = {
                            showMenu = false
                            onDelete()
                        }
                    )
                }
            }
        }
    }
}

// ---------------------------------------------------------------- folder
// A folder offer renders as ONE card (all file messages sharing a folderId),
// mirroring the Windows folder card: name, entry count + total size and a
// status line. Own folders are not downloadable; received folders open a tree
// picker and are saved as a whole.

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun FolderMessageBubble(
    group: FolderGroup,
    state: ChatViewModel.FolderDownloadState?,
    onSave: () -> Unit,
    onCancel: () -> Unit,
    onLongPress: () -> Unit = {}
) {
    val isFromMe = group.isFromMe
    val downloading = state?.downloading == true && !isFromMe
    // a paused folder keeps per-entry staging files: its resume entries carry
    // the addresses even when the restored offers were blanked (已过期)
    val paused = state?.paused == true && !isFromMe
    val expired = group.expired && !paused
    val alignment = if (isFromMe) Alignment.End else Alignment.Start
    val bgColor = if (isFromMe)
        MaterialTheme.colorScheme.primary
    else
        MaterialTheme.colorScheme.surfaceVariant
    val textColor = if (isFromMe)
        MaterialTheme.colorScheme.onPrimary
    else
        MaterialTheme.colorScheme.onSurfaceVariant

    val statusText = when {
        isFromMe -> "已发送"
        expired -> "已过期"
        downloading -> "保存中 ${state?.done ?: 0}/${state?.total ?: group.total}"
        paused -> "已暂停 ${state?.done ?: 0}/${state?.total ?: group.total}（点击续传）"
        state != null && state.message.isNotEmpty() -> state.message
        state != null && !state.downloading && state.savedPath.isNotEmpty() -> "已保存"
        else -> "点击保存"
    }
    val clickable = !expired && !isFromMe && when {
        downloading -> true
        else -> state == null || !state.downloading
    }

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = alignment
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 300.dp)
                .clip(RoundedCornerShape(16.dp))
                .background(bgColor)
                .combinedClickable(
                    onClick = {
                        if (!clickable) return@combinedClickable
                        if (downloading) onCancel() else onSave()
                    },
                    // long press = context entry point (delete via the screen's
                    // confirm dialog) — a folder card previously had no menu at
                    // all, which had removed the delete ability a plain file
                    // bubble still has
                    onLongClick = onLongPress
                )
                .padding(horizontal = 14.dp, vertical = 12.dp)
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Surface(
                    modifier = Modifier.size(40.dp),
                    shape = RoundedCornerShape(8.dp),
                    color = if (isFromMe)
                        MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.15f)
                    else
                        MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)
                ) {
                    Box(contentAlignment = Alignment.Center) {
                        Text("\uD83D\uDCC1", fontSize = 20.sp)
                    }
                }
                Spacer(modifier = Modifier.width(10.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = group.folderName.ifBlank { "文件夹" },
                        color = textColor,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Medium,
                        maxLines = 2,
                        overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = "${group.total} 个文件 · ${formatFileSize(group.size)}",
                        color = textColor.copy(alpha = 0.7f),
                        fontSize = 11.sp
                    )
                }
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = statusText,
                    color = textColor.copy(alpha = 0.8f),
                    fontSize = 11.sp
                )
            }
        }
    }
}

// ---------------------------------------------------------------- media
// Image/video messages: media received from a peer is downloaded into the
// app's media dir and rendered INLINE (image decoded, video thumbnail with a
// play overlay); tapping a downloaded item opens the system viewer/player.

/**
 * Decode an image file off the main thread with power-of-two downsampling so
 * a 12-megapixel photo does not allocate a full bitmap just to sit in a
 * ~260dp bubble. Returns null for unreadable/missing files.
 */
@Composable
internal fun rememberSampledBitmap(path: String, targetSize: Int = 1024): androidx.compose.ui.graphics.ImageBitmap? =
    produceState<androidx.compose.ui.graphics.ImageBitmap?>(initialValue = null, path) {
        value = withContext(Dispatchers.IO) {
            runCatching {
                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeFile(path, bounds)
                if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return@withContext null
                var sample = 1
                while (bounds.outWidth / (sample * 2) >= targetSize ||
                    bounds.outHeight / (sample * 2) >= targetSize
                ) {
                    sample *= 2
                }
                BitmapFactory.decodeFile(
                    path,
                    BitmapFactory.Options().apply { inSampleSize = sample }
                )
            }.getOrNull()?.asImageBitmap()
        }
    }.value

/**
 * Extract a video frame off the main thread for the inline thumbnail
 * (MINI_KIND keeps the decode small). Returns null when the platform cannot
 * produce a thumbnail — the bubble then renders a neutral placeholder.
 */
@Composable
internal fun rememberVideoThumbnail(path: String): androidx.compose.ui.graphics.ImageBitmap? =
    produceState<androidx.compose.ui.graphics.ImageBitmap?>(initialValue = null, path) {
        value = withContext(Dispatchers.IO) {
            runCatching {
                ThumbnailUtils.createVideoThumbnail(
                    path,
                    MediaStore.Images.Thumbnails.MINI_KIND
                )
            }.getOrNull()?.takeIf { it.width > 0 && it.height > 0 }?.asImageBitmap()
        }
    }.value

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun MediaMessageBubble(
    message: ChatMessage,
    state: ChatViewModel.DownloadState?,
    localPath: String?,
    onDownload: () -> Unit,
    onSaveAs: () -> Unit,
    onOpen: (String) -> Unit,
    onDelete: () -> Unit
) {
    val fileInfo = message.fileInfo ?: return
    val isVideo = fileInfo.kind == FileKind.VIDEO
    val isFromMe = message.isFromMe
    val alignment = if (isFromMe) Alignment.End else Alignment.Start

    var showMenu by remember { mutableStateOf(false) }
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()

    // an offer without a download address expired with its sender's previous
    // session (the short-lived download server is gone); a paused media
    // download resumes from its staging entry
    val paused = state is ChatViewModel.DownloadState.Paused
    val expired = fileInfo.downloadHost.isBlank() && !paused
    val downloading = state is ChatViewModel.DownloadState.Downloading
    val statusText = when (state) {
        is ChatViewModel.DownloadState.Downloading -> "下载中 ${state.percent}%"
        is ChatViewModel.DownloadState.Paused -> "已暂停 ${state.percent}%（点击续传）"
        is ChatViewModel.DownloadState.Failed -> state.message
        else -> when {
            localPath != null -> if (isVideo) "点击播放" else "点击查看"
            isFromMe -> "已发送"
            expired -> "已过期"
            else -> "点击查看"
        }
    }
    val clickable = localPath != null || (
        !expired && !downloading && !isFromMe
        )

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = alignment
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 280.dp)
                .combinedClickable(
                    onClick = {
                        when {
                            localPath != null -> onOpen(localPath)
                            clickable -> onDownload()
                        }
                    },
                    onLongClick = { showMenu = true }
                )
        ) {
            when {
                // downloaded image: render it directly in the conversation
                !isVideo && localPath != null -> {
                    val bitmap = rememberSampledBitmap(localPath)
                    if (bitmap != null) {
                        // fit the bitmap's own ratio into the bubble box
                        // (px used as dp — only the ratio matters here)
                        val maxW = minOf(260.dp, (LocalConfiguration.current.screenWidthDp * 0.72f).dp)
                        val w = bitmap.width.coerceAtLeast(1).toFloat()
                        val h = bitmap.height.coerceAtLeast(1).toFloat()
                        val fit = minOf(1f, maxW.value / w, 320f / h)
                        Image(
                            bitmap = bitmap,
                            contentDescription = fileInfo.fileName,
                            contentScale = ContentScale.Fit,
                            modifier = Modifier
                                .width((w * fit).dp)
                                .height((h * fit).dp)
                                .clip(RoundedCornerShape(14.dp))
                        )
                    } else {
                        MediaPlaceholderCard(
                            isVideo = false,
                            fileInfo = fileInfo,
                            isFromMe = isFromMe,
                            statusText = statusText
                        )
                    }
                }
                // downloaded video: thumbnail with a play overlay
                isVideo && localPath != null -> {
                    val thumbnail = rememberVideoThumbnail(localPath)
                    val maxW = minOf(260.dp, (LocalConfiguration.current.screenWidthDp * 0.72f).dp)
                    Box(
                        modifier = Modifier
                            .widthIn(max = maxW)
                            .clip(RoundedCornerShape(14.dp))
                            .background(Color(0xFF1B1B1F))
                    ) {
                        if (thumbnail != null) {
                            val w = thumbnail.width.coerceAtLeast(1).toFloat()
                            val h = thumbnail.height.coerceAtLeast(1).toFloat()
                            val fit = minOf(1f, maxW.value / w, 320f / h)
                            Image(
                                bitmap = thumbnail,
                                contentDescription = fileInfo.fileName,
                                contentScale = ContentScale.Fit,
                                modifier = Modifier
                                    .width((w * fit).dp)
                                    .height((h * fit).dp)
                            )
                        } else {
                            Box(
                                modifier = Modifier
                                    .width(220.dp)
                                    .height(140.dp)
                            )
                        }
                        Surface(
                            modifier = Modifier
                                .align(Alignment.Center)
                                .size(48.dp),
                            shape = CircleShape,
                            color = Color.Black.copy(alpha = 0.45f)
                        ) {
                            Icon(
                                Icons.Filled.PlayArrow,
                                contentDescription = "播放视频",
                                tint = Color.White,
                                modifier = Modifier.padding(10.dp)
                            )
                        }
                    }
                }
                // not downloaded yet (or unreadable): compact placeholder card
                else -> {
                    MediaPlaceholderCard(
                        isVideo = isVideo,
                        fileInfo = fileInfo,
                        isFromMe = isFromMe,
                        statusText = statusText
                    )
                }
            }
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false }
            ) {
                if (localPath != null) {
                    DropdownMenuItem(
                        text = { Text(if (isVideo) "播放" else "打开") },
                        onClick = {
                            showMenu = false
                            onOpen(localPath)
                        },
                        leadingIcon = {
                            Icon(Icons.AutoMirrored.Filled.OpenInNew, contentDescription = null)
                        }
                    )
                }
                if (!isFromMe && !expired) {
                    DropdownMenuItem(
                        text = { Text("保存到...") },
                        onClick = {
                            showMenu = false
                            onSaveAs()
                        }
                    )
                }
                DropdownMenuItem(
                    text = { Text("复制文件名") },
                    onClick = {
                        scope.launch {
                            clipboard.setClipEntry(
                                ClipEntry(ClipData.newPlainText("LocalChat", fileInfo.fileName))
                            )
                        }
                        showMenu = false
                    }
                )
                if (isFromMe) {
                    DropdownMenuItem(
                        text = { Text("删除") },
                        onClick = {
                            showMenu = false
                            onDelete()
                        }
                    )
                }
            }
        }
    }
}

@Composable
private fun MediaPlaceholderCard(
    isVideo: Boolean,
    fileInfo: FileInfo,
    isFromMe: Boolean,
    statusText: String
) {
    val bgColor = if (isFromMe)
        MaterialTheme.colorScheme.primary
    else
        MaterialTheme.colorScheme.surfaceVariant
    val textColor = if (isFromMe)
        MaterialTheme.colorScheme.onPrimary
    else
        MaterialTheme.colorScheme.onSurfaceVariant
    Row(
        modifier = Modifier
            .widthIn(max = 280.dp)
            .clip(RoundedCornerShape(16.dp))
            .background(bgColor)
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Surface(
            modifier = Modifier.size(40.dp),
            shape = RoundedCornerShape(8.dp),
            color = if (isFromMe)
                MaterialTheme.colorScheme.onPrimary.copy(alpha = 0.15f)
            else
                MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    if (isVideo) Icons.Default.Movie else Icons.Default.Image,
                    contentDescription = if (isVideo) "视频" else "图片",
                    tint = textColor
                )
            }
        }
        Spacer(modifier = Modifier.width(10.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = fileInfo.fileName,
                color = textColor,
                fontSize = 14.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 2,
                overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
            )
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                text = formatFileSize(fileInfo.fileSize),
                color = textColor.copy(alpha = 0.7f),
                fontSize = 11.sp
            )
        }
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = statusText,
            color = textColor.copy(alpha = 0.8f),
            fontSize = 11.sp
        )
    }
}

/**
 * One local call-log row rendered as a centered system-style line (no bubble,
 * never deletable/forwardable). Clicking it redials with the same media kind.
 */
@Composable
internal fun CallLogLine(log: CallLogEntity, onClick: () -> Unit) {
    val missed = log.result == CallResult.MISSED && log.direction == CallDirection.INCOMING
    val color = if (missed) MaterialTheme.colorScheme.error
    else MaterialTheme.colorScheme.onSurfaceVariant
    Box(
        modifier = Modifier.fillMaxWidth(),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = "${callLogText(log)}  ${timeText(log.startTime)}",
            fontSize = 12.sp,
            color = color,
            modifier = Modifier
                .clip(RoundedCornerShape(10.dp))
                .clickable(onClick = onClick)
                .padding(horizontal = 10.dp, vertical = 4.dp)
        )
    }
}

private fun timeText(timestamp: Long): String =
    java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
        .format(java.util.Date(timestamp))
