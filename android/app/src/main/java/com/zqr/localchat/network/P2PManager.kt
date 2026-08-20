package com.zqr.localchat.network

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.util.Log
import com.zqr.localchat.ChatApp
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.Peer
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

object Constants {
    const val TCP_PORT = 9999
}

/** One IPv4 address of this device, labeled by its network interface name. */
data class LocalAddress(val interfaceName: String, val address: String)

/** Packet types that carry call signaling. */
val CALL_PACKET_TYPES = setOf(
    "call_offer", "call_answer", "call_reject", "call_hangup", "call_failed"
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
            serverScope.launch { handleIncoming(client) }
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

        private fun closeSocket(socket: Socket?) {
            socket ?: return
            runCatching { socket.close() }
        }
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

    fun initializeAsHost(userName: String, group: String, password: String? = null) {
        myName = userName.trim()
        groupName = group.trim()
        groupId = "${groupName}@${hardwareId}"
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
     *  group name — the join identifier, separate from the display name. */
    val numericGroupId: String
        get() = numericGroupIdOf(groupName, hardwareId)

    val currentGroupPassword: String get() = groupPassword

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
                                response.members.forEach { peer ->
                                    if (peer.id != myId) {
                                        _peers.update { it + (peer.id to peer) }
                                    }
                                }
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
                    response.members?.forEach { peer ->
                        if (peer.id != myId) {
                            _peers.update { it + (peer.id to peer) }
                        }
                    }
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
        val ack = NetworkPacket(type = "join_ack", groupId = groupId, members = allMembers)
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
                // idempotent insert: a member's message reaches us over the
                // host relay AND over the mesh (whoever arrives first wins),
                // so a plain append would show duplicate bubbles
                _messages.update { list ->
                    if (list.any { it.id == msg.id }) list else list + markFromMe(msg, myId)
                }
            }
            "announce" -> packet.peer?.let { peer ->
                if (peer.id != myId) _peers.update { it + (peer.id to peer) }
            }
            "peer_left" -> packet.peer?.id?.let { peerId ->
                _peers.update { it - peerId }
            }
            "delete_message" -> packet.messageId?.let { id ->
                _messages.update { list -> list.filterNot { it.id == id } }
            }
            "ping" -> {
                // answer so the host knows we are still here
                val wire = hostWire
                if (wire != null) {
                    runCatching { wire.sendPacket(NetworkPacket(type = "pong")) }
                }
            }
            "pong" -> { /* traffic only; keeps the read loop alive */ }
            in CALL_PACKET_TYPES -> {
                // The host only delivers call packets to the addressed member,
                // so anything arriving here is for this node; double-check id.
                if (packet.call == null) return
                if (packet.targetId != null && packet.targetId != myId) return
                callSignalListener?.invoke(packet)
            }
        }
    }

    private fun processPacketFromClient(packet: NetworkPacket, senderId: String) {
        when (packet.type) {
            "chat", "file_message" -> packet.message?.let { msg ->
                if (msg.senderId != senderId || !isValidContent(msg.content)) {
                    Log.w(TAG, "drop invalid ${packet.type} from $senderId: senderId=${msg.senderId} contentLen=${msg.content.length}")
                    return
                }
                // idempotent insert (same message id can never arrive twice on
                // the relay path, but being defensive here costs nothing)
                _messages.update { list ->
                    if (list.any { it.id == msg.id }) list else list + markFromMe(msg, myId)
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
            "pong" -> { /* traffic only */ }
            in CALL_PACKET_TYPES -> routeCallPacket(packet, senderId)
        }
    }

    /**
     * Host-side routing for call signaling: validate the sender identity and
     * deliver the packet either locally (the host is the peer) or to the
     * addressed member's socket (never broadcast).
     */
    private fun routeCallPacket(packet: NetworkPacket, senderId: String) {
        val call = packet.call ?: return
        when (packet.type) {
            "call_offer", "call_failed" -> {
                if (call.callerId != senderId) {
                    Log.w(TAG, "drop ${packet.type} from $senderId: callerId mismatch")
                    return
                }
            }
            "call_answer", "call_reject" -> {
                if (call.calleeId != senderId) {
                    Log.w(TAG, "drop ${packet.type} from $senderId: calleeId mismatch")
                    return
                }
            }
            "call_hangup" -> {
                if (call.callerId != senderId && call.calleeId != senderId) {
                    Log.w(TAG, "drop call_hangup from $senderId: not a participant")
                    return
                }
            }
        }
        val targetId = packet.targetId
        if (targetId == null || targetId == myId) {
            callSignalListener?.invoke(packet)
            return
        }
        val conn = connectedClients[targetId]
        if (conn != null) {
            runCatching { conn.wire.sendPacket(packet) }
        }
    }

    /** Send a chat message through the host relay; returns the created
     *  message (or null when the content is invalid) so the caller can also
     *  broadcast it over the group mesh. */
    fun sendMessage(content: String): ChatMessage? {
        if (!isValidContent(content)) return null
        val msg = ChatMessage(
            id = UUID.randomUUID().toString(),
            content = content,
            timestamp = System.currentTimeMillis(),
            senderId = myId,
            senderName = myName,
            isFromMe = true
        )
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
        val packet = NetworkPacket(type = "delete_message", messageId = messageId, senderId = myId)
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

    /** Remove a message locally because a delete arrived over the group mesh
     *  (the mesh path validated the sender). Only the original sender may
     *  delete — mirrors the host relay's authorization. Does NOT rebroadcast:
     *  the mesh path forwards the delete to every other link itself. */
    fun removeLocalMessage(messageId: String, senderId: String): Boolean {
        val target = _messages.value.firstOrNull { it.id == messageId }
        if (target == null || target.senderId != senderId) return false
        _messages.update { list -> list.filterNot { it.id == messageId } }
        return true
    }

    /**
     * Offer a file to the group. The bytes are NOT sent over the message
     * stream: this opens a short-lived download server on a random port and
     * broadcasts a file_message carrying [FileInfo] (incl. the download
     * address). Receivers connect back to download the file.
     */
    fun sendFile(
        fileName: String,
        resolver: ContentResolver,
        uri: Uri,
        fileSize: Long
    ): ChatMessage? {
        if (!isValidContent(fileName)) return null
        if (fileSize > FileTransfer.MAX_DOWNLOAD_BYTES) {
            Log.w(TAG, "sendFile rejected: ${fileSize} bytes exceeds the ${FileTransfer.MAX_DOWNLOAD_BYTES} cap")
            return null
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
        val fileInfo = FileInfo(fileId, fileName, fileSize, advertised, port, Crypto.toB64(fileKey))
        fileServers[fileId] = server
        val msg = ChatMessage(
            id = fileId,
            content = fileName,
            timestamp = System.currentTimeMillis(),
            senderId = myId,
            senderName = myName,
            fileInfo = fileInfo,
            isFromMe = true
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
            fileName = fileName,
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
    fun downloadFile(fileInfo: FileInfo, out: OutputStream): FileTransfer.DownloadResult =
        FileTransfer.download(fileInfo, out)

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
