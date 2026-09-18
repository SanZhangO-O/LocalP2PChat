package com.zqr.localchat.ui.screen

import android.content.ClipData
import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.Peer
import com.zqr.localchat.network.P2PManager
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PeerListScreen(
    groupName: String,
    myName: String,
    localIpAddress: String,
    localPort: Int,
    isHost: Boolean,
    groupPassword: String?,
    numericGroupId: String?,
    peers: Map<String, Peer>,
    rejoinInProgress: Boolean,
    rejoinFailed: Boolean,
    connectionLost: Boolean,
    serverError: String?,
    connectionResult: P2PManager.ConnectionResult?,
    onClearConnectionResult: () -> Unit,
    onRetryHost: () -> Unit,
    onReconnect: () -> Unit,
    onLeave: () -> Unit,
    onBack: () -> Unit,
    onOpenChat: () -> Unit,
    onCallPeer: (String) -> Unit = {},
    announcement: String = "",
    onUpdateGroupInfo: (String?, String?) -> Unit = { _, _ -> },
    onKickMember: (String) -> Unit = {}
) {
    val snackbarHostState = remember { SnackbarHostState() }
    val clipboard = LocalClipboard.current
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    LaunchedEffect(connectionResult) {
        when (val result = connectionResult) {
            is P2PManager.ConnectionResult.Error -> {
                snackbarHostState.showSnackbar(result.message)
                onClearConnectionResult()
            }
            else -> {}
        }
    }

    var showLeaveDialog by remember { mutableStateOf(false) }
    var showGroupSettings by remember { mutableStateOf(false) }
    var pendingKick by remember { mutableStateOf<String?>(null) }
    // the group password is masked by default: it is a join secret, and
    // screenshots/overlays should not leak it
    var showPassword by remember { mutableStateOf(false) }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("群组: $groupName")
                        Text(
                            text = if (isHost) "创建者 · $localIpAddress:$localPort" else "成员",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
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
                    if (isHost) {
                        IconButton(onClick = { showGroupSettings = true }) {
                            Icon(
                                Icons.Filled.Edit,
                                contentDescription = "群设置",
                                tint = MaterialTheme.colorScheme.onSurface
                            )
                        }
                    }
                    IconButton(onClick = onOpenChat) {
                        Icon(
                            Icons.AutoMirrored.Filled.Chat,
                            contentDescription = "聊天",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
                    }
                    IconButton(onClick = { showLeaveDialog = true }) {
                        Icon(
                            Icons.AutoMirrored.Filled.ExitToApp,
                            contentDescription = "退出群组",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    titleContentColor = MaterialTheme.colorScheme.onSurface
                )
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            if (announcement.isNotBlank()) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.secondaryContainer
                    )
                ) {
                    Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                        Text(
                            text = "群公告",
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onSecondaryContainer
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = announcement,
                            fontSize = 14.sp,
                            color = MaterialTheme.colorScheme.onSecondaryContainer
                        )
                    }
                }
            }
            if (isHost) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.primaryContainer
                    )
                ) {
                    Column(
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                        horizontalAlignment = Alignment.CenterHorizontally
                    ) {
                        Text(
                            text = "将此地址分享给其他人加入",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onPrimaryContainer
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                text = "$localIpAddress:$localPort",
                                fontSize = 16.sp,
                                fontWeight = FontWeight.Medium,
                                color = MaterialTheme.colorScheme.onPrimaryContainer,
                                modifier = Modifier.weight(1f)
                            )
                            TextButton(
                                onClick = {
                                    scope.launch {
                                        clipboard.setClipEntry(
                                            ClipEntry(ClipData.newPlainText("LocalChat", "$localIpAddress:$localPort"))
                                        )
                                        Toast.makeText(context, "已复制地址", Toast.LENGTH_SHORT).show()
                                    }
                                }
                            ) {
                                Text("复制", fontSize = 13.sp)
                            }
                        }
                        if (numericGroupId != null) {
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    text = "群组数字ID: ${P2PManager.formatNumericGroupId(numericGroupId)}",
                                    fontSize = 14.sp,
                                    fontWeight = FontWeight.Medium,
                                    color = MaterialTheme.colorScheme.onPrimaryContainer,
                                    modifier = Modifier.weight(1f)
                                )
                                TextButton(
                                    onClick = {
                                        scope.launch {
                                            clipboard.setClipEntry(
                                                ClipEntry(ClipData.newPlainText("LocalChat", numericGroupId))
                                            )
                                            Toast.makeText(context, "已复制数字ID", Toast.LENGTH_SHORT).show()
                                        }
                                    }
                                ) {
                                    Text("复制", fontSize = 13.sp)
                                }
                            }
                        }
                        if (!groupPassword.isNullOrBlank()) {
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    text = "群组密码: " + if (showPassword) groupPassword else "••••••",
                                    fontSize = 14.sp,
                                    fontWeight = FontWeight.Medium,
                                    color = MaterialTheme.colorScheme.onPrimaryContainer,
                                    modifier = Modifier.weight(1f)
                                )
                                TextButton(onClick = { showPassword = !showPassword }) {
                                    Text(if (showPassword) "隐藏" else "显示", fontSize = 13.sp)
                                }
                                TextButton(
                                    onClick = {
                                        scope.launch {
                                            clipboard.setClipEntry(
                                                ClipEntry(ClipData.newPlainText("LocalChat", groupPassword))
                                            )
                                            Toast.makeText(context, "已复制群组密码", Toast.LENGTH_SHORT).show()
                                        }
                                    }
                                ) {
                                    Text("复制", fontSize = 13.sp)
                                }
                            }
                        }
                    }
                }
            }

            if (serverError != null) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer
                    )
                ) {
                    Column(
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
                    ) {
                        Text(
                            text = serverError,
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            fontSize = 14.sp
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = "其他设备将无法加入本群组。",
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            fontSize = 12.sp
                        )
                        if (isHost) {
                            Spacer(modifier = Modifier.height(4.dp))
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.End
                            ) {
                                TextButton(onClick = onRetryHost) {
                                    Text("重试监听", color = MaterialTheme.colorScheme.onErrorContainer)
                                }
                                TextButton(onClick = onBack) {
                                    Text("返回群组列表", color = MaterialTheme.colorScheme.onErrorContainer)
                                }
                            }
                        }
                    }
                }
            }

            if (connectionLost) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer
                    )
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = "与群组的连接已断开",
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            fontSize = 14.sp,
                            modifier = Modifier.weight(1f)
                        )
                        TextButton(onClick = onReconnect) {
                            Text("重连")
                        }
                    }
                }
            }

            if (peers.isEmpty()) {
                Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    when {
                        rejoinInProgress -> Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            CircularProgressIndicator(modifier = Modifier.size(28.dp))
                            Spacer(modifier = Modifier.height(12.dp))
                            Text(
                                text = "正在连接...",
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                fontSize = 16.sp
                            )
                        }
                        rejoinFailed && !connectionLost -> Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(
                                text = "连接失败",
                                color = MaterialTheme.colorScheme.error,
                                fontSize = 16.sp
                            )
                            Spacer(modifier = Modifier.height(12.dp))
                            TextButton(onClick = onReconnect) {
                                Text("重试连接")
                            }
                        }
                        isHost -> Text(
                            text = if (serverError != null) "监听失败，其他设备无法加入" else "等待其他设备加入...",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            fontSize = 16.sp
                        )
                        else -> Text(
                            text = if (connectionLost) "已断开连接" else "已连接到群组",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            fontSize = 16.sp
                        )
                    }
                }
            } else {
                Text(
                    text = "群组成员 (${peers.size + 1}人)",
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 14.sp
                )
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    item(key = "self") {
                        PeerItem(
                            peer = Peer(
                                id = "self",
                                name = myName.ifBlank { "我" },
                                ipAddress = localIpAddress,
                                port = localPort
                            ),
                            isSelf = true
                        )
                    }
                    items(peers.entries.toList(), key = { it.key }) { entry ->
                        PeerItem(
                            peer = entry.value,
                            onCall = {
                                if (!connectionLost) onCallPeer(entry.value.id)
                            },
                            onKick = if (isHost) {
                                { pendingKick = entry.value.id }
                            } else null
                        )
                    }
                }
            }
        }
    }

    if (showLeaveDialog) {
        AlertDialog(
            onDismissRequest = { showLeaveDialog = false },
            title = { Text("退出群组") },
            text = { Text("退出后群组仍会保留在列表中，可随时重新进入连接。聊天记录不会被删除。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showLeaveDialog = false
                        onLeave()
                    }
                ) {
                    Text("退出", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { showLeaveDialog = false }) {
                    Text("取消")
                }
            }
        )
    }

    pendingKick?.let { peerId ->
        val name = peers[peerId]?.name ?: peerId
        AlertDialog(
            onDismissRequest = { pendingKick = null },
            title = { Text("移出成员") },
            text = { Text("确定要将 $name 移出群组吗？\n对方将立即断开连接，且无法再收到本群消息。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        pendingKick = null
                        onKickMember(peerId)
                    }
                ) {
                    Text("移出", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingKick = null }) { Text("取消") }
            }
        )
    }

    if (showGroupSettings) {
        var nameText by remember { mutableStateOf(groupName) }
        var announcementText by remember { mutableStateOf(announcement) }
        AlertDialog(
            onDismissRequest = { showGroupSettings = false },
            title = { Text("群设置") },
            text = {
                Column {
                    OutlinedTextField(
                        value = nameText,
                        onValueChange = { nameText = it.take(20) },
                        singleLine = true,
                        label = { Text("群名称") },
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = announcementText,
                        onValueChange = { announcementText = it },
                        label = { Text("群公告") },
                        placeholder = { Text("留空则清除公告") },
                        minLines = 3,
                        maxLines = 5,
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        if (nameText.isNotBlank()) {
                            showGroupSettings = false
                            onUpdateGroupInfo(nameText.trim(), announcementText.trim())
                        }
                    }
                ) {
                    Text("保存")
                }
            },
            dismissButton = {
                TextButton(onClick = { showGroupSettings = false }) { Text("取消") }
            }
        )
    }
}

