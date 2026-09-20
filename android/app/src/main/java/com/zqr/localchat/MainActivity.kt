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
import androidx.core.content.FileProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zqr.localchat.call.CallManager
import com.zqr.localchat.call.GroupCallManager
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MAX_FOLDER_FILES
import com.zqr.localchat.data.detectMediaKind
import com.zqr.localchat.data.replyPreviewText
import com.zqr.localchat.network.GroupAuth
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.ui.screen.CallOverlay
import com.zqr.localchat.ui.screen.GroupCallOverlay
import com.zqr.localchat.ui.QrContactConfirmDialog
import com.zqr.localchat.ui.QrScanOverlay
import com.zqr.localchat.ui.QrShowDialog
import com.zqr.localchat.ui.screen.ChatScreen
import com.zqr.localchat.ui.screen.DirectChatScreen
import com.zqr.localchat.ui.screen.GroupListScreen
import com.zqr.localchat.ui.screen.MemberListScreen
import com.zqr.localchat.ui.screen.PeerListScreen
import com.zqr.localchat.ui.screen.SearchScreen
import com.zqr.localchat.ui.screen.SetupScreen
import com.zqr.localchat.ui.screen.SettingsScreen
import com.zqr.localchat.ui.theme.LocalChatTheme
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull

enum class Screen { GroupList, Setup, GroupLobby, Chat, MemberList, DirectChat, Settings, Search }

/** Reaction snapshot for one conversation: {msgId: [(emoji, actorId), …]}.
 *  A typealias keeps the state declarations below off the `>>>` token, which
 *  the newer Kotlin lexer otherwise reads as the unsigned-shift operator. */
private typealias ReactionMap = Map<String, List<Pair<String, String>>>

class MainActivity : ComponentActivity() {

    companion object {
        const val EXTRA_OPEN_GROUP_ID = "com.zqr.localchat.OPEN_GROUP_ID"
        /** Missed-call notification deep link: the peer whose 1:1 chat to open. */
        const val EXTRA_OPEN_DIRECT_PEER_ID = "com.zqr.localchat.OPEN_DIRECT_PEER_ID"
        const val EXTRA_OPEN_DIRECT_ID = "com.zqr.localchat.OPEN_DIRECT_ID"
    }

