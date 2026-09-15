package com.zqr.localchat.ui.screen

import android.content.ClipData
import android.graphics.BitmapFactory
import android.media.ThumbnailUtils
import android.provider.MediaStore
import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Forward
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Movie
import androidx.compose.material.icons.filled.PlayArrow
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Calendar

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    groupId: String,
    groupName: String,
    messages: List<ChatMessage>,
    groups: List<ChatViewModel.GroupMeta>,
    connectionLost: Boolean,
    downloadStates: Map<String, ChatViewModel.DownloadState> = emptyMap(),
    onSendMessage: (String) -> Boolean,
    onForward: (groupId: String, content: String) -> Boolean,
    onDelete: (String) -> Unit,
    onPickFile: () -> Unit = {},
    onPickImage: () -> Unit = {},
    onPickVideo: () -> Unit = {},
    onDownloadFile: (FileInfo) -> Unit = {},
    onDownloadMedia: (FileInfo) -> Unit = {},
    resolveMedia: (FileInfo) -> String? = { null },
    /** Bumped by the ViewModel when an own sent image lands in the media
     *  dir: re-keys the local-path lookups so the sender's own bubble flips
     *  to the inline render without any other recomposition trigger. */
    mediaVersion: Int = 0,
    onOpenFile: (String) -> Unit = {},
    onBack: () -> Unit
) {
    var inputText by rememberSaveable { mutableStateOf("") }
    var pendingForward by remember { mutableStateOf<String?>(null) }
    var pendingDelete by remember { mutableStateOf<String?>(null) }
    val listState = rememberLazyListState()
    var shouldAutoScroll by remember(groupName) { mutableStateOf(true) }
    val snackbarHostState = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()

    val contentTooLong = inputText.length > P2PManager.MAX_CONTENT_LENGTH

    fun sendInput() {
        // The IME send action still fires while the send button is disabled:
        // never truncate silently — the error is shown, the user shortens it.
        if (contentTooLong) return
        val text = inputText
        if (text.isNotBlank()) {
            if (onSendMessage(text)) {
                inputText = ""
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
                val last = lastItemIndex(messages)
                shouldAutoScroll = last < 0 || lastVisibleIndex == -1 || lastVisibleIndex >= last - 2
            }
    }

    LaunchedEffect(messages.size, shouldAutoScroll) {
        val last = lastItemIndex(messages)
        if (shouldAutoScroll && last >= 0) {
            val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            if (last - lastVisible > 20) {
                listState.scrollToItem(last)
            } else {
                listState.animateScrollToItem(last)
            }
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                title = { Text(groupName) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = "返回",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
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
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.Bottom
                    ) {
                    IconButton(
                        onClick = onPickFile,
                        enabled = !connectionLost
                    ) {
                        Icon(
                            Icons.Default.AttachFile,
                            contentDescription = "发送文件",
                            tint = if (!connectionLost)
                                MaterialTheme.colorScheme.primary
                            else
                                MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    IconButton(
                        onClick = onPickImage,
                        enabled = !connectionLost
                    ) {
                        Icon(
                            Icons.Default.Image,
                            contentDescription = "发送图片",
                            tint = if (!connectionLost)
                                MaterialTheme.colorScheme.primary
                            else
                                MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    IconButton(
                        onClick = onPickVideo,
                        enabled = !connectionLost
                    ) {
                        Icon(
                            Icons.Default.Movie,
                            contentDescription = "发送视频",
                            tint = if (!connectionLost)
                                MaterialTheme.colorScheme.primary
                            else
                                MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Spacer(modifier = Modifier.width(4.dp))
                    OutlinedTextField(
                        value = inputText,
                        onValueChange = { inputText = it },
                        placeholder = { Text("输入消息...") },
                        modifier = Modifier.weight(1f),
                        shape = RoundedCornerShape(20.dp),
                        minLines = 1,
                        maxLines = 5,
                        isError = contentTooLong,
                        supportingText = if (contentTooLong) {
                            { Text("消息过长（最多 ${P2PManager.MAX_CONTENT_LENGTH} 字）") }
                        } else if (inputText.length > P2PManager.MAX_CONTENT_LENGTH - 200) {
                            { Text("${inputText.length}/${P2PManager.MAX_CONTENT_LENGTH}") }
                        } else {
                            null
                        },
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                        keyboardActions = KeyboardActions(onSend = { sendInput() })
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    IconButton(
                        onClick = { sendInput() },
                        enabled = inputText.isNotBlank() && !contentTooLong && !connectionLost
                    ) {
                        Icon(
                            Icons.AutoMirrored.Filled.Send,
                            contentDescription = "发送",
                            tint = if (inputText.isNotBlank() && !contentTooLong && !connectionLost)
                                MaterialTheme.colorScheme.primary
                            else
                                MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
        }
    }
    ) { padding ->
        if (messages.isEmpty()) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(padding),
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
                    .padding(padding)
                    .padding(horizontal = 12.dp),
                state = listState,
                contentPadding = PaddingValues(vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                itemsIndexed(messages, key = { _, message -> message.id }) { index, message ->
                    val prev = messages.getOrNull(index - 1)
                    if (prev == null || !isSameDay(prev.timestamp, message.timestamp)) {
                        DateHeader(timestamp = message.timestamp)
                    }
                    if (message.fileInfo != null) {
                        val fi = message.fileInfo!!
                        if (fi.kind == FileKind.IMAGE || fi.kind == FileKind.VIDEO) {
                            val saved =
                                downloadStates[message.id] as? ChatViewModel.DownloadState.Done
                            MediaMessageBubble(
                                message = message,
                                state = downloadStates[message.id],
                                localPath = saved?.uri ?: resolveMedia(fi),
                                onDownload = { onDownloadMedia(fi) },
                                onSaveAs = { onDownloadFile(fi) },
                                onOpen = onOpenFile,
                                onDelete = { pendingDelete = message.id }
                            )
                        } else {
                            val saved =
                                downloadStates[message.id] as? ChatViewModel.DownloadState.Done
                            FileMessageBubble(
                                message = message,
                                state = downloadStates[message.id],
                                onDownload = { onDownloadFile(message.fileInfo!!) },
                                onOpen = saved?.let { done -> { onOpenFile(done.uri) } },
                                onDelete = { pendingDelete = message.id }
                            )
                        }
                    } else {
                        MessageBubble(
                            message = message,
                            onForward = { pendingForward = it },
                            onDelete = { pendingDelete = it }
                        )
                    }
                }
            }
        }
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
private fun lastItemIndex(messages: List<ChatMessage>): Int {
    if (messages.isEmpty()) return -1
    var headers = 0
    var prev: Long? = null
    for (m in messages) {
        if (prev == null || !isSameDay(prev, m.timestamp)) headers++
        prev = m.timestamp
    }
    return messages.size + headers - 1
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
    onForward: (String) -> Unit,
    onDelete: (String) -> Unit
) {
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
                .clip(shape)
                .background(bgColor)
                .combinedClickable(
                    // both taps open the actions menu: the bubble is clearly
                    // interactive, so an empty onClick was misleading dead UI
                    onClick = { showMenu = true },
                    onLongClick = { showMenu = true }
                )
                .padding(horizontal = 14.dp, vertical = 10.dp)
        ) {
            Column {
                if (!isFromMe) {
                    Text(
                        text = message.senderName,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Medium,
                        color = textColor.copy(alpha = 0.7f)
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                }
                Text(
                    text = message.content,
                    color = textColor,
                    fontSize = 15.sp
                )
                Text(
                    text = formatTime(message.timestamp),
                    color = textColor.copy(alpha = 0.6f),
                    fontSize = 11.sp,
                    modifier = Modifier.align(Alignment.End)
                )
            }
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false }
            ) {
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

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun FileMessageBubble(
    message: ChatMessage,
    state: ChatViewModel.DownloadState?,
    onDownload: () -> Unit,
    onOpen: (() -> Unit)? = null,
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
    // session (the short-lived download server is gone)
    val expired = fileInfo.downloadHost.isBlank()
    val statusText = when (state) {
        is ChatViewModel.DownloadState.Downloading -> "下载中..."
        is ChatViewModel.DownloadState.Done -> "已保存"
        is ChatViewModel.DownloadState.Failed -> state.message
        else -> when {
            isFromMe -> "已发送"
            expired -> "已过期"
            else -> "点击下载"
        }
    }
    val clickable = !expired && (
        state == null ||
            state is ChatViewModel.DownloadState.Failed
        )

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
                    onClick = { if (clickable && !isFromMe) onDownload() },
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
            DropdownMenu(
                expanded = showMenu,
                onDismissRequest = { showMenu = false }
            ) {
                if (!isFromMe && !expired) {
                    DropdownMenuItem(
                        text = { Text("下载") },
                        onClick = {
                            showMenu = false
                            onDownload()
                        }
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
    // session (the short-lived download server is gone)
    val expired = fileInfo.downloadHost.isBlank()
    val downloading = state is ChatViewModel.DownloadState.Downloading
    val statusText = when (state) {
        is ChatViewModel.DownloadState.Downloading -> "下载中..."
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
