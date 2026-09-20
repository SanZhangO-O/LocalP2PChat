package com.zqr.localchat.network

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.util.Log
import com.zqr.localchat.ChatApp
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MAX_FOLDER_FILES
import com.zqr.localchat.data.Peer
import com.zqr.localchat.data.sanitizeRelativePath
import com.zqr.localchat.data.withSanitizedExtras
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlin.coroutines.ContinuationInterceptor
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.io.PrintWriter
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

object Constants {
    const val TCP_PORT = 9999
}

/** One IPv4 address of this device, labeled by its network interface name. */
data class LocalAddress(val interfaceName: String, val address: String)

/** Packet types that carry call signaling. */
val CALL_PACKET_TYPES = setOf(
    "call_offer", "call_answer", "call_reject", "call_hangup", "call_failed"
)

/** Group voice conference signaling (star topology: the meeting host mixes).
 *  Routed by the host like call packets (the packet actor must be the
 *  authenticated sender; delivered only to the addressed member), but handed
 *  to the separate groupCallSignalListener so the 1:1 call path is untouched.
 *  Old peers ignore these unknown types (both read loops are when-chains
 *  without else). Windows parity: GROUP_CALL_PACKET_TYPES. */
val GROUP_CALL_PACKET_TYPES = setOf(
    "group_call_invite", "group_call_join", "group_call_leave", "group_call_sync"
)

/**
 * Single TCP listener for the whole program.
 *
 * The app uses ONE port: every host group registers here and the shared
 * server dispatches incoming query/join connections to the group named in
 * the packet (query_group/join already carry the group name). Only one
 * ServerSocket exists, so multiple host groups stay reachable through the
 * same address. Member connections are then owned by their group's
 * P2PManager (per-group broadcast/heartbeats are unaffected).
 *
 * The listener also serves direct member chats: a "direct_hello" connection
 * is auto-accepted by [DirectChatManager] with no confirmation, so ANY device
 * running the app can be pulled into a 1:1 chat. For that reason the listener
 * keeps running even when no host group is registered (it is only stopped by
 * an explicit [shutdown]).
 */
class HostGroupServer(port: Int) {

    private var port = port
    private val groups = ConcurrentHashMap<String, P2PManager>()
    private val serverScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var serverJob: Job? = null
    private val lock = Any()
    /** Bumped on every start/stop so a stale runServer() coroutine (from a
     *  cancelled/restarted generation) can never bind or publish state. */
    private var generation = 0

    /** 并发处理中的入站连接计数（上限 MAX_ACTIVE_CONNECTIONS）：无上限时
     *  每个连接都要跑一次 PBKDF2 握手，单台局域网主机可仅凭握手流量打满
     *  手机 CPU（PBKDF2 CPU DoS）。 */
    private val activeConnections = AtomicInteger(0)

    /** 每源 IP 握手限流：HANDSHAKE_RATE_WINDOW_MS 内每 IP 最多
     *  HANDSHAKE_RATE_LIMIT 次握手尝试（Windows 端 _allow_handshake 的
     *  对等实现）。 */
    private val handshakeLimiter =
        HandshakeRateLimiter(HANDSHAKE_RATE_LIMIT, HANDSHAKE_RATE_WINDOW_MS)

    @Volatile
    private var serverSocket: ServerSocket? = null

    @Volatile
    var isRunning = false
        private set

    fun register(p2p: P2PManager) {
        val old = groups.put(p2p.currentGroupName, p2p)
        // A same-name re-host replaces the previous registration: stop the old
        // instance so its heartbeats and sockets do not leak. stop() unregisters
        // conditionally, so it cannot remove the fresh registration.
        if (old != null && old !== p2p) old.stop()
        ensureRunning()
    }

    fun unregister(p2p: P2PManager) {
        // conditional remove: an old same-name instance being stopped must not
        // unregister the replacement that is already registered
        groups.remove(p2p.currentGroupName, p2p)
        // keep listening: the shared port also serves direct member chats
    }

    fun restart(newPort: Int = port) {
        port = newPort
        stop()
        ensureRunning()
    }

    fun hasGroups(): Boolean = groups.isNotEmpty()

    /** Resolve a group by its numeric join id. */
    fun resolveGroup(idOrName: String?): P2PManager? {
        if (idOrName == null) return null
        for (p2p in groups.values) {
            if (p2p.numericGroupId == idOrName) return p2p
        }
        return null
    }

    fun stop() {
        synchronized(lock) {
            generation++
            serverJob?.cancel()
            serverJob = null
            runCatching { serverSocket?.close() }
            serverSocket = null
            isRunning = false
        }
    }

    /** Stop the listener for good (app teardown). */
    fun shutdown() = stop()

    /** Re-key a host group after the owner renamed it (group_update): the
     *  registration key is the display name, and a stale entry under the old
     *  name would keep resolving joins to a dead key. */
    fun renameRegistration(p2p: P2PManager, oldName: String) {
        synchronized(lock) {
            if (groups[oldName] === p2p) groups.remove(oldName)
            if (groups[p2p.currentGroupName] == null || groups[p2p.currentGroupName] === p2p) {
                groups[p2p.currentGroupName] = p2p
            }
        }
    }

    /** Make sure the shared port is being listened on (direct chats need it
     *  even on devices with no host group). */
    fun ensureRunning() {
        synchronized(lock) {
            // "isActive" (not just isRunning) guards the STARTING window: a
            // second caller while the first bind/retry is still in flight
            // would otherwise spawn a duplicate runServer() that fights for
            // the port, and a stop() that cancelled only the latest job would
            // leave the older one rebinding after shutdown.
            if (isRunning || serverJob?.isActive == true) return
            val gen = ++generation
            serverJob = serverScope.launch { runServer(gen) }
        }
    }

    /** Bind the shared port, retrying until it succeeds: the local-network
     *  permission may be granted AFTER the first attempt (Android 16+), or a
     *  conflicting socket may release the port — without the retry the device
     *  would silently stop being reachable for direct chats and joins. */
    private suspend fun CoroutineScope.runServer(gen: Int) {
        var srv: ServerSocket? = null
        var errorShown = false
        while (srv == null && isActive) {
            // a stale generation (stopped/restarted while we were retrying)
            // must never bind: it would resurrect the listener after shutdown
            if (gen != generation) return
            srv = try {
                ServerSocket(port).apply { reuseAddress = true }
            } catch (e: Exception) {
                if (!errorShown) {
                    Log.w(TAG, "failed to bind shared port $port, retrying", e)
                    setError("无法监听端口 $port，请检查端口是否被占用或网络权限")
                    errorShown = true
                }
                delay(3000)
                null
            }
        }
        if (!isActive || gen != generation) {
            runCatching { srv?.close() }
            return
        }
        val server = srv!!
        serverSocket = server
        isRunning = true
        setError(null)
        while (isActive) {
            val client = try {
                server.accept()
            } catch (e: CancellationException) {
                break
            } catch (e: Exception) {
                Log.w(TAG, "shared server accept failed", e)
                break
            }
            client.tcpNoDelay = true
            // 并发连接上限：超限直接关闭，不进入握手（每个握手都要跑
            // PBKDF2，无上限会被握手风暴耗尽 CPU）
            if (activeConnections.get() >= MAX_ACTIVE_CONNECTIONS) {
                Log.w(TAG, "active connection cap ($MAX_ACTIVE_CONNECTIONS) reached, dropping $client.remoteSocketAddress")
                closeSocket(client)
                continue
            }
            activeConnections.incrementAndGet()
            serverScope.launch {
                try {
                    handleIncoming(client)
                } finally {
                    activeConnections.decrementAndGet()
                }
            }
        }
        runCatching { server.close() }
        synchronized(lock) {
            if (serverSocket === server) serverSocket = null
        }
        isRunning = false
    }

    /**
     * Resolves the group password for an incoming handshake (set by the
     * ViewModel, which sees host groups, member groups and mesh groups).
     * Returns null when this device knows no such group for that mode, ""
     * for a known group without a password.
     */
    @Volatile
    var passwordLookup: ((mode: String, groupId: String?) -> String?)? = null

    /**
     * Optional handler for query/join packets that target a group this device
     * belongs to as a MEMBER (set by the ViewModel). Any member can be the
     * join entry point, not just the creator: the handler validates the
     * numeric id + password, answers group_info / join_ack (with the member
     * list and the host's address) and announces the newcomer over the mesh.
     * Returns true when the packet was handled. The wire is already secured
     * (password verified during the handshake).
     */
    @Volatile
    var memberGroupHandler: ((NetworkPacket, Socket, Wire) -> Boolean)? = null

