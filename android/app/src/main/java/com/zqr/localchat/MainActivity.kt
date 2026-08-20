package com.zqr.localchat

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zqr.localchat.call.CallManager
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.ui.screen.CallOverlay
import com.zqr.localchat.ui.screen.ChatScreen
import com.zqr.localchat.ui.screen.DirectChatScreen
import com.zqr.localchat.ui.screen.GroupListScreen
import com.zqr.localchat.ui.screen.MemberListScreen
import com.zqr.localchat.ui.screen.PeerListScreen
import com.zqr.localchat.ui.screen.SetupScreen
import com.zqr.localchat.ui.screen.SettingsScreen
import com.zqr.localchat.ui.theme.LocalChatTheme
import com.zqr.localchat.viewmodel.ChatViewModel

enum class Screen { GroupList, Setup, GroupLobby, Chat, MemberList, DirectChat, Settings }

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            LocalChatTheme {
                LocalChatApp()
            }
        }
    }
}

@Composable
fun LocalChatApp(viewModel: ChatViewModel = viewModel()) {
    val context = LocalContext.current
    var currentScreenName by rememberSaveable { mutableStateOf(Screen.MemberList.name) }
    val currentScreen = runCatching { Screen.valueOf(currentScreenName) }.getOrDefault(Screen.MemberList)

    // the direct chat currently open (member-first navigation state)
    var activeDirectPeerId by remember { mutableStateOf<String?>(null) }
    // where the settings screen was opened from, so back returns there
    var settingsFrom by remember { mutableStateOf<String?>(null) }

    BackHandler(enabled = currentScreen != Screen.MemberList) {
        currentScreenName = when (currentScreen) {
            Screen.Chat -> Screen.GroupLobby.name
            Screen.Setup -> Screen.GroupList.name
            Screen.GroupLobby -> Screen.GroupList.name
            Screen.GroupList -> Screen.MemberList.name
            Screen.DirectChat -> Screen.MemberList.name
            Screen.Settings -> settingsFrom ?: Screen.MemberList.name
            Screen.MemberList -> Screen.MemberList.name
        }
        if (currentScreen == Screen.Settings) settingsFrom = null
        if (currentScreen == Screen.DirectChat) {
            activeDirectPeerId?.let { viewModel.closeDirectChat(it) }
            activeDirectPeerId = null
        }
    }

    val notifPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) {}

    var localNetworkDenied by remember { mutableStateOf(false) }
    var showPermissionDialog by remember { mutableStateOf(false) }
    var pendingPermissionAction by remember { mutableStateOf<(() -> Unit)?>(null) }
    // guards against stacking: while one local-network prompt is on screen a
    // second requireLocalNetworkPermission would overwrite the pending action
    var localNetworkRequestInFlight by remember { mutableStateOf(false) }

    val localNetworkPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        localNetworkRequestInFlight = false
        if (granted) {
            localNetworkDenied = false
            // the shared listener may have failed to bind before the grant;
            // (re)start it so this device is reachable for direct chats/joins
            viewModel.ensureListener()
            pendingPermissionAction?.invoke()
        } else {
            localNetworkDenied = true
        }
        pendingPermissionAction = null
    }

    /**
     * ACCESS_LOCAL_NETWORK is only defined on platforms that enforce local
     * network protection (some Android 16+ builds/images do not define it at
     * all). When the permission is absent there is nothing to enforce, so the
     * app must not block local TCP connections on it.
     */
    fun localNetworkPermissionDefined(): Boolean =
        runCatching {
            context.packageManager.getPermissionInfo(
                "android.permission.ACCESS_LOCAL_NETWORK",
                0
            )
        }.isSuccess

    fun hasLocalNetworkPermission(): Boolean =
        Build.VERSION.SDK_INT < 36 ||
            !localNetworkPermissionDefined() ||
            ContextCompat.checkSelfPermission(context, "android.permission.ACCESS_LOCAL_NETWORK") == PackageManager.PERMISSION_GRANTED

    fun requireLocalNetworkPermission(action: () -> Unit) {
        if (hasLocalNetworkPermission()) {
            action()
        } else if (localNetworkRequestInFlight) {
            // a prompt is already open; do not overwrite its pending action
            return
        } else if (localNetworkDenied) {
            pendingPermissionAction = action
            showPermissionDialog = true
        } else {
            pendingPermissionAction = action
            localNetworkRequestInFlight = true
            localNetworkPermissionLauncher.launch("android.permission.ACCESS_LOCAL_NETWORK")
        }
    }

    fun requestNotifPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    fun requestLocalNetworkPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 36 &&
            localNetworkPermissionDefined() &&
            ContextCompat.checkSelfPermission(context, "android.permission.ACCESS_LOCAL_NETWORK") != PackageManager.PERMISSION_GRANTED
        ) {
            localNetworkPermissionLauncher.launch("android.permission.ACCESS_LOCAL_NETWORK")
        }
    }

    val groups by viewModel.groups.collectAsState()
    val backgroundRunning by viewModel.backgroundRunning.collectAsState()
    val activePeers by viewModel.activePeers.collectAsState()
    val activeMessages by viewModel.activeMessages.collectAsState()
    val activeGroupName by viewModel.activeGroupName.collectAsState()
    val activeGroupId by viewModel.activeGroupId.collectAsState()
    val activeMyName by viewModel.activeMyName.collectAsState()
    val activeIsHost by viewModel.activeIsHost.collectAsState()
    val activeGroupPassword by viewModel.activeGroupPassword.collectAsState()
    val connectionResult by viewModel.connectionResult.collectAsState()
    val queriedGroupInfo by viewModel.queriedGroupInfo.collectAsState()
    val queryError by viewModel.queryError.collectAsState()
    val isQuerying by viewModel.isQueryingGroup.collectAsState()
    val isJoining by viewModel.isJoining.collectAsState()
    val rejoinInProgress by viewModel.rejoinInProgress.collectAsState()
    val rejoinFailed by viewModel.rejoinFailed.collectAsState()
    val activeServerError by viewModel.activeServerError.collectAsState()
    val activeConnectionLost by viewModel.activeConnectionLost.collectAsState()
    val downloadStates by viewModel.downloadStates.collectAsState()

    // --- video calls ---
    val callState by viewModel.callState.collectAsState()
    val callRemoteVideo by viewModel.callRemoteVideo.collectAsState()
    val callLocalVideo by viewModel.callLocalVideo.collectAsState()
    val callAudioMuted by viewModel.callAudioMuted.collectAsState()
    val callVideoMuted by viewModel.callVideoMuted.collectAsState()
    val callUsingFrontCamera by viewModel.callUsingFrontCamera.collectAsState()

    // --- direct member chats ---
    val directContacts by viewModel.directContacts.collectAsState()
    val directLastMessages by viewModel.directLastMessages.collectAsState()
    val directAliveSessions by viewModel.directAliveSessions.collectAsState()

    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(lifecycleOwner) {
        CallManager.attachLifecycle(lifecycleOwner)
    }

    var pendingCallAction by remember { mutableStateOf<(() -> Unit)?>(null) }
    var callRequestInFlight by remember { mutableStateOf(false) }
    val callPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        callRequestInFlight = false
        if (grants.values.all { it }) {
            pendingCallAction?.invoke()
        } else {
            Toast.makeText(context, "需要摄像头和麦克风权限才能进行视频通话", Toast.LENGTH_SHORT).show()
        }
        pendingCallAction = null
    }

    fun requireCallPermission(action: () -> Unit) {
        val needed = buildList {
            add(Manifest.permission.CAMERA)
            add(Manifest.permission.RECORD_AUDIO)
        }.filter {
            ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isEmpty()) {
            action()
        } else if (callRequestInFlight) {
            // a permission prompt is already open; do not overwrite its action
            return
        } else {
            pendingCallAction = action
            callRequestInFlight = true
            callPermissionLauncher.launch(needed.toTypedArray())
        }
    }

    LaunchedEffect(Unit) {
        viewModel.callEvents.collect { message ->
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
        }
    }

    LaunchedEffect(Unit) {
        // member-first: the app listens from startup so any member can pull up
        // a direct chat with no confirmation. Request the notification
        // permission up front too — a user who only ever direct-chats (never
        // creates/joins a group) must still be prompted once, or background
        // message notifications would be silently dropped for them.
        requestLocalNetworkPermissionIfNeeded()
        requestNotifPermissionIfNeeded()
        viewModel.ensureListener()
    }

    LaunchedEffect(Unit) {
        viewModel.directEvents.collect { message ->
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
        }
    }

    LaunchedEffect(Unit) {
        // a handshake revealed a placeholder contact's real device id: re-key
        // the open chat screen so it keeps showing the live message list
        viewModel.directChatMigrations.collect { (fromId, toId) ->
            if (activeDirectPeerId == fromId) activeDirectPeerId = toId
        }
    }

    // back during a call hangs up instead of navigating
    BackHandler(enabled = callState !is CallManager.CallState.Idle) {
        viewModel.hangupCall()
    }

    // --- file transfer ---
    // null = the ACTIVE GROUP chat, otherwise the direct-chat peer id
    var pendingFileChat by remember { mutableStateOf<String?>(null) }
    var pendingDownload by remember { mutableStateOf<FileInfo?>(null) }
    var pendingDownloadIsDirect by remember { mutableStateOf(false) }
    val filePickerLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        val target = pendingFileChat
        pendingFileChat = null
        if (uri != null) {
            val name = queryFileName(context, uri)
            val size = queryFileSize(context, uri)
            val sent = if (target != null) viewModel.sendDirectFile(target, uri, name, size)
            else viewModel.sendFile(uri, name, size)
            if (!sent) {
                Toast.makeText(context, "文件发送失败：文件过大或未连接", Toast.LENGTH_SHORT).show()
            }
        }
    }
    val fileSaverLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/octet-stream")
    ) { uri ->
        // always clear the pending offer, even when the user cancels the
        // save dialog, so a later download can never reuse a stale fileInfo
        pendingDownload?.let { fi ->
            val isDirect = pendingDownloadIsDirect
            pendingDownloadIsDirect = false
            if (uri != null) {
                if (isDirect) viewModel.downloadDirectFile(fi, uri)
                else viewModel.downloadFile(fi, uri)
            }
        }
        pendingDownload = null
    }

    LaunchedEffect(Unit) {
        // Navigate on the ViewModel's pending-join state (survives config
        // changes), not on a one-shot SharedFlow that a moment without a
        // collector could swallow.
        viewModel.pendingJoinNavigation.collect { groupId ->
            if (groupId != null) {
                requestNotifPermissionIfNeeded()
                currentScreenName = Screen.GroupLobby.name
                viewModel.consumeJoinNavigation()
                viewModel.clearConnectionResult()
                viewModel.clearJoinState()
            }
        }
    }

    Box(modifier = Modifier.fillMaxSize()) {
    when (currentScreen) {
        Screen.GroupList -> {
            GroupListScreen(
                groups = groups,
                onGroupClick = { groupId ->
                    viewModel.switchToGroup(groupId)
                    currentScreenName = Screen.GroupLobby.name
                },
                onAddGroup = {
                    requestLocalNetworkPermissionIfNeeded()
                    currentScreenName = Screen.Setup.name
                },
                onOpenSettings = {
                    settingsFrom = Screen.GroupList.name
                    currentScreenName = Screen.Settings.name
                },
                onRemoveGroup = { groupId ->
                    viewModel.removeGroup(groupId)
                }
            )
        }
        Screen.Settings -> {
            SettingsScreen(
                nickname = viewModel.currentNickname(),
                localIp = viewModel.localIpAddress,
                localPort = viewModel.localPort,
                backgroundRunning = backgroundRunning,
                allIps = viewModel.allLocalIpAddresses,
                securityCode = viewModel.securityCode,
                onSaveNickname = viewModel::setNickname,
                onSavePort = viewModel::setPort,
                onToggleBackgroundRunning = viewModel::setBackgroundRunning,
                onBack = {
                    currentScreenName = settingsFrom ?: Screen.MemberList.name
                    settingsFrom = null
                }
            )
        }
        Screen.MemberList -> {
            MemberListScreen(
                contacts = directContacts,
                lastMessages = directLastMessages,
                onOpenGroups = { currentScreenName = Screen.GroupList.name },
                onOpenSettings = {
                    settingsFrom = Screen.MemberList.name
                    currentScreenName = Screen.Settings.name
                },
                onOpenChat = { contact ->
                    requireLocalNetworkPermission {
                        // open the chat right away — persisted history is
                        // viewable without a connection, offline sends queue
                        // as pending, and the ViewModel keeps dialing in the
                        // background (the screen re-keys itself if the
                        // handshake reveals the member's real device id)
                        viewModel.openDirectChat(contact)
                        activeDirectPeerId = contact.id
                        currentScreenName = Screen.DirectChat.name
                    }
                },
                onAddContact = viewModel::addDirectContact,
                onRemoveContact = viewModel::removeDirectContact
            )
        }
        Screen.DirectChat -> {
            var peerId = activeDirectPeerId
            if (peerId != null) {
                var contact = directContacts.find { it.id == peerId }
                if (contact == null && peerId.startsWith("ip:")) {
                    // the placeholder id was replaced by the member's real
                    // device id by a handshake: re-key the open chat to the
                    // contact with the same endpoint (self-heal in case the
                    // migration event raced with composition)
                    val endpoint = peerId.removePrefix("ip:")
                    contact = directContacts.firstOrNull { "${it.ip}:${it.port}" == endpoint }
                    if (contact != null) {
                        activeDirectPeerId = contact.id
                        peerId = contact.id
                    }
                }
                val directMessages by viewModel.directMessages(peerId)
                    .collectAsState(initial = emptyList())
                DirectChatScreen(
                    contactName = contact?.name ?: peerId,
                    contactIp = contact?.let { "${it.ip}:${it.port}" } ?: "",
                    connected = peerId in directAliveSessions,
                    messages = directMessages,
                    downloadStates = downloadStates,
                    onBack = {
                        viewModel.closeDirectChat(peerId)
                        activeDirectPeerId = null
                        currentScreenName = Screen.MemberList.name
                    },
                    onSend = { content -> viewModel.sendDirectMessage(peerId, content) },
                    onDelete = { msg ->
                        viewModel.deleteDirectMessage(peerId, msg.id, msg.senderId)
                    },
                    onCopy = { content ->
                        val clipboard = context.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                            as android.content.ClipboardManager
                        clipboard.setPrimaryClip(
                            android.content.ClipData.newPlainText("消息", content)
                        )
                    },
                    onCall = {
                        requireCallPermission {
                            viewModel.startDirectCall(peerId)
                        }
                    },
                    onPickFile = {
                        pendingFileChat = peerId
                        filePickerLauncher.launch(arrayOf("*/*"))
                    },
                    onDownloadFile = { fileInfo ->
                        pendingDownload = fileInfo
                        pendingDownloadIsDirect = true
                        fileSaverLauncher.launch(fileInfo.fileName)
                    }
                )
            } else {
                LaunchedEffect(Unit) { currentScreenName = Screen.MemberList.name }
            }
        }
        Screen.Setup -> {
            SetupScreen(
                localIpAddress = viewModel.localIpAddress,
                localPort = viewModel.localPort,
                isQuerying = isQuerying,
                isJoining = isJoining,
                queriedGroupInfo = queriedGroupInfo,
                queryError = queryError,
                connectionError = (connectionResult as? P2PManager.ConnectionResult.Error)?.message,
                onCreateGroup = { name, group ->
                    requireLocalNetworkPermission {
                        viewModel.createGroup(name, group)
                        requestNotifPermissionIfNeeded()
                        currentScreenName = Screen.GroupLobby.name
                    }
                },
                onQueryGroup = { name, group, ip, password ->
                    requireLocalNetworkPermission {
                        viewModel.queryGroup(name, group, ip, password)
                    }
                },
                onConfirmJoin = {
                    requireLocalNetworkPermission {
                        viewModel.confirmJoin()
                    }
                },
                onCancelJoin = viewModel::cancelJoin,
                onClearError = viewModel::clearConnectionResult,
                onBack = {
                    currentScreenName = Screen.GroupList.name
                }
            )
        }
        Screen.GroupLobby -> {
            PeerListScreen(
                groupName = activeGroupName,
                myName = activeMyName,
                localIpAddress = viewModel.localIpAddress,
                localPort = viewModel.localPort,
                isHost = activeIsHost,
                groupPassword = activeGroupPassword,
                numericGroupId = activeGroupId?.let { viewModel.activeGroupNumericId() },
                peers = activePeers,
                rejoinInProgress = rejoinInProgress,
                rejoinFailed = rejoinFailed,
                connectionLost = activeConnectionLost,
                serverError = activeServerError,
                connectionResult = connectionResult,
                onClearConnectionResult = {
                    viewModel.clearConnectionResult()
                },
                onRetryHost = viewModel::retryHostListening,
                onReconnect = viewModel::reconnectActiveGroup,
                onLeave = {
                    viewModel.leaveActiveGroup()
                    currentScreenName = Screen.GroupList.name
                },
                onBack = {
                    currentScreenName = Screen.GroupList.name
                },
                onOpenChat = {
                    activeGroupId?.let { viewModel.clearUnread(it) }
                    currentScreenName = Screen.Chat.name
                },
                onCallPeer = { peerId ->
                    requireCallPermission {
                        viewModel.startCall(peerId)
                    }
                }
            )
        }
        Screen.Chat -> {
            ChatScreen(
                groupId = activeGroupId ?: "",
                groupName = activeGroupName,
                messages = activeMessages,
                groups = groups,
                connectionLost = activeConnectionLost,
                downloadStates = downloadStates,
                onSendMessage = viewModel::sendMessage,
                onForward = viewModel::sendMessageToGroup,
                onDelete = viewModel::deleteMessage,
                onPickFile = {
                    filePickerLauncher.launch(arrayOf("*/*"))
                },
                onDownloadFile = { fileInfo ->
                    pendingDownload = fileInfo
                    fileSaverLauncher.launch(fileInfo.fileName)
                },
                onOpenFile = { uriString -> openDownloadedFile(context, uriString) },
                onBack = {
                    currentScreenName = Screen.GroupLobby.name
                }
            )
        }
    }

    if (showPermissionDialog) {
        AlertDialog(
            onDismissRequest = {
                showPermissionDialog = false
                pendingPermissionAction = null
            },
            title = { Text("需要本地网络权限") },
            text = { Text("创建或加入群组需要访问本地网络的权限。请在权限弹窗中允许“附近的设备”，否则将无法连接群组。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showPermissionDialog = false
                        localNetworkPermissionLauncher.launch("android.permission.ACCESS_LOCAL_NETWORK")
                    }
                ) {
                    Text("重新请求")
                }
            },
            dismissButton = {
                TextButton(
                    onClick = {
                        showPermissionDialog = false
                        pendingPermissionAction = null
                    }
                ) {
                    Text("取消")
                }
            }
        )
    }

    (callState as? CallManager.CallState.Incoming)?.let { incoming ->
        AlertDialog(
            onDismissRequest = { viewModel.rejectCall() },
            title = { Text("📹 视频通话邀请") },
            text = { Text("${incoming.callerName} 邀请你进行视频通话") },
            confirmButton = {
                TextButton(
                    onClick = {
                        requireCallPermission { viewModel.acceptCall() }
                    }
                ) {
                    Text("接听", color = androidx.compose.ui.graphics.Color(0xFF2E7D32))
                }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.rejectCall() }) {
                    Text("拒绝", color = MaterialTheme.colorScheme.error)
                }
            }
        )
    }

    CallOverlay(
        state = callState,
        remoteVideo = callRemoteVideo,
        localVideo = callLocalVideo,
        audioMuted = callAudioMuted,
        videoMuted = callVideoMuted,
        usingFrontCamera = callUsingFrontCamera,
        onToggleAudio = { viewModel.setCallAudioMuted(!callAudioMuted) },
        onToggleVideo = { viewModel.setCallVideoMuted(!callVideoMuted) },
        onSwitchCamera = viewModel::switchCallCamera,
        onHangup = viewModel::hangupCall
    )
    }
}

/** Display name of a content Uri, falling back to "文件". */
private fun queryFileName(context: Context, uri: Uri): String {
    var name = "文件"
    context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
        val idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
        if (idx >= 0 && cursor.moveToFirst()) {
            name = cursor.getString(idx) ?: "文件"
        }
    }
    return name
}

/** Byte size of a content Uri, 0 when unknown. */
private fun queryFileSize(context: Context, uri: Uri): Long {
    context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
        val idx = cursor.getColumnIndex(OpenableColumns.SIZE)
        if (idx >= 0 && cursor.moveToFirst() && !cursor.isNull(idx)) {
            return cursor.getLong(idx)
        }
    }
    return 0L
}

/** Open a downloaded file with the default viewer. The stored uri is a
 *  content uri from CreateDocument; request read access for the target app. */
private fun openDownloadedFile(context: Context, uriString: String) {
    val uri = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
    runCatching {
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/octet-stream")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(intent)
    }.onFailure {
        Toast.makeText(context, "无法打开文件", Toast.LENGTH_SHORT).show()
    }
}