@Composable
private fun PeerItem(
    peer: com.zqr.localchat.data.Peer,
    isSelf: Boolean = false,
    onCall: (() -> Unit)? = null,
    onKick: (() -> Unit)? = null
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                modifier = Modifier
                    .size(44.dp)
                    .clip(CircleShape),
                contentAlignment = Alignment.Center
            ) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    shape = CircleShape,
                    color = MaterialTheme.colorScheme.primaryContainer
                ) {
                    Box(contentAlignment = Alignment.Center) {
                        Text(
                            text = avatarChar(peer.name),
                            fontSize = 18.sp,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onPrimaryContainer
                        )
                    }
                }
            }
            Spacer(modifier = Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = peer.name,
                        fontSize = 15.sp,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                    if (isSelf) {
                        Spacer(modifier = Modifier.width(6.dp))
                        Text(
                            text = "我",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.primary,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }
                Text(text = peer.ipAddress, fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (!isSelf && onCall != null) {
                IconButton(onClick = onCall) {
                    Icon(
                        Icons.Filled.Videocam,
                        contentDescription = "视频通话",
                        tint = MaterialTheme.colorScheme.primary
                    )
                }
            }
            if (!isSelf && onKick != null) {
                IconButton(onClick = onKick) {
                    Icon(
                        Icons.Filled.PersonRemove,
                        contentDescription = "移出群组",
                        tint = MaterialTheme.colorScheme.error
                    )
                }
            }
        }
    }
}

/** Take the first full Unicode code point so emoji/Chinese are never split into half-surrogates. */
internal fun avatarChar(name: String): String {
    if (name.isEmpty()) return "?"
    val cp = name.codePointAt(0)
    return String(Character.toChars(cp)).uppercase()
}
