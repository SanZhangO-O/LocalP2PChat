package com.zqr.localchat.viewmodel

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Context.MODE_PRIVATE
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.util.Log
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import androidx.core.app.RemoteInput
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import androidx.lifecycle.viewModelScope
import com.zqr.localchat.ChatApp
import com.zqr.localchat.MainActivity
import com.zqr.localchat.NotificationDismissReceiver
import com.zqr.localchat.NotificationReplyReceiver
import com.zqr.localchat.call.CallManager
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.crypto.StoreCipher
import com.zqr.localchat.data.CallDirection
import com.zqr.localchat.data.CallLogEntity
import com.zqr.localchat.data.CallMedia
import com.zqr.localchat.data.CallResult
import com.zqr.localchat.data.ChatDao
import com.zqr.localchat.data.ChatDatabase
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.DeletedMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MAX_FOLDER_FILES
import com.zqr.localchat.data.MessageSearch
import com.zqr.localchat.data.Peer
import com.zqr.localchat.data.SavedChatMessage
import com.zqr.localchat.data.SavedGroup
import com.zqr.localchat.data.sanitizeRelativePath
import com.zqr.localchat.network.Constants
import com.zqr.localchat.network.DeviceIdentity
import com.zqr.localchat.network.DirectChatManager
import com.zqr.localchat.network.FileTransfer
import com.zqr.localchat.network.GroupInfo
import com.zqr.localchat.network.GroupMeshManager
import com.zqr.localchat.network.HostGroupServer
import com.zqr.localchat.network.LocalAddress
import com.zqr.localchat.network.NetworkPacket
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.network.Protocol
import com.zqr.localchat.network.Wire
import com.zqr.localchat.ui.screen.isValidHost
import com.zqr.localchat.ui.screen.parseHostPort
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.OutputStream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModel(application: Application) : AndroidViewModel(application) {

    data class GroupMeta(
        val groupId: String,
        val groupName: String,
        val isHost: Boolean,
        val hostIp: String = "",
        val hostPort: Int = 0,
        val memberCount: Int = 1,
        val lastMessage: String = "",
        val lastMessageTime: Long = 0L,
        val unreadCount: Int = 0,
        val muted: Boolean = false,
        val connected: Boolean = false,
        /** Owner-published group announcement (group_update), shown as a lobby
         *  banner and kept across restarts / while the host is offline. */
        val announcement: String = ""
    )

    private val groupP2pMap = mutableMapOf<String, P2PManager>()
    private val monitoringJobs = mutableMapOf<String, List<Job>>()
    private val _groupP2pVersion = MutableStateFlow(0)
    private var pendingP2pManager: P2PManager? = null
    private var pendingHostIp: String = ""
    private var pendingHostPort: Int = 0
    private var pendingGroupId: String? = null
    // Guard against re-adding a group whose deletion is in flight. Touched
    // from both the main thread (removeGroup) and IO coroutines (upsertGroup),
    // so it must be a concurrent set.
    private val removedGroupIds: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private val persistedMessageIds = mutableMapOf<String, MutableSet<String>>()
    private val persistedPeerCounts = mutableMapOf<String, Int>()
    private val persistedMyNames = mutableMapOf<String, String>()
    /** Holds the P2PManager instance that completed history replay for a
     *  group — NOT a bare set of group ids. The reverse-delete guard keys off
     *  this: an OLD connection's replay finishing after a reconnect must not
     *  mark history restored for the NEW connection (which would let the new
     *  (still-loading) message list wipe rows that only the old instance had
     *  seen). */
    private val replayDone = mutableMapOf<String, P2PManager>()
    /** History-replay jobs per group; cancelled on reconnect/remove so a stale
     *  load can never finish late and touch state it no longer owns. */
    private val replayJobs = mutableMapOf<String, Job>()

    /** Serializes Room writes per conversation (messages + groups + deletes):
     *  Dispatchers.IO is multi-threaded, so an insert launched after a delete
     *  could complete first and resurrect a deleted message in the DB. All
     *  writes for one key share one mutex so they commit in submission order. */
    private val dbLocks = ConcurrentHashMap<String, Mutex>()
    private suspend fun <T> withDbLock(key: String, block: suspend () -> T): T =
        dbLocks.getOrPut(key) { Mutex() }.withLock { block() }

    /**
     * Tombstones for a group whose row does not exist yet: join_ack
     * deletedIds arrive DURING the join, while the success path persists the
     * saved_groups row afterwards. deleted_messages has a CASCADE foreign key
     * onto saved_groups, so inserting earlier would fail the FK and the
     * silently swallowed error would let a later history replay resurrect the
     * message. Buffered here, flushed the moment the group row is written.
     */
    private val pendingTombstones = ConcurrentHashMap<String, MutableSet<String>>()

    /**
     * Record tombstones for locally-applied deletes (own delete, a relayed or
     * meshed delete_message, a peer's deletedIds cleanup) so members that
     * were OFFLINE converge later via join_ack / history_reply deletedIds.
     * Idempotent upserts, bounded per group (trim to the newest CAP); buffered
     * in memory while the group row is still missing (see [pendingTombstones]).
     */
    private fun writeTombstones(groupId: String, messageIds: List<String>) {
        if (messageIds.isEmpty()) return
        val now = System.currentTimeMillis()
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                withDbLock(groupId) {
                    if (!persistTombstones(groupId, messageIds, now)) {
                        bufferTombstones(groupId, messageIds)
                    }
                }
            }
        }
    }

    /**
     * Blocking variant for the join_ack path: the tombstones must be in the
     * database BEFORE the join result triggers the history replay (a
     * fire-and-forget write could land after the replay had already read the
     * table). Runs on the network thread, like [deletedIdsFor]; buffered in
     * memory when the group row is not written yet.
     */
    private fun writeTombstonesBlocking(groupId: String, messageIds: List<String>) {
        if (messageIds.isEmpty()) return
        runBlocking {
            runCatching {
                withDbLock(groupId) {
                    if (!persistTombstones(groupId, messageIds, System.currentTimeMillis())) {
                        bufferTombstones(groupId, messageIds)
                    }
                }
            }
        }
    }

    private fun bufferTombstones(groupId: String, messageIds: Collection<String>) {
        if (messageIds.isEmpty()) return
        // computeIfAbsent is atomic on ConcurrentHashMap: a concurrent
        // buffer/flush must never drop the other side's ids
        pendingTombstones.computeIfAbsent(groupId) { ConcurrentHashMap.newKeySet<String>() }
            .addAll(messageIds)
    }

    /** Insert tombstones (idempotent) + trim the table; false when the group
     *  row is missing so the caller buffers instead of losing them. */
    private suspend fun persistTombstones(
        groupId: String,
        messageIds: Collection<String>,
        deletedAt: Long
    ): Boolean {
        if (chatDao.getGroup(groupId) == null) return false
        chatDao.upsertDeletedMessages(messageIds.map { DeletedMessage(groupId, it, deletedAt) })
        chatDao.trimDeletedMessages(groupId, DeletedMessage.CAP)
        return true
    }

    /** Flush tombstones buffered before the group row existed; on failure they
     *  go back to the buffer (the next flush or join retries). */
    private suspend fun flushPendingTombstones(groupId: String) {
        val ids = pendingTombstones.remove(groupId) ?: return
        if (ids.isEmpty()) return
        val ok = runCatching {
            withDbLock(groupId) { persistTombstones(groupId, ids, System.currentTimeMillis()) }
        }.getOrDefault(false)
        if (!ok) bufferTombstones(groupId, ids)
    }

    /**
     * Tombstoned message ids of a group, served to join_ack / history_reply
     * senders. Called on network worker threads (no coroutine context there),
     * so the short indexed read blocks the caller briefly. Buffered pending
     * ids count too: a peer asking while our group row is not written yet must
     * still learn the delete.
     */
    private fun deletedIdsFor(groupId: String): List<String> {
        val persisted = runBlocking {
            runCatching { chatDao.getDeletedMessages(groupId).map { it.msgId } }
                .getOrDefault(emptyList())
        }
        val pending = pendingTombstones[groupId]?.toList().orEmpty()
        if (pending.isEmpty()) return persisted
        return (persisted + pending).distinct()
    }

    private val _activeGroupPassword = MutableStateFlow<String?>(null)
    val activeGroupPassword: StateFlow<String?> = _activeGroupPassword.asStateFlow()

    // --------------------------------------------------------- direct chats
    // Members are first-class: a known member can be pulled into a 1:1 chat
    // immediately (auto-accepted on the other side, no confirmation).

    private val directJobs = mutableMapOf<String, Job>()
    private val persistedDirectIds = mutableMapOf<String, MutableSet<String>>()
    /** Last persisted pending flag per message id, so a pending->sent flip
     *  (outbox flush) is written back to the database. */
    private val persistedDirectPending = mutableMapOf<String, MutableMap<String, Boolean>>()

    /** Own direct messages' persisted read state (peer read_receipts):
     *  message id -> read. Mirrors [persistedDirectPending]. */
    private val persistedDirectRead = mutableMapOf<String, MutableMap<String, Boolean>>()

    /** Fires when a direct chat's key moves from a manually added "ip:..."
     *  placeholder id to the member's real device id (revealed by a
     *  handshake); the UI re-keys the open chat screen. */
    private val _directChatMigrations = MutableSharedFlow<Pair<String, String>>(extraBufferCapacity = 16)
    val directChatMigrations: SharedFlow<Pair<String, String>> = _directChatMigrations.asSharedFlow()

    val directContacts: StateFlow<List<DirectChatManager.Contact>> = DirectChatManager.contacts
        .map { map -> map.values.sortedWith(compareBy(String.CASE_INSENSITIVE_ORDER) { it.name }) }
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    /** Pending contact requests (the "message box"): incoming first-contact
     *  and re-add attempts parked for the user to accept or ignore. */
    val directContactRequests: StateFlow<List<DirectChatManager.ContactRequest>> =
        DirectChatManager.contactRequests

    /** Last message per member, for conversation previews on the home page. */
    val directLastMessages: StateFlow<Map<String, ChatMessage>> = DirectChatManager.lastMessages

    /** Members with a currently live direct session (reactive online state). */
    val directAliveSessions: StateFlow<Set<String>> = DirectChatManager.aliveSessions

    /** Live message list of one direct chat (seeded with history on open). */
    fun directMessages(peerId: String): Flow<List<ChatMessage>> =
        DirectChatManager.messagesFor(peerId)

    // -------------------------------------------------------------- search

    /** One global-search hit: the message plus the conversation it lives in. */
    data class SearchHit(
        val message: ChatMessage,
        val conversationId: String,
        val conversationName: String,
        val isDirect: Boolean
    )

    /** One range-selector entry for the search screen ("全部" is added by the
     *  UI as a null id). */
    data class SearchScope(val id: String, val name: String)

    /** Range-selector entries: every known group, then every direct contact. */
    fun searchScopes(): List<SearchScope> {
        val scopes = ArrayList<SearchScope>()
        val seen = HashSet<String>()
        _groups.value.forEach { g ->
            if (seen.add(g.groupId)) scopes.add(SearchScope(g.groupId, "群聊：${g.groupName}"))
        }
        DirectChatManager.contacts.value.values.forEach { c ->
            if (seen.add(c.id)) scopes.add(SearchScope("direct:${c.id}", "成员：${c.name}"))
        }
        return scopes
    }

    /**
     * Keyword search over persisted group + direct-chat history, newest match
     * first. Message bodies are encrypted at rest, so the DAO's name-column
     * prefilter is combined with a body pass over the decrypted scope rows
     * ([MessageSearch]). Conversation display names are resolved here so the
     * search UI only renders.
     */
    suspend fun searchHistory(
        keyword: String,
        scopeGroupId: String? = null
    ): List<SearchHit> {
        val kw = keyword.trim()
        if (kw.isEmpty()) return emptyList()
        val rows = runCatching {
            val nameHits = chatDao.searchByNameColumns(
                scopeGroupId, MessageSearch.likePattern(kw), MessageSearch.MAX_RESULTS
            )
            val bodyHits = chatDao.searchScopeRows(scopeGroupId).filter { row ->
                MessageSearch.matches(row, StoreCipher.unprotect(row.content), kw)
            }
            MessageSearch.merge(nameHits, bodyHits)
        }.getOrElse { emptyList() }
        return rows.map { toSearchHit(it) }
    }

    private suspend fun toSearchHit(row: SavedChatMessage): SearchHit {
        val plain = row.withPlainContent()
        val isDirect = plain.groupId.startsWith("direct:")
        val name = if (isDirect) {
            val peerId = plain.groupId.removePrefix("direct:")
            DirectChatManager.contacts.value[peerId]?.name ?: peerId
        } else {
            _groups.value.find { it.groupId == plain.groupId }?.groupName
                ?: runCatching { chatDao.getGroup(plain.groupId)?.groupName }.getOrNull()
                ?: plain.groupId
        }
        return SearchHit(
            message = ChatMessage(
                id = plain.id,
                content = plain.content,
                timestamp = plain.timestamp,
                senderId = plain.senderId,
                senderName = plain.senderName,
                isFromMe = plain.isFromMe,
                fileInfo = restoredFileInfo(plain)
            ),
            conversationId = plain.groupId,
            conversationName = name,
            isDirect = isDirect
        )
    }

    /** Transient direct-chat events surfaced as toasts. */
    val directEvents: SharedFlow<String> = DirectChatManager.events

    // ------------------------------------------------------ typing indicators

    /** Direct-chat peers whose typing indicator is currently live. */
    private val _directTypingPeers = MutableStateFlow<Set<String>>(emptySet())
    val directTypingPeers: StateFlow<Set<String>> = _directTypingPeers.asStateFlow()

    /** Group id -> (sender id -> display name) for live typing indicators. */
    private val _groupTyping = MutableStateFlow<Map<String, Map<String, String>>>(emptyMap())
    val groupTyping: StateFlow<Map<String, Map<String, String>>> = _groupTyping.asStateFlow()

    /** Received-indicator deadlines (peer/member id -> expiry), mirroring the
     *  Windows ViewModel: an indicator that saw no refresh for
     *  [TYPING_TIMEOUT_MS] is dropped by the ticker. */
    private val directTypingDeadlines = ConcurrentHashMap<String, Long>()
    private val groupTypingDeadlines = ConcurrentHashMap<String, ConcurrentHashMap<String, Long>>()
    private val groupTypingNames = ConcurrentHashMap<String, ConcurrentHashMap<String, String>>()
    private val typingTickerStarted = AtomicBoolean(false)

    /** Outbound typing throttle state per conversation scope. */
    private class TypingOut(var lastSentAt: Long = 0L, var job: Job? = null)
    private val outboundTyping = ConcurrentHashMap<String, TypingOut>()

    /** Refresh our typing indicator for a direct chat (throttled; the stop is
     *  scheduled automatically). Call on every input change. */
    fun notifyDirectTyping(peerId: String) {
        if (peerId.isBlank()) return
        typingActivity("direct:$peerId") { active ->
            DirectChatManager.sendTyping(peerId, active)
        }
    }

    /** Our message went out or the chat closed: stop the indicator now. */
    fun endDirectTyping(peerId: String) = stopTyping("direct:$peerId")

    /** Refresh our typing indicator for the active group (throttled). */
    fun notifyGroupTyping() {
        val gid = _activeGroupId.value ?: return
        typingActivity(gid) { active -> sendGroupTyping(gid, active) }
    }

    private fun sendGroupTyping(groupId: String, active: Boolean) {
        val p2p = groupP2pMap[groupId]
        p2p?.sendTyping(active)
        // host-offline path: the mesh carries the indicator too
        GroupMeshManager.broadcastTyping(groupId, p2p?.myIdValue ?: "", active)
    }

    private fun typingActivity(scopeKey: String, send: (Boolean) -> Unit) {
        val state = outboundTyping.getOrPut(scopeKey) { TypingOut() }
        val now = System.currentTimeMillis()
        if (now - state.lastSentAt >= TYPING_ACTIVE_INTERVAL_MS) {
            state.lastSentAt = now
            send(true)
        }
        state.job?.cancel()
        state.job = viewModelScope.launch {
            delay(TYPING_STOP_DELAY_MS)
            outboundTyping.remove(scopeKey)
            send(false)
        }
    }

    private fun stopTyping(scopeKey: String) {
        val state = outboundTyping.remove(scopeKey) ?: return
        state.job?.cancel()
        when {
            scopeKey.startsWith("direct:") ->
                DirectChatManager.sendTyping(scopeKey.removePrefix("direct:"), false)
            else -> sendGroupTyping(scopeKey, false)
        }
    }

    /** A peer's typing indicator changed (direct session read thread). */
    private fun onDirectTyping(peerId: String, active: Boolean) {
        if (active) {
            directTypingDeadlines[peerId] = System.currentTimeMillis() + TYPING_TIMEOUT_MS
        } else {
            directTypingDeadlines.remove(peerId)
        }
        publishTyping()
        ensureTypingTicker()
    }

    /** A group member's typing indicator changed (host relay / mesh thread). */
    private fun onGroupTyping(groupId: String, senderId: String, active: Boolean) {
        if (groupId.isBlank() || senderId.isBlank() || senderId == currentMyId()) return
        val deadlines = groupTypingDeadlines.getOrPut(groupId) { ConcurrentHashMap() }
        val names = groupTypingNames.getOrPut(groupId) { ConcurrentHashMap() }
        if (active) {
            deadlines[senderId] = System.currentTimeMillis() + TYPING_TIMEOUT_MS
            names[senderId] = groupP2pMap[groupId]?.peers?.value?.get(senderId)?.name
                ?.ifBlank { senderId } ?: senderId
        } else {
            deadlines.remove(senderId)
            names.remove(senderId)
            if (deadlines.isEmpty()) {
                groupTypingDeadlines.remove(groupId)
                groupTypingNames.remove(groupId)
            }
        }
        publishTyping()
        ensureTypingTicker()
    }

    private fun currentMyId(): String =
        groupP2pMap.values.firstOrNull()?.myIdValue ?: DirectChatManager.myIdValue

    private fun publishTyping() {
        _directTypingPeers.value = directTypingDeadlines.keys.toSet()
        val snapshot = HashMap<String, Map<String, String>>()
        for ((gid, names) in groupTypingNames) {
            if (names.isNotEmpty()) snapshot[gid] = HashMap(names)
        }
        _groupTyping.value = snapshot
    }

    private fun hasAnyTyping(): Boolean =
        directTypingDeadlines.isNotEmpty() || groupTypingDeadlines.isNotEmpty()

    /** Expiry ticker: runs only while some indicator is live (Windows parity:
     *  the ViewModel's 1s timer). */
    private fun ensureTypingTicker() {
        if (!hasAnyTyping()) return
        if (!typingTickerStarted.compareAndSet(false, true)) return
        viewModelScope.launch {
            try {
                while (hasAnyTyping()) {
                    delay(TYPING_TICK_MS)
                    val now = System.currentTimeMillis()
                    var changed = false
                    for ((peerId, deadline) in directTypingDeadlines) {
                        if (deadline <= now) {
                            directTypingDeadlines.remove(peerId)
                            changed = true
                        }
                    }
                    for ((gid, deadlines) in groupTypingDeadlines) {
                        for ((senderId, deadline) in deadlines) {
                            if (deadline <= now) {
                                deadlines.remove(senderId)
                                groupTypingNames[gid]?.remove(senderId)
                                changed = true
                            }
                        }
                        if (deadlines.isEmpty()) {
                            groupTypingDeadlines.remove(gid)
                            groupTypingNames.remove(gid)
                        }
                    }
                    if (changed) publishTyping()
                }
            } finally {
                typingTickerStarted.set(false)
                // a new indicator may have arrived as the loop exited
                if (hasAnyTyping()) ensureTypingTicker()
            }
        }
    }

    /** Transient group-management events surfaced as toasts (kicked out,
     *  member removed). Buffered so an emit without a collector is not lost. */
    private val _groupEvents = MutableSharedFlow<String>(extraBufferCapacity = 8)
    val groupEvents: SharedFlow<String> = _groupEvents.asSharedFlow()

    /**
     * Start a video/audio call with a direct-chat member. Signaling rides the
     * 1:1 session socket; when the session is not alive it is pulled up first
     * (the other side auto-accepts) and the call is offered right after.
     * [media] is "video" (default) or "audio".
     */
    fun startDirectCall(peerId: String, media: String = CallMedia.VIDEO) {
        val contact = DirectChatManager.contacts.value[peerId] ?: return
        val peer = Peer(contact.id, contact.name, contact.ip, contact.port)
        if (peer.ipAddress.isBlank()) return
        val channel = CallManager.CallChannel { pid, pkt -> DirectChatManager.sendPacket(pid, pkt) }
        if (DirectChatManager.isChatAlive(peerId)) {
            CallManager.startCall(
                peer, channel, DirectChatManager,
                DirectChatManager.myIdValue, DirectChatManager.myNameValue, media
            )
        } else {
            // connect first; the handshake reveals the member's REAL id, which
            // may differ from a manually added placeholder — key the offer by it
            DirectChatManager.startChat(peer) { realId ->
                if (realId != null) {
                    val realContact = DirectChatManager.contacts.value[realId]
                    val target = realContact ?: contact
                    CallManager.startCall(
                        Peer(target.id, target.name, target.ip, target.port),
                        channel, DirectChatManager,
                        DirectChatManager.myIdValue, DirectChatManager.myNameValue, media
                    )
                }
            }
        }
    }

    /** Bumped whenever an own sent image finishes copying into the media dir;
     *  the chat UI keys its media path lookups on it so the sender's own
     *  image bubble flips to the inline render as soon as the copy lands. */
    private val _mediaVersion = MutableStateFlow(0)
    val mediaVersion: StateFlow<Int> = _mediaVersion.asStateFlow()

    /** Offer a file to a direct-chat member. */
    fun sendDirectFile(
        peerId: String,
        uri: Uri,
        fileName: String,
        fileSize: Long,
        kind: String = FileKind.FILE
    ): Boolean {
        if (fileName.isBlank()) return false
        val msg = DirectChatManager.sendFile(
            peerId, fileName, getApplication<Application>().contentResolver, uri, fileSize, kind
        ) ?: return false
        mirrorOwnMedia(msg.id, uri, fileName, kind)
        return true
    }

    /** One folder entry collected from the picked SAF tree: its relative
     *  "/"-separated path inside the folder and the file's content URI. */
    private data class FolderEntry(val relativePath: String, val uri: Uri, val size: Long)

    /** Result of walking a picked SAF tree: the sorted entries, the root
     *  folder's sanitized display name and whether sendable files existed
     *  beyond [MAX_FOLDER_FILES] (the offer was truncated). */
    private data class FolderScan(
        val entries: List<FolderEntry>,
        val folderName: String,
        val truncated: Boolean
    )

    /**
     * Walk a picked folder tree (SAF [treeUri]) recursively and return its
     * file entries sorted for a stable order, plus the root folder's sanitized
     * display name and a truncation flag. Directories are skipped; entries
     * whose path cannot be sanitized are dropped and the list is capped at
     * [MAX_FOLDER_FILES] (truncated=true when sendable files existed beyond
     * the cap). Mirrors the Windows _collect_folder_entries. Blocking SAF
     * queries: call from a worker dispatcher.
     */
    private fun collectFolderEntries(treeUri: Uri, rootName: String): FolderScan {
        val resolver = getApplication<Application>().contentResolver
        val rootDoc = DocumentsContract.buildDocumentUriUsingTree(
            treeUri, DocumentsContract.getTreeDocumentId(treeUri)
        )
        val entries = ArrayList<FolderEntry>()
        val root = rootName.replace(Regex("[\\\\/:*?\"<>|]"), "_").trim('.', ' ').ifBlank { "文件夹" }.take(80)
        var truncated = false
        fun walk(dirUri: Uri, prefix: String) {
            if (truncated) return
            val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(
                dirUri, DocumentsContract.getDocumentId(dirUri)
            )
            val children = runCatching {
                resolver.query(
                    childrenUri,
                    arrayOf(
                        DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                        DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                        DocumentsContract.Document.COLUMN_MIME_TYPE
                    ),
                    null, null, null
                )?.use { c ->
                    val rows = ArrayList<FolderChild>()
                    while (c.moveToNext()) {
                        val name = c.getString(1) ?: continue
                        val isDir = c.getString(2) == DocumentsContract.Document.MIME_TYPE_DIR
                        rows.add(FolderChild(name, c.getString(0) ?: "", isDir))
                    }
                    rows
                }
            }.getOrNull().orEmpty()
            // deterministic order (the SAF query has none): sort by name, and
            // files/folders stay in one combined list like the Windows walk
            for (child in children.sortedBy { it.name }) {
                if (truncated) return
                val childUri = DocumentsContract.buildDocumentUriUsingTree(dirUri, child.docId)
                val rel = sanitizeRelativePath(
                    if (prefix.isEmpty()) child.name else "$prefix/${child.name}"
                )
                if (rel.isEmpty()) continue
                if (child.isDir) {
                    walk(childUri, rel)
                } else {
                    val size = runCatching {
                        resolver.query(
                            childUri,
                            arrayOf(DocumentsContract.Document.COLUMN_SIZE),
                            null, null, null
                        )?.use { c -> if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else 0L } ?: 0L
                    }.getOrDefault(0L)
                    if (entries.size >= MAX_FOLDER_FILES) {
                        // one sendable file beyond the cap: the scan is short
                        truncated = true
                        return
                    }
                    entries.add(FolderEntry(rel, childUri, size))
                }
            }
        }
        walk(rootDoc, "")
        entries.sortBy { it.relativePath }
        return FolderScan(entries, root, truncated)
    }

    private data class FolderChild(val name: String, val docId: String, val isDir: Boolean)

    /**
     * Offer a folder to the active group as one file_message per entry (each
     * carrying folderId/folderName/relativePath/folderTotal) so receivers can
     * group them and rebuild the tree. Old peers still see ordinary file
     * offers. The SAF walk + per-entry offers run on Dispatchers.IO so a
     * large folder never blocks the main thread (ANR); [onDone] fires on the
     * main thread with (ok, truncated).
     */
    fun sendFolder(treeUri: Uri, displayName: String, onDone: (ok: Boolean, truncated: Boolean) -> Unit) {
        val gid = _activeGroupId.value
        val p2p = gid?.let { groupP2pMap[it] }
        if (gid == null || p2p == null || (!p2p.isConnected && !GroupMeshManager.hasLinks(gid))) {
            onDone(false, false)
            return
        }
        val resolver = getApplication<Application>().contentResolver
        viewModelScope.launch(Dispatchers.IO) {
            val scan = collectFolderEntries(treeUri, displayName)
            if (scan.entries.isEmpty()) {
                withContext(Dispatchers.Main) { onDone(false, false) }
                return@launch
            }
            val folderId = java.util.UUID.randomUUID().toString()
            var sent = 0
            for (entry in scan.entries) {
                // folder entries are always plain "file" kind: P2PManager
                // forces FILE for every folder entry (never rendered inline)
                val msg = p2p.sendFile(
                    entry.relativePath.substringAfterLast('/'), resolver, entry.uri, entry.size,
                    FileKind.FILE,
                    folderId = folderId,
                    folderName = scan.folderName,
                    relativePath = entry.relativePath,
                    folderTotal = scan.entries.size
                ) ?: continue
                // same dual delivery as a single file: relay + mesh, dedup by id
                GroupMeshManager.broadcast(gid, msg)
                sent++
            }
            val ok = sent > 0
            withContext(Dispatchers.Main) { onDone(ok, scan.truncated) }
        }
    }

    /** Offer a folder over a direct session as one file_message per entry
     *  (see [sendFolder]); [onDone] fires on the main thread with
     *  (ok, truncated). */
    fun sendDirectFolder(
        peerId: String,
        treeUri: Uri,
        displayName: String,
        onDone: (ok: Boolean, truncated: Boolean) -> Unit
    ) {
        val resolver = getApplication<Application>().contentResolver
        viewModelScope.launch(Dispatchers.IO) {
            val scan = collectFolderEntries(treeUri, displayName)
            if (scan.entries.isEmpty()) {
                withContext(Dispatchers.Main) { onDone(false, false) }
                return@launch
            }
            val folderId = java.util.UUID.randomUUID().toString()
            var sent = 0
            for (entry in scan.entries) {
                val msg = DirectChatManager.sendFile(
                    peerId, entry.relativePath.substringAfterLast('/'), resolver, entry.uri, entry.size,
                    FileKind.FILE,
                    folderId = folderId,
                    folderName = scan.folderName,
                    relativePath = entry.relativePath,
                    folderTotal = scan.entries.size
                ) ?: continue
                sent++
            }
            val ok = sent > 0
            withContext(Dispatchers.Main) { onDone(ok, scan.truncated) }
        }
    }

    /** After an own IMAGE goes out, copy it into the media dir (worker
     *  thread) so the sender's own bubble renders inline like a received one
     *  — and keeps doing so after a restart. Videos are NOT copied: they are
     *  far larger and the placeholder card already opens fine for the
     *  sender. Failures are silently ignored: the copy is a rendering
     *  convenience, the offer itself was already delivered. */
    private fun mirrorOwnMedia(fileId: String, uri: Uri, fileName: String, kind: String) {
        if (kind != FileKind.IMAGE) return
        viewModelScope.launch(Dispatchers.IO) {
            val ok = runCatching {
                val target = mediaTargetFile(fileId, fileName)
                target.parentFile?.mkdirs()
                // .part + rename so a half-copied image is never visible to
                // the UI's media path probe (Windows parity)
                val tmp = java.io.File(target.absolutePath + ".part")
                tmp.outputStream().use { output ->
                    getApplication<Application>().contentResolver.openInputStream(uri)?.use { input ->
                        input.copyTo(output)
                    } ?: return@runCatching false
                }
                if (!tmp.renameTo(target)) {
                    tmp.delete()
                    return@runCatching false
                }
                true
            }.getOrDefault(false)
            if (ok) _mediaVersion.update { it + 1 }
        }
    }

    /** Download a media (image/video) message into the app's media dir so it
     *  renders inline in the conversation; NO system save dialog. Progress is
     *  surfaced via [downloadStates] keyed by the file message id. */
    fun downloadMedia(fileInfo: FileInfo, isDirect: Boolean) {
        val fileId = fileInfo.fileId
        val target = mediaTargetFile(fileId, fileInfo.fileName)
        if (fileInfo.fileSize > 0 && target.isFile && target.length() == fileInfo.fileSize) {
            _downloadStates.update { map ->
                map + (fileId to DownloadState.Done(target.absolutePath))
            }
            return
        }
        // register the cancel handle + throttled progress (5% or 256KB) for
        // this media download: the transfer socket is handed to the canceller
        // so a stalled read can be shut down, and [downloadStates] shows the
        // percent while the row is 下载中
        val handle = DownloadHandle()
        activeDownloads[fileId] = handle
        var lastBytes = 0L
        var lastPercent = -5
        val progress: (Long, Long) -> Unit = { received, total ->
            val percent = if (total > 0) {
                ((received * 100) / total).toInt().coerceIn(0, 100)
            } else 0
            if (received - lastBytes >= 256 * 1024 ||
                percent >= lastPercent + 5 ||
                (total > 0 && received >= total)
            ) {
                lastBytes = received
                lastPercent = percent
                _downloadStates.update { it + (fileId to DownloadState.Downloading(percent)) }
            }
        }
        _downloadStates.update { it + (fileId to DownloadState.Downloading(0)) }
        viewModelScope.launch(Dispatchers.IO) {
            // download to a .part file and rename on success: the UI probes
            // the target path to render inline media, so a partially written
            // target must never be visible there (Windows parity)
            val tmp = java.io.File(target.absolutePath + ".part")
            val result = runCatching {
                tmp.parentFile?.mkdirs()
                tmp.outputStream().use { out ->
                    if (isDirect) {
                        DirectChatManager.downloadFile(
                            fileInfo, out, progress, { handle.cancelled.get() }, handle.socks
                        )
                    } else {
                        val gid = _activeGroupId.value
                        val p2p = gid?.let { groupP2pMap[it] }
                            ?: return@runCatching FileTransfer.DownloadResult(false, "未连接到群组")
                        p2p.downloadFile(
                            fileInfo, out, progress, { handle.cancelled.get() }, handle.socks
                        )
                    }
                }
                FileTransfer.DownloadResult(true)
            }.getOrElse { FileTransfer.DownloadResult(false, it.message ?: "未知错误") }
            activeDownloads.remove(fileId)
            if (result.ok) {
                if (!tmp.renameTo(target)) {
                    tmp.delete()
                    _downloadStates.update { map ->
                        map + (fileId to DownloadState.Failed("无法保存媒体文件"))
                    }
                    return@launch
                }
            } else {
                tmp.delete()
            }
            _downloadStates.update { map ->
                map + (fileId to when {
                    result.ok -> DownloadState.Done(target.absolutePath)
                    handle.cancelled.get() -> DownloadState.Failed("已取消")
                    else -> DownloadState.Failed(result.message)
                })
            }
        }
    }

    /** Deterministic local target for a downloaded media message, keyed by
     *  the file id so same-named files from different messages never collide.
     *  The UI checks [File.exists] on this path to re-render inline media
     *  after a restart. */
    fun mediaTargetFile(fileId: String, fileName: String): java.io.File {
        val dir = java.io.File(getApplication<Application>().filesDir, "media")
        val safe = fileName.replace(Regex("[\\\\/:*?\"<>|]"), "_")
            .trim('.', ' ')
            .ifBlank { "file" }
            .take(80)
        return java.io.File(dir, "${fileId}_$safe")
    }

    /** Path of an already-downloaded media copy, or null. Lets the chat UI
     *  render image/video messages inline (also after a restart). */
    fun localMediaPath(fileInfo: FileInfo): String? {
        if (fileInfo.kind != FileKind.IMAGE && fileInfo.kind != FileKind.VIDEO) return null
        val f = mediaTargetFile(fileInfo.fileId, fileInfo.fileName)
        return if (f.isFile) f.absolutePath else null
    }

    /** Download a file offered in a direct chat into [targetUri]; progress is
     *  surfaced via [downloadStates] keyed by the file message id. */
    fun downloadDirectFile(fileInfo: FileInfo, targetUri: Uri) {
        runDownload(fileInfo.fileId, targetUri) { out, onProgress, cancelled, socks ->
            DirectChatManager.downloadFile(fileInfo, out, onProgress, cancelled, socks)
        }
    }

    /** Open a 1:1 chat with a member WITHOUT requiring the peer to be
     *  online: persisted history loads immediately, messages sent while
     *  offline queue as pending, and a background dial keeps trying to
     *  connect. Flows that genuinely need a live session first (calls) dial
     *  through DirectChatManager themselves. */
    fun openDirectChat(contact: DirectChatManager.Contact) {
        DirectChatManager.openChat(contact)
        observeDirectChat(contact.id)
        if (!DirectChatManager.isChatAlive(contact.id)) {
            // best-effort connect in the background; failure surfaces as a
            // toast but the chat (with its history) stays open and usable
            DirectChatManager.startChat(
                Peer(contact.id, contact.name, contact.ip, contact.port)
            ) { realId ->
                if (realId != null) observeDirectChat(realId)
            }
        }
    }

    fun sendDirectMessage(
        peerId: String,
        content: String,
        replyTo: String? = null,
        replyPreview: String? = null,
        replySender: String? = null
    ): Boolean {
        val sent = DirectChatManager.sendMessage(
            peerId, content, replyTo, replyPreview, replySender
        )
        if (sent) {
            // the message supersedes any "typing" we were showing
            endDirectTyping(peerId)
        }
        return sent
    }

    fun deleteDirectMessage(peerId: String, messageId: String, senderId: String) =
        DirectChatManager.deleteMessage(peerId, messageId, senderId)

    fun addDirectContact(ipPort: String, name: String): Boolean {
        val parsed = parseHostPort(ipPort)
        // validate: a syntactically broken endpoint (mangled IP, bad port)
        // used to be accepted silently — the member row then appeared in the
        // list but could never connect, which looks like "adding by IP has
        // no effect". Reject it here; the UI surfaces 地址无效 (Windows
        // parity).
        if (!isValidHost(parsed.host) || parsed.port !in 1..65535) return false
        val nick = name.trim().ifBlank { parsed.host }.take(20)
        val contact = DirectChatManager.Contact(
            "ip:${parsed.host}:${parsed.port}", nick, parsed.host, parsed.port
        )
        DirectChatManager.addContact(contact)
        // addContact keeps the REAL-id contact when this endpoint is already
        // known (a manual placeholder must not clobber it): dial the stored
        // contact, not the placeholder, so the session keys under the real
        // id (Windows parity)
        val effective = DirectChatManager.contacts.value.values.firstOrNull {
            it.ip == parsed.host && it.port == parsed.port
        } ?: contact
        // A manual add is an explicit user action: dial LOUD right away
        // (startChat's default). The presence sweep also picks the new
        // contact up, but it is deliberately silent — without this loud
        // dial, adding an unreachable member gives NO feedback at all.
        // Success: "已连接 X" toast; failure: the reason toast.
        if (DirectChatManager.aliveSessions.value.contains(effective.id)) {
            // already connected (re-adding an existing member): the dial
            // short-circuits with no event, so surface the state here —
            // without this, the second add silently does nothing
            DirectChatManager.emitEvent("已连接 ${effective.name}")
            return true
        }
        DirectChatManager.startChat(
            Peer(effective.id, effective.name, effective.ip, effective.port)
        ) { }
        return true
    }

    fun removeDirectContact(id: String) {
        directJobs.remove(id)?.cancel()
        persistedDirectIds.remove(id)
        persistedDirectPending.remove(id)
        persistedDirectRead.remove(id)
        DirectChatManager.closeChat(id)
        DirectChatManager.removeContact(id)
    }

    /** Accept a parked contact request: adds the member (clearing any
     *  removal marks) and dials it back. */
    fun acceptContactRequest(id: String) = DirectChatManager.acceptContactRequest(id)

    /** Ignore a parked contact request: drops the box entry; the peer is
     *  never auto-added, and a later retry simply re-parks it. */
    fun ignoreContactRequest(id: String) = DirectChatManager.ignoreContactRequest(id)

    /** A direct chat's key moved from a "ip:..." placeholder to the real
     *  device id: swap the observer job, move persisted rows, and tell the
     *  UI to re-key the open screen. */
    private fun migrateDirectChat(fromId: String, toId: String) {
        directJobs.remove(fromId)?.cancel()
        persistedDirectIds.remove(fromId)
        persistedDirectPending.remove(fromId)
        persistedDirectRead.remove(fromId)
        viewModelScope.launch(Dispatchers.IO) {
            runCatching {
                val toKey = "direct:$toId"
                if (chatDao.getGroup(toKey) == null) {
                    val name = DirectChatManager.contacts.value[toId]?.name ?: toId
                    chatDao.insertGroup(
                        SavedGroup(
                            groupId = toKey,
                            groupName = name,
                            isHost = false,
                            hostIp = "",
                            hostPort = 0,
                            myName = ""
                        )
                    )
                }
                // rows first, then the placeholder group row: its CASCADE
                // delete must not wipe the rows being moved
                chatDao.moveMessages("direct:$fromId", toKey)
                chatDao.moveCallLogs("direct:$fromId", toKey)
                chatDao.deleteGroup("direct:$fromId")
            }
        }
        observeDirectChat(toId)
        _directChatMigrations.tryEmit(fromId to toId)
    }

    private fun observeDirectChat(peerId: String) {
        if (directJobs.containsKey(peerId)) return
        directJobs[peerId] = viewModelScope.launch(Dispatchers.IO) {
            // Direct chats live under a synthetic "direct:<peerId>" key in the
            // messages table, which has a foreign key to saved_groups — insert
            // a placeholder group row so message persistence never violates it
            // (it is filtered out of the group list by loadPersistedGroups).
            val peerName = DirectChatManager.contacts.value[peerId]?.name ?: peerId
            // INSERT OR REPLACE deletes+reinserts the row, and the message
            // table's CASCADE foreign key would wipe this chat's saved history
            // on every restart; only insert the placeholder when missing.
            if (chatDao.getGroup("direct:$peerId") == null) {
                chatDao.insertGroup(
                    SavedGroup(
                        groupId = "direct:$peerId",
                        groupName = peerName,
                        isHost = false,
                        hostIp = "",
                        hostPort = 0,
                        myName = ""
                    )
                )
            }
            val saved = chatDao.getMessagesForGroup("direct:$peerId").first()
            persistedDirectIds[peerId] = saved.map { it.id }.toMutableSet()
            persistedDirectPending[peerId] = saved.associate { it.id to it.pending }.toMutableMap()
            // read flips from the peer's read_receipts: persisted like pending
            persistedDirectRead[peerId] = saved.associate { it.id to it.read }.toMutableMap()
            DirectChatManager.seedMessages(
                peerId,
                saved.map { sm ->
                    val plain = sm.withPlainContent()
                    ChatMessage(
                        id = plain.id,
                        content = plain.content,
                        timestamp = plain.timestamp,
                        senderId = plain.senderId,
                        senderName = plain.senderName,
                        isFromMe = plain.isFromMe,
                        fileInfo = restoredFileInfo(plain),
                        pending = plain.pending,
                        replyTo = plain.replyTo.ifEmpty { null },
                        replyPreview = plain.replyPreview.ifEmpty { null },
                        replySender = plain.replySender.ifEmpty { null },
                        read = plain.read
                    )
                }
            )
            DirectChatManager.messagesFor(peerId).collect { msgs ->
                val persisted = persistedDirectIds.getOrPut(peerId) { mutableSetOf() }
                val pendingMap = persistedDirectPending.getOrPut(peerId) { mutableMapOf() }
                val readMap = persistedDirectRead.getOrPut(peerId) { mutableMapOf() }
                val currentIds = msgs.map { it.id }.toSet()
                val removed = persisted.filter { it !in currentIds }
                val newOnes = msgs.filter { it.id !in persisted }
                // background direct messages post a notification too (Android
                // parity with the Windows tray): it carries the same
                // quick-reply action as a group notification, keyed by
                // "direct:<peerId>"
                val incomingNew = newOnes.filter { !it.isFromMe }
                if (!isAppForeground.value && incomingNew.isNotEmpty()) {
                    notifyNewMessages(
                        "$DIRECT_PREFIX$peerId",
                        DirectChatManager.contacts.value[peerId]?.name ?: peerId,
                        incomingNew.map { NotifEntry(it.senderName, it.content, it.timestamp) }
                    )
                }
                // pending -> delivered flips (outbox flush) are plain updates
                val flagChanges = msgs.filter { m ->
                    m.isFromMe && pendingMap.containsKey(m.id) && pendingMap[m.id] != m.pending
                }
                // peer read_receipts flip own messages to 已读
                val readChanges = msgs.filter { m ->
                    m.isFromMe && readMap.containsKey(m.id) && readMap[m.id] != m.read
                }
                if (removed.isNotEmpty() || newOnes.isNotEmpty() ||
                    flagChanges.isNotEmpty() || readChanges.isNotEmpty()
                ) {
                    withDbLock("direct:$peerId") {
                        if (removed.isNotEmpty()) {
                            // drop ids from the persisted set only after a
                            // confirmed delete (same rule as groups): a failed
                            // delete is retried on the next emission
                            val deleted = runCatching {
                                removed.forEach { chatDao.deleteMessage("direct:$peerId", it) }
                            }.isSuccess
                            if (deleted) {
                                persisted.removeAll(removed)
                                removed.forEach { pendingMap.remove(it) }
                                removed.forEach { readMap.remove(it) }
                            } else {
                                Log.w("ChatViewModel", "failed to delete ${removed.size} direct messages for $peerId")
                            }
                        }
                        if (newOnes.isNotEmpty()) {
                            val inserted = runCatching {
                                chatDao.insertMessages(newOnes.map { msg ->
                                    SavedChatMessage(
                                        id = msg.id,
                                        groupId = "direct:$peerId",
                                        content = StoreCipher.protect(msg.content),
                                        timestamp = msg.timestamp,
                                        senderId = msg.senderId,
                                        senderName = msg.senderName,
                                        isFromMe = msg.isFromMe,
                                        fileSize = msg.fileInfo?.fileSize ?: 0L,
                                        downloadHost = msg.fileInfo?.downloadHost ?: "",
                                        downloadPort = msg.fileInfo?.downloadPort ?: 0,
                                        kind = msg.fileInfo?.kind ?: FileKind.FILE,
                                        folderId = msg.fileInfo?.folderId ?: "",
                                        folderName = msg.fileInfo?.folderName ?: "",
                                        relativePath = msg.fileInfo?.relativePath ?: "",
                                        folderTotal = msg.fileInfo?.folderTotal ?: 0,
                                        pending = msg.pending,
                                        replyTo = msg.replyTo ?: "",
                                        replyPreview = msg.replyPreview ?: "",
                                        replySender = msg.replySender ?: "",
                                        read = msg.read
                                    )
                                })
                            }.isSuccess
                            if (inserted) {
                                // mark persisted only after a successful write;
                                // a failed insert is retried on the next emission
                                persisted.addAll(newOnes.map { it.id })
                                newOnes.forEach { pendingMap[it.id] = it.pending }
                                newOnes.forEach { readMap[it.id] = it.read }
                            } else {
                                Log.w("ChatViewModel", "failed to persist direct messages for $peerId")
                            }
                        }
                        if (flagChanges.isNotEmpty()) {
                            val updated = runCatching {
                                flagChanges.forEach { m ->
                                    chatDao.updateMessagePending("direct:$peerId", m.id, m.pending)
                                }
                            }.isSuccess
                            if (updated) {
                                flagChanges.forEach { pendingMap[it.id] = it.pending }
                            } else {
                                Log.w("ChatViewModel", "failed to update pending flags for $peerId")
                            }
                        }
                        if (readChanges.isNotEmpty()) {
                            val updated = runCatching {
                                readChanges.forEach { m ->
                                    chatDao.updateMessageRead("direct:$peerId", m.id, m.read)
                                }
                            }.isSuccess
                            if (updated) {
                                readChanges.forEach { readMap[it.id] = it.read }
                            } else {
                                Log.w("ChatViewModel", "failed to update read flags for $peerId")
                            }
                        }
                    }
                }
            }
        }
    }

    private fun loadDirectContacts(): List<DirectChatManager.Contact> {
        val raw = directContactsPrefs.getString("direct_contacts", null) ?: return emptyList()
        return runCatching { directJson.decodeFromString<List<DirectChatManager.Contact>>(raw) }
            .getOrDefault(emptyList())
    }

    private fun saveDirectContacts(contacts: List<DirectChatManager.Contact>) {
        runCatching {
            directContactsPrefs.edit()
                .putString("direct_contacts", directJson.encodeToString(contacts))
                .apply()
        }
    }

    /** Removed-contact marks persisted across restarts, so a peer that keeps
     *  announcing cannot resurrect a contact the user deleted. */
    @Serializable
    private data class RemovedMarks(
        val ids: Map<String, Long> = emptyMap(),
        val endpoints: Map<String, Long> = emptyMap()
    )

    private fun loadDirectRemovedMarks(): Pair<Map<String, Long>, Map<String, Long>> {
        val empty = emptyMap<String, Long>()
        val raw = directContactsPrefs.getString("direct_removed_marks", null)
            ?: return empty to empty
        val parsed = runCatching { directJson.decodeFromString<RemovedMarks>(raw) }.getOrNull()
            ?: return empty to empty
        return parsed.ids to parsed.endpoints
    }

    private fun saveDirectRemovedMarks() {
        val (ids, endpoints) = DirectChatManager.removedMarks()
        runCatching {
            directContactsPrefs.edit()
                .putString("direct_removed_marks", directJson.encodeToString(RemovedMarks(ids, endpoints)))
                .apply()
        }
    }

    /** The request box persisted by a previous process: unanswered requests
     *  must still be answerable after a restart. */
    private fun loadDirectContactRequests(): List<DirectChatManager.ContactRequest> {
        val raw = directContactsPrefs.getString("direct_contact_requests", null)
            ?: return emptyList()
        return runCatching {
            directJson.decodeFromString<List<DirectChatManager.ContactRequest>>(raw)
        }.getOrDefault(emptyList())
    }

    private fun saveDirectContactRequests(requests: List<DirectChatManager.ContactRequest>) {
        runCatching {
            directContactsPrefs.edit()
                .putString("direct_contact_requests", directJson.encodeToString(requests))
                .apply()
        }
    }

    // ------------------------------------------------------------- group mesh
    // Member-to-member links inside a group: host-offline messaging + history
    // backfill. Peers are persisted so links survive the host going away.

    private fun groupPeersPrefsKey(groupId: String) = "group_peers_$groupId"

    private fun groupJoinIdKey(groupId: String) = "group_join_id_$groupId"

    private fun savedGroupJoinId(groupId: String): String =
        directContactsPrefs.getString(groupJoinIdKey(groupId), "") ?: ""

    private fun saveGroupJoinId(groupId: String, joinId: String) {
        if (joinId.isBlank()) return
        directContactsPrefs.edit().putString(groupJoinIdKey(groupId), joinId).apply()
    }

    /** The active group's numeric join id (host side display / share). */
    fun activeGroupNumericId(): String? =
        groupP2pMap[_activeGroupId.value]?.takeIf { it.isHostNode }?.numericGroupId

    /** (Re)start the shared listener — call after the local-network
     *  permission is granted so this device becomes reachable for direct
     *  chats and joins. */
    fun ensureListener() {
        hostServer.ensureRunning()
    }

    /** Persisted display nickname (used by new group/direct-chat sessions). */
    fun currentNickname(): String =
        ChatApp.savedNickname(getApplication()).ifBlank { "用户" }

    fun setNickname(name: String) {
        val nick = name.trim().take(20)
        if (nick.isBlank()) return
        ChatApp.saveNickname(getApplication(), nick)
    }

    private fun loadGroupPeers(groupId: String): List<Peer> {
        val raw = directContactsPrefs.getString(groupPeersPrefsKey(groupId), null) ?: return emptyList()
        return runCatching { directJson.decodeFromString<List<Peer>>(raw) }.getOrDefault(emptyList())
    }

    private fun saveGroupPeers(groupId: String, peers: Collection<Peer>) {
        runCatching {
            directContactsPrefs.edit()
                .putString(groupPeersPrefsKey(groupId), directJson.encodeToString(peers.toList()))
                .apply()
        }
    }

    /** Enter the mesh for a member group: link to every other member and seed
     *  the mesh with the persisted history. The host relays to everyone, so
     *  the host itself does not mesh. */
    private fun setupGroupMesh(groupId: String, p2p: P2PManager) {
        if (p2p.isHostNode) return
        val my = Peer(p2p.myIdValue, p2p.myNameValue, P2PManager.getLocalIpAddress(), port)
        val peers = (p2p.peers.value.values.toList() + loadGroupPeers(groupId))
            .distinctBy { it.id }
        // the group password authenticates mesh handshakes: only members who
        // know it may link and read the group's history
        val password = p2p.currentGroupPassword
            .ifBlank { ChatApp.savedGroupPassword(getApplication(), groupId) }
        GroupMeshManager.enterGroup(groupId, my, peers, p2p.messages.value, password)
    }

    private fun teardownGroupMesh(groupId: String) {
        GroupMeshManager.leaveGroup(groupId)
    }

    /**
     * Member-sponsored join: answer query_group / join for a group this
     * device belongs to as a MEMBER, so the target IP only has to be in the
     * group — not the creator. The newcomer gets the member list and the
     * host's address, and is announced over the mesh so everyone links with
     * it (works even when the host is offline, via the mesh).
     */
    private fun handleMemberGroupRequest(
        packet: NetworkPacket,
        socket: java.net.Socket,
        wire: Wire
    ): Boolean {
        val idOrName = packet.groupId ?: return false
        val p2p = groupP2pMap.values.firstOrNull {
            !it.isHostNode && it.joinIdValue == idOrName
        } ?: return false
        val groupId = p2p.currentGroupId
        try {
            when (packet.type) {
                Protocol.MODE_QUERY -> {
                    val info = GroupInfo(
                        groupName = p2p.currentGroupName,
                        creatorName = p2p.myNameValue,
                        creatorId = p2p.myIdValue,
                        memberCount = p2p.peers.value.size + 1
                    )
                    wire.sendPacket(NetworkPacket(type = "group_info", groupInfo = info))
                }
                Protocol.MODE_JOIN -> {
                    val peer = packet.peer ?: return false
                    // the group password was already verified by the
                    // password-bound handshake on this wire
                    val host = findHostPeer(p2p)
                    // exclude the host from the mesh member list: it relays to
                    // everyone, linking to it would be redundant
                    val members = p2p.peers.value.values.filter { host == null || it.id != host.id }
                    // offline-member delete convergence: the sponsor's ack
                    // carries this group's tombstoned ids as well (null
                    // omitted; empty list must not serialize either)
                    wire.sendPacket(
                        NetworkPacket(
                            type = "join_ack",
                            groupId = groupId,
                            members = members,
                            host = host,
                            // the owner's current announcement (the sponsor
                            // mirrors it, like the host itself would)
                            announcement = p2p.currentGroupAnnouncement.ifEmpty { null },
                            deletedIds = deletedIdsFor(groupId).ifEmpty { null }
                        )
                    )
                    socket.close()
                    // tell every member about the newcomer so the mesh links up
                    GroupMeshManager.announcePeer(groupId, peer)
                }
                else -> return false
            }
        } catch (e: Exception) {
            Log.w("ChatViewModel", "member group request failed", e)
        } finally {
            runCatching { socket.close() }
        }
        return true
    }

    private fun findHostPeer(p2p: P2PManager): Peer? {
        val meta = _groups.value.find { it.groupId == p2p.currentGroupId } ?: return null
        val hostIp = meta.hostIp
        if (hostIp.isBlank()) return null
        val parsed = parseHostPort(hostIp)
        return p2p.peers.value.values.find { it.ipAddress == parsed.host && it.port == parsed.port }
            ?: Peer("host", p2p.currentGroupName, parsed.host, parsed.port)
    }

    // ------------------------------------------------------------- group dao

    private val chatDao: ChatDao = ChatDatabase.getInstance(application).chatDao()

    private val directJson = Json { ignoreUnknownKeys = true }
    private val directContactsPrefs
        get() = getApplication<Application>().getSharedPreferences("localchat_prefs", MODE_PRIVATE)

    /** The whole program uses ONE port (default 9999, changeable in settings);
     * a single shared host server listens on it and serves every host group. */
    private val port: Int
        get() = ChatApp.savedPort(getApplication())

    private val hostServer = HostGroupServer(ChatApp.savedPort(getApplication()))

    private val isAppForeground = MutableStateFlow(true)

    /** Registered in init to re-announce on network changes; unregistered
     *  in onCleared. */
    private var connectivityManager: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    private val _groups = MutableStateFlow<List<GroupMeta>>(emptyList())
    val groups: StateFlow<List<GroupMeta>> = _groups.asStateFlow()

    private val _activeGroupId = MutableStateFlow<String?>(null)

    private val _activeGroupName = MutableStateFlow("")
    val activeGroupName: StateFlow<String> = _activeGroupName.asStateFlow()

    private val _activeIsHost = MutableStateFlow(false)
    val activeIsHost: StateFlow<Boolean> = _activeIsHost.asStateFlow()

    private val _activeMyName = MutableStateFlow("")
    val activeMyName: StateFlow<String> = _activeMyName.asStateFlow()

    val activeGroupId: StateFlow<String?> get() = _activeGroupId.asStateFlow()

    private val _rejoinInProgress = MutableStateFlow(false)
    val rejoinInProgress: StateFlow<Boolean> = _rejoinInProgress.asStateFlow()

    private val _rejoinFailed = MutableStateFlow(false)
    val rejoinFailed: StateFlow<Boolean> = _rejoinFailed.asStateFlow()

    /** Download state per file message id, surfaced to the chat UI. */
    sealed class DownloadState {
        /** [percent] is 0..100; updated throttled (5% or 256KB steps). */
        data class Downloading(val percent: Int = 0) : DownloadState()
        data class Done(val uri: String) : DownloadState()
        data class Failed(val message: String) : DownloadState()
    }

    private val _downloadStates = MutableStateFlow<Map<String, DownloadState>>(emptyMap())
    val downloadStates: StateFlow<Map<String, DownloadState>> = _downloadStates.asStateFlow()

    /** Progress/outcome of a folder download, keyed by folderId (analogous to
     *  [DownloadState] for a single file). [total] is the number of entries
     *  being saved, [done] how many finished so far. */
    data class FolderDownloadState(
        val downloading: Boolean = false,
        val done: Int = 0,
        val total: Int = 0,
        val savedPath: String = "",
        val message: String = ""
    )

    private val _folderDownloadStates =
        MutableStateFlow<Map<String, FolderDownloadState>>(emptyMap())
    val folderDownloadStates: StateFlow<Map<String, FolderDownloadState>> =
        _folderDownloadStates.asStateFlow()

    /** One running download: the cancel flag the transfer loop polls per chunk
     *  plus every socket the transfer opened, so a cancel can shut a blocked
     *  read down at once (the flag alone only lands at the next chunk). */
    private class DownloadHandle {
        val cancelled = AtomicBoolean(false)
        val socks: MutableList<java.net.Socket> =
            java.util.Collections.synchronizedList(mutableListOf())
    }

    /** Active downloads keyed by file message id. Companion-held so the chat
     *  UI can cancel via [cancelDownload] without needing the ViewModel
     *  instance (FileMessageBubble is shared by the group and direct
     *  screens). */
    companion object {
        private const val CHANNEL_MESSAGES = "localchat_messages"

        /** Typing indicator cadence (Windows parity): while the user keeps
         *  typing, refresh at most once per interval; stop after the delay
         *  with no input; expire a received indicator after the timeout. */
        const val TYPING_ACTIVE_INTERVAL_MS = 2_000L
        const val TYPING_STOP_DELAY_MS = 4_000L
        const val TYPING_TIMEOUT_MS = 6_000L
        private const val TYPING_TICK_MS = 1_000L
        /** Call-log history kept per conversation (newest kept, oldest
         *  trimmed) — Windows ChatStore.CALL_LOG_CAP parity. */
        private const val CALL_LOG_CAP = 200
        private val activeDownloads = ConcurrentHashMap<String, DownloadHandle>()

        /** Active folder downloads keyed by folderId, so the UI can cancel a
         *  whole folder save via [cancelDownload] without the ViewModel. */
        private val activeFolderDownloads = ConcurrentHashMap<String, DownloadHandle>()

        /** User tapped 取消下载: the download aborts, the partial file is
         *  deleted and the state becomes 失败（已取消）. The transfer socket is
         *  shut down so an in-flight read returns immediately (a stalled peer
         *  must not hold the cancel for the whole 120s read timeout). The key
         *  is a file message id OR a folderId. */
        fun cancelDownload(fileId: String) {
            val handle = activeDownloads[fileId] ?: activeFolderDownloads[fileId] ?: return
            handle.cancelled.set(true)
            for (s in handle.socks.toList()) {
                runCatching { s.shutdownInput() }
                runCatching { s.shutdownOutput() }
            }
        }

        private const val GROUP_KEY_MESSAGES = "localchat_message_group"
        private const val SUMMARY_NOTIFICATION_ID = 20001
        private const val MAX_NOTIFICATION_MESSAGES = 8
        const val ACTION_NOTIF_DISMISSED = "com.zqr.localchat.NOTIF_DISMISSED"
        const val EXTRA_DISMISSED_GROUP_ID = "com.zqr.localchat.DISMISSED_GROUP_ID"

        /** Notification quick reply: the reply BroadcastReceiver reuses the
         *  ViewModel's send paths through [tryDeliverQuickReply]. */
        const val ACTION_NOTIF_REPLY = "com.zqr.localchat.NOTIF_REPLY"
        const val EXTRA_REPLY_CONVERSATION_ID = "com.zqr.localchat.REPLY_CONVERSATION_ID"
        const val REMOTE_INPUT_QUICK_REPLY = "com.zqr.localchat.QUICK_REPLY"

        /** Conversation key prefix of a 1:1 chat ("direct:<peerId>"), shared
         *  with the persisted message table's synthetic group id. */
        private const val DIRECT_PREFIX = "direct:"

        /**
         * Quick-reply sink installed by the live ViewModel instance. The
         * reply BroadcastReceiver runs outside the ViewModel lifecycle, so —
         * like the other process-wide hooks (P2PManager.onPeerDeletedIds …) —
         * this companion hook routes its text back into the real send paths.
         * Null while no ViewModel is alive.
         */
        @Volatile
        private var quickReplySink: ((String, String) -> Unit)? = null

        fun setQuickReplySink(sink: ((String, String) -> Unit)?) {
            quickReplySink = sink
        }

        /**
         * Deliver a text typed in a notification's RemoteInput action to the
         * conversation it came from. Returns false when nothing could deliver
         * it: with a live ViewModel the send always runs (offline direct sends
         * queue, exactly like the UI), without one only a 1:1 reply can go out
         * through the process-wide DirectChatManager — a group needs the
         * ViewModel's live P2PManager.
         */
        fun tryDeliverQuickReply(conversationId: String, text: String): Boolean {
            if (conversationId.isBlank() || text.isBlank()) return false
            quickReplySink?.let { sink ->
                sink(conversationId, text)
                return true
            }
            return if (conversationId.startsWith(DIRECT_PREFIX)) {
                DirectChatManager.sendMessage(
                    conversationId.removePrefix(DIRECT_PREFIX), text
                )
            } else {
                false
            }
        }

        /** Process-wide mirror of the current group names (kept in sync with
         *  [groups] in init): the setup screen reads it to confirm before a
         *  same-name creation silently replaces the old group instance. */
        val savedGroupNames = MutableStateFlow<List<String>>(emptyList())

        private data class NotifEntry(val sender: String, val text: String, val time: Long)

        /**
         * Recent incoming messages per group: a follow-up notification
         * re-renders the whole recent conversation (MessagingStyle) instead
         * of a single orphan bubble. Lives on the companion — NOT the
         * instance — because it must stay in sync with what is actually in
         * the shade across activity/ViewModel recreation, and because
         * [NotificationDismissReceiver] clears entries from outside any
         * instance. Cleared when a notification is cancelled or swiped away,
         * so it only ever holds messages the user has NOT seen as
         * notifications yet.
         */
        private val notificationLog = ConcurrentHashMap<String, MutableList<NotifEntry>>()

        private fun notificationsPermissionGranted(context: Context): Boolean =
            Build.VERSION.SDK_INT < 33 ||
                ContextCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

        private fun ensureMessageChannel(context: Context) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val channel = NotificationChannel(
                    CHANNEL_MESSAGES,
                    "新消息",
                    NotificationManager.IMPORTANCE_HIGH
                ).apply {
                    description = "收到新群聊消息时通知"
                }
                context.getSystemService(NotificationManager::class.java)
                    .createNotificationChannel(channel)
            }
        }

        /**
         * Swipe-away hook for [NotificationDismissReceiver]: a dismissed
         * bubble must drop its log entry, or the group's next message
         * re-renders already-dismissed messages and the summary keeps stale
         * counts. A null group id means the whole summary / group stack was
         * dismissed — clear everything.
         */
        fun onNotificationsDismissed(context: Context, groupId: String?) {
            if (groupId == null) notificationLog.clear()
            else notificationLog.remove(groupId)
            refreshMessageSummary(context)
        }

        /** Reconcile the grouped summary with the log: posted only while two
         *  or more conversations still have unread notifications (a single
         *  child stands alone), cancelled otherwise. */
        private fun refreshMessageSummary(context: Context) {
            if (!notificationsPermissionGranted(context)) return
            ensureMessageChannel(context)
            val nm = NotificationManagerCompat.from(context)
            if (notificationLog.size < 2) {
                nm.cancel(SUMMARY_NOTIFICATION_ID)
                return
            }
            val total = notificationLog.values.sumOf { it.size }
            val openIntent = Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            }
            val contentPendingIntent = PendingIntent.getActivity(
                context, SUMMARY_NOTIFICATION_ID, openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            // no group id extra = swipe of the whole stack clears the log
            val deleteIntent = Intent(context, NotificationDismissReceiver::class.java)
                .setAction(ACTION_NOTIF_DISMISSED)
            val deletePendingIntent = PendingIntent.getBroadcast(
                context, SUMMARY_NOTIFICATION_ID, deleteIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val summary = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setGroup(GROUP_KEY_MESSAGES)
                .setGroupSummary(true)
                .setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)
                .setContentTitle("LocalChat")
                .setContentText("${notificationLog.size} 个会话共 $total 条新消息")
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .setAutoCancel(true)
                .setContentIntent(contentPendingIntent)
                .setDeleteIntent(deletePendingIntent)
                .build()
            nm.notify(SUMMARY_NOTIFICATION_ID, summary)
        }
    }

    /**
     * Shared runner for group and direct downloads: registers a cancel flag
     * and the transfer's sockets, streams into [targetUri] with throttled
     * progress (5% or 256KB, the UI shows 下载中 N%), deletes the partial file
     * on any failure and marks the final state. [transfer] performs the actual
     * blocking transfer.
     */
    private fun runDownload(
        fileId: String,
        targetUri: Uri,
        transfer: (
            OutputStream,
            (Long, Long) -> Unit,
            () -> Boolean,
            MutableList<java.net.Socket>
        ) -> FileTransfer.DownloadResult
    ) {
        val handle = DownloadHandle()
        activeDownloads[fileId] = handle
        _downloadStates.update { it + (fileId to DownloadState.Downloading(0)) }
        viewModelScope.launch(Dispatchers.IO) {
            val resolver = getApplication<Application>().contentResolver
            val result = runCatching {
                val out = resolver.openOutputStream(targetUri, "w")
                    ?: error("无法打开输出流")
                var lastBytes = 0L
                var lastPercent = -5
                out.use { os ->
                    transfer(
                        os,
                        { received, total ->
                            // throttle: report at 5%-of-total OR 256KB steps,
                            // whichever comes first (a known total also forces
                            // the final 100% update)
                            val percent = if (total > 0) {
                                ((received * 100) / total).toInt().coerceIn(0, 100)
                            } else 0
                            if (received - lastBytes >= 256 * 1024 ||
                                percent >= lastPercent + 5 ||
                                (total > 0 && received >= total)
                            ) {
                                lastBytes = received
                                lastPercent = percent
                                _downloadStates.update {
                                    it + (fileId to DownloadState.Downloading(percent))
                                }
                            }
                        },
                        { handle.cancelled.get() },
                        handle.socks
                    )
                }
            }.getOrElse { FileTransfer.DownloadResult(false, it.message ?: "未知错误") }
            activeDownloads.remove(fileId)
            if (!result.ok) {
                // drop the partially written file so a failed download does
                // not leave a corrupt copy behind
                runCatching { resolver.delete(targetUri, null, null) }
            }
            _downloadStates.update { map ->
                map + (fileId to when {
                    result.ok -> DownloadState.Done(targetUri.toString())
                    handle.cancelled.get() -> DownloadState.Failed("已取消")
                    else -> DownloadState.Failed(result.message)
                })
            }
        }
    }

    /**
     * Save every entry of a folder offer (all messages sharing [folderId]) to
     * the user-picked [destTreeUri] tree, sequentially on one IO coroutine.
     * Missing child directories/files are created under the tree (parents
     * first) and every path segment is re-sanitized, so a crafted relativePath
     * can never write outside the chosen folder. Progress and the outcome are
     * surfaced via [folderDownloadStates]; cancel via [cancelDownload] keyed by
     * folderId. Returns false when the folder has no entries.
     */
    fun downloadFolder(folderId: String, destTreeUri: Uri): Boolean {
        val gid = _activeGroupId.value ?: return false
        val p2p = groupP2pMap[gid] ?: return false
        return startFolderDownload(folderId, destTreeUri, p2p.messages.value) { fi, out, progress, cancelled, socks ->
            p2p.downloadFile(fi, out, progress, cancelled, socks)
        }
    }

    /** Save every entry of a direct-chat folder offer (see [downloadFolder]). */
    fun downloadDirectFolder(peerId: String, folderId: String, destTreeUri: Uri): Boolean {
        val messages = DirectChatManager.messagesFor(peerId).value
        return startFolderDownload(folderId, destTreeUri, messages) { fi, out, progress, cancelled, socks ->
            DirectChatManager.downloadFile(fi, out, progress, cancelled, socks)
        }
    }

    private fun startFolderDownload(
        folderId: String,
        destTreeUri: Uri,
        messages: List<ChatMessage>,
        transfer: (
            FileInfo,
            OutputStream,
            (Long, Long) -> Unit,
            () -> Boolean,
            MutableList<java.net.Socket>
        ) -> FileTransfer.DownloadResult
    ): Boolean {
        // de-duplicate by message id (the group offers the folder over both the
        // relay and the mesh, so the same entry can appear twice) and order by
        // relativePath for a deterministic, parent-before-child creation order
        val seen = HashSet<String>()
        val entries = messages
            .filter { m ->
                val fi = m.fileInfo
                fi != null && fi.folderId == folderId && seen.add(m.id)
            }
            .sortedBy { it.fileInfo!!.relativePath.ifEmpty { it.fileInfo!!.fileName } }
        if (entries.isEmpty()) return false

        val handle = DownloadHandle()
        activeFolderDownloads[folderId] = handle
        val rootName = entries.firstNotNullOfOrNull {
            it.fileInfo!!.folderName.ifBlank { null }
        } ?: ""
        val total = entries.size
        _folderDownloadStates.update {
            it + (folderId to FolderDownloadState(downloading = true, done = 0, total = total))
        }

        val resolver = getApplication<Application>().contentResolver
        val rootDoc = DocumentsContract.buildDocumentUriUsingTree(
            destTreeUri, DocumentsContract.getTreeDocumentId(destTreeUri)
        )
        viewModelScope.launch(Dispatchers.IO) {
            var done = 0
            var failedMessage = ""
            for (entry in entries) {
                if (handle.cancelled.get()) {
                    failedMessage = "已取消"
                    break
                }
                val fi = entry.fileInfo!!
                if (fi.downloadHost.isBlank() || fi.downloadPort <= 0) {
                    failedMessage = "部分文件已过期，请对方重新发送"
                    break
                }
                val rel = sanitizeRelativePath(fi.relativePath.ifEmpty { fi.fileName })
                if (rel.isEmpty()) {
                    failedMessage = "无效的文件路径"
                    break
                }
                // defense in depth: relativePath is already sanitized, but
                // re-sanitize every segment here and reject any that comes back
                // empty so nothing can traverse out of the picked tree
                val segments = rel.split('/').filter { it.isNotEmpty() }
                if (segments.isEmpty()) {
                    failedMessage = "无效的文件路径"
                    break
                }
                val parentUri = ensureFolderDirs(resolver, rootDoc, rootName, segments.dropLast(1))
                if (parentUri == null) {
                    failedMessage = "无法创建保存目录"
                    break
                }
                val fileName = segments.last()
                val existing = findChildByName(resolver, parentUri, fileName)
                val targetUri = existing ?: runCatching {
                    DocumentsContract.createDocument(
                        resolver, parentUri, "application/octet-stream", fileName
                    )
                }.getOrNull()
                if (targetUri == null) {
                    failedMessage = "无法创建文件"
                    break
                }
                val result = runCatching {
                    resolver.openOutputStream(targetUri, "wt")?.use { out ->
                        transfer(fi, out, { _, _ -> }, { handle.cancelled.get() }, handle.socks)
                    } ?: FileTransfer.DownloadResult(false, "无法打开输出流")
                }.getOrElse { FileTransfer.DownloadResult(false, it.message ?: "未知错误") }
                if (!result.ok) {
                    // drop the partial file so a failed entry leaves no corrupt copy
                    runCatching { DocumentsContract.deleteDocument(resolver, targetUri) }
                    failedMessage = result.message.ifBlank { "下载失败" }
                    break
                }
                done++
                _folderDownloadStates.update {
                    it + (folderId to FolderDownloadState(
                        downloading = true, done = done, total = total
                    ))
                }
            }
            activeFolderDownloads.remove(folderId)
            val cancelled = handle.cancelled.get()
            _folderDownloadStates.update {
                it + (folderId to FolderDownloadState(
                    downloading = false,
                    done = done,
                    total = total,
                    savedPath = if (failedMessage.isEmpty()) destTreeUri.toString() else "",
                    message = if (failedMessage.isEmpty()) "" else failedMessage
                ))
            }
            if (cancelled) Log.i("ChatViewModel", "folder download cancelled: $folderId")
        }
        return true
    }

    /** Walk (and create) the child directories [segments] under [rootDoc],
     *  returning the URI of the innermost one. [segments] is already sanitized;
     *  each name is re-sanitized and a null return means the tree could not be
     *  created. */
    private fun ensureFolderDirs(
        resolver: android.content.ContentResolver,
        rootDoc: Uri,
        rootName: String,
        segments: List<String>
    ): Uri? {
        var current = rootDoc
        val rootSegment = sanitizePathSegmentForTree(rootName)
        if (rootSegment.isNotEmpty()) {
            val created = findChildByName(resolver, current, rootSegment) ?: runCatching {
                DocumentsContract.createDocument(resolver, current, DocumentsContract.Document.MIME_TYPE_DIR, rootSegment)
            }.getOrNull()
            if (created == null) return null
            current = created
        }
        for (raw in segments) {
            val segment = sanitizePathSegmentForTree(raw)
            if (segment.isEmpty()) return null
            val child = findChildByName(resolver, current, segment) ?: runCatching {
                DocumentsContract.createDocument(resolver, current, DocumentsContract.Document.MIME_TYPE_DIR, segment)
            }.getOrNull()
            if (child == null) return null
            current = child
        }
        return current
    }

    /** Find an existing child document named [name] (directory or file) directly
     *  under [parentUri], or null. */
    private fun findChildByName(
        resolver: android.content.ContentResolver,
        parentUri: Uri,
        name: String
    ): Uri? {
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(
            parentUri, DocumentsContract.getDocumentId(parentUri)
        )
        return runCatching {
            resolver.query(
                childrenUri,
                arrayOf(
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME
                ),
                null, null, null
            )?.use { c ->
                while (c.moveToNext()) {
                    if (c.getString(1) == name) {
                        return@use DocumentsContract.buildDocumentUriUsingTree(
                            parentUri, c.getString(0)
                        )
                    }
                }
                null
            }
        }.getOrNull()
    }

    /** Sanitize one path segment for a SAF document name: strip separators and
     *  control characters, drop trailing dots/spaces and cap the length. An
     *  empty result is rejected by the callers so no entry can escape the
     *  picked tree. */
    private fun sanitizePathSegmentForTree(raw: String): String {
        val base = raw.replace('\\', '/').substringAfterLast('/')
        val cleaned = base.filter { ch -> ch.code >= 32 && ch.code !in 0x7F..0xA0 && ch != ':' }
        return cleaned.trim('.', ' ').take(200).trim('.', ' ')
    }

    private fun activeP2pFlow(): Flow<P2PManager?> =
        combine(_activeGroupId, _groupP2pVersion) { gid, _ -> gid }.flatMapLatest { gid ->
            gid?.let { groupP2pMap[it] }?.let { flowOf(it) } ?: flowOf(null)
        }

    val activePeers: StateFlow<Map<String, Peer>> = activeP2pFlow()
        .flatMapLatest { it?.peers ?: flowOf(emptyMap()) }
        .stateIn(viewModelScope, SharingStarted.Eagerly, emptyMap())

    /** Live message list of the active group. When the group has no live
     *  connection (rejoin still running, or it failed) the persisted history
     *  is served instead — the chat log must stay readable even when the
     *  host/peers are unreachable. A successful reconnect bumps
     *  [_groupP2pVersion] and switches the flow back to the live list. */
    val activeMessages: StateFlow<List<ChatMessage>> =
        combine(_activeGroupId, _groupP2pVersion) { gid, _ -> gid }
            .flatMapLatest { gid ->
                val p2p = gid?.let { groupP2pMap[it] }
                when {
                    p2p != null -> p2p.messages
                    gid != null -> chatDao.getMessagesForGroup(gid).map { rows ->
                        rows.map { sm ->
                            val plain = sm.withPlainContent()
                            ChatMessage(
                                id = plain.id,
                                content = plain.content,
                                timestamp = plain.timestamp,
                                senderId = plain.senderId,
                                senderName = plain.senderName,
                                isFromMe = plain.isFromMe,
                                fileInfo = restoredFileInfo(plain)
                            )
                        }
                    }
                    else -> flowOf(emptyList())
                }
            }
            .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    val activeServerError: StateFlow<String?> = activeP2pFlow()
        .flatMapLatest { it?.serverError ?: flowOf(null) }
        .stateIn(viewModelScope, SharingStarted.Eagerly, null)

    val activeConnectionLost: StateFlow<Boolean> = activeP2pFlow()
        .flatMapLatest { p2p ->
            if (p2p == null) {
                flowOf(false)
            } else {
                // still usable while the group mesh links are alive: the host
                // may be gone but members can keep chatting directly
                combine(p2p.connectionLost, GroupMeshManager.hasLinksFlow(p2p.currentGroupId)) { lost, meshAlive ->
                    lost && !meshAlive
                }
            }
        }
        .stateIn(viewModelScope, SharingStarted.Eagerly, false)

    val localIpAddress: String
        get() = P2PManager.getLocalIpAddress()

    /** All non-loopback IPv4 addresses (the advertised one first), for the
     *  settings page: a peer on another network segment needs a different
     *  one of them. */
    val allLocalIpAddresses: List<LocalAddress>
        get() = P2PManager.getAllLocalIpAddresses()

    /** Short fingerprint of this device's long-term identity key ("安全码"):
     *  compare it with the peer's code out-of-band (e.g. read it aloud) to
     *  rule out a man-in-the-middle on the first direct chat / call. */
    val securityCode: String
        get() = DeviceIdentity.fingerprint() ?: ""

    val localPort: Int
        get() = port

    /** Change the program-wide port and rebind the shared host server.
     * Existing member connections keep working; new joins use the new port.
     * Unconditional (Windows view_model.py parity): the shared listener also
     * serves direct member chats, so it must rebind even when no group is
     * hosted. */
    fun setPort(newPort: Int) {
        if (newPort !in 1..65535) return
        ChatApp.savePort(getApplication(), newPort)
        hostServer.restart(newPort)
        _groups.update { list ->
            list.map { g -> if (g.isHost) g.copy(hostPort = newPort) else g }
        }
        _groupP2pVersion.value++
    }

    private val _setupP2p = MutableStateFlow<P2PManager?>(null)

    /** Whether the foreground service keeps connections alive in the background. */
    private val _backgroundRunning = MutableStateFlow(ChatApp.isBackgroundRunning(getApplication()))
    val backgroundRunning: StateFlow<Boolean> = _backgroundRunning.asStateFlow()

    val queriedGroupInfo: StateFlow<GroupInfo?> = _setupP2p.flatMapLatest {
        it?.queriedGroupInfo ?: flowOf(null)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, null)

    val queryError: StateFlow<String?> = _setupP2p.flatMapLatest {
        it?.queryError ?: flowOf(null)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, null)

    val isQueryingGroup: StateFlow<Boolean> = _setupP2p.flatMapLatest {
        it?.isQuerying ?: flowOf(false)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, false)

    val isJoining: StateFlow<Boolean> = _setupP2p.flatMapLatest {
        it?.isJoining ?: flowOf(false)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, false)

    val connectionResult: StateFlow<P2PManager.ConnectionResult?> = _setupP2p.flatMapLatest {
        it?.connectionResult ?: flowOf(null)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, null)

    /**
     * Pending "successfully joined/connected to a group" signal for the UI to
     * navigate on. A StateFlow (not a one-shot SharedFlow) because a SharedFlow
     * without replay silently loses the event during configuration changes or
     * a moment with no active collector — the user would be stuck on the join
     * form even though the group really connected. The UI consumes it with
     * [consumeJoinNavigation] so it fires exactly once.
     */
    private val _pendingJoinNavigation = MutableStateFlow<String?>(null)
    val pendingJoinNavigation: StateFlow<String?> = _pendingJoinNavigation.asStateFlow()

    fun consumeJoinNavigation() {
        _pendingJoinNavigation.value = null
    }

    init {
        // Long-term device identity for direct chats and call media (loaded
        // once; the private key never leaves app storage).
        DeviceIdentity.ensureLoaded(application)

        // Mirror the group list's names into the companion flow so the setup
        // screen can detect a same-name re-creation without extra wiring.
        viewModelScope.launch {
            _groups.collect { list -> savedGroupNames.value = list.map { it.groupName } }
        }

        // Every finished call is persisted to the local call log; a missed
        // incoming call additionally posts a click-to-open notification.
        viewModelScope.launch {
            CallManager.finishedCalls.collect { call -> handleFinishedCall(call) }
        }

        // Group mesh: messages arriving over member-to-member links (host
        // offline, or history backfill) flow into the owning group's list.
        GroupMeshManager.onGroupMessage = { groupId, msgs ->
            groupP2pMap[groupId]?.mergeIncoming(msgs)
        }
        // Mesh-received deletes apply locally (the mesh path validated the
        // sender itself; senderId == null means a tombstone-synced delete —
        // trusted); the _messages collector then mirrors the removal to the
        // database exactly like relay-delivered deletes, and the tombstone is
        // recorded so a later join_ack / history_reply carries it onward.
        GroupMeshManager.onGroupDelete = { groupId, messageId, senderId ->
            groupP2pMap[groupId]?.removeLocalMessage(messageId, senderId)
            writeTombstones(groupId, listOf(messageId))
        }
        // history_reply tombstone sync: the provider is read on mesh worker
        // threads when a link's history is pushed (see GroupMeshManager).
        GroupMeshManager.deletedIdsProvider = { groupId -> deletedIdsFor(groupId) }
        // Typing indicators: mesh links (host offline) and direct sessions
        // report transitions on their worker threads; the ViewModel state
        // updates are Main-safe (StateFlow) and the ticker expires stale ones.
        GroupMeshManager.onGroupTyping = { groupId, senderId, active ->
            onGroupTyping(groupId, senderId, active)
        }
        DirectChatManager.onTypingChanged = { peerId, active ->
            onDirectTyping(peerId, active)
        }
        // Owner management packets (group_update / kick_member) are only
        // accepted on a mesh link when senderId is the group's creator.
        GroupMeshManager.creatorIdProvider = { groupId ->
            ChatApp.savedGroupCreatorId(getApplication(), groupId)
        }
        // Mesh-received owner commands apply like the relayed ones: refresh the
        // metadata, or run the kicked member's teardown.
        GroupMeshManager.onGroupAdmin = { groupId, packet ->
            applyGroupAdminPacket(groupId, packet)
        }
        // join_ack tombstone sync: tombstones learned from a peer's ack are
        // persisted so a history replay cannot resurrect the deleted rows.
        // Global callbacks (one shared database), set before any join runs.
        // BLOCKING on purpose: the ack is processed before the join result, and
        // the replay that follows must already see these tombstones (a
        // fire-and-forget write could land after the replay had read them).
        P2PManager.onPeerDeletedIds = { groupId, deletedIds ->
            writeTombstonesBlocking(groupId, deletedIds)
        }
        P2PManager.deletedIdsProvider = { groupId -> deletedIdsFor(groupId) }

        // Password resolution for incoming handshakes on the shared listener:
        // host groups by numeric join id, member groups (join sponsors) by
        // their saved password, and mesh groups by their internal id.
        hostServer.passwordLookup = { mode, groupId ->
            val gid = groupId?.takeIf { it.isNotBlank() }
            when {
                gid == null -> null
                mode == Protocol.MODE_MESH -> GroupMeshManager.passwordFor(gid)
                else -> {
                    val host = hostServer.resolveGroup(gid)
                    when {
                        host != null -> host.currentGroupPassword
                        else -> {
                            val member = groupP2pMap.values.firstOrNull {
                                !it.isHostNode && it.joinIdValue == gid
                            }
                            if (member != null) {
                                ChatApp.savedGroupPassword(getApplication(), member.currentGroupId)
                            } else null
                        }
                    }
                }
            }
        }

        // Any member can be the join entry point: query/join packets targeting
        // a group we belong to as a MEMBER are answered here, so newcomers
        // only need the IP of SOME member, not the creator's.
        hostServer.memberGroupHandler = { packet, socket, wire ->
            handleMemberGroupRequest(packet, socket, wire)
        }

        // Direct member chats: the shared listener must be reachable even on
        // devices with no host group, and the local identity must match the
        // one used in groups so contacts unify.
        hostServer.ensureRunning()
        // honor contact removals from previous processes BEFORE announcing:
        // a peer that keeps presenting itself must not resurrect a contact
        // the user deleted (marks carry id + endpoint + removal time)
        val (removedIds, removedEndpoints) = loadDirectRemovedMarks()
        DirectChatManager.restoreRemovedMarks(removedIds, removedEndpoints)
        // unanswered contact requests survive a restart too: they are the
        // user's pending decisions, not transient state
        DirectChatManager.restoreContactRequests(loadDirectContactRequests())
        DirectChatManager.configure(
            myId = ChatApp.savedDeviceId(getApplication()),
            myName = ChatApp.savedNickname(getApplication()).ifBlank { "用户" },
            myIp = P2PManager.getLocalIpAddress(),
            myPort = port,
            savedContacts = loadDirectContacts()
        )
        // Direct-chat call signaling: packets arriving on a 1:1 session are
        // routed into CallManager with the session as the signaling channel.
        DirectChatManager.onCallSignal = { packet ->
            packet.call?.let { call ->
                val callerIp = DirectChatManager.contacts.value[call.callerId]?.ip
                CallManager.handleDirectSignal(
                    channel = CallManager.CallChannel { pid, pkt -> DirectChatManager.sendPacket(pid, pkt) },
                    identity = DirectChatManager,
                    packet = packet,
                    callerIp = callerIp,
                    localId = DirectChatManager.myIdValue,
                    localName = DirectChatManager.myNameValue
                )
            }
        }
        // A dead direct session ends only a call with that same peer. The
        // manager owns every direct contact, so another contact reconnecting
        // must not hang up an active call.
        DirectChatManager.onSessionClosed = { peerId ->
            CallManager.endIfOn(DirectChatManager, "连接已断开", peerId)
        }
        // A session established by the OTHER side must be persisted too:
        // without this, messages received in a chat the local user never
        // opened exist only in memory and vanish when the process dies.
        DirectChatManager.onSessionEstablished = { peerId ->
            observeDirectChat(peerId)
        }
        // A handshake revealed a placeholder "ip:..." contact's real device
        // id: move that chat's observer, persisted rows and open screen over.
        DirectChatManager.onChatMigrated = { fromId, toId ->
            migrateDirectChat(fromId, toId)
        }
        // Removal marks changed (contact removed / re-added): persist them
        // so a restart keeps honoring the removals. May fire on session
        // threads; SharedPreferences.apply() is thread-safe.
        DirectChatManager.onRemovedMarksChanged = { saveDirectRemovedMarks() }
        viewModelScope.launch {
            DirectChatManager.contacts.collect { contacts ->
                saveDirectContacts(contacts.values.toList())
            }
        }
        viewModelScope.launch {
            DirectChatManager.contactRequests.collect { requests ->
                saveDirectContactRequests(requests)
            }
        }
        // Notification quick reply: while this ViewModel is alive, route the
        // reply receiver's text into the normal send paths (see
        // tryDeliverQuickReply / handleQuickReply).
        setQuickReplySink { conversationId, text -> handleQuickReply(conversationId, text) }

        ProcessLifecycleOwner.get().lifecycle.addObserver(object : LifecycleEventObserver {
            override fun onStateChanged(source: LifecycleOwner, event: Lifecycle.Event) {
                isAppForeground.value = event.targetState.isAtLeast(Lifecycle.State.STARTED)
                // sockets rarely survive doze/background: returning to the
                // foreground re-announces (reconnects) every dead contact
                // session instead of waiting for the presence sweep
                if (event == Lifecycle.Event.ON_RESUME) {
                    DirectChatManager.announceOnline()
                }
            }
        })

        // Network changes (Wi-Fi switch, DHCP renewal) kill every session
        // and invalidate our advertised address: re-announce as soon as a
        // LAN-capable network comes up. Registration needs no more than the
        // already-declared ACCESS_NETWORK_STATE.
        runCatching {
            val cm = getApplication<Application>()
                .getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .addTransportType(NetworkCapabilities.TRANSPORT_ETHERNET)
                .build()
            val callback = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    DirectChatManager.announceOnline()
                }
            }
            cm.registerNetworkCallback(request, callback)
            connectivityManager = cm
            networkCallback = callback
        }

        loadPersistedGroups()
        restoreDirectSummaries()

        viewModelScope.launch {
            connectionResult.collect { result ->
                if (result is P2PManager.ConnectionResult.Success) {
                    _rejoinInProgress.value = false
                    _rejoinFailed.value = false
                    val p2p = pendingP2pManager ?: return@collect
                    val rejoinedGroupId = pendingGroupId
                    pendingP2pManager = null
                    pendingGroupId = null
                    val groupId = p2p.currentGroupId
                    // When the join went through a member sponsor, the ack
                    // revealed the real host: persist THAT address (not the
                    // sponsor the user typed), so a later rejoin connects to
                    // the host and never fails just because the sponsor is
                    // offline.
                    val hostPeer = p2p.connectedHost
                    val hostIp = hostPeer?.let { "${it.ipAddress}:${it.port}" } ?: pendingHostIp
                    val hostPort = hostPeer?.port ?: pendingHostPort
                    // Remember the creator (owner) device id: only owner
                    // packets (group_update / kick_member) whose senderId
                    // matches it are accepted. A sponsor join reveals the host
                    // in the ack; a direct join has the host at the address we
                    // dialed; the query response carries creatorId.
                    val creatorId = hostPeer?.id
                        ?: p2p.queriedGroupInfo.value?.creatorId?.takeIf { it.isNotBlank() }
                        ?: run {
                            val dialHost = parseHostPort(pendingHostIp).host
                            p2p.peers.value.values.firstOrNull {
                                it.ipAddress == dialHost && it.port == hostPort
                            }?.id
                        } ?: ""
                    if (creatorId.isNotBlank()) {
                        ChatApp.saveGroupCreatorId(getApplication(), groupId, creatorId)
                    }
                    val announcement = p2p.currentGroupAnnouncement
                    registerGroupP2p(groupId, p2p)
                    startMonitoringGroup(groupId, p2p)
                    setupGroupMesh(groupId, p2p)
                    saveGroupJoinId(groupId, p2p.joinIdValue)
                    _groups.update { groups ->
                        val exists = groups.any { it.groupId == groupId }
                        if (exists) {
                            groups.map { g ->
                                if (g.groupId == groupId) {
                                    g.copy(
                                        isHost = false,
                                        hostIp = hostIp,
                                        hostPort = hostPort,
                                        connected = true,
                                        announcement = announcement
                                    )
                                } else g
                            }
                        } else {
                            listOf(
                                GroupMeta(
                                    groupId,
                                    p2p.currentGroupName,
                                    false,
                                    hostIp = hostIp,
                                    hostPort = hostPort,
                                    muted = ChatApp.isGroupMuted(getApplication(), groupId),
                                    connected = true,
                                    announcement = announcement
                                )
                            ) + groups
                        }
                    }
                    if (rejoinedGroupId == null) {
                        _activeGroupId.value = groupId
                        _activeGroupName.value = p2p.currentGroupName
                        _activeMyName.value = p2p.myNameValue
                        _activeIsHost.value = false
                        _activeGroupPassword.value = null
                    }
                    // a fresh join or rejoin finished: drop the setup handle so
                    // stale success/query state cannot linger (mirrors the
                    // Windows client, which clears setup_p2p after a join)
                    _setupP2p.value = null
                    _pendingJoinNavigation.value = groupId
                    ChatApp.startChatService(getApplication())
                    ChatApp.saveGroupPassword(getApplication(), groupId, p2p.currentGroupPassword)

                    viewModelScope.launch(Dispatchers.IO) {
                        upsertGroup(SavedGroup(
                            groupId = groupId,
                            groupName = p2p.currentGroupName,
                            isHost = false,
                            hostIp = hostIp,
                            hostPort = hostPort,
                            myName = p2p.myNameValue,
                            announcement = announcement
                        ))
                    }
                    loadAndReplayMessages(groupId, p2p)
                } else if (result is P2PManager.ConnectionResult.Error) {
                    if (_rejoinInProgress.value) {
                        _rejoinInProgress.value = false
                        _rejoinFailed.value = true
                    }
                    // keep _setupP2p so the error stays visible in UI until the user acts
                    stopPendingP2p()
                }
            }
        }

        var lastGroupCount = -1
        viewModelScope.launch {
            _groups.collect { groups ->
                if (groups.size != lastGroupCount) {
                    lastGroupCount = groups.size
                    if (groups.isEmpty()) {
                        ChatApp.stopChatService(getApplication())
                    } else {
                        ChatApp.refreshNotification(getApplication(), groups.size)
                    }
                }
            }
        }
    }

    private fun registerGroupP2p(groupId: String, p2p: P2PManager) {
        groupP2pMap[groupId] = p2p
        p2p.callSignalListener = { packet -> CallManager.handleSignal(p2p, packet) }
        // typing indicators relayed by the host (or seen by the host itself)
        p2p.typingListener = { senderId, active ->
            onGroupTyping(groupId, senderId, active)
        }
        // Owner management: refresh/persist after a host-side update or an
        // applied member-side group_update; tear the group down when kicked.
        p2p.adminNotify = { refreshGroupAdmin(groupId, p2p) }
        p2p.kickedNotify = { onKickedFromGroup(groupId, p2p) }
        p2p.serverErrorNotify = { message ->
            _groups.update { list ->
                list.map { g ->
                    if (g.groupId == groupId && g.isHost) g.copy(connected = message == null) else g
                }
            }
        }
        _groupP2pVersion.value++
    }

    /** Reflect an owner group_update (name/announcement) into the group list
     *  and the database. Called from relay or mesh worker threads. */
    private fun refreshGroupAdmin(groupId: String, p2p: P2PManager?) {
        val name = p2p?.currentGroupName
            ?: _groups.value.find { it.groupId == groupId }?.groupName
            ?: return
        val announcement = p2p?.currentGroupAnnouncement
            ?: _groups.value.find { it.groupId == groupId }?.announcement
            ?: ""
        _groups.update { list ->
            list.map { g ->
                if (g.groupId == groupId) g.copy(groupName = name, announcement = announcement) else g
            }
        }
        if (_activeGroupId.value == groupId) _activeGroupName.value = name
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { chatDao.updateGroupAdminInfo(groupId, name, announcement) }
        }
    }

    /** Apply an owner management packet received over the group mesh. */
    private fun applyGroupAdminPacket(groupId: String, packet: NetworkPacket) {
        val p2p = groupP2pMap[groupId]
        when (packet.type) {
            "group_update" -> {
                p2p?.applyRemoteGroupUpdate(
                    packet.groupName?.trim()?.takeIf { it.isNotEmpty() },
                    packet.announcement
                )
                refreshGroupAdmin(groupId, p2p)
            }
            "kick_member" -> {
                val target = packet.targetId ?: return
                if (p2p != null && target == p2p.myIdValue) {
                    onKickedFromGroup(groupId, p2p)
                    return
                }
                p2p?.removePeerLocally(target)
                refreshGroupAdmin(groupId, p2p)
            }
        }
    }

    /** The owner removed this device: tear every connection of the group down,
     *  keep the local history, hide the group and tell the user. Hopped to the
     *  main thread: the map/job state is also mutated there. */
    private fun onKickedFromGroup(groupId: String, p2p: P2PManager) {
        viewModelScope.launch {
            if (groupP2pMap[groupId] !== p2p) return@launch
            val name = _groups.value.find { it.groupId == groupId }?.groupName
                ?: p2p.currentGroupName
            groupP2pMap.remove(groupId)
            monitoringJobs.remove(groupId)?.forEach { it.cancel() }
            p2p.stop()
            teardownGroupMesh(groupId)
            persistedPeerCounts.remove(groupId)
            _groups.update { list -> list.filter { it.groupId != groupId } }
            if (_activeGroupId.value == groupId) {
                _activeGroupId.value = null
                _activeGroupName.value = ""
                _activeMyName.value = ""
                _activeIsHost.value = false
                _activeGroupPassword.value = null
            }
            // the row (and its message history via the FK) stays; kickedAt hides it
            runCatching { chatDao.markGroupKicked(groupId, System.currentTimeMillis()) }
            _groupEvents.tryEmit("你已被移出群组「$name」")
            if (_groups.value.isEmpty()) ChatApp.stopChatService(getApplication())
        }
    }

    /** Owner action: publish a new group name and/or announcement. */
    fun updateGroupInfo(groupName: String?, announcement: String?) {
        val gid = _activeGroupId.value ?: return
        val p2p = groupP2pMap[gid] ?: return
        if (!p2p.isHostNode) return
        val name = groupName?.trim()?.take(20)
        if (name.isNullOrEmpty() && announcement == null) return
        if (p2p.sendGroupUpdate(name, announcement)) {
            refreshGroupAdmin(gid, p2p)
        }
    }

    /** Owner action: remove a member from the active group. */
    fun kickMember(peerId: String) {
        val gid = _activeGroupId.value ?: return
        val p2p = groupP2pMap[gid] ?: return
        if (!p2p.isHostNode) return
        val name = p2p.peers.value[peerId]?.name ?: peerId
        if (p2p.kickMember(peerId)) {
            refreshGroupAdmin(gid, p2p)
            _groupEvents.tryEmit("已将 $name 移出群组")
        }
    }

    /**
     * Merges a group write into the DB without clobbering fields that were not
     * supplied by the caller: existing hostIp/myName/createdAt are preserved when
     * the new row carries empty/default values, so a metadata refresh (member
     * count, last message, unread state) can never wipe reconnect info.
     */
    private suspend fun upsertGroup(group: SavedGroup) {
        if (group.groupId in removedGroupIds) return
        // lastMessage is conversation content: stored Keystore-encrypted at
        // rest, decrypted on load — so the summary logic works on plaintext
        // and everything written back to the DB is protected.
        val old = chatDao.getGroup(group.groupId)?.let { it.copy(lastMessage = StoreCipher.unprotect(it.lastMessage)) }
        val persistedSummary = StoreCipher.protect(group.lastMessage)
        if (old == null) {
            chatDao.insertGroup(group.copy(lastMessage = persistedSummary))
        } else {
            // The last-message summary must never move BACKWARDS: concurrent
            // summary writes can land out of order, and without this an older
            // emission can overwrite a newer lastMessage/lastMessageTime.
            val keepOldSummary = old.lastMessageTime > group.lastMessageTime
            chatDao.updateGroup(
                groupId = group.groupId,
                groupName = group.groupName,
                isHost = group.isHost,
                hostIp = group.hostIp.ifEmpty { old.hostIp },
                hostPort = if (group.hostPort > 0) group.hostPort else old.hostPort,
                myName = group.myName.ifEmpty { old.myName },
                memberCount = group.memberCount,
                lastMessage = if (keepOldSummary) StoreCipher.protect(old.lastMessage) else persistedSummary,
                lastMessageTime = if (keepOldSummary) old.lastMessageTime else group.lastMessageTime
            )
            // updateGroup never writes the management columns (a metadata
            // refresh must not revert a rename), so write a non-empty
            // announcement explicitly
            if (group.announcement.isNotEmpty()) {
                chatDao.updateGroupAdminInfo(group.groupId, group.groupName, group.announcement)
            }
        }
        // the group row now exists: tombstones that arrived during the join
        // (join_ack deletedIds) can finally be written — they were buffered
        // because the FK onto saved_groups would have rejected them
        flushPendingTombstones(group.groupId)
    }

    /** Rebuild a FileInfo from a persisted row. Every restored offer is shown
     *  as expired: the download address was captured in a previous session,
     *  and the sender's download server dies with its process — once EITHER
     *  side restarted, a stale address only produces a failed download. The
     *  bubble renders it as "已过期" instead of offering a download that can
     *  no longer succeed; the sender can re-share the file for a fresh
     *  address. The kind is preserved so image/video messages keep rendering
     *  inline (from their local media copy) across restarts.
     *
     *  Callers must pass a row whose content is already decrypted (see
     *  [withPlainContent]): the body doubles as the file display name. */
    private fun restoredFileInfo(sm: SavedChatMessage): FileInfo? {
        if (sm.fileSize <= 0 && sm.downloadHost.isEmpty()) return null
        // blank the address for every restored offer, own or received
        return FileInfo(
            sm.id, sm.content, sm.fileSize, "", 0, kind = sm.kind,
            folderId = sm.folderId,
            folderName = sm.folderName,
            relativePath = sm.relativePath,
            folderTotal = sm.folderTotal
        )
    }

    /** DB boundary for message bodies: persisted rows carry the content
     *  Keystore-encrypted ("enc1:...", see [StoreCipher]); decryption happens
     *  exactly here, on the read path. */
    private fun SavedChatMessage.withPlainContent(): SavedChatMessage =
        copy(content = StoreCipher.unprotect(content))

    private fun loadPersistedGroups() {
        viewModelScope.launch(Dispatchers.IO) {
            chatDao.getAllGroups().collect { savedGroups ->
                // Host groups now always use the single program-wide port;
                // normalize any previously persisted per-group ports.
                val currentPort = port
                val normalized = savedGroups.filter { it.isHost && it.hostPort != currentPort }
                _groups.update { currentGroups ->
                    val currentIds = currentGroups.map { it.groupId }.toSet()
                    val persisted = savedGroups
                        // direct chats live under a synthetic "direct:..." key
                        // in the same table; never surface them as groups
                        .filter { !it.groupId.startsWith("direct:") }
                        // a group this member was kicked out of keeps its
                        // history row but must never reappear in the list
                        .filter { it.kickedAt == 0L }
                        .map { sg ->
                            GroupMeta(
                                groupId = sg.groupId,
                                groupName = sg.groupName,
                                isHost = sg.isHost,
                                hostIp = sg.hostIp,
                                hostPort = if (sg.isHost) currentPort else sg.hostPort,
                                memberCount = sg.memberCount,
                                lastMessage = StoreCipher.unprotect(sg.lastMessage),
                                lastMessageTime = sg.lastMessageTime,
                                muted = ChatApp.isGroupMuted(getApplication(), sg.groupId),
                                announcement = sg.announcement
                            )
                        }
                        .filter { it.groupId !in currentIds && it.groupId !in removedGroupIds }
                    currentGroups + persisted
                }
                for (sg in savedGroups) {
                    persistedMyNames[sg.groupId] = sg.myName
                }
                for (sg in normalized) {
                    upsertGroup(SavedGroup(
                        groupId = sg.groupId,
                        groupName = sg.groupName,
                        isHost = true,
                        hostIp = P2PManager.getLocalIpAddress(),
                        hostPort = currentPort
                    ))
                }
            }
        }
    }

    /**
     * Restore each persisted direct chat's LAST message after a process
     * restart, so the home-page previews are populated without reconnecting
     * to every member (previews live in the in-memory DirectChatManager,
     * which starts empty on a fresh process). Messages persisted as still
     * pending (offline sends from the previous process) are additionally
     * re-queued into the outbox so they deliver once the peer is reachable.
     */
    private fun restoreDirectSummaries() {
        viewModelScope.launch(Dispatchers.IO) {
            val directGroups = chatDao.getAllGroups().first()
                .filter { it.groupId.startsWith("direct:") }
            for (sg in directGroups) {
                val peerId = sg.groupId.removePrefix("direct:")
                val msgs = chatDao.getMessagesForGroup(sg.groupId).first()
                val last = msgs.lastOrNull()?.withPlainContent() ?: continue
                DirectChatManager.seedLastMessage(
                    peerId,
                    ChatMessage(
                        id = last.id,
                        content = last.content,
                        timestamp = last.timestamp,
                        senderId = last.senderId,
                        senderName = last.senderName,
                        isFromMe = last.isFromMe,
                        fileInfo = restoredFileInfo(last),
                        pending = last.pending
                    )
                )
            }
            runCatching {
                chatDao.getPendingDirectMessages().groupBy { it.groupId }.forEach { (groupId, rows) ->
                    DirectChatManager.restorePending(
                        groupId.removePrefix("direct:"),
                        rows.map { sm ->
                            val plain = sm.withPlainContent()
                            ChatMessage(
                                id = plain.id,
                                content = plain.content,
                                timestamp = plain.timestamp,
                                senderId = plain.senderId,
                                senderName = plain.senderName,
                                isFromMe = plain.isFromMe,
                                fileInfo = restoredFileInfo(plain),
                                pending = true
                            )
                        }
                    )
                }
            }
        }
    }

    private fun loadAndReplayMessages(groupId: String, p2p: P2PManager) {
        val job = viewModelScope.launch(Dispatchers.IO) {
            try {
                val saved = chatDao.getMessagesForGroup(groupId).first()
                // a stale load (this p2p already replaced by a reconnect) must
                // neither publish history into the new instance's message list
                // nor mark replay done — otherwise an old connection's replay
                // finishing late can make the new connection treat persisted
                // messages as deleted and wipe them from the database
                if (groupP2pMap[groupId] !== p2p) return@launch
                // offline-member delete convergence: rows whose delete was
                // applied before this load (join_ack deletedIds arriving while
                // the group was offline) must not replay — drop them from the
                // DB as well so they cannot resurface on a later load either.
                // Tombstones buffered before the group row existed (join) and
                // any that failed to flush count too.
                runCatching { flushPendingTombstones(groupId) }
                val tombstonedIds = runCatching {
                    chatDao.getDeletedMessages(groupId).map { it.msgId }
                }.getOrDefault(emptyList()) + pendingTombstones[groupId].orEmpty()
                if (tombstonedIds.isNotEmpty()) {
                    val staleRows = saved.filter { it.id in tombstonedIds }.map { it.id }
                    if (staleRows.isNotEmpty()) {
                        runCatching {
                            withDbLock(groupId) {
                                staleRows.forEach { chatDao.deleteMessage(groupId, it) }
                            }
                        }
                    }
                }
                val fresh = saved.filter { it.id !in tombstonedIds }
                persistedMessageIds[groupId] = fresh.map { it.id }.toMutableSet()
                if (fresh.isNotEmpty()) {
                    val msgs = fresh.map { sm ->
                        val plain = sm.withPlainContent()
                        ChatMessage(
                            id = plain.id,
                            content = plain.content,
                            timestamp = plain.timestamp,
                            senderId = plain.senderId,
                            senderName = plain.senderName,
                            isFromMe = plain.isFromMe,
                            fileInfo = restoredFileInfo(plain),
                            replyTo = plain.replyTo.ifEmpty { null },
                            replyPreview = plain.replyPreview.ifEmpty { null },
                            replySender = plain.replySender.ifEmpty { null }
                        )
                    }
                    p2p.replaySavedMessages(msgs)
                }
            } finally {
                // close the replay window even on failure: a raised replay must
                // not leave the group permanently unable to mirror deletes
                // (and record their tombstones)
                if (groupP2pMap[groupId] === p2p) {
                    replayDone[groupId] = p2p
                }
            }
        }
        replayJobs[groupId] = job
    }

    fun createGroup(userName: String, groupName: String) {
        if (userName.isBlank() || groupName.isBlank()) return
        val name = groupName.trim().take(20)
        if (name.isBlank()) return
        val nick = userName.trim().take(20)
        ChatApp.saveNickname(getApplication(), nick)

        // Crypto-random group password (8 chars ≈ 47.6 bits): high enough
        // entropy that the PBKDF2-bound handshake cannot be brute-forced
        // offline from a recorded exchange.
        val password = Crypto.randomPassword(8)
        val p2p = P2PManager(getApplication(), port, hostServer)
        p2p.initializeAsHost(nick, name, password)
        // Freeze the numeric join id at creation: a later owner rename
        // (group_update) must not change the id members saved to rejoin.
        p2p.setJoinId(p2p.numericGroupId)
        saveGroupJoinId(p2p.currentGroupId, p2p.joinIdValue)
        p2p.startAsHost()

        val groupId = p2p.currentGroupId
        // The same group name on this device derives the SAME group id: stop
        // the previous instance instead of leaking its sockets/coroutines, and
        // drop its row (a duplicate id would also crash the list's keys).
        groupP2pMap.remove(groupId)?.let { old ->
            monitoringJobs.remove(groupId)?.forEach { it.cancel() }
            CallManager.endIfOn(old, "通话已结束")
            old.stop()
        }
        ChatApp.saveGroupPassword(getApplication(), groupId, password)
        _activeGroupPassword.value = password
        registerGroupP2p(groupId, p2p)
        startMonitoringGroup(groupId, p2p)

        _groups.update { list ->
            listOf(GroupMeta(groupId, name, true, hostPort = port, connected = true)) +
                    list.filter { it.groupId != groupId }
        }
        _activeGroupId.value = groupId
        _activeGroupName.value = name
        _activeMyName.value = nick
        _activeIsHost.value = true
        ChatApp.startChatService(getApplication())

        viewModelScope.launch(Dispatchers.IO) {
            upsertGroup(SavedGroup(
                groupId = groupId,
                groupName = name,
                isHost = true,
                hostIp = P2PManager.getLocalIpAddress(),
                hostPort = port,
                myName = nick
            ))
            // a re-created same-id group is a fresh one: no announcement, and
            // a previous kick marker must not keep it hidden
            runCatching {
                chatDao.updateGroupAdminInfo(groupId, name, "")
                chatDao.markGroupKicked(groupId, 0L)
            }
        }
    }

    /** Query a group by its numeric join id: the id is the join identifier
     *  (the group name is only a display label, learned from the host). */
    fun queryGroup(userName: String, groupId: String, hostIp: String, password: String? = null) {
        val id = groupId.trim().filter { it.isDigit() }
        val ip = hostIp.trim()
        // VM-side double check with the UI gating (UI can be bypassed): the
        // numeric join id must be 8 digits and the endpoint a usable
        // host:port — anything else can never connect.
        if (id.length != 8 || ip.isBlank()) return
        val parsed = parseHostPort(ip)
        if (!isValidHost(parsed.host) || parsed.port !in 1..65535) return
        // A blank nickname (spaces) must not overwrite the saved nickname;
        // keep the current one and continue the join rather than stalling.
        val rawNick = userName.trim().take(20)
        val nick = rawNick.ifBlank { currentNickname() }
        if (rawNick.isNotBlank()) {
            ChatApp.saveNickname(getApplication(), rawNick)
        }
        stopPendingP2p()
        _rejoinInProgress.value = false
        _rejoinFailed.value = false
        val p2p = P2PManager(getApplication(), port, hostServer)
        p2p.initializeAsClient(nick, "", password)
        p2p.setJoinId(id)
        pendingP2pManager = p2p
        pendingHostIp = ip
        pendingHostPort = parsed.port
        pendingGroupId = null
        _setupP2p.value = p2p
        p2p.clearQueryState()
        p2p.clearConnectionResult()
        p2p.queryGroup(parsed.host, parsed.port)
    }

    fun confirmJoin() {
        val p2p = pendingP2pManager ?: return
        val hostIp = pendingHostIp
        if (hostIp.isBlank()) return
        val parsed = parseHostPort(hostIp)
        p2p.confirmJoin(parsed.host, parsed.port)
    }

    fun cancelJoin() {
        stopPendingP2p()
        _setupP2p.value = null
        pendingHostIp = ""
    }

    fun clearJoinState() {
        _setupP2p.value = null
    }

    /** Toggles background running: when enabled the foreground service keeps
     * connections alive while the app is backgrounded; when disabled the
     * service is stopped and connections only live while the app is active. */
    fun setBackgroundRunning(enabled: Boolean) {
        if (_backgroundRunning.value == enabled) return
        ChatApp.setBackgroundRunning(getApplication(), enabled)
        _backgroundRunning.value = enabled
        if (enabled) {
            if (_groups.value.isNotEmpty()) ChatApp.startChatService(getApplication())
        } else {
            ChatApp.stopChatService(getApplication())
        }
    }

    private fun stopPendingP2p() {
        pendingP2pManager?.stop()
        pendingP2pManager = null
        pendingGroupId = null
        pendingHostPort = 0
    }

    fun clearConnectionResult() {
        _setupP2p.value?.clearConnectionResult()
    }

    fun switchToGroup(groupId: String) {
        if (pendingGroupId != null && pendingGroupId != groupId) {
            // a rejoin for another group is pending; cancel it so this group can connect
            stopPendingP2p()
            _setupP2p.value = null
            _rejoinInProgress.value = false
            _rejoinFailed.value = false
        }
        _groups.update { list ->
            list.map { g ->
                if (g.groupId == groupId && g.unreadCount > 0) g.copy(unreadCount = 0) else g
            }
        }
        cancelGroupNotification(groupId)
        val p2p = groupP2pMap[groupId]
        if (p2p != null) {
            _activeGroupId.value = groupId
            _activeGroupName.value = p2p.currentGroupName
            _activeIsHost.value = p2p.isHostNode
            _activeMyName.value = p2p.myNameValue
            _activeGroupPassword.value =
                if (p2p.isHostNode) ChatApp.savedGroupPassword(getApplication(), groupId) else null
            _groups.update { list ->
                list.map { g ->
                    if (g.groupId == groupId) g.copy(connected = !p2p.connectionLost.value) else g
                }
            }
            setupGroupMesh(groupId, p2p)
        } else {
            val meta = _groups.value.find { it.groupId == groupId } ?: return
            _activeGroupId.value = groupId
            _activeGroupName.value = meta.groupName
            _activeMyName.value = persistedMyNames[groupId] ?: "用户"
            if (meta.isHost) {
                // host group survives process restart: re-host on the single
                // program-wide port and replay history
                val nick = persistedMyNames[groupId]?.ifBlank { null } ?: "用户"
                val password = ChatApp.savedGroupPassword(getApplication(), groupId)
                val newP2p = P2PManager(getApplication(), port, hostServer)
                // re-host under the ORIGINAL group id and numeric join id: a
                // rename (group_update) changed only the display name
                newP2p.initializeAsHost(nick, meta.groupName, password.ifBlank { null }, groupId = groupId)
                val storedJoinId = savedGroupJoinId(groupId)
                newP2p.setJoinId(storedJoinId.ifBlank { newP2p.numericGroupId })
                if (storedJoinId.isBlank()) saveGroupJoinId(groupId, newP2p.joinIdValue)
                newP2p.startAsHost()
                _activeGroupPassword.value = password.ifBlank { null }
                registerGroupP2p(groupId, newP2p)
                startMonitoringGroup(groupId, newP2p)
                loadAndReplayMessages(groupId, newP2p)
                viewModelScope.launch(Dispatchers.IO) {
                    upsertGroup(SavedGroup(
                        groupId = groupId,
                        groupName = meta.groupName,
                        isHost = true,
                        hostIp = P2PManager.getLocalIpAddress(),
                        hostPort = port,
                        myName = nick
                    ))
                }
                _activeIsHost.value = true
                _activeMyName.value = nick
                ChatApp.startChatService(getApplication())
            } else {
                _activeIsHost.value = false
                _activeGroupPassword.value = null
                rejoinGroup(groupId)
            }
        }
    }

    private fun rejoinGroup(groupId: String) {
        if (groupP2pMap.containsKey(groupId)) return
        if (pendingP2pManager != null) return
        _rejoinInProgress.value = true
        _rejoinFailed.value = false
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val sg = chatDao.getGroup(groupId)
                if (sg == null || sg.isHost || sg.hostIp.isBlank()) {
                    _rejoinInProgress.value = false
                    _rejoinFailed.value = true
                    return@launch
                }
                val p2p = P2PManager(getApplication(), port, hostServer)
                p2p.initializeAsClient(
                    sg.myName.ifBlank { "用户" },
                    sg.groupName,
                    ChatApp.savedGroupPassword(getApplication(), groupId).ifBlank { null }
                )
                p2p.setJoinId(savedGroupJoinId(groupId))
                pendingP2pManager = p2p
                pendingHostIp = sg.hostIp
                pendingGroupId = groupId
                _setupP2p.value = p2p
                val parsed = parseHostPort(sg.hostIp)
                pendingHostPort = parsed.port
                p2p.confirmJoin(parsed.host, parsed.port)
            } catch (e: Exception) {
                // never leave "正在连接..." stuck: a failed rejoin must always
                // surface as rejoinFailed so the user can retry instead of
                // deleting and re-adding the group
                Log.w("ChatViewModel", "rejoin failed for $groupId", e)
                stopPendingP2p()
                _rejoinInProgress.value = false
                _rejoinFailed.value = true
            }
        }
    }

    fun reconnectActiveGroup() {
        val gid = _activeGroupId.value ?: return
        if (pendingP2pManager != null && pendingGroupId != gid) {
            // a stale join/rejoin for another group would silently block this
            // reconnect; cancel it first
            stopPendingP2p()
            _setupP2p.value = null
            _rejoinInProgress.value = false
            _rejoinFailed.value = false
        }
        if (pendingP2pManager != null) return
        val old = groupP2pMap.remove(gid)
        monitoringJobs.remove(gid)?.forEach { it.cancel() }
        old?.let { CallManager.endIfOn(it, "连接已断开") }
        old?.stop()
        _groups.update { list ->
            list.map { g -> if (g.groupId == gid) g.copy(connected = false) else g }
        }
        rejoinGroup(gid)
    }

    fun retryHostListening() {
        val gid = _activeGroupId.value ?: return
        val meta = _groups.value.find { it.groupId == gid } ?: return
        if (!meta.isHost) return
        // With the program-wide server, retry only rebinds the shared
        // listener; the registered groups stay intact.
        hostServer.restart()
    }

    fun leaveActiveGroup() {
        val gid = _activeGroupId.value ?: return
        if (pendingGroupId == gid) {
            stopPendingP2p()
            _setupP2p.value = null
            pendingHostIp = ""
        }
        val p2p = groupP2pMap.remove(gid)
        monitoringJobs.remove(gid)?.forEach { it.cancel() }
        teardownGroupMesh(gid)
        p2p?.let { CallManager.endIfOn(it, "通话已结束") }
        p2p?.stop()
        // Leaving stops this group's server; the group stays in the list and
        // re-hosts on the same port when re-entered.
        _rejoinInProgress.value = false
        _rejoinFailed.value = false
        _groups.update { list ->
            list.map { g -> if (g.groupId == gid) g.copy(connected = false) else g }
        }
        _activeGroupId.value = null
        _activeGroupName.value = ""
        _activeMyName.value = ""
        _activeIsHost.value = false
        _activeGroupPassword.value = null
    }

    fun removeGroup(groupId: String) {
        if (pendingGroupId == groupId) {
            stopPendingP2p()
            _setupP2p.value = null
            pendingHostIp = ""
            _rejoinInProgress.value = false
            _rejoinFailed.value = false
        }
        val p2p = groupP2pMap.remove(groupId)
        monitoringJobs.remove(groupId)?.forEach { it.cancel() }
        teardownGroupMesh(groupId)
        p2p?.let { CallManager.endIfOn(it, "通话已结束") }
        p2p?.stop()
        removedGroupIds.add(groupId)
        persistedMessageIds.remove(groupId)
        persistedPeerCounts.remove(groupId)
        // tombstones buffered for a group row that never got written are moot
        // (the group is gone): drop them so a later same-id group cannot adopt
        // stale deletes
        pendingTombstones.remove(groupId)
        // drop the group's notification AND its mute flag: group ids can be
        // reused (they derive from fingerprint + port), so a stale flag would
        // silently mute a future group with the same id
        cancelGroupNotification(groupId)
        ChatApp.setGroupMuted(getApplication(), groupId, false)
        _groups.update { list -> list.filter { it.groupId != groupId } }
        if (_activeGroupId.value == groupId) {
            _activeGroupId.value = null
            _activeGroupName.value = ""
            _activeMyName.value = ""
        }
        viewModelScope.launch(Dispatchers.IO) {
            // single transaction: the group row + its messages (FK cascade)
            // are removed atomically, so a process death can never leave the
            // "group still listed but history already gone" state that the
            // old two-statement delete could produce
            runCatching {
                chatDao.deleteGroupAndMessages(groupId)
            }
            removedGroupIds.remove(groupId)
        }
        if (_groups.value.isEmpty()) ChatApp.stopChatService(getApplication())
    }

    fun sendMessage(
        content: String,
        replyTo: String? = null,
        replyPreview: String? = null,
        replySender: String? = null
    ): Boolean {
        if (content.isBlank()) return false
        val gid = _activeGroupId.value ?: return false
        val p2p = groupP2pMap[gid] ?: return false
        // messages can go out over the host relay OR the group mesh (host
        // offline) — either path suffices
        if (!p2p.isConnected && !GroupMeshManager.hasLinks(gid)) return false
        val msg = p2p.sendMessage(content, replyTo, replyPreview, replySender) ?: return false
        GroupMeshManager.broadcast(gid, msg)
        // the message supersedes any "typing" we were showing
        stopTyping(gid)
        return true
    }

    fun sendMessageToGroup(groupId: String, content: String): Boolean {
        if (content.isBlank()) return false
        val p2p = groupP2pMap[groupId] ?: return false
        if (!p2p.isConnected && !GroupMeshManager.hasLinks(groupId)) return false
        val msg = p2p.sendMessage(content) ?: return false
        GroupMeshManager.broadcast(groupId, msg)
        stopTyping(groupId)
        return true
    }

    fun clearUnread(groupId: String) {
        _groups.update { list ->
            list.map { g ->
                if (g.groupId == groupId && g.unreadCount > 0) g.copy(unreadCount = 0) else g
            }
        }
        // also drops a notification still showing from an earlier background
        // session once the user opens the group
        cancelGroupNotification(groupId)
    }

    fun deleteMessage(messageId: String) {
        val gid = _activeGroupId.value ?: return
        val removed = groupP2pMap[gid]?.removeMessage(messageId) ?: false
        if (removed) {
            // host-offline path: the relay may be unreachable, so also push the
            // delete over the group mesh so every member converges
            GroupMeshManager.broadcastDelete(gid, messageId)
            viewModelScope.launch(Dispatchers.IO) {
                // suppressed: a stale DB write racing a group removal is
                // already swallowed by the FK-bound collector path below
                runCatching {
                    withDbLock(gid) {
                        chatDao.deleteMessage(gid, messageId)
                        // offline members learn the delete later via this
                        // device's join_ack / history_reply deletedIds
                        chatDao.upsertDeletedMessages(
                            listOf(DeletedMessage(gid, messageId, System.currentTimeMillis()))
                        )
                        chatDao.trimDeletedMessages(gid, DeletedMessage.CAP)
                    }
                }
            }
        }
    }

    /** Offer a file (from a content Uri) to the active group. */
    fun sendFile(
        uri: Uri,
        fileName: String,
        fileSize: Long,
        kind: String = FileKind.FILE
    ): Boolean {
        if (fileName.isBlank()) return false
        val gid = _activeGroupId.value ?: return false
        val p2p = groupP2pMap[gid] ?: return false
        // Sending depends only on the SENDER being online: the host relay OR a
        // live mesh link is enough, so the host going offline never blocks it.
        if (!p2p.isConnected && !GroupMeshManager.hasLinks(gid)) return false
        val msg = p2p.sendFile(fileName, getApplication<Application>().contentResolver, uri, fileSize, kind)
            ?: return false
        // p2p.sendFile relays the offer to the group when the host is up; the
        // mesh delivers it to every linked member either way (receivers dedup
        // by message id)
        GroupMeshManager.broadcast(gid, msg)
        mirrorOwnMedia(msg.id, uri, fileName, kind)
        return true
    }

    /** Download a file offer into [targetUri]; progress is surfaced via
     * [downloadStates] keyed by the file message id. */
    fun downloadFile(fileInfo: FileInfo, targetUri: Uri) {
        val gid = _activeGroupId.value ?: return
        if (groupP2pMap[gid] == null) return
        runDownload(fileInfo.fileId, targetUri) { out, onProgress, cancelled, socks ->
            FileTransfer.download(fileInfo, out, onProgress, cancelled, socks)
        }
    }

    // ------------------------------------------------------------- video call

    val callState: StateFlow<CallManager.CallState> = CallManager.state
    val callRemoteVideo: StateFlow<Bitmap?> = CallManager.remoteVideo
    val callLocalVideo: StateFlow<Bitmap?> = CallManager.localVideo
    val callAudioMuted: StateFlow<Boolean> = CallManager.audioMuted
    val callVideoMuted: StateFlow<Boolean> = CallManager.videoMuted
    val callUsingFrontCamera: StateFlow<Boolean> = CallManager.usingFrontCamera
    val callEvents: SharedFlow<String> = CallManager.events

    /** Start a video/audio call with a member of the active group. */
    fun startCall(peerId: String, media: String = CallMedia.VIDEO) {
        val gid = _activeGroupId.value ?: return
        val p2p = groupP2pMap[gid] ?: return
        if (p2p.connectionLost.value) return
        val peer = p2p.peers.value[peerId] ?: return
        CallManager.startCall(p2p, peer, media)
    }

    /** Local call history of one 1:1 conversation (oldest first), for the
     *  system-style rows interleaved into the chat flow. */
    fun callLogsFor(peerId: String): Flow<List<CallLogEntity>> =
        chatDao.callLogsFor("direct:$peerId")

    fun acceptCall() = CallManager.acceptCall()

    fun rejectCall() = CallManager.rejectCall()

    fun hangupCall() = CallManager.hangup()

    fun setCallAudioMuted(muted: Boolean) = CallManager.setAudioMuted(muted)

    fun setCallVideoMuted(muted: Boolean) = CallManager.setVideoMuted(muted)

    fun switchCallCamera() = CallManager.switchCamera()

    /**
     * Persist one finished call to the local call log (Room, never synced)
     * and post a clickable notification for a missed incoming call. The call
     * log row lives under the 1:1 conversation with the other participant,
     * where the chat UI interleaves it as a system-style line.
     */
    private suspend fun handleFinishedCall(call: CallManager.FinishedCall) {
        if (call.peerId.isBlank()) return
        val key = "direct:${call.peerId}"
        runCatching {
            val logId = call.callId.ifBlank { java.util.UUID.randomUUID().toString() }
            chatDao.insertCallLog(
                CallLogEntity(
                    id = logId,
                    conversationKey = key,
                    peerId = call.peerId,
                    peerName = call.peerName,
                    direction = call.direction,
                    result = call.result,
                    media = call.media,
                    startTime = call.startedAt,
                    duration = call.duration
                )
            )
            chatDao.trimCallLogs(key, CALL_LOG_CAP)
        }.onFailure { Log.w("ChatViewModel", "failed to persist call log", it) }
        if (call.result == CallResult.MISSED && call.direction == CallDirection.INCOMING) {
            notifyMissedCall(call)
        }
    }

    /** Missed incoming call: a high-priority, click-to-open notification that
     *  jumps straight into the 1:1 chat with the caller. */
    private fun notifyMissedCall(call: CallManager.FinishedCall) {
        val context = getApplication<Application>()
        if (!notificationsPermissionGranted(context)) return
        ensureMessageChannel(context)
        val kind = if (call.media == CallMedia.AUDIO) "语音通话" else "视频通话"
        val caller = call.peerName.ifBlank { call.peerId }
        val openIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            putExtra(MainActivity.EXTRA_OPEN_DIRECT_PEER_ID, call.peerId)
        }
        val contentPendingIntent = PendingIntent.getActivity(
            context, ("missed:" + call.peerId).hashCode(), openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_missed_call)
            .setContentTitle("未接来电")
            .setContentText("$caller 的${kind}未接听")
            .setCategory(NotificationCompat.CATEGORY_MISSED_CALL)
            .setAutoCancel(true)
            .setWhen(call.startedAt)
            .setContentIntent(contentPendingIntent)
            .build()
        NotificationManagerCompat.from(context)
            .notify(("missed:" + call.peerId).hashCode(), notification)
    }

    private fun startMonitoringGroup(groupId: String, p2p: P2PManager) {
        monitoringJobs.remove(groupId)?.forEach { it.cancel() }
        replayDone.remove(groupId)
        replayJobs.remove(groupId)?.cancel()
        val jobPeers = viewModelScope.launch {
            p2p.peers.collect { peerMap ->
                // Losing the host clears the peer map; syncing that emptiness
                // would tear down the group mesh and wipe the persisted peer
                // list exactly when the members need them most (host-offline
                // chatting). Keep the last known members while disconnected.
                if (p2p.connectionLost.value && peerMap.isEmpty()) return@collect
                // every member seen in a group becomes a contact: the member
                // list is the universal address book for direct chats
                peerMap.values.forEach { peer ->
                    DirectChatManager.addContact(
                        DirectChatManager.Contact(peer.id, peer.name, peer.ipAddress, peer.port)
                    )
                }
                // keep the group mesh in sync and persist peers so links can
                // survive the host going offline
                if (!p2p.isHostNode) {
                    GroupMeshManager.syncPeers(groupId, peerMap.values)
                    saveGroupPeers(groupId, peerMap.values)
                }
                val count = peerMap.size + 1
                _groups.update { list ->
                    list.map { g ->
                        if (g.groupId == groupId) g.copy(memberCount = count) else g
                    }
                }
                if (persistedPeerCounts[groupId] != count) {
                    persistedPeerCounts[groupId] = count
                    viewModelScope.launch(Dispatchers.IO) {
                        upsertGroup(SavedGroup(
                            groupId = groupId,
                            groupName = p2p.currentGroupName,
                            isHost = p2p.isHostNode,
                            hostPort = if (p2p.isHostNode) p2p.currentPort else
                                (_groups.value.find { it.groupId == groupId }?.hostPort ?: 0),
                            memberCount = count,
                            lastMessage = _groups.value.find { it.groupId == groupId }?.lastMessage ?: "",
                            lastMessageTime = _groups.value.find { it.groupId == groupId }?.lastMessageTime ?: 0L
                        ))
                    }
                }
            }
        }
        val jobConnection = viewModelScope.launch {
            p2p.connectionLost.collect { lost ->
                if (lost) CallManager.endIfOn(p2p, "连接已断开")
                _groups.update { list ->
                    list.map { g ->
                        if (g.groupId == groupId) g.copy(connected = !lost) else g
                    }
                }
            }
        }
        val jobMessages = viewModelScope.launch {
            p2p.messages.collect { msgs ->
                val last = msgs.lastOrNull()
                _groups.update { list ->
                    list.map { g ->
                        if (g.groupId == groupId) g.copy(
                            lastMessage = last?.content ?: "",
                            lastMessageTime = last?.timestamp ?: 0L
                        ) else g
                    }
                }
                val persistedIds = persistedMessageIds.getOrPut(groupId) { mutableSetOf() }
                val newMessages = msgs.filter { it.id !in persistedIds }
                // reverse-delete ONLY when the CURRENT p2p finished its replay:
                // a stale connection's replayDone entry must not let a fresh
                // connection treat not-yet-loaded rows as deleted and wipe
                // them from the database.
                val removedIds = if (replayDone[groupId] === p2p) {
                    val currentIds = msgs.map { it.id }.toSet()
                    persistedIds.filter { it !in currentIds }
                } else {
                    emptyList()
                }

                if (newMessages.isNotEmpty()) {
                    val incoming = newMessages.filter { !it.isFromMe }
                    if (groupId != _activeGroupId.value) {
                        if (incoming.isNotEmpty()) {
                            _groups.update { list ->
                                list.map { g ->
                                    if (g.groupId == groupId) g.copy(unreadCount = g.unreadCount + incoming.size) else g
                                }
                            }
                        }
                    }
                    if (!isAppForeground.value && incoming.isNotEmpty()) {
                        notifyNewMessages(
                            groupId,
                            _groups.value.find { it.groupId == groupId }?.groupName ?: "群聊",
                            incoming.map { NotifEntry(it.senderName, it.content, it.timestamp) }
                        )
                    }
                }

                if (removedIds.isNotEmpty() || newMessages.isNotEmpty()) {
                    viewModelScope.launch(Dispatchers.IO) {
                        withDbLock(groupId) {
                            // deletes and inserts for this group commit in
                            // submission order: without the lock an insert
                            // enqueued after a delete can finish first and
                            // resurrect a deleted message on restart
                            if (removedIds.isNotEmpty()) {
                                // mirror the insert below: only drop ids from
                                // the persisted set after a confirmed delete,
                                // so a failed delete is retried on the next
                                // emission instead of being forgotten
                                val deleted = runCatching {
                                    removedIds.forEach { chatDao.deleteMessage(groupId, it) }
                                    // every applied delete becomes a tombstone
                                    // so later join_ack / history_reply carry
                                    // it to members that were offline
                                    chatDao.upsertDeletedMessages(
                                        removedIds.map { DeletedMessage(groupId, it, System.currentTimeMillis()) }
                                    )
                                    chatDao.trimDeletedMessages(groupId, DeletedMessage.CAP)
                                }.isSuccess
                                if (deleted) {
                                    persistedIds.removeAll(removedIds)
                                } else {
                                    Log.w("ChatViewModel", "failed to delete ${removedIds.size} messages for $groupId")
                                }
                            }
                            if (newMessages.isNotEmpty()) {
                                val saved = newMessages.map { msg ->
                                    SavedChatMessage(
                                        id = msg.id,
                                        groupId = groupId,
                                        content = StoreCipher.protect(msg.content),
                                        timestamp = msg.timestamp,
                                        senderId = msg.senderId,
                                        senderName = msg.senderName,
                                        isFromMe = msg.isFromMe,
                                        fileSize = msg.fileInfo?.fileSize ?: 0L,
                                        downloadHost = msg.fileInfo?.downloadHost ?: "",
                                        downloadPort = msg.fileInfo?.downloadPort ?: 0,
                                        kind = msg.fileInfo?.kind ?: FileKind.FILE,
                                        folderId = msg.fileInfo?.folderId ?: "",
                                        folderName = msg.fileInfo?.folderName ?: "",
                                        relativePath = msg.fileInfo?.relativePath ?: "",
                                        folderTotal = msg.fileInfo?.folderTotal ?: 0
                                    )
                                }
                                val inserted = runCatching { chatDao.insertMessages(saved) }.isSuccess
                                if (inserted) {
                                    // mark persisted only AFTER a successful
                                    // write; a failed insert stays out of the
                                    // set and is retried on the next emission
                                    persistedIds.addAll(newMessages.map { it.id })
                                } else {
                                    Log.w("ChatViewModel", "failed to persist ${newMessages.size} messages for $groupId")
                                }
                            }
                        }
                        if (last != null) {
                            val meta = _groups.value.find { it.groupId == groupId }
                            runCatching {
                                upsertGroup(SavedGroup(
                                    groupId = groupId,
                                    groupName = p2p.currentGroupName,
                                    isHost = p2p.isHostNode,
                                    hostPort = if (p2p.isHostNode) p2p.currentPort else (meta?.hostPort ?: 0),
                                    memberCount = meta?.memberCount ?: 1,
                                    lastMessage = last.content,
                                    lastMessageTime = last.timestamp
                                ))
                            }
                        }
                    }
                }
            }
        }
        monitoringJobs[groupId] = listOf(jobPeers, jobConnection, jobMessages)
    }

    // ------------------------------------------------ message notifications

    /**
     * A text typed in a notification's quick-reply action: send it through the
     * same path the chat UI uses (group relay / direct session, offline direct
     * sends queue as pending), then clear the conversation's unread state and
     * dismiss its notification — exactly like opening the chat. A failed group
     * reply (no live connection) surfaces a toast instead of dropping the
     * text silently.
     */
    private fun handleQuickReply(conversationId: String, text: String) {
        if (text.isBlank()) return
        val ok = if (conversationId.startsWith(DIRECT_PREFIX)) {
            sendDirectMessage(conversationId.removePrefix(DIRECT_PREFIX), text)
        } else {
            sendMessageToGroup(conversationId, text)
        }
        if (ok) {
            clearUnread(conversationId)
        } else {
            Toast.makeText(
                getApplication(), "回复未发送：会话未连接", Toast.LENGTH_SHORT
            ).show()
        }
    }

    private fun notifyNewMessages(groupId: String, groupName: String, incoming: List<NotifEntry>) {
        if (incoming.isEmpty()) return
        val context = getApplication<Application>()
        if (!notificationsPermissionGranted(context)) return
        if (ChatApp.isGroupMuted(getApplication(), groupId)) return
        ensureMessageChannel(context)
        val log = notificationLog.getOrPut(groupId) { mutableListOf() }
        log.addAll(incoming)
        while (log.size > MAX_NOTIFICATION_MESSAGES) log.removeAt(0)

        val myName = persistedMyNames[groupId]?.ifBlank { null } ?: "我"
        val style = NotificationCompat.MessagingStyle(Person.Builder().setName(myName).build())
            .setConversationTitle(groupName)
        for (m in log) {
            style.addMessage(m.text, m.time, Person.Builder().setName(m.sender).build())
        }
        // distinct requestCodes per group: a shared one would let the last
        // posted intent overwrite every other group's deep-link extra
        val openIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
            if (groupId.startsWith(DIRECT_PREFIX)) {
                putExtra(MainActivity.EXTRA_OPEN_DIRECT_ID, groupId.removePrefix(DIRECT_PREFIX))
            } else {
                putExtra(MainActivity.EXTRA_OPEN_GROUP_ID, groupId)
            }
        }
        val contentPendingIntent = PendingIntent.getActivity(
            context, groupId.hashCode(), openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        // quick reply: the RemoteInput result is delivered to the reply
        // receiver (MUTABLE pending intent, the system fills in the text),
        // which routes it back through tryDeliverQuickReply -> the live
        // ViewModel's normal send path
        val replyIntent = Intent(context, NotificationReplyReceiver::class.java).apply {
            action = ACTION_NOTIF_REPLY
            putExtra(EXTRA_REPLY_CONVERSATION_ID, groupId)
        }
        val replyPendingIntent = PendingIntent.getBroadcast(
            context, groupId.hashCode(), replyIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    PendingIntent.FLAG_MUTABLE
                } else {
                    0
                })
        )
        val replyAction = NotificationCompat.Action.Builder(
            android.R.drawable.ic_menu_send, "回复", replyPendingIntent
        ).addRemoteInput(
            RemoteInput.Builder(REMOTE_INPUT_QUICK_REPLY).setLabel("回复 $groupName").build()
        ).build()
        val deleteIntent = Intent(context, NotificationDismissReceiver::class.java).apply {
            action = ACTION_NOTIF_DISMISSED
            putExtra(EXTRA_DISMISSED_GROUP_ID, groupId)
        }
        val deletePendingIntent = PendingIntent.getBroadcast(
            context, groupId.hashCode(), deleteIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            // MessagingStyle renders the conversation; the title/text are the
            // fallback for launchers that do not style it
            .setContentTitle(groupName)
            .setContentText(log.last().text)
            .setStyle(style)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setWhen(log.last().time)
            .setAutoCancel(true)
            .setContentIntent(contentPendingIntent)
            .addAction(replyAction)
            // swiping the bubble away must drop its log entry too, or the
            // group's next message resurrects dismissed bubbles and the
            // summary keeps stale counts
            .setDeleteIntent(deletePendingIntent)
            .setGroup(GROUP_KEY_MESSAGES)
            // children alert, the summary stays silent
            .setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)
            .build()
        NotificationManagerCompat.from(context).notify(groupId.hashCode(), notification)
        refreshMessageSummary(context)
    }

    private fun cancelGroupNotification(groupId: String) {
        val context = getApplication<Application>()
        notificationLog.remove(groupId)
        NotificationManagerCompat.from(context).cancel(groupId.hashCode())
        refreshMessageSummary(context)
    }

    /** Per-group mute: persisted across restarts. Muting also drops any
     *  notification the group currently shows. */
    fun setGroupMuted(groupId: String, muted: Boolean) {
        ChatApp.setGroupMuted(getApplication(), groupId, muted)
        _groups.update { list ->
            list.map { g ->
                if (g.groupId == groupId) g.copy(muted = muted) else g
            }
        }
        if (muted) cancelGroupNotification(groupId)
    }

    override fun onCleared() {
        super.onCleared()
        monitoringJobs.values.flatten().forEach { it.cancel() }
        replayJobs.values.forEach { it.cancel() }
        replayJobs.clear()
        directJobs.values.forEach { it.cancel() }
        directJobs.clear()
        // detach global callbacks so a cleared ViewModel is never invoked by
        // still-running sessions (they also capture this ViewModel strongly)
        DirectChatManager.onSessionClosed = null
        DirectChatManager.onSessionEstablished = null
        DirectChatManager.onChatMigrated = null
        DirectChatManager.onCallSignal = null
        DirectChatManager.onRemovedMarksChanged = null
        // drop the quick-reply hook: a cleared ViewModel must never be invoked
        // by the reply receiver (a stale sink would send on dead managers)
        setQuickReplySink(null)
        runCatching {
            networkCallback?.let { connectivityManager?.unregisterNetworkCallback(it) }
        }
        networkCallback = null
        connectivityManager = null
        GroupMeshManager.shutdown()
        groupP2pMap.values.forEach { it.stop() }
        pendingP2pManager?.stop()
        hostServer.shutdown()
        ChatApp.stopChatService(getApplication())
    }

}