    // notification tap deep link: the group to jump straight into (null = none)
    private val openGroupId = mutableStateOf<String?>(null)
    // missed-call notification tap: the direct-chat peer to jump into
    private val openDirectPeerId = mutableStateOf<String?>(null)
    // notification tap deep link: the direct chat's peer id (null = none)
    private val openDirectId = mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // only a FRESH create delivers the launch intent's deep link: on
        // recreation the link was already consumed (or is still pending in
        // the running effect), and the screen state is restored by
        // rememberSaveable — re-reading here would re-navigate on rotation
        if (savedInstanceState == null) {
            openGroupId.value = intent?.getStringExtra(EXTRA_OPEN_GROUP_ID)
            openDirectPeerId.value = intent?.getStringExtra(EXTRA_OPEN_DIRECT_PEER_ID)
            openDirectId.value = intent?.getStringExtra(EXTRA_OPEN_DIRECT_ID)
        }
        enableEdgeToEdge()
        setContent {
            LocalChatTheme {
                LocalChatApp(
                    openGroupId = openGroupId,
                    openDirectPeerId = openDirectPeerId,
                    openDirectId = openDirectId
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.getStringExtra(EXTRA_OPEN_GROUP_ID)?.let { openGroupId.value = it }
        intent.getStringExtra(EXTRA_OPEN_DIRECT_PEER_ID)?.let { openDirectPeerId.value = it }
        intent.getStringExtra(EXTRA_OPEN_DIRECT_ID)?.let { openDirectId.value = it }
    }
}

@Composable
fun LocalChatApp(
    viewModel: ChatViewModel = viewModel(),
    openGroupId: MutableState<String?> = remember { mutableStateOf<String?>(null) },
    openDirectPeerId: MutableState<String?> = remember { mutableStateOf<String?>(null) },
    openDirectId: MutableState<String?> = remember { mutableStateOf<String?>(null) }
) {
    val context = LocalContext.current
    val mediaVersion by viewModel.mediaVersion.collectAsState()
    var currentScreenName by rememberSaveable { mutableStateOf(Screen.MemberList.name) }
    val currentScreen = runCatching { Screen.valueOf(currentScreenName) }.getOrDefault(Screen.MemberList)

    // the direct chat currently open (member-first navigation state)
    var activeDirectPeerId by remember { mutableStateOf<String?>(null) }
    // where the settings screen was opened from, so back returns there
    var settingsFrom by remember { mutableStateOf<String?>(null) }
    // a search-result jump: (conversationKey, messageId) the chat screen must
    // scroll to and highlight once it is up; cleared when handled
    var revealTarget by remember { mutableStateOf<Pair<String, String>?>(null) }

    BackHandler(enabled = currentScreen != Screen.MemberList) {
        currentScreenName = when (currentScreen) {
            Screen.Chat -> Screen.GroupLobby.name
            Screen.Setup -> Screen.GroupList.name
            Screen.GroupLobby -> Screen.GroupList.name
            Screen.GroupList -> Screen.MemberList.name
            Screen.DirectChat -> Screen.MemberList.name
            Screen.Settings -> settingsFrom ?: Screen.MemberList.name
            Screen.Search -> Screen.MemberList.name
            Screen.MemberList -> Screen.MemberList.name
        }
        if (currentScreen == Screen.Settings) settingsFrom = null
        if (currentScreen == Screen.DirectChat) {
            // keep the session alive: with presence ("app running = online")
            // backing out of a chat must not tear the connection down
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
    // Group member device-identity bindings (TOFU, GroupAuth): recomputed when
    // the member set or the messages change, so a member that just sent its
    // first signed message immediately shows the 已验证 badge + 安全码.
    val activeMemberFingerprints: Map<String, String> = remember(
        activeGroupId, activePeers, activeMessages
    ) {
        val gid = activeGroupId ?: return@remember emptyMap()
        activePeers.keys.associateWith { pid ->
            GroupAuth.memberFingerprint(gid, pid).orEmpty()
        }
    }
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
    val folderDownloadStates by viewModel.folderDownloadStates.collectAsState()
    // message-experience extras (reactions / pins / group read receipts)
    val extrasVersion by viewModel.extrasVersion.collectAsState()
    var groupReactions by remember { mutableStateOf<ReactionMap>(emptyMap()) }
    var groupPins by remember {
        mutableStateOf<List<com.zqr.localchat.data.PinnedMessage>>(emptyList())
    }
    var groupReaders by remember { mutableStateOf<Map<String, List<String>>>(emptyMap()) }
    LaunchedEffect(activeGroupId, extrasVersion) {
        val gid = activeGroupId ?: return@LaunchedEffect
        groupReactions = viewModel.reactionsFor(gid)
        groupPins = viewModel.pinsFor(gid)
        groupReaders = viewModel.groupReadersFor(gid)
    }
    // direct-chat reactions are keyed by "direct:<peerId>"; re-read on open
    // and on any extras bump
    var directReactions by remember { mutableStateOf<ReactionMap>(emptyMap()) }
    var directPins by remember {
        mutableStateOf<List<com.zqr.localchat.data.PinnedMessage>>(emptyList())
    }
    LaunchedEffect(activeDirectPeerId, extrasVersion) {
        val pid = activeDirectPeerId ?: return@LaunchedEffect
        directReactions = viewModel.reactionsFor("direct:$pid")
        directPins = viewModel.pinsFor("direct:$pid")
    }

    // --- video calls ---
    val callState by viewModel.callState.collectAsState()
    val callRemoteVideo by viewModel.callRemoteVideo.collectAsState()
    val callLocalVideo by viewModel.callLocalVideo.collectAsState()
    val callAudioMuted by viewModel.callAudioMuted.collectAsState()
    val callVideoMuted by viewModel.callVideoMuted.collectAsState()
    val callUsingFrontCamera by viewModel.callUsingFrontCamera.collectAsState()

    // --- group voice conference ---
    val groupCallState by viewModel.groupCallState.collectAsState()
    val groupCallParticipants by viewModel.groupCallParticipants.collectAsState()
    val groupCallAudioMuted by viewModel.groupCallAudioMuted.collectAsState()

    // --- QR invites ---
    // null = no scan in progress; otherwise the scan purpose ("contact" |
    // "group") decides what happens with the decoded payload
    var qrScanPurpose by remember { mutableStateOf<String?>(null) }
    var pendingContactInvite by remember {
        mutableStateOf<com.zqr.localchat.network.ContactInvite?>(null)
    }
    var scannedGroupInvite by remember {
        mutableStateOf<com.zqr.localchat.network.GroupInvite?>(null)
    }
    var showMyQr by remember { mutableStateOf(false) }
    var showInviteQr by remember { mutableStateOf(false) }

    fun handleScannedPayload(text: String) {
        when (val invite = viewModel.parseQrInvite(text)) {
            is com.zqr.localchat.network.ContactInvite -> {
                if (qrScanPurpose == "contact") {
                    pendingContactInvite = invite
                } else {
                    Toast.makeText(context, "这是联系人二维码，请在成员页“添加成员”中使用", Toast.LENGTH_SHORT).show()
                }
            }
            is com.zqr.localchat.network.GroupInvite -> {
                if (qrScanPurpose == "group") {
                    if (invite.ip.isBlank()) {
                        // Windows parity: a relay-only invite cannot prefill a
                        // joinable address on this end
                        Toast.makeText(
                            context,
                            "该邀请未包含加入地址：双方需填写同一个中继服务器后加入",
                            Toast.LENGTH_LONG
                        ).show()
                    }
                    scannedGroupInvite = invite
                    currentScreenName = Screen.Setup.name
                } else {
                    Toast.makeText(context, "这是群邀请二维码，请在“加入群组”页使用", Toast.LENGTH_SHORT).show()
                }
            }
            else -> {
                Toast.makeText(context, "二维码内容无法识别", Toast.LENGTH_SHORT).show()
            }
        }
        qrScanPurpose = null
    }

    // --- direct member chats ---
    val directContacts by viewModel.directContacts.collectAsState()
    val directLastMessages by viewModel.directLastMessages.collectAsState()
    val directAliveSessions by viewModel.directAliveSessions.collectAsState()
    val directContactRequests by viewModel.directContactRequests.collectAsState()
    val directTypingPeers by viewModel.directTypingPeers.collectAsState()
    val groupTyping by viewModel.groupTyping.collectAsState()

    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(lifecycleOwner) {
        CallManager.attachLifecycle(lifecycleOwner)
    }

    var pendingCallAction by remember { mutableStateOf<(() -> Unit)?>(null) }
    var callRequestInFlight by remember { mutableStateOf(false) }
    var callRequestAudioOnly by remember { mutableStateOf(false) }
    val callPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        callRequestInFlight = false
        if (grants.values.all { it }) {
            pendingCallAction?.invoke()
        } else {
            Toast.makeText(
                context,
                if (callRequestAudioOnly) "需要麦克风权限才能进行语音通话"
                else "需要摄像头和麦克风权限才能进行视频通话",
                Toast.LENGTH_SHORT
            ).show()
        }
        pendingCallAction = null
    }

    fun requireCallPermission(audioOnly: Boolean = false, action: () -> Unit) {
        val needed = buildList {
            // a voice-only call needs the microphone but never the camera
            if (!audioOnly) add(Manifest.permission.CAMERA)
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
            callRequestAudioOnly = audioOnly
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
        viewModel.groupCallEvents.collect { message ->
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
        // group management notices: kicked out, member removed
        viewModel.groupEvents.collect { message ->
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
    BackHandler(enabled = groupCallState !is GroupCallManager.GroupCallState.Idle) {
        viewModel.hangupGroupCall()
    }

    // --- file transfer ---
    // null = the ACTIVE GROUP chat, otherwise the direct-chat peer id
    var pendingFileChat by remember { mutableStateOf<String?>(null) }
    // kind preset for the pending pick: FILE = the generic picker (kind is
    // detected from the picked document), IMAGE/VIDEO = the media pickers
    var pendingFileKind by remember { mutableStateOf(FileKind.FILE) }
    var pendingDownload by remember { mutableStateOf<FileInfo?>(null) }
    var pendingDownloadIsDirect by remember { mutableStateOf(false) }
    // pending folder send/save: the chat the folder pick was launched from
    // (null = active group, otherwise the direct-chat peer id)
    var pendingFolderSendChat by remember { mutableStateOf<String?>(null) }
    // pending folder save: (folderId, direct-chat peer id) awaiting the tree
    // picker result; a null peer id means the active group
    var pendingFolderDownload by remember { mutableStateOf<Pair<String, String?>?>(null) }
    val filePickerLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        val target = pendingFileChat
        val presetKind = pendingFileKind
        pendingFileChat = null
        pendingFileKind = FileKind.FILE
        if (uri != null) {
            val name = queryFileName(context, uri)
            val size = queryFileSize(context, uri)
            val kind = if (presetKind != FileKind.FILE) presetKind
            else detectKind(context, uri, name)
            val sent = if (target != null) viewModel.sendDirectFile(target, uri, name, size, kind)
            else viewModel.sendFile(uri, name, size, kind)
            if (!sent) {
                Toast.makeText(context, "文件发送失败：文件过大或未连接", Toast.LENGTH_SHORT).show()
            }
        }
    }
    val folderPickerLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri ->
        val target = pendingFolderSendChat
        pendingFolderSendChat = null
        if (uri != null) {
            val name = queryTreeDisplayName(context, uri)
            // sendFolder/sendDirectFolder do the SAF walk + per-entry offers on
            // Dispatchers.IO (a large folder must never block the main thread,
            // ANR); the result — including the truncation notice — comes back
            // here on the main thread
            val onSent: (Boolean, Boolean) -> Unit = { sent, truncated ->
                if (truncated) {
                    Toast.makeText(
                        context,
                        "文件夹超过 ${MAX_FOLDER_FILES} 个文件，仅发送前 ${MAX_FOLDER_FILES} 个",
                        Toast.LENGTH_SHORT
                    ).show()
                }
                if (!sent) {
                    Toast.makeText(
                        context, "文件夹发送失败：没有可发送的文件或未连接", Toast.LENGTH_SHORT
                    ).show()
                }
            }
            if (target != null) viewModel.sendDirectFolder(target, uri, name, onSent)
            else viewModel.sendFolder(uri, name, onSent)
        }
    }
    val folderSaverLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri ->
        // always clear the pending folder, even when the user cancels the
        // picker, so a later save can never reuse a stale folderId
        val pending = pendingFolderDownload
        pendingFolderDownload = null
        if (uri != null && pending != null) {
            val (folderId, peerId) = pending
            if (peerId != null) viewModel.downloadDirectFolder(peerId, folderId, uri)
            else viewModel.downloadFolder(folderId, uri)
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

    fun launchFilePicker(chatId: String?, kind: String, mimes: Array<String>) {
        pendingFileChat = chatId
        pendingFileKind = kind
        filePickerLauncher.launch(mimes)
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

    LaunchedEffect(Unit) {
        // notification tap: jump straight into that group's chat. Collected
        // as a flow, NOT keyed on the state — nulling the state inside a
        // keyed effect would change the key and cancel the effect mid-wait.
        // On a cold start the persisted-group load is async, so wait for the
        // target to appear (timeout = drop a link to a group that no longer
        // exists).
        snapshotFlow { openGroupId.value }
            .filterNotNull()
            .collect { gid ->
                openGroupId.value = null
                val found = withTimeoutOrNull(5000) {
                    snapshotFlow { groups.any { it.groupId == gid } }.first { it }
                } ?: false
                if (!found) return@collect
                viewModel.switchToGroup(gid)
                viewModel.clearUnread(gid)
                currentScreenName = Screen.Chat.name
            }
    }

    LaunchedEffect(Unit) {
        // missed-call notification tap: jump straight into that 1:1 chat. On a
        // cold start the persisted contacts load asynchronously, so wait for
        // the peer to appear (timeout = drop a link to a removed contact).
        snapshotFlow { openDirectPeerId.value }
            .filterNotNull()
            .collect { pid ->
                openDirectPeerId.value = null
                val contact = withTimeoutOrNull(5000) {
                    snapshotFlow { directContacts.find { it.id == pid } }.first { it != null }
                }
                if (contact == null) return@collect
                requireLocalNetworkPermission {
                    viewModel.openDirectChat(contact)
                    activeDirectPeerId = contact.id
                    currentScreenName = Screen.DirectChat.name
                }
            }
    }

    LaunchedEffect(Unit) {
        // notification tap on a 1:1 chat: open it once the contact is known
        // (a cold start loads the contact list asynchronously). Kept in its
        // own effect: collect never returns, so two collectors cannot share
        // one LaunchedEffect — the second would never start.
        snapshotFlow { openDirectId.value }
            .filterNotNull()
            .collect { peerId ->
                openDirectId.value = null
                val found = withTimeoutOrNull(5000) {
                    snapshotFlow { directContacts.any { it.id == peerId } }.first { it }
                } ?: false
                if (!found) return@collect
                val contact = directContacts.find { it.id == peerId } ?: return@collect
                requireLocalNetworkPermission {
                    viewModel.openDirectChat(contact)
                    activeDirectPeerId = peerId
                    currentScreenName = Screen.DirectChat.name
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
                },
                onToggleMute = viewModel::setGroupMuted
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
                requests = directContactRequests,
                lastMessages = directLastMessages,
                onOpenGroups = { currentScreenName = Screen.GroupList.name },
                onOpenSettings = {
                    settingsFrom = Screen.MemberList.name
                    currentScreenName = Screen.Settings.name
                },
                onOpenSearch = { currentScreenName = Screen.Search.name },
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
                onRemoveContact = viewModel::removeDirectContact,
                onAcceptRequest = viewModel::acceptContactRequest,
                onIgnoreRequest = viewModel::ignoreContactRequest,
                onScanContactQr = { qrScanPurpose = "contact" },
                onShowMyQr = { showMyQr = true }
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
                val callLogs by viewModel.callLogsFor(peerId)
                    .collectAsState(initial = emptyList())
                DirectChatScreen(
                    contactName = contact?.name ?: peerId,
                    contactIp = contact?.let { "${it.ip}:${it.port}" } ?: "",
                    connected = peerId in directAliveSessions,
                    messages = directMessages,
                    callLogs = callLogs,
                    downloadStates = downloadStates,
                    peerTyping = peerId in directTypingPeers,
                    onBack = {
                        // keep the session alive (presence re-establishes
                        // anyway; closing only causes offline flicker)
                        viewModel.endDirectTyping(peerId)
                        activeDirectPeerId = null
                        currentScreenName = Screen.MemberList.name
                    },
                    onSend = { content, reply ->
                        viewModel.sendDirectMessage(
                            peerId,
                            content,
                            reply?.id,
                            reply?.replyPreviewText(),
                            reply?.senderName
                        )
                    },
                    onTyping = { viewModel.notifyDirectTyping(peerId) },
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
                    onCallAudio = {
                        requireCallPermission(audioOnly = true) {
                            viewModel.startDirectCall(peerId, CallManager.MEDIA_AUDIO)
                        }
                    },
                    onCallBack = { media ->
                        requireCallPermission(audioOnly = media == CallManager.MEDIA_AUDIO) {
                            viewModel.startDirectCall(peerId, media)
                        }
                    },
                    onPickFile = {
                        launchFilePicker(peerId, FileKind.FILE, arrayOf("*/*"))
                    },
                    onPickImage = {
                        launchFilePicker(peerId, FileKind.IMAGE, arrayOf("image/*"))
                    },
                    onPickVideo = {
                        launchFilePicker(peerId, FileKind.VIDEO, arrayOf("video/*"))
                    },
                    onDownloadFile = { fileInfo ->
                        // a paused download resumes into its stored target;
                        // only a fresh download opens the save dialog
                        if (!viewModel.resumeFile(fileInfo, isDirect = true)) {
                            pendingDownload = fileInfo
                            pendingDownloadIsDirect = true
                            fileSaverLauncher.launch(fileInfo.fileName)
                        }
                    },
                    onPickFolder = {
                        pendingFolderSendChat = peerId
                        folderPickerLauncher.launch(null)
                    },
                    folderDownloadStates = folderDownloadStates,
                    onDownloadFolder = { folderId ->
                        // paused save: reuse the remembered destination tree
                        val resumeTarget = viewModel.folderResumeTarget(folderId)
                        if (resumeTarget != null) {
                            viewModel.downloadDirectFolder(
                                peerId, folderId, android.net.Uri.parse(resumeTarget)
                            )
                        } else {
                            pendingFolderDownload = folderId to peerId
                            folderSaverLauncher.launch(null)
                        }
                    },
                    onDeleteFolder = { group ->
                        group.entries.forEach {
                            viewModel.deleteDirectMessage(peerId, it.id, it.senderId)
                        }
                    },
                    onDownloadMedia = { fileInfo ->
                        viewModel.downloadMedia(fileInfo, isDirect = true)
                    },
                    resolveMedia = { fileInfo -> viewModel.localMediaPath(fileInfo) },
                    mediaVersion = mediaVersion,
                    onOpenFile = { uriString -> openDownloadedFile(context, uriString) },
                    revealMessageId = revealTarget
                        ?.takeIf { it.first == "direct:$peerId" }?.second,
                    onRevealHandled = { revealTarget = null },
                    reactions = directReactions,
                    pins = directPins,
                    myDeviceId = viewModel.myDeviceId,
                    onToggleReaction = { messageId, emoji, active ->
                        viewModel.toggleDirectReaction(peerId, messageId, emoji, active)
                    },
                    onTogglePin = { messageId, active ->
                        viewModel.toggleDirectPin(peerId, messageId, active)
                    },
                    onEditMessage = { messageId, newContent ->
                        viewModel.editDirectMessage(peerId, messageId, newContent)
                    },
                    onSendVoice = { path ->
                        val f = java.io.File(path)
                        viewModel.sendDirectFile(
                            peerId,
                            android.net.Uri.fromFile(f), f.name, f.length(), FileKind.AUDIO
                        )
                    }
                )
            } else {
                LaunchedEffect(Unit) { currentScreenName = Screen.MemberList.name }
            }
        }
        Screen.Search -> {
            SearchScreen(
                viewModel = viewModel,
                onOpenResult = { conversationId, messageId ->
                    // remember the jump target: the chat screen scrolls to it
                    // and highlights the message, then clears it
                    revealTarget = conversationId to messageId
                    if (conversationId.startsWith("direct:")) {
                        val peerId = conversationId.removePrefix("direct:")
                        val contact = directContacts.find { it.id == peerId }
                        if (contact != null) {
                            requireLocalNetworkPermission {
                                viewModel.openDirectChat(contact)
                                activeDirectPeerId = peerId
                                currentScreenName = Screen.DirectChat.name
                            }
                        }
                    } else {
                        viewModel.switchToGroup(conversationId)
                        viewModel.clearUnread(conversationId)
                        currentScreenName = Screen.Chat.name
                    }
                },
                onBack = { currentScreenName = Screen.MemberList.name }
            )
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
                scannedGroupInvite = scannedGroupInvite,
                onScanGroupQr = { qrScanPurpose = "group" },
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
                },
                onCallAudioPeer = { peerId ->
                    requireCallPermission(audioOnly = true) {
                        viewModel.startCall(peerId, CallManager.MEDIA_AUDIO)
                    }
                },
                onStartConference = {
                    requireCallPermission(audioOnly = true) { viewModel.startGroupCall() }
                },
                announcement = groups.find { it.groupId == activeGroupId }?.announcement ?: "",
                onUpdateGroupInfo = viewModel::updateGroupInfo,
                onKickMember = viewModel::kickMember,
                groupId = activeGroupId,
                memberFingerprints = activeMemberFingerprints,
                onShowInviteQr = { showInviteQr = true }
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
                typingNames = groupTyping[activeGroupId]?.values?.toList() ?: emptyList(),
                onSendMessage = { content, reply, mentions ->
                    viewModel.sendMessage(
                        content,
                        reply?.id,
                        reply?.replyPreviewText(),
                        reply?.senderName,
                        mentions
                    )
                },
                onTyping = { viewModel.notifyGroupTyping() },
                onVisible = { viewModel.notifyGroupReadReceipt() },
                onForward = viewModel::sendMessageToGroup,
                onDelete = viewModel::deleteMessage,
                onPickFile = {
                    launchFilePicker(null, FileKind.FILE, arrayOf("*/*"))
                },
                onPickImage = {
                    launchFilePicker(null, FileKind.IMAGE, arrayOf("image/*"))
                },
                onPickVideo = {
                    launchFilePicker(null, FileKind.VIDEO, arrayOf("video/*"))
                },
                onDownloadFile = { fileInfo ->
                    // a paused download resumes into its stored target; only a
                    // fresh download opens the save dialog
                    if (!viewModel.resumeFile(fileInfo, isDirect = false)) {
                        pendingDownload = fileInfo
                        fileSaverLauncher.launch(fileInfo.fileName)
                    }
                },
                onPickFolder = {
                    pendingFolderSendChat = null
                    folderPickerLauncher.launch(null)
                },
                folderDownloadStates = folderDownloadStates,
                onDownloadFolder = { folderId ->
                    // paused save: reuse the remembered destination tree
                    val resumeTarget = viewModel.folderResumeTarget(folderId)
                    if (resumeTarget != null) {
                        viewModel.downloadFolder(folderId, android.net.Uri.parse(resumeTarget))
                    } else {
                        pendingFolderDownload = folderId to null
                        folderSaverLauncher.launch(null)
                    }
                },
                onDeleteFolder = { group ->
                    group.entries.forEach { viewModel.deleteMessage(it.id) }
                },
                reactions = groupReactions,
                pins = groupPins,
                groupReaders = groupReaders,
                // denominator = OTHER members, from the PERSISTED group meta
                // (Windows member_count - 1): the live peer set shrinks as
                // members disconnect, which made a stalled receipt show 已读
                memberCount = (
                    (groups.firstOrNull { it.groupId == activeGroupId }?.memberCount
                        ?: (activePeers.size + 1)) - 1
                    ).coerceAtLeast(1),
                myDeviceId = viewModel.myDeviceId,
                members = activePeers.values.map { it.id to it.name },
                onToggleReaction = { messageId, emoji, active ->
                    viewModel.toggleGroupReaction(messageId, emoji, active)
                },
                onTogglePin = { messageId, active ->
                    viewModel.toggleGroupPin(messageId, active)
                },
                onEditMessage = { messageId, newContent ->
                    viewModel.editMessage(messageId, newContent)
                },
                onSendVoice = { path ->
                    val f = java.io.File(path)
                    viewModel.sendFile(
                        android.net.Uri.fromFile(f), f.name, f.length(), FileKind.AUDIO
                    )
                },
                onDownloadMedia = { fileInfo ->
                    viewModel.downloadMedia(fileInfo, isDirect = false)
                },
                resolveMedia = { fileInfo -> viewModel.localMediaPath(fileInfo) },
                mediaVersion = mediaVersion,
                onOpenFile = { uriString -> openDownloadedFile(context, uriString) },
                revealMessageId = revealTarget
                    ?.takeIf { it.first == activeGroupId }?.second,
                onRevealHandled = { revealTarget = null },
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
        val incomingAudio = incoming.media == CallManager.MEDIA_AUDIO
        AlertDialog(
            onDismissRequest = { viewModel.rejectCall() },
            title = { Text(if (incomingAudio) "🎙 语音通话邀请" else "📹 视频通话邀请") },
            text = {
                Text(
                    "${incoming.callerName} 邀请你进行" +
                        (if (incomingAudio) "语音通话" else "视频通话")
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        requireCallPermission(audioOnly = incomingAudio) { viewModel.acceptCall() }
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

    (groupCallState as? GroupCallManager.GroupCallState.Incoming)?.let { incoming ->
        AlertDialog(
            onDismissRequest = { viewModel.declineGroupCall() },
            title = { Text("🎙 语音会议邀请") },
            text = { Text("${incoming.hostName} 邀请你加入群组语音会议") },
            confirmButton = {
                TextButton(
                    onClick = {
                        requireCallPermission(audioOnly = true) { viewModel.acceptGroupCall() }
                    }
                ) {
                    Text("加入", color = androidx.compose.ui.graphics.Color(0xFF2E7D32))
                }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.declineGroupCall() }) {
                    Text("拒绝", color = MaterialTheme.colorScheme.error)
                }
            }
        )
    }

    GroupCallOverlay(
        state = groupCallState,
        participants = groupCallParticipants,
        audioMuted = groupCallAudioMuted,
        onToggleAudio = { viewModel.setGroupCallAudioMuted(!groupCallAudioMuted) },
        onHangup = viewModel::hangupGroupCall
    )

    // --- QR invite dialogs ---
    if (showMyQr) {
        QrShowDialog(
            title = "我的二维码",
            payload = viewModel.qrContactPayload(),
            extraRows = listOf(
                "名字" to viewModel.currentNickname().ifBlank { "用户" },
                "本机安全码（请与对方核对）" to viewModel.securityCode.ifBlank { "未生成" }
            ),
            onDismiss = { showMyQr = false }
        )
    }
    if (showInviteQr) {
        val payload = viewModel.qrGroupInvitePayload()
        if (payload == null) {
            // the lobby only shows the button for a host; this guards a state
            // flip between tap and render
            SideEffect { showInviteQr = false }
        } else {
            QrShowDialog(
                title = "群邀请二维码",
                payload = payload,
                extraRows = listOf(
                    "群组名称" to activeGroupName,
                    "群组数字ID" to (activeGroupId?.let { viewModel.activeGroupNumericId() } ?: "")
                ),
                onDismiss = { showInviteQr = false }
            )
        }
    }
    pendingContactInvite?.let { invite ->
        QrContactConfirmDialog(
            invite = invite,
            onConfirm = {
                if (!viewModel.addDirectContact(
                        "${invite.ip}:${invite.port}", invite.name, invite.fingerprint
                    )
                ) {
                    Toast.makeText(context, "二维码中的地址无效", Toast.LENGTH_SHORT).show()
                }
                pendingContactInvite = null
            },
            onDismiss = { pendingContactInvite = null }
        )
    }
    if (qrScanPurpose != null) {
        QrScanOverlay(
            onFound = { handleScannedPayload(it) },
            onDismiss = { qrScanPurpose = null }
        )
    }
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

/** Display name of a picked folder tree, falling back to "文件夹". */
private fun queryTreeDisplayName(context: Context, treeUri: Uri): String {
    return runCatching {
        val docId = android.provider.DocumentsContract.getTreeDocumentId(treeUri)
        val docUri = android.provider.DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)
        context.contentResolver.query(
            docUri,
            arrayOf(android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME),
            null, null, null
        )?.use { c ->
            if (c.moveToFirst() && !c.isNull(0)) c.getString(0) else null
        }
    }.getOrNull()?.takeIf { it.isNotBlank() } ?: "文件夹"
}

/** Classify a picked document: the provider-reported MIME type wins, the
 *  file-name extension is the fallback. Used so an image/video picked with
 *  the GENERIC picker still goes out as an inline media message. */
private fun detectKind(context: Context, uri: Uri, fileName: String): String {
    return when (context.contentResolver.getType(uri)) {
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
        "image/heic", "image/heif" -> FileKind.IMAGE
        "video/mp4", "video/quicktime", "video/x-matroska", "video/webm",
        "video/x-msvideo", "video/3gpp" -> FileKind.VIDEO
        else -> detectMediaKind(fileName)
    }
}

/** MIME type for a local media file path (by extension). */
private fun mimeForPath(path: String): String {
    return when (path.substringAfterLast('.', "").lowercase()) {
        "jpg", "jpeg" -> "image/jpeg"
        "png" -> "image/png"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "bmp" -> "image/bmp"
        "heic" -> "image/heic"
        "mp4", "m4v" -> "video/mp4"
        "mov" -> "video/quicktime"
        "mkv" -> "video/x-matroska"
        "webm" -> "video/webm"
        "avi" -> "video/x-msvideo"
        "3gp" -> "video/3gpp"
        else -> "application/octet-stream"
    }
}

/** Open a downloaded file with the default viewer. The stored value is either
 *  a content uri from CreateDocument or a local media path from downloadMedia
 *  (shared through FileProvider, which the target app is allowed to read). */
private fun openDownloadedFile(context: Context, uriString: String) {
    val parsed = runCatching { Uri.parse(uriString) }.getOrNull() ?: return
    val uri = if (parsed.scheme == null || parsed.scheme == "file") {
        val file = java.io.File(uriString)
        if (!file.isFile) {
            Toast.makeText(context, "文件不存在", Toast.LENGTH_SHORT).show()
            return
        }
        runCatching {
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        }.getOrNull() ?: Uri.fromFile(file)
    } else {
        parsed
    }
    runCatching {
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, mimeForPath(uriString))
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(intent)
    }.onFailure {
        Toast.makeText(context, "无法打开文件", Toast.LENGTH_SHORT).show()
    }
}
