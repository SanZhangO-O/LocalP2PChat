package com.zqr.localchat.viewmodel

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
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
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import androidx.lifecycle.viewModelScope
import com.zqr.localchat.ChatApp
import com.zqr.localchat.MainActivity
import com.zqr.localchat.call.CallManager
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.ChatDao
import com.zqr.localchat.data.ChatDatabase
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.Peer
import com.zqr.localchat.data.SavedChatMessage
import com.zqr.localchat.data.SavedGroup
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
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.ConcurrentHashMap
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
        val connected: Boolean = false
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

    /** Transient direct-chat events surfaced as toasts. */
    val directEvents: SharedFlow<String> = DirectChatManager.events

    /**
     * Start a video call with a direct-chat member. Signaling rides the 1:1
     * session socket; when the session is not alive it is pulled up first
     * (the other side auto-accepts) and the call is offered right after.
     */
    fun startDirectCall(peerId: String) {
        val contact = DirectChatManager.contacts.value[peerId] ?: return
        val peer = Peer(contact.id, contact.name, contact.ip, contact.port)
        if (peer.ipAddress.isBlank()) return
        val channel = CallManager.CallChannel { pid, pkt -> DirectChatManager.sendPacket(pid, pkt) }
        if (DirectChatManager.isChatAlive(peerId)) {
            CallManager.startCall(
                peer, channel, DirectChatManager,
                DirectChatManager.myIdValue, DirectChatManager.myNameValue
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
                        DirectChatManager.myIdValue, DirectChatManager.myNameValue
                    )
                }
            }
        }
    }

    /** Offer a file to a direct-chat member. */
    fun sendDirectFile(peerId: String, uri: Uri, fileName: String, fileSize: Long): Boolean {
        if (fileName.isBlank()) return false
        return DirectChatManager.sendFile(
            peerId, fileName, getApplication<Application>().contentResolver, uri, fileSize
        )
    }

    /** Download a file offered in a direct chat into [targetUri]; progress is
     *  surfaced via [downloadStates] keyed by the file message id. */
    fun downloadDirectFile(fileInfo: FileInfo, targetUri: Uri) {
        val fileId = fileInfo.fileId
        _downloadStates.update { it + (fileId to DownloadState.Downloading) }
        viewModelScope.launch(Dispatchers.IO) {
            val resolver = getApplication<Application>().contentResolver
            val result = runCatching {
                val out = resolver.openOutputStream(targetUri, "w")
                    ?: error("无法打开输出流")
                out.use { DirectChatManager.downloadFile(fileInfo, it) }
            }.getOrElse { FileTransfer.DownloadResult(false, it.message ?: "未知错误") }
            if (!result.ok) {
                // drop the partially written file so a failed download does
                // not leave a corrupt copy behind
                runCatching { resolver.delete(targetUri, null, null) }
            }
            _downloadStates.update { map ->
                map + (fileId to if (result.ok)
                    DownloadState.Done(targetUri.toString())
                else
                    DownloadState.Failed(result.message))
            }
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

    fun sendDirectMessage(peerId: String, content: String): Boolean =
        DirectChatManager.sendMessage(peerId, content)

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
        val nick = name.trim().ifBlank { parsed.host }
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
            DirectChatManager.seedMessages(
                peerId,
                saved.map { sm ->
                    ChatMessage(
                        id = sm.id,
                        content = sm.content,
                        timestamp = sm.timestamp,
                        senderId = sm.senderId,
                        senderName = sm.senderName,
                        isFromMe = sm.isFromMe,
                        fileInfo = restoredFileInfo(sm),
                        pending = sm.pending
                    )
                }
            )
            DirectChatManager.messagesFor(peerId).collect { msgs ->
                val persisted = persistedDirectIds.getOrPut(peerId) { mutableSetOf() }
                val pendingMap = persistedDirectPending.getOrPut(peerId) { mutableMapOf() }
                val currentIds = msgs.map { it.id }.toSet()
                val removed = persisted.filter { it !in currentIds }
                val newOnes = msgs.filter { it.id !in persisted }
                // pending -> delivered flips (outbox flush) are plain updates
                val flagChanges = msgs.filter { m ->
                    m.isFromMe && pendingMap.containsKey(m.id) && pendingMap[m.id] != m.pending
                }
                if (removed.isNotEmpty() || newOnes.isNotEmpty() || flagChanges.isNotEmpty()) {
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
                                        content = msg.content,
                                        timestamp = msg.timestamp,
                                        senderId = msg.senderId,
                                        senderName = msg.senderName,
                                        isFromMe = msg.isFromMe,
                                        fileSize = msg.fileInfo?.fileSize ?: 0L,
                                        downloadHost = msg.fileInfo?.downloadHost ?: "",
                                        downloadPort = msg.fileInfo?.downloadPort ?: 0,
                                        pending = msg.pending
                                    )
                                })
                            }.isSuccess
                            if (inserted) {
                                // mark persisted only after a successful write;
                                // a failed insert is retried on the next emission
                                persisted.addAll(newOnes.map { it.id })
                                newOnes.forEach { pendingMap[it.id] = it.pending }
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
        val nick = name.trim()
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
                    wire.sendPacket(
                        NetworkPacket(type = "join_ack", groupId = groupId, members = members, host = host)
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
        data object Downloading : DownloadState()
        data class Done(val uri: String) : DownloadState()
        data class Failed(val message: String) : DownloadState()
    }

    private val _downloadStates = MutableStateFlow<Map<String, DownloadState>>(emptyMap())
    val downloadStates: StateFlow<Map<String, DownloadState>> = _downloadStates.asStateFlow()

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
                            ChatMessage(
                                id = sm.id,
                                content = sm.content,
                                timestamp = sm.timestamp,
                                senderId = sm.senderId,
                                senderName = sm.senderName,
                                isFromMe = sm.isFromMe,
                                fileInfo = restoredFileInfo(sm)
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
     * Existing member connections keep working; new joins use the new port. */
    fun setPort(newPort: Int) {
        if (newPort !in 1..65535) return
        ChatApp.savePort(getApplication(), newPort)
        if (hostServer.hasGroups()) hostServer.restart(newPort)
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

        // Group mesh: messages arriving over member-to-member links (host
        // offline, or history backfill) flow into the owning group's list.
        GroupMeshManager.onGroupMessage = { groupId, msgs ->
            groupP2pMap[groupId]?.mergeIncoming(msgs)
        }
        // Mesh-received deletes apply locally (the mesh path validated the
        // sender itself); the _messages collector then mirrors the removal to
        // the database exactly like relay-delivered deletes.
        GroupMeshManager.onGroupDelete = { groupId, messageId, senderId ->
            groupP2pMap[groupId]?.removeLocalMessage(messageId, senderId)
        }

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
                    registerGroupP2p(groupId, p2p)
                    startMonitoringGroup(groupId, p2p)
                    setupGroupMesh(groupId, p2p)
                    saveGroupJoinId(groupId, p2p.joinIdValue)
                    _groups.update { groups ->
                        val exists = groups.any { it.groupId == groupId }
                        if (exists) {
                            groups.map { g ->
                                if (g.groupId == groupId) {
                                    g.copy(isHost = false, hostIp = hostIp, hostPort = hostPort, connected = true)
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
                                    connected = true
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
                            myName = p2p.myNameValue
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
        p2p.serverErrorNotify = { message ->
            _groups.update { list ->
                list.map { g ->
                    if (g.groupId == groupId && g.isHost) g.copy(connected = message == null) else g
                }
            }
        }
        _groupP2pVersion.value++
    }

    /**
     * Merges a group write into the DB without clobbering fields that were not
     * supplied by the caller: existing hostIp/myName/createdAt are preserved when
     * the new row carries empty/default values, so a metadata refresh (member
     * count, last message, unread state) can never wipe reconnect info.
     */
    private suspend fun upsertGroup(group: SavedGroup) {
        if (group.groupId in removedGroupIds) return
        val old = chatDao.getGroup(group.groupId)
        if (old == null) {
            chatDao.insertGroup(group)
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
                lastMessage = if (keepOldSummary) old.lastMessage else group.lastMessage,
                lastMessageTime = if (keepOldSummary) old.lastMessageTime else group.lastMessageTime
            )
        }
    }

    /** Rebuild a FileInfo from a persisted row. Every restored offer is shown
     *  as expired: the download address was captured in a previous session,
     *  and the sender's download server dies with its process — once EITHER
     *  side restarted, a stale address only produces a failed download. The
     *  bubble renders it as "已过期" instead of offering a download that can
     *  no longer succeed; the sender can re-share the file for a fresh
     *  address. */
    private fun restoredFileInfo(sm: SavedChatMessage): FileInfo? {
        if (sm.fileSize <= 0 && sm.downloadHost.isEmpty()) return null
        // blank the address for every restored offer, own or received
        return FileInfo(sm.id, sm.content, sm.fileSize, "", 0)
    }

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
                        .map { sg ->
                            GroupMeta(
                                groupId = sg.groupId,
                                groupName = sg.groupName,
                                isHost = sg.isHost,
                                hostIp = sg.hostIp,
                                hostPort = if (sg.isHost) currentPort else sg.hostPort,
                                memberCount = sg.memberCount,
                                lastMessage = sg.lastMessage,
                                lastMessageTime = sg.lastMessageTime
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
                val last = msgs.lastOrNull() ?: continue
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
                            ChatMessage(
                                id = sm.id,
                                content = sm.content,
                                timestamp = sm.timestamp,
                                senderId = sm.senderId,
                                senderName = sm.senderName,
                                isFromMe = sm.isFromMe,
                                fileInfo = restoredFileInfo(sm),
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
            val saved = chatDao.getMessagesForGroup(groupId).first()
            // a stale load (this p2p already replaced by a reconnect) must
            // neither publish history into the new instance's message list
            // nor mark replay done — otherwise an old connection's replay
            // finishing late can make the new connection treat persisted
            // messages as deleted and wipe them from the database
            if (groupP2pMap[groupId] !== p2p) return@launch
            persistedMessageIds[groupId] = saved.map { it.id }.toMutableSet()
            if (saved.isNotEmpty()) {
                val msgs = saved.map { sm ->
                    ChatMessage(
                        id = sm.id,
                        content = sm.content,
                        timestamp = sm.timestamp,
                        senderId = sm.senderId,
                        senderName = sm.senderName,
                        isFromMe = sm.isFromMe,
                        fileInfo = restoredFileInfo(sm)
                    )
                }
                p2p.replaySavedMessages(msgs)
            }
            if (groupP2pMap[groupId] === p2p) {
                replayDone[groupId] = p2p
            }
        }
        replayJobs[groupId] = job
    }

    fun createGroup(userName: String, groupName: String) {
        if (userName.isBlank() || groupName.isBlank()) return
        val name = groupName.trim()
        if (name.isBlank()) return
        val nick = userName.trim()
        ChatApp.saveNickname(getApplication(), nick)

        // Crypto-random group password (8 chars ≈ 47.6 bits): high enough
        // entropy that the PBKDF2-bound handshake cannot be brute-forced
        // offline from a recorded exchange.
        val password = Crypto.randomPassword(8)
        val p2p = P2PManager(getApplication(), port, hostServer)
        p2p.initializeAsHost(nick, name, password)
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
        }
    }

    /** Query a group by its numeric join id: the id is the join identifier
     *  (the group name is only a display label, learned from the host). */
    fun queryGroup(userName: String, groupId: String, hostIp: String, password: String? = null) {
        val id = groupId.trim().filter { it.isDigit() }
        val ip = hostIp.trim()
        if (id.isBlank() || ip.isBlank()) return
        val parsed = parseHostPort(ip)
        if (parsed.host.isBlank()) return
        val nick = userName.trim()
        ChatApp.saveNickname(getApplication(), nick)
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
                newP2p.initializeAsHost(nick, meta.groupName, password.ifBlank { null })
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

    fun sendMessage(content: String): Boolean {
        if (content.isBlank()) return false
        val gid = _activeGroupId.value ?: return false
        val p2p = groupP2pMap[gid] ?: return false
        // messages can go out over the host relay OR the group mesh (host
        // offline) — either path suffices
        if (!p2p.isConnected && !GroupMeshManager.hasLinks(gid)) return false
        val msg = p2p.sendMessage(content) ?: return false
        GroupMeshManager.broadcast(gid, msg)
        return true
    }

    fun sendMessageToGroup(groupId: String, content: String): Boolean {
        if (content.isBlank()) return false
        val p2p = groupP2pMap[groupId] ?: return false
        if (!p2p.isConnected && !GroupMeshManager.hasLinks(groupId)) return false
        val msg = p2p.sendMessage(content) ?: return false
        GroupMeshManager.broadcast(groupId, msg)
        return true
    }

    fun clearUnread(groupId: String) {
        _groups.update { list ->
            list.map { g ->
                if (g.groupId == groupId && g.unreadCount > 0) g.copy(unreadCount = 0) else g
            }
        }
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
                runCatching { withDbLock(gid) { chatDao.deleteMessage(gid, messageId) } }
            }
        }
    }

    /** Offer a file (from a content Uri) to the active group. */
    fun sendFile(uri: Uri, fileName: String, fileSize: Long): Boolean {
        if (fileName.isBlank()) return false
        val gid = _activeGroupId.value ?: return false
        val p2p = groupP2pMap[gid] ?: return false
        // Sending depends only on the SENDER being online: the host relay OR a
        // live mesh link is enough, so the host going offline never blocks it.
        if (!p2p.isConnected && !GroupMeshManager.hasLinks(gid)) return false
        val msg = p2p.sendFile(fileName, getApplication<Application>().contentResolver, uri, fileSize) ?: return false
        // p2p.sendFile relays the offer to the group when the host is up; the
        // mesh delivers it to every linked member either way (receivers dedup
        // by message id)
        GroupMeshManager.broadcast(gid, msg)
        return true
    }

    /** Download a file offer into [targetUri]; progress is surfaced via
     * [downloadStates] keyed by the file message id. */
    fun downloadFile(fileInfo: FileInfo, targetUri: Uri) {
        val gid = _activeGroupId.value ?: return
        val p2p = groupP2pMap[gid] ?: return
        val fileId = fileInfo.fileId
        _downloadStates.update { it + (fileId to DownloadState.Downloading) }
        viewModelScope.launch(Dispatchers.IO) {
            val resolver = getApplication<Application>().contentResolver
            val result = runCatching {
                val out = resolver.openOutputStream(targetUri, "w")
                    ?: error("无法打开输出流")
                out.use { p2p.downloadFile(fileInfo, it) }
            }.getOrElse { FileTransfer.DownloadResult(false, it.message ?: "未知错误") }
            if (!result.ok) {
                // drop the partially written file so a failed download does
                // not leave a corrupt copy behind
                runCatching { resolver.delete(targetUri, null, null) }
            }
            _downloadStates.update { map ->
                map + (fileId to if (result.ok)
                    DownloadState.Done(targetUri.toString())
                else
                    DownloadState.Failed(result.message))
            }
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

    /** Start a video call with a member of the active group. */
    fun startCall(peerId: String) {
        val gid = _activeGroupId.value ?: return
        val p2p = groupP2pMap[gid] ?: return
        if (p2p.connectionLost.value) return
        val peer = p2p.peers.value[peerId] ?: return
        CallManager.startCall(p2p, peer)
    }

    fun acceptCall() = CallManager.acceptCall()

    fun rejectCall() = CallManager.rejectCall()

    fun hangupCall() = CallManager.hangup()

    fun setCallAudioMuted(muted: Boolean) = CallManager.setAudioMuted(muted)

    fun setCallVideoMuted(muted: Boolean) = CallManager.setVideoMuted(muted)

    fun switchCallCamera() = CallManager.switchCamera()

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
                        notifyNewMessages(groupId, newMessages.first().senderName, incoming.last().content, incoming.size)
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
                                        content = msg.content,
                                        timestamp = msg.timestamp,
                                        senderId = msg.senderId,
                                        senderName = msg.senderName,
                                        isFromMe = msg.isFromMe,
                                        fileSize = msg.fileInfo?.fileSize ?: 0L,
                                        downloadHost = msg.fileInfo?.downloadHost ?: "",
                                        downloadPort = msg.fileInfo?.downloadPort ?: 0
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

    private fun notifyNewMessages(groupId: String, senderName: String, content: String, count: Int) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(getApplication(), android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        val context = getApplication<Application>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_MESSAGES,
                "新消息",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "收到新群聊消息时通知"
            }
            val nm = context.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(channel)
        }
        val tapIntent = Intent(context, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = android.app.PendingIntent.getActivity(
            context, 0, tapIntent,
            android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_MESSAGES)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle(if (count > 1) "$senderName 等 $count 条新消息" else "$senderName")
            .setContentText(content)
            .setStyle(NotificationCompat.BigTextStyle().bigText(content))
            .setAutoCancel(true)
            .setContentIntent(pendingIntent)
            .build()
        NotificationManagerCompat.from(context).notify(groupId.hashCode(), notification)
    }

    private fun cancelGroupNotification(groupId: String) {
        NotificationManagerCompat.from(getApplication()).cancel(groupId.hashCode())
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

    companion object {
        private const val CHANNEL_MESSAGES = "localchat_messages"
    }
}