    private suspend fun handleIncoming(socket: Socket) {
        try {
            // 每源 IP 握手限流：必须放在读 hs_start、跑 PBKDF2 之前，超限
            // 的连接在读任何数据前就被关闭
            val sourceIp = socket.inetAddress?.hostAddress ?: "unknown"
            if (!handshakeLimiter.allow(sourceIp)) {
                closeSocket(socket)
                return
            }
            socket.soTimeout = 15000
            val reader = BufferedReader(InputStreamReader(socket.getInputStream()))
            val writer = PrintWriter(socket.getOutputStream(), true)
            val firstLine = P2PManager.readLineLimited(reader) ?: run { closeSocket(socket); return }
            val start = runCatching { json.decodeFromString<NetworkPacket>(firstLine) }
                .getOrNull() ?: run { closeSocket(socket); return }
            // Only the secured handshake is accepted — a legacy plaintext
            // query_group/join/direct_hello/mesh_hello must never work again.
            if (start.type != Protocol.HS_START) {
                closeSocket(socket)
                return
            }
            val wire = Wire(BufferedReaderLineIn(reader), writer)
            val mode = start.hsMode
            when (mode) {
                Protocol.MODE_DIRECT -> {
                    val secured = Handshake.acceptDirect(wire, start, null) ?: run { closeSocket(socket); return }
                    val hello = wire.recvPacket() ?: run { closeSocket(socket); return }
                    if (hello.type != Protocol.DIRECT_HELLO) {
                        closeSocket(socket)
                        return
                    }
                    DirectChatManager.handleDirectHello(socket, wire, hello, secured.peerIdent)
                }
                Protocol.MODE_MESH -> {
                    val lookup = passwordLookup ?: run { closeSocket(socket); return }
                    val secured = Handshake.accept(wire, start, lookup) ?: run { closeSocket(socket); return }
                    val hello = wire.recvPacket() ?: run { closeSocket(socket); return }
                    if (hello.type != "mesh_hello") {
                        closeSocket(socket)
                        return
                    }
                    // 密码是为 start.groupId 验证的，hello 不得指向其他群：
                    // 否则可用 A 群密码读取 B 群的 mesh 状态与全部历史
                    if (hello.groupId != start.groupId) {
                        closeSocket(socket)
                        return
                    }
                    GroupMeshManager.handleMeshHello(socket, wire, hello)
                }
                Protocol.MODE_QUERY, Protocol.MODE_JOIN -> {
                    val p2p = start.groupId?.let { resolveGroup(it) }
                    val lookup = passwordLookup
                    val secured = if (lookup != null) {
                        Handshake.accept(wire, start, lookup) ?: run { closeSocket(socket); return }
                    } else {
                        closeSocket(socket); return
                    }
                    val packet = wire.recvPacket() ?: run { closeSocket(socket); return }
                    if (packet.type != mode) {
                        closeSocket(socket)
                        return
                    }
                    // 内层 packet 必须指向握手认证的同一个群：密码是为
                    // start.groupId 验证的，放行其他 id 会让 A 群成员借
                    // 成员赞助路径探测 B 群的成员列表/主机地址
                    if (packet.groupId != start.groupId) {
                        closeSocket(socket)
                        return
                    }
                    if (p2p == null) {
                        val handled = memberGroupHandler?.invoke(packet, socket, wire) ?: false
                        if (!handled) {
                            runCatching { wire.sendPacket(NetworkPacket(type = "join_rejected")) }
                            closeSocket(socket)
                        }
                    } else if (mode == Protocol.MODE_QUERY) {
                        p2p.handleQueryGroup(socket, wire, packet)
                    } else {
                        // the group's P2PManager takes over the socket (join_ack,
                        // member registration, then its read loop)
                        p2p.handleJoin(socket, wire, packet)
                    }
                }
                else -> closeSocket(socket)
            }
        } catch (e: Exception) {
            Log.w(TAG, "shared server handle failed", e)
            closeSocket(socket)
        }
    }

    /** Publish the bind state to every registered host group so the lobby can
     * show (and clear) the error. A fresh successful bind is silent. */
    private fun setError(message: String?) {
        val wasError = groups.values.firstOrNull { it.serverError.value != null } != null
        groups.values.forEach { p2p ->
            p2p.setHostServerError(message)
            if (message == null && !wasError) return@forEach
            p2p.serverErrorNotify?.invoke(message)
        }
    }

    companion object {
        private const val TAG = "HostGroupServer"
        private val json = Json { ignoreUnknownKeys = true }

        /** 并发处理中的入站连接上限：手机端低于 Windows 的 256——一个
         *  群聊的成员规模远小于该值，纯握手风暴则被挡在外面。 */
        private const val MAX_ACTIVE_CONNECTIONS = 64

        /** 每源 IP 握手限流参数：60 秒窗口内最多 60 次握手尝试，正常
         *  群聊流量远低于该限制（与 Windows 端一致）。 */
        private const val HANDSHAKE_RATE_LIMIT = 60
        private const val HANDSHAKE_RATE_WINDOW_MS = 60_000L

        private fun closeSocket(socket: Socket?) {
            socket ?: return
            runCatching { socket.close() }
        }
    }
}

/**
 * 每源 IP 的滑动窗口握手限流器：[windowMs] 时间窗内每 IP 最多 [limit]
 * 次握手尝试。超限的连接在读 hs_start、跑 PBKDF2 之前就被关闭，防止单
 * 台局域网主机用无休止的握手耗尽 CPU（PBKDF2 CPU DoS）。已跟踪 IP 数
 * 达到上限时先清理整个窗口都已过期的 key，保证 IP 表不会无界增长。
 *
 * 时钟用 System.nanoTime()（单调）：currentTimeMillis 是墙钟，NTP 校时
 * 回跳会让窗口永久卡死、前跳则让限流瞬间失效。
 */
internal class HandshakeRateLimiter(
    private val limit: Int,
    private val windowMs: Long,
    private val clock: () -> Long = System::nanoTime
) {
    private val attempts = HashMap<String, MutableList<Long>>()

    /** 最多跟踪的源 IP 数：正常局域网远达不到；达到后先清理过期 key。 */
    private val maxTrackedKeys = 1024

    @Synchronized
    fun allow(ip: String): Boolean {
        val now = clock()
        // 清理整个窗口都已过期的 key，防止 IP 集合无界增长
        if (attempts.size >= maxTrackedKeys) {
            val iter = attempts.entries.iterator()
            while (iter.hasNext()) {
                val entry = iter.next()
                if (entry.value.all { ts -> now - ts >= windowMs }) iter.remove()
            }
        }
        val list = attempts[ip]
        if (list == null) {
            if (attempts.size >= maxTrackedKeys) return false
            attempts[ip] = arrayListOf(now)
            return true
        }
        list.removeAll { ts -> now - ts >= windowMs }
        if (list.size >= limit) return false
        list.add(now)
        return true
    }
}

class P2PManager(
    private val context: Context,
    private val port: Int = Constants.TCP_PORT,
    private val hostServer: HostGroupServer
) {

    private val json = Json { ignoreUnknownKeys = true }

    private val _peers = MutableStateFlow<Map<String, Peer>>(emptyMap())
    val peers: StateFlow<Map<String, Peer>> = _peers.asStateFlow()

    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages.asStateFlow()

    private val _connectionResult = MutableStateFlow<ConnectionResult?>(null)
    val connectionResult: StateFlow<ConnectionResult?> = _connectionResult.asStateFlow()

    private val _serverError = MutableStateFlow<String?>(null)
    val serverError: StateFlow<String?> = _serverError.asStateFlow()

    private val _connectionLost = MutableStateFlow(false)
    val connectionLost: StateFlow<Boolean> = _connectionLost.asStateFlow()

    private val _isJoining = MutableStateFlow(false)
    val isJoining: StateFlow<Boolean> = _isJoining.asStateFlow()

    private val _queriedGroupInfo = MutableStateFlow<GroupInfo?>(null)
    val queriedGroupInfo: StateFlow<GroupInfo?> = _queriedGroupInfo.asStateFlow()

    private val _queryError = MutableStateFlow<String?>(null)
    val queryError: StateFlow<String?> = _queryError.asStateFlow()

    private val _isQuerying = MutableStateFlow(false)
    val isQuerying: StateFlow<Boolean> = _isQuerying.asStateFlow()

    private var myId: String = ChatApp.savedDeviceId(context)
    private var myName: String = ""
    private var myIpAddress: String = ""
    private var groupName: String = ""
    private var groupId: String = ""
    private var groupPassword: String = ""
    /** The numeric group ID used as the join identifier (typed by members;
     *  computed by the host from its machine fingerprint + group name). */
    private var joinId: String = ""
    private var isHost: Boolean = false

    /** Owner-published announcement (group_update), mirrored by members and
     *  persisted by the ViewModel. */
    @Volatile
    private var groupAnnouncement: String = ""

    /** Invoked after a host-side management action (group_update / kick_member)
     *  so the ViewModel can refresh and persist the group metadata. */
    @Volatile
    var adminNotify: (() -> Unit)? = null

    /** Invoked when the owner kicked this device out: the ViewModel tears every
     *  connection down, hides the group and tells the user (exactly once). */
    @Volatile
    var kickedNotify: (() -> Unit)? = null

    /** Once this device was kicked the teardown must run exactly once even
     *  when the packet arrives over both the relay and the mesh. */
    @Volatile
    private var kickedFromGroup = false

    /** One connected group member on the host side: its socket plus the
     *  per-connection encrypted wire (each join negotiated its own key). */
    private class MemberConn(val socket: Socket, val wire: Wire)

    private val connectedClients = ConcurrentHashMap<String, MemberConn>()
    @Volatile
    private var hostConnection: Socket? = null
    /** Encrypted wire of the client->host relay connection. */
    @Volatile
    private var hostWire: Wire? = null

    /**
     * The group's real host (creator) address, learned when joining through a
     * member sponsor (the sponsor's join_ack reveals it). The ViewModel
     * persists this as the group's address so a later rejoin connects to the
     * HOST — otherwise the group would be saved with the sponsor's address and
     * show "connection failed" whenever that one member is offline, even
     * though the host is up.
     */
    @Volatile
    var connectedHost: Peer? = null
        private set

    /** Outbound file servers keyed by fileId; each offers one file until stop(). */
    private val fileServers = ConcurrentHashMap<String, ServerSocket>()

    /**
     * Optional call-signaling listener: invoked on the network thread for
     * call_* packets addressed to this node. The ViewModel wires it to the
     * global CallManager with this P2PManager captured in the closure.
     */
    @Volatile
    var callSignalListener: ((NetworkPacket) -> Unit)? = null

    /**
     * Group voice conference signaling listener: invoked on the network
     * thread for group_call_* packets addressed to this node (the routing
     * already checked the addressing). The ViewModel wires it to the global
     * GroupCallManager with this P2PManager captured in the closure.
     */
    @Volatile
    var groupCallSignalListener: ((NetworkPacket) -> Unit)? = null

    /** A member's typing indicator changed (host relay path). Advisory: the
     *  ViewModel also expires an indicator that received no refresh. Invoked
     *  on the read-loop thread. */
    @Volatile
    var typingListener: ((senderId: String, active: Boolean) -> Unit)? = null

    /** Message-experience callbacks (edit / reaction / pin / group read
     *  receipt) relayed or mesh-delivered; set by the ViewModel. */
    var editListener: ((messageId: String, newContent: String, senderId: String) -> Unit)? = null
    var reactionListener: ((messageId: String, emoji: String, senderId: String, active: Boolean) -> Unit)? = null
    var pinListener: ((messageId: String, senderId: String, active: Boolean) -> Unit)? = null
    var groupReceiptListener: ((readerId: String, upToId: String) -> Unit)? = null

    /**
     * Invoked by the shared HostGroupServer when the program-wide listener
     * bind state changes (error message, or null when it recovers).
     */
    @Volatile
    var serverErrorNotify: ((String?) -> Unit)? = null

    /** Called by the shared HostGroupServer to publish its bind state. */
    fun setHostServerError(message: String?) {
        _serverError.value = message
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    /**
     * Single-threaded scope for UI-originated outbound packets (chat/delete).
     * Dispatchers.IO is multi-threaded, so two launches there are unordered:
     * a delete sent right after a send could reach the peer before the chat
     * packet, leaving the peer with a message the sender already removed.
     * Serializing these sends on one thread preserves submission order.
     */
    private val sendScope = CoroutineScope(newSingleThreadContext("LocalChat-sender") + SupervisorJob())

    /** Stable per-device fingerprint (persisted fallback when ANDROID_ID is
     *  unavailable), used for the group id and the numeric join id. */
    val hardwareId: String
        get() = ChatApp.savedFingerprint(context)

    sealed class ConnectionResult {
        data object Success : ConnectionResult()
        data class Error(val message: String) : ConnectionResult()
    }

    fun initializeAsHost(userName: String, group: String, password: String? = null, groupId: String? = null) {
        myName = userName.trim()
        groupName = group.trim()
        // a re-host keeps the ORIGINAL group id (a rename changed only the
        // display name; the id keys history + password)
        this.groupId = groupId ?: "${groupName}@${hardwareId}"
        myIpAddress = getLocalIpAddress()
        if (password != null) groupPassword = password
    }

    fun initializeAsClient(userName: String, group: String, password: String? = null) {
        myName = userName.trim()
        groupName = group.trim()
        myIpAddress = getLocalIpAddress()
        if (password != null) groupPassword = password
    }

    /** The numeric ID members type to join this group (host side). */
    fun setJoinId(id: String) {
        joinId = id
    }

    val joinIdValue: String
        get() = joinId

    /** Stable 8-digit group ID derived from the machine fingerprint and the
     *  group name — the join identifier, separate from the display name. A
     *  join id set with [setJoinId] wins: the owner may RENAME the group
     *  (group_update) and the numeric id must stay the same, or every
     *  member's saved join id would stop matching. */
    val numericGroupId: String
        get() = joinId.ifBlank { numericGroupIdOf(groupName, hardwareId) }

    val currentGroupPassword: String get() = groupPassword

    val currentGroupAnnouncement: String get() = groupAnnouncement

    /** Owner-only: publish a new display name and/or announcement. Applies
     *  locally, re-registers the renamed host group, broadcasts a group_update
     *  and notifies the ViewModel to persist. */
    fun sendGroupUpdate(newName: String?, announcement: String?): Boolean {
        if (!isHost) return false
        val name = newName?.trim().orEmpty()
        if (name.isEmpty() && announcement == null) return false
        val oldName = groupName
        if (name.isNotEmpty()) groupName = name
        if (announcement != null) groupAnnouncement = announcement
        if (groupName != oldName) hostServer.renameRegistration(this, oldName)
        val packet = GroupAuth.signPacket(
            NetworkPacket(
                type = "group_update",
                groupId = groupId,
                senderId = myId,
                groupName = name.ifEmpty { null },
                announcement = announcement
            ),
            GroupAuth.groupUpdateParts(groupId, myId, name.ifEmpty { null }, announcement)
        )
        sendScope.launch {
            try {
                broadcastToClients(packet)
            } catch (e: Exception) {
                Log.w(TAG, "sendGroupUpdate failed", e)
            }
        }
        adminNotify?.invoke()
        return true
    }

    /** Owner-only: remove a member. The target gets a directed kick_member
     *  (then its connection is closed); everyone else gets the broadcast so it
     *  drops the target. An offline member (known but not connected) is kicked
     *  too — only the broadcast goes out — matching the Windows behavior.
     *  Returns false when the target is unknown. */
    fun kickMember(targetId: String): Boolean {
        if (!isHost || targetId.isBlank()) return false
        val known = targetId in _peers.value
        val conn = connectedClients.remove(targetId)
        if (!known && conn == null) return false
        _peers.update { it - targetId }
        val packet = GroupAuth.signPacket(
            NetworkPacket(
                type = "kick_member",
                groupId = groupId,
                senderId = myId,
                targetId = targetId
            ),
            GroupAuth.kickParts(groupId, myId, targetId)
        )
        sendScope.launch {
            // the broadcast must not depend on the directed send: a target
            // that dropped mid-kick would otherwise keep the kicked peer in
            // every other member's list/mesh
            if (conn != null) {
                try {
                    conn.wire.sendPacket(packet)
                } catch (e: Exception) {
                    Log.w(TAG, "kickMember directed send failed", e)
                }
            }
            broadcastToClients(packet, exclude = targetId)
            if (conn != null) {
                // give the directed packet a moment to flush, then detach
                delay(500)
                runCatching { conn.socket.close() }
            }
        }
        adminNotify?.invoke()
        return true
    }

    /** The group creator (owner) device id: the host is the creator, a member
     *  learned it when joining. Empty means unknown -> owner packets are
     *  refused (fail-closed). */
    private fun creatorId(): String {
        val gid = groupId
        if (gid.isEmpty()) return ""
        if (isHost) return myId
        return ChatApp.savedGroupCreatorId(context, gid)
    }

    val currentPort: Int get() = port

    fun startAsHost() {
        isHost = true
        // program-wide single-port server takes over listening
        hostServer.register(this)
        startHostHeartbeat()
    }

    fun stop() {
        hostServer.unregister(this)
        fileServers.values.forEach { runCatching { it.close() } }
        fileServers.clear()
        runCatching { hostConnection?.close() }
        hostConnection = null
        hostWire = null
        connectedHost = null
        connectedClients.values.forEach { closeSocket(it.socket) }
        connectedClients.clear()
        scope.cancel()
        sendScope.cancel()
        // cancel() only stops sendScope's coroutines; the executor backing
        // newSingleThreadContext keeps its thread alive until closed, so every
        // create/join/leave cycle would otherwise leak one thread per
        // P2PManager. Close it after the sends are cancelled (runCatching: a
        // close() failure must never crash teardown of the whole app).
        runCatching {
            (sendScope.coroutineContext[ContinuationInterceptor]
                as? kotlinx.coroutines.ExecutorCoroutineDispatcher)?.close()
        }
        isHost = false
        _connectionLost.value = false
        _isJoining.value = false
    }

    fun queryGroup(targetIp: String, targetPort: Int = Constants.TCP_PORT) {
        scope.launch {
            _isQuerying.value = true
            var socket: Socket? = null
            try {
                val s = Socket()
                socket = s
                s.soTimeout = 15000
                s.connect(InetSocketAddress(targetIp, targetPort), 5000)
                s.tcpNoDelay = true
                // Secured handshake (password-bound when a password was typed):
                // the group_info response is never plaintext on the wire.
                val wire = Wire(
                    BufferedReaderLineIn(BufferedReader(InputStreamReader(s.getInputStream()))),
                    PrintWriter(s.getOutputStream(), true)
                )
                Handshake.initiate(wire, Protocol.MODE_QUERY, joinId, groupPassword)
                wire.sendPacket(NetworkPacket(type = Protocol.MODE_QUERY, groupId = joinId))
                val response = wire.recvPacket()
                when {
                    response == null -> _queryError.value = "无响应"
                    else -> {
                        when {
                            response.type == "group_info" && response.groupInfo != null -> {
                                _queriedGroupInfo.value = response.groupInfo
                                // the display name comes from the host; the
                                // numeric id is the join identifier
                                groupName = response.groupInfo.groupName
                            }
                            response.type == "join_rejected" ->
                                _queryError.value = "该设备不存在此群组"
                            else -> _queryError.value = "未知的响应"
                        }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "queryGroup failed", e)
                _queryError.value = "查询失败: ${e.message}"
            } finally {
                closeSocket(socket)
                _isQuerying.value = false
            }
        }
    }

    fun clearQueryState() {
        _queriedGroupInfo.value = null
        _queryError.value = null
    }

    fun confirmJoin(targetIp: String, targetPort: Int = Constants.TCP_PORT) {
        if (_isJoining.value) return
        _isJoining.value = true
        _connectionLost.value = false
        scope.launch {
            var socket: Socket? = null
            try {
                val s = Socket()
                socket = s
                s.soTimeout = 15000
                s.connect(InetSocketAddress(targetIp, targetPort), 5000)
                s.tcpNoDelay = true
                // password-bound secured handshake: the join (and everything
                // after it) is encrypted and the host is authenticated by its
                // knowledge of the group password
                val wire = Wire(
                    BufferedReaderLineIn(BufferedReader(InputStreamReader(s.getInputStream()))),
                    PrintWriter(s.getOutputStream(), true)
                )
                Handshake.initiate(wire, Protocol.MODE_JOIN, joinId, groupPassword)
                val myPeer = Peer(id = myId, name = myName, ipAddress = myIpAddress, port = port)
                wire.sendPacket(
                    // no password field: the password-bound handshake already
                    // authenticated the joiner — it never appears in a packet
                    NetworkPacket(
                        type = Protocol.MODE_JOIN,
                        groupId = joinId,
                        peer = myPeer
                    )
                )
                val response = wire.recvPacket()
                when {
                    response == null -> {
                        _connectionResult.value = ConnectionResult.Error("连接被关闭")
                    }
                    else -> {
                        when {
                            response.type == "join_ack" && response.members != null -> {
                                groupId = response.groupId ?: groupId
                                response.announcement?.let { groupAnnouncement = it }
                                response.members.forEach { peer ->
                                    if (peer.id != myId) {
                                        _peers.update { it + (peer.id to peer) }
                                    }
                                }
                                applyPeerDeletedIds(groupId, response.deletedIds)
                                val host = response.host
                                if (host != null && (host.ipAddress != targetIp || host.port != targetPort)) {
                                    // joined through a member sponsor: the ack
                                    // reveals the host, so complete the join by
                                    // connecting to the host for the relay path
                                    // (best effort — mesh works without it)
                                    // Record the REAL host address synchronously
                                    // (before Success) so the ViewModel can
                                    // persist the right rejoin address.
                                    connectedHost = host
                                    _isJoining.value = false
                                    _connectionResult.value = ConnectionResult.Success
                                    // the sponsor socket only served the join
                                    // ack; it is not the relay path, so close it
                                    // (a leak otherwise) and let connectToHost
                                    // establish the real host link.
                                    socket?.let { closeSocket(it) }
                                    socket = null
                                    connectToHost(host)
                                } else {
                                    _isJoining.value = false
                                    _connectionResult.value = ConnectionResult.Success
                                    // this socket IS the host relay: hand it to
                                    // the heartbeat + read loop, transfer
                                    // ownership, and never touch it in finally
                                    socket = null
                                    hostConnection = s
                                    hostWire = wire
                                    startClientHeartbeat()
                                    readLoopFromHost(s, wire)
                                }
                            }
                            response.type == "join_rejected" -> {
                                _connectionResult.value = ConnectionResult.Error("群组不匹配，连接被拒绝")
                            }
                            response.type == "error" -> {
                                _connectionResult.value = ConnectionResult.Error(
                                    response.errorMessage ?: "加入被拒绝"
                                )
                            }
                            else -> {
                                _connectionResult.value = ConnectionResult.Error("未知的响应")
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "confirmJoin failed", e)
                _connectionResult.value = ConnectionResult.Error("连接失败: ${e.message}")
            } finally {
                // Only clean up sockets we still own: after a successful
                // hand-off (host relay or sponsor->connectToHost) the local
                // socket is null, so a concurrently-established hostConnection
                // can never be clobbered by this finally.
                socket?.let { closeSocket(it) }
                socket = null
                _isJoining.value = false
            }
        }
    }

    fun clearConnectionResult() {
        _connectionResult.value = null
    }

    /** Join the group's HOST after a member-sponsored join revealed its
     *  address: establishes the standard host relay path. Best effort — when
     *  the host is unreachable the member stays mesh-only. */
    private fun connectToHost(host: Peer) {
        scope.launch {
            var socket: Socket? = null
            try {
                val s = Socket()
                socket = s
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(host.ipAddress, host.port), 5000)
                s.soTimeout = 15000
                val wire = Wire(
                    BufferedReaderLineIn(BufferedReader(InputStreamReader(s.getInputStream()))),
                    PrintWriter(s.getOutputStream(), true)
                )
                Handshake.initiate(wire, Protocol.MODE_JOIN, joinId, groupPassword)
                val myPeer = Peer(id = myId, name = myName, ipAddress = myIpAddress, port = port)
                wire.sendPacket(
                    // no password field: the password-bound handshake already
                    // authenticated the joiner — it never appears in a packet
                    NetworkPacket(
                        type = Protocol.MODE_JOIN,
                        groupId = joinId,
                        peer = myPeer
                    )
                )
                val response = wire.recvPacket() ?: throw IllegalStateException("no ack")
                if (response.type == "join_ack") {
                    response.announcement?.let { groupAnnouncement = it }
                    response.members?.forEach { peer ->
                        if (peer.id != myId) {
                            _peers.update { it + (peer.id to peer) }
                        }
                    }
                    applyPeerDeletedIds(groupId, response.deletedIds)
                    socket = null
                    hostConnection = s
                    hostWire = wire
                    startClientHeartbeat()
                    readLoopFromHost(s, wire)
                } else {
                    throw IllegalStateException("host rejected join")
                }
            } catch (e: Exception) {
                Log.w(TAG, "host connect after sponsor join failed", e)
                closeSocket(socket)
                hostConnection = null
                hostWire = null
            }
        }
    }

    /** Host-side heartbeat: pings every client so members can detect a dead
     *  host and so member read loops keep receiving traffic. */
    private fun startHostHeartbeat() {
        scope.launch {
            while (isActive) {
                delay(HEARTBEAT_INTERVAL_MS)
                broadcastToClients(NetworkPacket(type = "ping"))
            }
        }
    }

    /** Client-side heartbeat: pings the host so the host can detect a dead
     *  member and the client read loop keeps receiving traffic. */
    private fun startClientHeartbeat() {
        scope.launch {
            while (isActive) {
                delay(HEARTBEAT_INTERVAL_MS)
                val wire = hostWire
                if (wire != null) {
                    runCatching { wire.sendPacket(NetworkPacket(type = "ping")) }
                }
            }
        }
    }

    /** True when [idOrName] is this group's numeric join id. */
    fun matchesGroupId(idOrName: String): Boolean =
        idOrName == numericGroupId

    internal fun handleQueryGroup(socket: Socket, wire: Wire, packet: NetworkPacket) {
        try {
            if (packet.groupId.isNullOrBlank() || !matchesGroupId(packet.groupId)) {
                wire.sendPacket(NetworkPacket(type = "join_rejected"))
            } else {
                val info = GroupInfo(
                    groupName = groupName,
                    creatorName = myName,
                    creatorId = myId,
                    memberCount = _peers.value.size + 1
                )
                wire.sendPacket(NetworkPacket(type = "group_info", groupInfo = info))
            }
        } finally {
            closeSocket(socket)
        }
    }

    internal suspend fun handleJoin(socket: Socket, wire: Wire, packet: NetworkPacket) {
        // The handshake already verified the group password (password-bound
        // ECDH); only the packet shape is validated here.
        if (packet.groupId.isNullOrBlank() || !matchesGroupId(packet.groupId) || packet.peer == null) {
            wire.sendPacket(NetworkPacket(type = "join_rejected"))
            closeSocket(socket)
            return
        }
        val newPeer = packet.peer
        // 拒绝身份无效的加入：空 id 无法寻址/去重，伪造宿主自己的 myId
        // 会被成员当作宿主本人（组内身份冒充）。同 id 重连替换旧连接的
        // 重连语义不受影响（有效的同 id 仍走下面的替换逻辑）。
        if (newPeer.id.isBlank() || newPeer.id == myId) {
            wire.sendPacket(NetworkPacket(type = "join_rejected"))
            closeSocket(socket)
            return
        }
        _peers.update { it + (newPeer.id to newPeer) }
        // A rejoin with the same stable peer id replaces the old connection: the
        // stale connection would otherwise keep a live input channel for that
        // identity (duplicate messages, forged packets, or its read-loop
        // finally broadcasting peer_left for a member that just rejoined).
        val conn = MemberConn(socket, wire)
        val previous = connectedClients.put(newPeer.id, conn)
        if (previous != null && previous !== conn) {
            runCatching { previous.socket.close() }
        }
        val allMembers = listOf(Peer(myId, myName, myIpAddress, port)) +
                _peers.value.values.filter { it.id != myId && it.id != newPeer.id }
        // offline-member delete convergence: the ack carries this group's
        // tombstoned ids so a member that was offline drops what it missed
        // (null omitted on the wire; empty list must not serialize either)
        val ack = NetworkPacket(
            type = "join_ack",
            groupId = groupId,
            members = allMembers,
            // the owner's current announcement so a newcomer sees it at once
            // (omitted when empty, matching kotlinx.serialization defaults)
            announcement = groupAnnouncement.ifEmpty { null },
            deletedIds = deletedIdsProvider?.invoke(groupId).orEmpty().ifEmpty { null }
        )
        wire.sendPacket(ack)
        val announcement = NetworkPacket(type = "announce", peer = newPeer)
        broadcastToClients(announcement, exclude = newPeer.id)
        socket.soTimeout = 0
        readLoopFromClient(conn, newPeer.id)
    }

    private fun readLoopFromHost(socket: Socket, wire: Wire) {
        try {
            // no traffic for HEARTBEAT_TIMEOUT_MS means the host is gone
            // (half-open connection); the peer's pings keep this from firing
            socket.soTimeout = HEARTBEAT_TIMEOUT_MS
            while (true) {
                val packet = wire.recvPacket() ?: break
                try {
                    processPacketAsClient(packet)
                } catch (e: Exception) {
                    Log.w(TAG, "bad packet from host", e)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "host read loop ended", e)
        } finally {
            closeSocket(socket)
            hostConnection = null
            hostWire = null
            // Order matters: the ViewModel's peers collector keys its
            // "keep last-known members" guard off connectionLost, so the flag
            // must be set BEFORE the peer map is cleared — otherwise a
            // collector that runs between the two updates would see an empty
            // map with lost=false and tear down the mesh + persisted peers.
            _connectionLost.value = true
            _peers.update { emptyMap() }
        }
    }

    private fun readLoopFromClient(conn: MemberConn, peerId: String) {
        try {
            // no traffic for HEARTBEAT_TIMEOUT_MS means this member is gone
            // (half-open connection); the member's pings keep this from firing
            conn.socket.soTimeout = HEARTBEAT_TIMEOUT_MS
            while (true) {
                val packet = conn.wire.recvPacket() ?: break
                try {
                    processPacketFromClient(packet, peerId)
                } catch (e: Exception) {
                    Log.w(TAG, "bad packet from client $peerId", e)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "client read loop ended: $peerId", e)
        } finally {
            closeSocket(conn.socket)
            // Only clean up when this connection is still the registered one: a
            // rejoin with the same stable peer id may have replaced it with a
            // fresh socket, and this old loop's cleanup must not remove the
            // just-rejoined member.
            if (connectedClients.remove(peerId, conn)) {
                _peers.update { it - peerId }
                broadcastPeerLeft(peerId)
            }
        }
    }

    private fun processPacketAsClient(packet: NetworkPacket) {
        when (packet.type) {
            "chat", "file_message" -> packet.message?.let { msg ->
                // author identity gate (TOFU) BEFORE the message reaches the
                // list (LESSONS 2026-09-19 #3: gate listener/persistence)
                if (!GroupAuth.verifyMessage(groupId, msg)) return
                // idempotent insert: a member's message reaches us over the
                // host relay AND over the mesh (whoever arrives first wins),
                // so a plain append would show duplicate bubbles
                _messages.update { list ->
                    if (list.any { it.id == msg.id }) list
                    else list + markFromMe(msg.withSanitizedExtras(), myId)
                }
            }
            "announce" -> packet.peer?.let { peer ->
                if (peer.id != myId) _peers.update { it + (peer.id to peer) }
            }
            "peer_left" -> packet.peer?.id?.let { peerId ->
                _peers.update { it - peerId }
            }
            "group_update" -> handleGroupUpdateAsClient(packet)
            "kick_member" -> handleKickAsClient(packet)
            "delete_message" -> {
                val id = packet.messageId
                val sender = packet.senderId
                if (id == null || sender == null) {
                    Log.w(TAG, "drop delete_message from host: messageId=$id packet senderId=$sender")
                    return
                }
                val target = _messages.value.firstOrNull { it.id == id } ?: return
                if (target.senderId != sender) {
                    Log.w(TAG, "reject delete_message $id from $sender: message senderId=${target.senderId}")
                    return
                }
                if (!GroupAuth.verifyDelete(groupId, sender, id, packet.senderPubId, packet.senderSig)) return
                _messages.update { list -> list.filterNot { it.id == id } }
            }
            "ping" -> {
                // answer so the host knows we are still here
                val wire = hostWire
                if (wire != null) {
                    runCatching { wire.sendPacket(NetworkPacket(type = "pong")) }
                }
            }
            "typing" -> {
                // advisory typing indicator for a member, attributed by the
                // host relay (the host validated the sender before forwarding)
                val sender = packet.senderId
                val active = packet.active
                if (sender != null && active != null && sender != myId) {
                    typingListener?.invoke(sender, active)
                }
            }
            "edit_message" -> applyRelayedEdit(packet)
            "reaction" -> applyRelayedReaction(packet)
            "pin_message" -> applyRelayedPin(packet)
            "read_receipt" -> {
                // group-scope read receipt relayed by the host; the reader id
                // was validated by the host against the member's connection
                val upTo = packet.upToId
                val reader = packet.readerId
                if (upTo != null && reader != null && packet.groupId == groupId) {
                    groupReceiptListener?.invoke(reader, upTo)
                }
            }
            "pong" -> { /* traffic only; keeps the read loop alive */ }
            in CALL_PACKET_TYPES -> {
                // Call packets are never broadcast on the relay path: a client
                // only accepts packets explicitly addressed to this node and
                // whose participant fields are consistent with its role
                // (parity with the Windows P2PManager checks).
                val call = packet.call ?: return
                if (packet.targetId != myId) return
                when (packet.type) {
                    "call_offer" -> if (call.calleeId != myId) return
                    "call_answer", "call_reject", "call_failed" ->
                        if (call.callerId != myId) return
                    "call_hangup" ->
                        if (call.callerId != myId && call.calleeId != myId) return
                }
                callSignalListener?.invoke(packet)
            }
            in GROUP_CALL_PACKET_TYPES -> {
                // Group voice conference signaling relayed by the host: only
                // packets explicitly addressed to this node whose declared
                // parties include this node reach the listener (semantic
                // checks — group id / meeting id / host identity — belong to
                // the GroupCallManager). Windows parity: processPacketAsClient.
                val call = packet.call ?: return
                if (packet.targetId != myId) return
                if (call.calleeId != myId && call.callerId != myId) return
                groupCallSignalListener?.invoke(packet)
            }
        }
    }

    /** A relayed owner packet whose senderId is not the creator (or an unknown
     *  creator) is ignored and the host connection dropped (fail-closed). A
     *  signed owner packet must additionally verify against the creatorId's
     *  remembered device key (TOFU). */
    private fun handleGroupUpdateAsClient(packet: NetworkPacket) {
        val creator = creatorId()
        if (creator.isEmpty() || packet.senderId != creator) {
            Log.w(TAG, "reject group_update from ${packet.senderId}: not the group owner ($creator)")
            disconnectFromHost()
            return
        }
        if (!GroupAuth.verifyGroupUpdate(
                groupId, creator, packet.groupName, packet.announcement,
                packet.senderPubId, packet.senderSig
            )
        ) {
            disconnectFromHost()
            return
        }
        var changed = false
        val name = packet.groupName?.trim().orEmpty()
        if (name.isNotEmpty() && name != groupName) {
            groupName = name
            changed = true
        }
        packet.announcement?.let {
            groupAnnouncement = it
            changed = true
        }
        if (changed) adminNotify?.invoke()
        // forward to members whose own host relay is down (no re-forward there)
        GroupMeshManager.broadcastAdmin(groupId, packet)
    }

    private fun handleKickAsClient(packet: NetworkPacket) {
        val creator = creatorId()
        if (creator.isEmpty() || packet.senderId != creator) {
            Log.w(TAG, "reject kick_member from ${packet.senderId}: not the group owner ($creator)")
            disconnectFromHost()
            return
        }
        if (!GroupAuth.verifyKick(groupId, creator, packet.targetId, packet.senderPubId, packet.senderSig)) {
            disconnectFromHost()
            return
        }
        val target = packet.targetId ?: return
        if (target == myId) {
            if (!kickedFromGroup) {
                kickedFromGroup = true
                kickedNotify?.invoke()
            }
            return
        }
        _peers.update { it - target }
        GroupMeshManager.broadcastAdmin(groupId, packet)
        adminNotify?.invoke()
    }

    /** Drop the host relay after a protocol violation: the read loop's cleanup
     *  then publishes connectionLost so the UI can reconnect. */
    private fun disconnectFromHost() {
        runCatching { hostConnection?.close() }
    }

    /** Detach one member after a protocol violation and tell the others. */
    private fun dropClient(peerId: String) {
        val conn = connectedClients.remove(peerId)
        _peers.update { it - peerId }
        runCatching { conn?.socket?.close() }
        broadcastToClients(
            NetworkPacket(type = "peer_left", peer = Peer(peerId, "", "", 0)),
            exclude = peerId
        )
    }

    private fun processPacketFromClient(packet: NetworkPacket, senderId: String) {
        if (packet.type == "group_update" || packet.type == "kick_member") {
            // only the group owner (creator) may send management packets; the
            // owner is this host, so a member sending one is a protocol
            // violation: drop it and detach that member
            Log.w(TAG, "reject ${packet.type} from member $senderId: only the group owner may send it")
            dropClient(senderId)
            return
        }
        when (packet.type) {
            "chat", "file_message" -> packet.message?.let { msg ->
                if (msg.senderId != senderId || !isValidContent(msg.content)) {
                    Log.w(TAG, "drop invalid ${packet.type} from $senderId: senderId=${msg.senderId} contentLen=${msg.content.length}")
                    return
                }
                // author identity gate before the message is stored or relayed
                if (!GroupAuth.verifyMessage(groupId, msg)) return
                // idempotent insert (same message id can never arrive twice on
                // the relay path, but being defensive here costs nothing)
                _messages.update { list ->
                    if (list.any { it.id == msg.id }) list
                    else list + markFromMe(msg.withSanitizedExtras(), myId)
                }
                broadcastToClients(packet, exclude = senderId)
            }
            "delete_message" -> {
                val id = packet.messageId
                if (id == null || packet.senderId != senderId) {
                    Log.w(TAG, "reject delete_message from $senderId: packet senderId=${packet.senderId}")
                    return
                }
                val target = _messages.value.firstOrNull { it.id == id }
                if (target == null || target.senderId != senderId) {
                    Log.w(TAG, "reject delete_message $id from $senderId: message senderId=${target?.senderId}")
                    return
                }
                if (!GroupAuth.verifyDelete(groupId, senderId, id, packet.senderPubId, packet.senderSig)) return
                _messages.update { list -> list.filterNot { it.id == id } }
                broadcastToClients(packet, exclude = senderId)
            }
            "ping" -> {
                // answer so the member knows the host is still here
                val conn = connectedClients[senderId]
                if (conn != null) {
                    runCatching { conn.wire.sendPacket(NetworkPacket(type = "pong")) }
                }
            }
            "typing" -> {
                // a member is composing: attribute the indicator to the
                // authenticated connection (never to a packet-carried id we
                // did not verify), show it to the host itself, and relay it
                // to the other members (exclude the sender, like chat/delete)
                if (packet.senderId != senderId || packet.active == null) {
                    Log.w(TAG, "drop typing from $senderId: packet senderId=${packet.senderId}")
                    return
                }
                typingListener?.invoke(senderId, packet.active)
                broadcastToClients(packet, exclude = senderId)
            }
            "edit_message" -> {
                val id = packet.messageId
                val content = packet.newContent
                if (id == null || content == null || !isValidContent(content) ||
                    packet.senderId != senderId
                ) {
                    Log.w(TAG, "reject edit_message from $senderId: senderId=${packet.senderId}")
                    return
                }
                val target = _messages.value.firstOrNull { it.id == id }
                if (target == null || target.senderId != senderId) {
                    Log.w(TAG, "reject edit_message $id from $senderId: author=${target?.senderId}")
                    return
                }
                if (!GroupAuth.verifyEdit(groupId, senderId, id, content, packet.senderPubId, packet.senderSig)) return
                applyEditLocal(id, content, senderId)
                // the ViewModel persists the edit (Windows listener
                // .message_edited parity) — without this the host's stored row
                // keeps the pre-edit text until some mesh link re-delivers it
                editListener?.invoke(id, content, senderId)
                broadcastToClients(packet, exclude = senderId)
            }
            "reaction" -> {
                val emoji = packet.emoji?.let { com.zqr.localchat.data.sanitizeEmoji(it) }
                if (packet.senderId != senderId || packet.messageId == null || emoji.isNullOrEmpty()) {
                    Log.w(TAG, "reject reaction from $senderId: senderId=${packet.senderId}")
                    return
                }
                if (_messages.value.none { it.id == packet.messageId }) return
                reactionListener?.invoke(packet.messageId!!, emoji, senderId, packet.active == true)
                // relays carry the SANITIZED value: a member must not push an
                // over-long emoji into every client's reaction table
                broadcastToClients(packet.copy(emoji = emoji), exclude = senderId)
            }
            "pin_message" -> {
                if (packet.senderId != senderId || packet.messageId == null) {
                    Log.w(TAG, "reject pin_message from $senderId: senderId=${packet.senderId}")
                    return
                }
                if (_messages.value.none { it.id == packet.messageId }) return
                pinListener?.invoke(packet.messageId!!, senderId, packet.active == true)
                broadcastToClients(packet, exclude = senderId)
            }
            "read_receipt" -> {
                val upTo = packet.upToId
                if (upTo == null || packet.readerId != senderId || packet.groupId != groupId) {
                    return
                }
                groupReceiptListener?.invoke(senderId, upTo)
                broadcastToClients(packet, exclude = senderId)
            }
            "pong" -> { /* traffic only */ }
            in CALL_PACKET_TYPES -> routeCallPacket(packet, senderId)
            in GROUP_CALL_PACKET_TYPES -> routeGroupCallPacket(packet, senderId)
        }
    }

    /**
     * Host-side routing for call signaling: validate the sender identity and
     * the packet's targetId against the participant implied by the sender's
     * role, then deliver the packet either locally (the host is the peer) or
     * to the addressed member's socket (never broadcast). Parity with the
     * Windows P2PManager._route_call_packet.
     */
    private fun routeCallPacket(packet: NetworkPacket, senderId: String) {
        val call = packet.call ?: return
        val expectedTarget: String
        when (packet.type) {
            "call_offer" -> {
                if (call.callerId != senderId) {
                    Log.w(TAG, "drop call_offer from $senderId: callerId mismatch")
                    return
                }
                expectedTarget = call.calleeId
            }
            "call_answer", "call_reject", "call_failed" -> {
                if (call.calleeId != senderId) {
                    Log.w(TAG, "drop ${packet.type} from $senderId: calleeId mismatch")
                    return
                }
                expectedTarget = call.callerId
            }
            "call_hangup" -> {
                if (call.callerId != senderId && call.calleeId != senderId) {
                    Log.w(TAG, "drop call_hangup from $senderId: not a participant")
                    return
                }
                expectedTarget = if (senderId == call.callerId) call.calleeId else call.callerId
            }
            else -> return
        }
        val targetId = packet.targetId
        if (targetId != null && targetId != expectedTarget) {
            Log.w(TAG, "drop ${packet.type}: targetId $targetId does not match expected $expectedTarget")
            return
        }
        if (targetId == null) {
            if (expectedTarget != myId) {
                Log.w(TAG, "drop ${packet.type}: missing targetId but expected $expectedTarget is not this host")
                return
            }
            callSignalListener?.invoke(packet)
            return
        }
        if (targetId == myId) {
            callSignalListener?.invoke(packet)
            return
        }
        val conn = connectedClients[targetId]
        if (conn != null) {
            runCatching { conn.wire.sendPacket(packet) }
        }
    }

    /**
     * Host-side routing for group voice conference signaling: the packet
     * actor (call.callerId — invite sender / joiner / leaver / meeting host)
     * must be the authenticated sender connection; deliver locally when this
     * host is the addressee, otherwise forward to the targeted member's
     * socket (never broadcast). Parity with the Windows
     * P2PManager._route_group_call_packet.
     */
    private fun routeGroupCallPacket(packet: NetworkPacket, senderId: String) {
        val call = packet.call ?: return
        if (call.callerId != senderId) {
            Log.w(TAG, "drop group call ${packet.type} from $senderId: callerId mismatch")
            return
        }
        val targetId = packet.targetId
            // invite/sync address the invited member, join/leave address the
            // meeting host — both are call.calleeId
            ?: call.calleeId
        if (targetId == myId) {
            groupCallSignalListener?.invoke(packet)
            return
        }
        val conn = connectedClients[targetId]
        if (conn != null) {
            runCatching { conn.wire.sendPacket(packet) }
        }
    }

    /** Send a chat message through the host relay; returns the created
     *  message (or null when the content is invalid) so the caller can also
     *  broadcast it over the group mesh. The optional reply triple attaches a
     *  quoted header (see ChatMessage); a forward passes none of them.
     *  [mentions] carries the @-mentioned peer ids ("all" = everyone). */
    fun sendMessage(
        content: String,
        replyTo: String? = null,
        replyPreview: String? = null,
        replySender: String? = null,
        mentions: List<String>? = null
    ): ChatMessage? {
        if (!isValidContent(content)) return null
        var msg = ChatMessage(
            id = UUID.randomUUID().toString(),
            content = content,
            timestamp = System.currentTimeMillis(),
            senderId = myId,
            senderName = myName,
            isFromMe = true,
            replyTo = replyTo,
            replyPreview = replyPreview,
            replySender = replySender,
            mentions = mentions?.takeIf { it.isNotEmpty() }
        )
        // author identity signature (TOFU binding, GroupAuth): no-op when this
        // device has no identity key yet (legacy behavior)
        msg = GroupAuth.signMessage(currentGroupId, msg)
        // Update local state synchronously so a delete issued right after the
        // send (removeMessage) can find this message immediately.
        _messages.update { it + msg }
        val packet = NetworkPacket(type = "chat", message = msg)
        sendScope.launch {
            try {
                if (isHost) {
                    broadcastToClients(packet)
                } else {
                    hostWire?.sendPacket(packet)
                }
            } catch (e: Exception) {
                Log.w(TAG, "sendMessage failed", e)
            }
        }
        return msg
    }

    /** Broadcast a typing indicator to the group over the host relay (the
     *  ViewModel mirrors it over the mesh). Advisory and never queued
     *  offline: without a live connection there is nobody to show it to. */
    fun sendTyping(active: Boolean) {
        val packet = NetworkPacket(
            type = "typing",
            groupId = currentGroupId,
            senderId = myId,
            active = active
        )
        sendScope.launch {
            try {
                if (isHost) {
                    broadcastToClients(packet)
                } else {
                    hostWire?.sendPacket(packet)
                }
            } catch (e: Exception) {
                Log.w(TAG, "sendTyping failed", e)
            }
        }
    }

    /** Merge messages that arrived on the group mesh (or history sync) into
     *  the group's message list in ONE update, deduplicating against
     *  host-relayed copies. A whole history batch is merged at once so the UI
     *  and persistence see a single change instead of one per message. */
    fun mergeIncoming(messages: List<ChatMessage>) {
        if (messages.isEmpty()) return
        _messages.update { list ->
            val ids = list.mapTo(HashSet()) { it.id }
            val fresh = messages.filter { it.id !in ids }.map { markFromMe(it, myId) }
            if (fresh.isEmpty()) list else (list + fresh).sortedBy { it.timestamp }
        }
    }

    fun removeMessage(messageId: String): Boolean {
        val target = _messages.value.firstOrNull { it.id == messageId }
        if (target == null || target.senderId != myId) return false
        _messages.update { list -> list.filterNot { it.id == messageId } }
        val packet = GroupAuth.signPacket(
            NetworkPacket(type = "delete_message", messageId = messageId, senderId = myId),
            GroupAuth.deleteParts(currentGroupId, myId, messageId)
        )
        // Network I/O must never run on the main thread: sendMessage() already
        // dispatches off the UI thread; do the same here so a UI-triggered
        // delete cannot hit NetworkOnMainThreadException and silently drop the
        // packet. sendScope keeps chat/delete pairs ordered on the wire.
        sendScope.launch {
            try {
                if (isHost) {
                    broadcastToClients(packet)
                } else {
                    hostWire?.sendPacket(packet)
                }
            } catch (e: Exception) {
                Log.w(TAG, "removeMessage send failed", e)
            }
        }
        return true
    }

    /** Author-only text edit: applies locally (content + edited flag) and
     *  broadcasts edit_message so every member's copy follows. The group mesh
     *  is mirrored by the ViewModel (like a chat send). */
    fun editMessage(messageId: String, newContent: String): Boolean {
        if (!isValidContent(newContent)) return false
        val target = _messages.value.firstOrNull { it.id == messageId }
        if (target == null || target.senderId != myId) return false
        val signedEdit = GroupAuth.signParts(
            GroupAuth.editParts(currentGroupId, myId, messageId, newContent)
        )
        _messages.update { list ->
            list.map {
                if (it.id == messageId) {
                    // re-sign the local copy so mesh history pushes stay
                    // content-signature consistent after the edit
                    GroupAuth.signMessage(currentGroupId, it.copy(content = newContent, edited = true))
                } else it
            }
        }
        var packet = NetworkPacket(
            type = "edit_message",
            groupId = currentGroupId,
            messageId = messageId,
            senderId = myId,
            newContent = newContent
        )
        if (signedEdit != null) {
            packet = packet.copy(senderPubId = signedEdit.first, senderSig = signedEdit.second)
        }
        enqueueSend(packet)
        return true
    }

    /** Toggle OUR emoji reaction on [messageId]. Advisory: never queued
     *  offline (a member with no live path simply misses it). */
    fun sendReaction(messageId: String, emoji: String, active: Boolean): Boolean {
        val clean = com.zqr.localchat.data.sanitizeEmoji(emoji)
        if (clean.isEmpty()) return false
        if (_messages.value.none { it.id == messageId }) return false
        enqueueSend(
            NetworkPacket(
                type = "reaction",
                groupId = currentGroupId,
                messageId = messageId,
                senderId = myId,
                emoji = clean,
                active = active
            )
        )
        return true
    }

    /** Pin/unpin a message in the group. Any member may pin (the group's
     *  trust model is its password); every receiver validates the claimed
     *  sender against its authenticated identity. */
    fun sendPin(messageId: String, active: Boolean): Boolean {
        if (_messages.value.none { it.id == messageId }) return false
        enqueueSend(
            NetworkPacket(
                type = "pin_message",
                groupId = currentGroupId,
                messageId = messageId,
                senderId = myId,
                active = active
            )
        )
        return true
    }

    /** Tell the group we have read up to [upToId] (host relay path; the
     *  ViewModel mirrors it over the mesh). Receivers mark only their OWN
     *  covered messages. */
    fun sendGroupReadReceipt(upToId: String) {
        if (upToId.isBlank()) return
        enqueueSend(
            NetworkPacket(
                type = "read_receipt",
                groupId = currentGroupId,
                upToId = upToId,
                readerId = myId
            )
        )
    }

    /** Apply an edit to the local list because an edit_message arrived (relay
     *  or mesh path; both validated the author). Idempotent. When the verified
     *  packet carried author identity fields, the copy adopts them so a later
     *  mesh history push stays content-signature consistent. */
    fun applyEditLocal(
        messageId: String,
        newContent: String,
        senderId: String,
        senderPubId: String? = null,
        senderSig: String? = null
    ): Boolean {
        var changed = false
        _messages.update { list ->
            list.map { msg ->
                if (msg.id == messageId) {
                    if (msg.senderId == senderId && (msg.content != newContent || !msg.edited)) {
                        changed = true
                        val editedMsg = msg.copy(content = newContent, edited = true)
                        if (!senderPubId.isNullOrBlank() && !senderSig.isNullOrBlank()) {
                            editedMsg.copy(senderPubId = senderPubId, senderSig = senderSig)
                        } else editedMsg
                    } else msg
                } else msg
            }
        }
        return changed
    }

    // --------------------------------------------- message-experience apply

    private fun applyRelayedEdit(packet: NetworkPacket) {
        val id = packet.messageId ?: return
        val content = packet.newContent ?: return
        val sender = packet.senderId ?: return
        // the relayed packet is only authenticated by the host, and this
        // device's decoder performs no validation (Windows validates at
        // from_dict): check the scope and the content like every other path
        if (packet.groupId != groupId || !isValidContent(content)) {
            Log.w(
                TAG,
                "reject relayed edit_message $id: group=${packet.groupId} len=${content.length}"
            )
            return
        }
        val target = _messages.value.firstOrNull { it.id == id }
        if (target == null || sender != target.senderId) {
            Log.w(
                TAG,
                "reject edit_message $id: packet senderId=$sender, message senderId=${target?.senderId}"
            )
            return
        }
        if (!GroupAuth.verifyEdit(groupId, sender, id, content, packet.senderPubId, packet.senderSig)) return
        applyEditLocal(id, content, sender, packet.senderPubId, packet.senderSig)
        editListener?.invoke(id, content, sender)
    }

    private fun applyRelayedReaction(packet: NetworkPacket) {
        val id = packet.messageId ?: return
        val sender = packet.senderId ?: return
        // the relayed packet comes from another member: sanitize here too (the
        // emoji is part of the reaction's primary key and must never be
        // stored over-long), mirroring the host/direct/mesh paths
        val emoji = com.zqr.localchat.data.sanitizeEmoji(packet.emoji ?: return)
        if (emoji.isEmpty()) return
        if (_messages.value.none { it.id == id }) return
        reactionListener?.invoke(id, emoji, sender, packet.active == true)
    }

    private fun applyRelayedPin(packet: NetworkPacket) {
        val id = packet.messageId ?: return
        val sender = packet.senderId ?: return
        if (_messages.value.none { it.id == id }) return
        pinListener?.invoke(id, sender, packet.active == true)
    }

    private fun enqueueSend(packet: NetworkPacket) {
        sendScope.launch {
            try {
                if (isHost) {
                    broadcastToClients(packet)
                } else {
                    hostWire?.sendPacket(packet)
                }
            } catch (e: Exception) {
                Log.w(TAG, "${packet.type} send failed", e)
            }
        }
    }

    /** Send a packet addressed to a specific member (call signaling). As the
     *  host the packet goes straight to that member's socket; as a client it is
     *  relayed through the host with targetId set. Never broadcast. */
    fun sendTargeted(peerId: String, packet: NetworkPacket) {
        val targeted = packet.copy(targetId = peerId)
        sendScope.launch {
            try {
                if (isHost) {
                    connectedClients[peerId]?.wire?.sendPacket(targeted)
                } else {
                    hostWire?.sendPacket(targeted)
                }
            } catch (e: Exception) {
                Log.w(TAG, "sendTargeted failed", e)
            }
        }
    }

    private fun broadcastPeerLeft(peerId: String) {
        val packet = NetworkPacket(type = "peer_left", peer = Peer(id = peerId, name = "", ipAddress = "", port = 0))
        broadcastToClients(packet, exclude = peerId)
    }

    /** Apply an owner group_update received over the mesh (the mesh layer
     *  already validated senderId against the creator). No rebroadcast: the
     *  mesh is a complete graph. */
    fun applyRemoteGroupUpdate(newName: String?, announcement: String?) {
        if (!newName.isNullOrEmpty()) groupName = newName
        if (announcement != null) groupAnnouncement = announcement
    }

    /** Drop a member locally after receiving the owner's kick broadcast. */
    fun removePeerLocally(peerId: String) {
        _peers.update { it - peerId }
    }

    /** Remove a message locally because a delete arrived over the group mesh
     *  (the mesh path validated the sender). Only the original sender may
     *  delete — mirrors the host relay's authorization. A null [senderId]
     *  means a tombstone-synced delete (join_ack/history_reply deletedIds):
     *  trusted, no author check. Does NOT rebroadcast: the sender already
     *  told everyone else (loop prevention). */
    fun removeLocalMessage(messageId: String, senderId: String?): Boolean {
        val target = _messages.value.firstOrNull { it.id == messageId }
        if (target == null) return false
        if (senderId != null && target.senderId != senderId) return false
        _messages.update { list -> list.filterNot { it.id == messageId } }
        return true
    }

    /**
     * Apply a peer's tombstone sync (join_ack deletedIds): drop the messages
     * from the live list and hand them to [onPeerDeletedIds] so the ViewModel
     * persists tombstones — a later history replay must not resurrect them.
     * Never rebroadcast: the sender already told everyone (loop prevention).
     */
    private fun applyPeerDeletedIds(groupId: String, deletedIds: List<String>?) {
        val ids = sanitizeDeletedIds(deletedIds)
        if (ids.isEmpty()) return
        ids.forEach { id ->
            _messages.update { list -> list.filterNot { it.id == id } }
        }
        onPeerDeletedIds?.invoke(groupId, ids)
    }

    /**
     * Offer a file to the group. The bytes are NOT sent over the message
     * stream: this opens a short-lived download server on a random port and
     * broadcasts a file_message carrying [FileInfo] (incl. the download
     * address). Receivers connect back to download the file.
     *
     * A folder entry passes [folderId]/[folderName]/[relativePath]/[folderTotal]
     * so receivers can group the offers and rebuild the tree; folder entries
     * are always plain "file" kind (never rendered inline).
     */
    fun sendFile(
        fileName: String,
        resolver: ContentResolver,
        uri: Uri,
        fileSize: Long,
        kind: String = FileKind.FILE,
        folderId: String = "",
        folderName: String = "",
        relativePath: String = "",
        folderTotal: Int = 0
    ): ChatMessage? {
        // receivers use the advertised name as their default save name: strip
        // path separators and ".." so a crafted offer cannot traverse out of
        // the directory the user picked
        val safeName = sanitizeFileName(fileName)
        if (!isValidContent(safeName)) return null
        if (fileSize > FileTransfer.MAX_DOWNLOAD_BYTES) {
            Log.w(TAG, "sendFile rejected: ${fileSize} bytes exceeds the ${FileTransfer.MAX_DOWNLOAD_BYTES} cap")
            return null
        }
        // folder entries carry a safe relative path and are capped by count;
        // an unusable path or an over-cap total is rejected outright
        val safeRelativePath: String
        val effectiveKind: String
        if (folderId.isNotEmpty()) {
            safeRelativePath = sanitizeRelativePath(relativePath)
            if (safeRelativePath.isEmpty() || folderTotal > MAX_FOLDER_FILES) return null
            effectiveKind = FileKind.FILE
        } else {
            safeRelativePath = ""
            effectiveKind = kind
        }
        val fileId = UUID.randomUUID().toString()
        val server = try {
            ServerSocket(0)
        } catch (e: Exception) {
            Log.w(TAG, "failed to open file server", e)
            return null
        }
        val port = server.localPort
        // per-file random key: travels INSIDE the encrypted message channel
        // and protects the raw download stream
        val fileKey = Crypto.randomBytes(Crypto.KEY_LEN)
        // refresh the advertised address at offer time: myIpAddress was
        // snapshotted when the group was joined/started, and a client that
        // switched Wi-Fi since would otherwise advertise a stale, unreachable
        // download host
        val advertised = P2PManager.getLocalIpAddress().ifBlank { myIpAddress }
        val fileInfo = FileInfo(
            fileId, safeName, fileSize, advertised, port, Crypto.toB64(fileKey), effectiveKind,
            folderId = folderId,
            folderName = folderName,
            relativePath = safeRelativePath,
            folderTotal = folderTotal
        )
        fileServers[fileId] = server
        val msg = GroupAuth.signMessage(
            currentGroupId,
            ChatMessage(
                id = fileId,
                content = safeName,
                timestamp = System.currentTimeMillis(),
                senderId = myId,
                senderName = myName,
                fileInfo = fileInfo,
                isFromMe = true
            )
        )
        _messages.update { it + msg }
        val packet = NetworkPacket(type = "file_message", message = msg)
        sendScope.launch {
            try {
                if (isHost) {
                    broadcastToClients(packet)
                } else {
                    hostWire?.sendPacket(packet)
                }
            } catch (e: Exception) {
                Log.w(TAG, "sendFile broadcast failed", e)
            }
        }
        FileTransfer.runServer(
            fileId = fileId,
            server = server,
            resolver = resolver,
            uri = uri,
            fileName = safeName,
            fileSize = fileSize,
            fileKey = fileKey,
            isActive = { fileServers[fileId] === server },
            onRemove = { fileServers.remove(it) }
        )
        return msg
    }

    /**
     * Download a file offered via [fileInfo] into [out]. Blocking; call from a
     * background thread. Verifies the received byte count against the offer.
     */
    /** Download an offered file into [out]. Blocking; call from a background
     *  thread. Progress/cancel ride through [FileTransfer]; the opened socket
     *  is appended to [sockHolder] so the canceller can shut a blocked read
     *  down immediately (Android parity with the direct-chat path). */
    fun downloadFile(
        fileInfo: FileInfo,
        out: OutputStream,
        onProgress: (Long, Long) -> Unit = { _, _ -> },
        cancelled: () -> Boolean = { false },
        sockHolder: MutableList<java.net.Socket>? = null,
        offset: Long = 0L
    ): FileTransfer.DownloadResult =
        FileTransfer.download(fileInfo, out, onProgress, cancelled, sockHolder, offset)

    private fun broadcastToClients(packet: NetworkPacket, exclude: String? = null) {
        for ((id, conn) in connectedClients) {
            if (id != exclude && !conn.socket.isClosed) {
                try {
                    conn.wire.sendPacket(packet)
                } catch (e: Exception) {
                    Log.w(TAG, "broadcast to $id failed", e)
                }
            }
        }
    }

    private fun closeSocket(socket: Socket?) {
        socket ?: return
        runCatching { socket.close() }
    }

    val currentGroupId: String get() = groupId
    val currentGroupName: String get() = groupName
    val isHostNode: Boolean get() = isHost
    val myNameValue: String get() = myName
    val myIdValue: String get() = myId

    val isConnected: Boolean
        get() = if (isHost) {
            hostServer.isRunning
        } else {
            val s = hostConnection
            s != null && !s.isClosed
        }

    fun replaySavedMessages(messages: List<ChatMessage>) {
        _messages.update { current ->
            val currentIds = current.map { it.id }.toSet()
            current + messages.filter { it.id !in currentIds }
        }
    }

    companion object {
        private const val TAG = "P2PManager"
        const val MAX_CONTENT_LENGTH = 5000
        const val MAX_LINE_LENGTH = 64 * 1024

        /**
         * Tombstone intake bounds for ONE packet. The deleted_messages table
         * keeps at most DeletedMessage.CAP ids per group (trimmed), so a peer
         * gains nothing by sending more — but a hostile or merely large list
         * must not make us do unbounded work (state updates, DB rows, delete
         * tombstones for peers). Message ids are UUID-sized; anything much
         * longer is not one.
         */
        const val MAX_DELETED_IDS = 200
        const val MAX_DELETED_ID_LEN = 128

        /** Dedupe, drop blanks/oversized ids and cap a tombstone list received
         *  from the wire (single-use guard for join_ack / history_reply). */
        fun sanitizeDeletedIds(ids: List<String>?): List<String> =
            ids.orEmpty()
                .asSequence()
                .filter { it.isNotBlank() && it.length <= MAX_DELETED_ID_LEN }
                .distinct()
                .take(MAX_DELETED_IDS)
                .toList()

        /**
         * Global callback: this device learned tombstoned message ids from a
         * peer's join_ack (offline-member delete convergence). The ViewModel
         * persists them as DeletedMessage tombstones so a later history
         * replay cannot resurrect the rows. Global (like the mesh callbacks)
         * because it only touches the shared database; set once at ViewModel
         * init — BEFORE any join can complete (join_ack racing callback
         * registration must never drop tombstones).
         */
        @Volatile
        var onPeerDeletedIds: ((groupId: String, deletedIds: List<String>) -> Unit)? = null

        /**
         * Global tombstone provider for the host's join_ack (offline-member
         * delete convergence): returns the group's tombstoned message ids,
         * empty when there are none (omitted on the wire). Set once at
         * ViewModel init, which owns the database.
         */
        @Volatile
        var deletedIdsProvider: ((groupId: String) -> List<String>)? = null

        /** Peer-presence heartbeat: both sides send a ping every interval; a
         * read loop that sees no traffic for [HEARTBEAT_TIMEOUT_MS] declares
         * the peer offline (detects half-open TCP connections instead of
         * failing only when a message is sent). */
        const val HEARTBEAT_INTERVAL_MS = 15_000L
        const val HEARTBEAT_TIMEOUT_MS = 45_000

        /**
         * Pick the most plausible LAN address to advertise for mesh links,
         * direct chats and file downloads. Iterates network interfaces and
         * prefers Wi-Fi / ethernet; skips loopback, point-to-point links
         * (VPN tunnels), and obviously virtual interfaces — the first
         * non-loopback IPv4 a device reports is often a VPN/cellular/hotspot
         * address that other members cannot reach.
         */
        fun getLocalIpAddress(): String {
            try {
                val interfaces = NetworkInterface.getNetworkInterfaces()
                var fallback: String? = null
                while (interfaces.hasMoreElements()) {
                    val intf = interfaces.nextElement()
                    if (intf.isLoopback || intf.isPointToPoint) continue
                    val name = intf.name.lowercase()
                    if (name.startsWith("tun") || name.startsWith("ppp") ||
                        name.contains("vpn") || name.contains("virtual")
                    ) continue
                    val addrs = intf.inetAddresses
                    while (addrs.hasMoreElements()) {
                        val addr = addrs.nextElement()
                        if (addr is Inet4Address && !addr.isLoopbackAddress) {
                            val host = addr.hostAddress ?: continue
                            if (name.startsWith("wlan") || name.startsWith("eth")) return host
                            if (fallback == null) fallback = host
                        }
                    }
                }
                return fallback ?: ""
            } catch (_: Exception) {}
            return ""
        }

        /**
         * Every non-loopback IPv4 address of this device (interface name +
         * address), the address [getLocalIpAddress] advertises sorted first.
         * The settings page shows them all: a device can sit on several
         * networks at once (Wi-Fi, hotspot, VPN, USB tethering...) and only
         * one of them is reachable for a given peer, so the user needs to
         * see — and copy — the right one. No filtering here (unlike
         * [getLocalIpAddress]): an address the advertiser skips (a VPN's
         * tun0, say) may still be exactly what a specific peer needs.
         */
        fun getAllLocalIpAddresses(): List<LocalAddress> {
            val out = ArrayList<LocalAddress>()
            try {
                val interfaces = NetworkInterface.getNetworkInterfaces()
                while (interfaces.hasMoreElements()) {
                    val intf = interfaces.nextElement()
                    if (intf.isLoopback || !runCatching { intf.isUp }.getOrDefault(false)) continue
                    val name = intf.name ?: continue
                    val addrs = intf.inetAddresses
                    while (addrs.hasMoreElements()) {
                        val addr = addrs.nextElement()
                        if (addr is Inet4Address && !addr.isLoopbackAddress) {
                            addr.hostAddress?.let { out.add(LocalAddress(name, it)) }
                        }
                    }
                }
            } catch (_: Exception) {}
            val preferred = getLocalIpAddress()
            return out.sortedByDescending { it.address == preferred }
        }

        fun isValidContent(content: String): Boolean =
            content.isNotBlank() && content.length <= MAX_CONTENT_LENGTH

        /** Strip path separators and ".." from a file name: receivers use
         *  [com.zqr.localchat.data.FileInfo.fileName] as the default save
         *  name, so a crafted offer must never escape the directory the user
         *  picked. */
        fun sanitizeFileName(name: String): String =
            name.replace('/', '_').replace('\\', '_').replace("..", "_")

        /**
         * Reads a single line with a hard cap: accumulates chars until '\n'
         * or MAX_LINE_LENGTH chars. Returns null when the line exceeds the cap
         * or the stream ends, so callers close the connection and never buffer
         * unboundedly.
         *
         * Reads ONE character at a time (never `read(char[])`): a chunked read
         * can pull several JSON lines in a single call, and anything after the
         * first '\n' inside that chunk is consumed but discarded, randomly
         * dropping the following packets (chat, delete_message, announce,
         * ping, call signaling...). TCP does not preserve println() boundaries,
         * so coalesced reads are normal. Per-char reads from a BufferedReader
         * are cheap (they hit its internal buffer) and cannot lose data.
         *
         * Shared by every line-based TCP reader in the app (host server,
         * direct chats, group mesh, file handshake) so no path can buffer
         * without a bound.
         */
        internal fun readLineLimited(reader: BufferedReader): String? {
            val buffer = StringBuilder(256)
            try {
                while (true) {
                    val c = reader.read()
                    if (c == -1) return if (buffer.isEmpty()) null else buffer.toString()
                    if (c == '\n'.code) return buffer.toString().removeSuffix("\r")
                    buffer.append(c.toChar())
                    if (buffer.length > MAX_LINE_LENGTH) return null
                }
            } catch (e: Exception) {
                return null
            }
        }

        /** Stable 8-digit numeric ID for a group: FNV-1a hash of the machine
         *  fingerprint + group name, so it is machine-bound yet distinct per
         *  group. Used as the join identifier (members type this instead of
         *  the group name). */
        fun numericGroupIdOf(groupName: String, fingerprint: String): String {
            val s = "$groupName\u0000$fingerprint"
            var hash = 0x811c9dc5L
            for (ch in s) {
                hash = (hash xor ch.code.toLong()) and 0xFFFFFFFFL
                hash = (hash * 0x01000193L) and 0xFFFFFFFFL
            }
            val digits = ((hash % 100_000_000L) + 100_000_000L) % 100_000_000L
            return digits.toString().padStart(8, '0')
        }

        /** "1234 5678" display form of a numeric group id. */
        fun formatNumericGroupId(id: String): String =
            id.chunked(4).joinToString(" ")

        fun markFromMe(msg: ChatMessage, myId: String): ChatMessage =
            msg.copy(isFromMe = msg.senderId == myId)
    }
}
