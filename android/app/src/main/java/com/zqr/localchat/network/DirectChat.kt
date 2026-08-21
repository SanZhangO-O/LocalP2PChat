package com.zqr.localchat.network

import android.content.ContentResolver
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.Peer
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Direct member-to-member chat: the management unit is the member, not the
 * group. Picking a member immediately pulls up a 1:1 chat over a direct TCP
 * connection; the other side auto-accepts (no confirmation) as long as the
 * app is running and listening on the program-wide port.
 *
 * Handshake (see SecureWire.kt): the connection starts with an identity
 * handshake — ephemeral ECDH signed by both devices' long-term identity
 * keys. Every line after it is AES-256-GCM encrypted. Then the connector
 * sends "direct_hello" {peer} and the listener replies "direct_ack" {peer},
 * both INSIDE the encrypted channel; a known peer whose identity key changed
 * is rejected (possible MITM). Packets reuse the group types:
 * chat / file_message / delete_message / ping / pong.
 * A session dies when its socket times out or closes; the contact stays, so
 * the user can pull the chat up again.
 *
 * Pending send: messages composed while the peer is offline are appended to
 * the local list (marked pending) and parked in a per-peer outbox. A
 * background redial loop keeps trying the peer with growing backoff; the
 * moment a session is established — by our dial OR by the peer dialing us —
 * the outbox flushes over it in order. A manually added contact only knows a
 * placeholder "ip:..." id until the handshake reveals the real device id;
 * queued state keyed by the placeholder is migrated to the real id then.
 *
 * Beyond text, a direct session also carries:
 *  - file transfer: "file_message" offers a file served by [FileTransfer] on
 *    a short-lived download server (same protocol as group chats);
 *  - video/audio call signaling: call_* packets are forwarded verbatim to
 *    [onCallSignal] (the ViewModel routes them into CallManager with this
 *    manager as the signaling channel); media itself travels over a separate
 *    TCP connection managed by CallManager.
 */
object DirectChatManager {

    /** A member we can pull up a chat with. */
    @Serializable
    data class Contact(
        val id: String,
        val name: String,
        val ip: String,
        val port: Int
    )

    private const val TAG = "DirectChat"
    private const val CONNECT_TIMEOUT_MS = 8_000
    private const val READ_TIMEOUT_MS = 45_000
    private const val PING_INTERVAL_MS = 15_000L

    /** How long the session sender thread parks on an empty queue before
     *  rechecking liveness (so a replaced/closed session's loop exits). */
    private const val SEND_POLL_MS = 1_000L

    /** Redial cadence while an outbox waits for the peer: starts at
     *  [REDIAL_BACKOFF_MS] after a failed dial and doubles up to
     *  [REDIAL_MAX_BACKOFF_MS]; the loop gives up entirely (message stays
     *  queued) after [REDIAL_LIFETIME_MS] so it never polls forever. */
    private const val REDIAL_BACKOFF_MS = 5_000L
    private const val REDIAL_MAX_BACKOFF_MS = 30_000L
    private const val REDIAL_LIFETIME_MS = 10 * 60_000L

    private class Session(
        val peerId: String,
        val peerName: String,
        val socket: Socket,
        val wire: Wire
    ) {
        @Volatile
        var alive = true

        /** FIFO of outbound packets, drained by the session's single sender
         *  thread — enqueue order IS wire order, so a chat sent right before
         *  a delete reaches the peer in that order. */
        val sendQueue = LinkedBlockingQueue<NetworkPacket>()
    }

    private val sessions = ConcurrentHashMap<String, Session>()
    private val messageStates = ConcurrentHashMap<String, MutableStateFlow<List<ChatMessage>>>()

    /** Pending-send outbox per peer key: messages composed while offline,
     *  flushed in order when a session comes up. Guarded per-queue. */
    private val outbox = ConcurrentHashMap<String, MutableList<ChatMessage>>()

    /** peer key -> "ip:port" endpoint it was last associated with. Lets a
     *  handshake migrate alias keys (manually added "ip:..." placeholders)
     *  to the peer's REAL device id. */
    private val chatEndpoints = ConcurrentHashMap<String, String>()

    /** Keys with a redial loop currently running. */
    private val redialLoops = ConcurrentHashMap.newKeySet<String>()
    private val redialGuard = Any()

    /** Outbound file servers keyed by fileId; each offers one file until its
     *  accept loop ends (timeout, socket close or explicit removal). */
    private val fileServers = ConcurrentHashMap<String, ServerSocket>()

    /** Incoming call_* packets from a direct session, forwarded to the
     *  ViewModel (which routes them into CallManager). Invoked on the session
     *  read thread. */
    @Volatile
    var onCallSignal: ((NetworkPacket) -> Unit)? = null

    /** A direct session closed (socket lost/timed out); the ViewModel ends any
     *  call still riding that session. Invoked on the session read thread. */
    @Volatile
    var onSessionClosed: ((String) -> Unit)? = null

    /** A direct session was ESTABLISHED (either direction): the connector or
     *  the listener just finished the handshake. The ViewModel uses this to
     *  start persistence for sessions the LOCAL user never opened — otherwise
     *  messages received from a peer-initiated chat are only kept in memory
     *  and vanish when the process dies. */
    @Volatile
    var onSessionEstablished: ((String) -> Unit)? = null

    /** A chat's state moved from an alias key (manually added "ip:..."
     *  placeholder id) to the member's real device id, revealed by a
     *  handshake. The ViewModel moves the persisted rows and the observer
     *  job; the UI re-keys the open chat. Invoked on a session thread. */
    @Volatile
    var onChatMigrated: ((fromId: String, toId: String) -> Unit)? = null

    private val _contacts = MutableStateFlow<Map<String, Contact>>(emptyMap())
    val contacts: StateFlow<Map<String, Contact>> = _contacts.asStateFlow()

    /** Members with a currently live direct session (reactive online state). */
    private val _aliveSessions = MutableStateFlow<Set<String>>(emptySet())
    val aliveSessions: StateFlow<Set<String>> = _aliveSessions.asStateFlow()

    /** Last message per member, for the conversation preview on the home page. */
    private val _lastMessages = MutableStateFlow<Map<String, ChatMessage>>(emptyMap())
    val lastMessages: StateFlow<Map<String, ChatMessage>> = _lastMessages.asStateFlow()

    /** Transient events (connected / disconnected / unreachable...). */
    private val _events = MutableSharedFlow<String>(extraBufferCapacity = 16)
    val events: SharedFlow<String> = _events.asSharedFlow()

    @Volatile
    private var myId = ""
    @Volatile
    private var myName = ""
    @Volatile
    private var myIp = ""
    @Volatile
    private var myPort = 0

    // ------------------------------------------------------------ identity

    /** The ViewModel sets the local identity and preloads persisted contacts. */
    fun configure(myId: String, myName: String, myIp: String, myPort: Int, savedContacts: List<Contact>) {
        this.myId = myId
        this.myName = myName
        this.myIp = myIp
        this.myPort = myPort
        _contacts.value = savedContacts.associateBy { it.id }
    }

    /** Local device id (set by [configure]); used by call signaling. */
    val myIdValue: String get() = myId

    /** Local display name (set by [configure]); used by call signaling. */
    val myNameValue: String get() = myName

    private fun myPeer(): Peer {
        // refresh on every handshake: the local IP may have changed (Wi-Fi
        // switch) since configure() ran at app start
        val ip = P2PManager.getLocalIpAddress().ifBlank { myIp }
        myIp = ip
        return Peer(myId, myName, ip, myPort)
    }

    /** Send an arbitrary packet over a live session (call signaling rides the
     *  direct connection). Returns false when there is no session. Enqueued,
     *  not written: the session's single sender thread drains the queue in
     *  FIFO order, so concurrent callers cannot interleave or reorder their
     *  lines inside the TCP stream. */
    fun sendPacket(peerId: String, packet: NetworkPacket): Boolean {
        val s = sessions[peerId] ?: return false
        s.sendQueue.put(packet)
        return true
    }

    /** The session's ONLY writer: one thread per session draining the send
     *  queue in order. A write failure means the socket is dead — close it so
     *  the read loop unblocks immediately and the session tears down instead
     *  of lingering until the peer read timeout. */
    private fun sendLoop(s: Session) {
        try {
            while (s.alive && !s.socket.isClosed) {
                val packet = s.sendQueue.poll(SEND_POLL_MS, TimeUnit.MILLISECONDS) ?: continue
                try {
                    s.wire.sendPacket(packet)
                } catch (e: Exception) {
                    Log.w(TAG, "send on session ${s.peerId} failed", e)
                    closeSocket(s.socket)
                    break
                }
            }
        } catch (_: InterruptedException) {
            // closing
        }
    }

    // ------------------------------------------------------------- contacts

    fun addContact(contact: Contact) {
        _contacts.update { map ->
            // dedupe by endpoint: a manually added placeholder (id from ip)
            // is replaced by the real contact once a handshake reveals the id
            var next = map
            for (old in map.values) {
                if (old.ip == contact.ip && old.port == contact.port && old.id != contact.id) {
                    next = next - old.id
                }
            }
            next + (contact.id to contact)
        }
    }

    fun removeContact(id: String) {
        _contacts.update { it - id }
    }

    // -------------------------------------------------------------- messages

    /** Per-member message list for the chat UI. */
    fun messagesFor(peerId: String): StateFlow<List<ChatMessage>> =
        messageStates.getOrPut(peerId) { MutableStateFlow(emptyList()) }

    /** Seed a freshly opened chat with its persisted history, MERGING into
     *  any messages already received live (a session may have delivered
     *  messages before the chat UI opened — overwriting would lose them). */
    fun seedMessages(peerId: String, messages: List<ChatMessage>) {
        messageStates.getOrPut(peerId) { MutableStateFlow(emptyList()) }.update { current ->
            val ids = current.mapTo(HashSet()) { it.id }
            (current + messages.filter { it.id !in ids }).sortedBy { it.timestamp }
        }
        refreshLastMessage(peerId)
    }

    /** Restore just the LAST message of a peer's history after a process
     *  restart, without opening a full chat — so the home page previews are
     *  populated even before the user reconnects to that peer. */
    fun seedLastMessage(peerId: String, message: ChatMessage) {
        messageStates.getOrPut(peerId) { MutableStateFlow(emptyList()) }.update { current ->
            if (current.any { it.id == message.id }) current
            else (current + message).sortedBy { it.timestamp }
        }
        refreshLastMessage(peerId)
    }

    private fun refreshLastMessage(peerId: String) {
        val last = messageStates[peerId]?.value?.lastOrNull()
        _lastMessages.update { map ->
            if (last == null) map - peerId else map + (peerId to last)
        }
    }

    private fun appendMessage(peerId: String, msg: ChatMessage) {
        messageStates.getOrPut(peerId) { MutableStateFlow(emptyList()) }.update { it + msg }
        refreshLastMessage(peerId)
    }

    private fun removeMessage(peerId: String, messageId: String) {
        messageStates.getOrPut(peerId) { MutableStateFlow(emptyList()) }.update { list ->
            list.filterNot { it.id == messageId }
        }
        refreshLastMessage(peerId)
    }

    // ---------------------------------------------------------------- actions

    /** Pull up a chat with a member: connect and handshake on a background
     *  thread (socket I/O must never run on the main thread), then report the
     *  member's REAL device id via [onResult] on the main thread — a manually
     *  added contact only knows the placeholder "ip:..." id until the
     *  handshake reveals the real one. */
    fun startChat(peer: Peer, onResult: (String?) -> Unit) {
        thread(name = "direct-connect") {
            val result = startChatSync(peer)
            Handler(Looper.getMainLooper()).post { onResult(result) }
        }
    }

    /** [quiet] suppresses the user-facing failure event (background redials
     *  would otherwise toast every few seconds); the log always gets it. */
    private fun startChatSync(peer: Peer, quiet: Boolean = false): String? {
        if (myId.isEmpty() || peer.id == myId) return null
        sessions[peer.id]?.let { if (it.alive) return peer.id }
        addContact(Contact(peer.id, peer.name, peer.ipAddress, peer.port))
        var sock: Socket? = null
        return try {
            sock = Socket()
            sock.tcpNoDelay = true
            sock.connect(InetSocketAddress(peer.ipAddress, peer.port), CONNECT_TIMEOUT_MS)
            sock.soTimeout = CONNECT_TIMEOUT_MS
            // Identity-based secured handshake: ephemeral ECDH signed by both
            // devices' long-term identity keys — the session (chat, files,
            // call signaling) is encrypted end-to-end and a MITM fails the
            // signature / TOFU check.
            val wire = Wire(
                BufferedReaderLineIn(BufferedReader(InputStreamReader(sock.getInputStream()))),
                PrintWriter(sock.getOutputStream(), true)
            )
            val secured = Handshake.initiateDirect(
                wire,
                expectedPeerId = peer.id.takeIf { it.isNotEmpty() && !it.startsWith("ip:") },
                onIdentityMismatch = {
                    _events.tryEmit("安全警告：${peer.name} 的设备身份发生变化，连接已拒绝（可能存在中间人攻击）")
                }
            )
            wire.sendPacket(NetworkPacket(type = Protocol.DIRECT_HELLO, peer = myPeer()))
            val ack = wire.recvPacket() ?: throw IllegalStateException("no direct_ack")
            if (ack.type != Protocol.DIRECT_ACK || ack.peer == null) {
                throw IllegalStateException("bad direct_ack")
            }
            val remote = ack.peer
            // the handshake revealed the peer's identity key; bind it to the
            // real device id the ack just disclosed (TOFU)
            if (!DeviceIdentity.checkPeer(remote.id, secured.peerIdent ?: "")) {
                _events.tryEmit("安全警告：${remote.name} 的设备身份发生变化，连接已拒绝（可能存在中间人攻击）")
                throw IllegalStateException("peer identity changed")
            }
            onEstablished(sock, wire, remote)
            // onEstablished migrated aliases by the peer's ADVERTISED address;
            // a multi-homed peer advertises a different local IP than the one
            // we dialed, so also migrate by the dialed endpoint — our dial
            // reaching THAT device is proof enough of identity — and flush
            // again: onEstablished's flush ran before this merge
            migrateAliasesFor("${peer.ipAddress}:${peer.port}", remote.id)
            sessions[remote.id]?.takeIf { it.alive }?.let { flushOutbox(remote.id, it) }
            sock = null // ownership transferred to the session
            remote.id
        } catch (e: Exception) {
            Log.w(TAG, "direct connect to ${peer.ipAddress}:${peer.port} failed", e)
            if (!quiet) {
                val reason = when (e) {
                    is java.net.SocketTimeoutException ->
                        "无响应（请确认对方应用在运行且在同一网络）"
                    is java.net.ConnectException ->
                        "连接被拒绝（对方应用未运行或端口不对）"
                    is java.net.NoRouteToHostException ->
                        "网络不可达（请检查双方是否在同一局域网）"
                    is SecurityException ->
                        "缺少本地网络权限"
                    else -> e.message ?: "未知错误"
                }
                _events.tryEmit("无法连接成员 ${peer.name}（${peer.ipAddress}:${peer.port}）：$reason")
            }
            null
        } finally {
            // connect/handshake failure must close the socket; a successful
            // handoff set sock=null so it is not closed here
            if (sock != null) closeSocket(sock)
        }
    }

    /** Listener side: a secured direct_hello arrived on the shared port —
     *  auto-accept, no confirmation needed (the handshake already
     *  authenticated the dialer's identity key). */
    fun handleDirectHello(socket: Socket, wire: Wire, hello: NetworkPacket, peerIdent: String?) {
        val peer = hello.peer
        if (peer == null || peer.id == myId) {
            closeSocket(socket)
            return
        }

        // Identity-first: when the handshake already proves the peer's KNOWN
        // long-term key, the peer is authenticated by CRYPTOGRAPHY — an
        // address mismatch is then multi-homing (VPN/second NIC) or DHCP
        // churn, not impersonation, and must not block the session. The
        // address binding below only guards FIRST CONTACT (unknown identity),
        // where an attacker claiming another member's UUID could otherwise
        // poison the TOFU map before the real member ever connects.
        val identityProven = peerIdent != null &&
            DeviceIdentity.hasPeer(peer.id) &&
            DeviceIdentity.checkPeer(peer.id, peerIdent!!, remember = false)
        if (!identityProven) {
            val actualIp = runCatching { socket.inetAddress?.hostAddress }.getOrNull() ?: ""
            // Loopback is exempt: a 127.x source is necessarily THIS device
            // (the dialer may advertise its LAN address while connecting over
            // loopback), never a LAN impostor. IPv4-only, matching the rest
            // of the stack (AF_INET sockets).
            val fromLoopback = actualIp.startsWith("127.")
            if (!fromLoopback && actualIp.isNotEmpty() && peer.ipAddress.isNotEmpty() &&
                actualIp != peer.ipAddress
            ) {
                _events.tryEmit("安全警告：${peer.name} 声称的地址与连接来源不一致，连接已拒绝")
                closeSocket(socket)
                return
            }
            val existing = _contacts.value[peer.id]
            if (existing != null && existing.ip.isNotEmpty() && peer.ipAddress.isNotEmpty() &&
                existing.ip != peer.ipAddress
            ) {
                _events.tryEmit("安全警告：${peer.name} 的地址与已知成员不一致，连接已拒绝")
                closeSocket(socket)
                return
            }
        }
        // TOFU: a changed identity key for a KNOWN peer id means someone is
        // impersonating or intercepting it — refuse the session. (For an
        // unknown peer this is also the moment the key gets remembered: the
        // address binding above has already passed.)
        if (peerIdent != null && !DeviceIdentity.checkPeer(peer.id, peerIdent)) {
            _events.tryEmit("安全警告：${peer.name} 的设备身份发生变化，连接已拒绝（可能存在中间人攻击）")
            closeSocket(socket)
            return
        }
        wire.sendPacket(NetworkPacket(type = Protocol.DIRECT_ACK, peer = myPeer()))
        addContact(Contact(peer.id, peer.name, peer.ipAddress, peer.port))
        onEstablished(socket, wire, peer)
    }

    /**
     * Send a text message. The peer does NOT have to be online: with no live
     * session the message is appended locally (marked pending), parked in the
     * outbox, and delivered automatically once a session comes up — our
     * redial or the peer dialing us. Returns false only when there is no
     * known contact AND no session to deliver to.
     */
    fun sendMessage(peerId: String, content: String): Boolean {
        // validate with the SAME rule the receiver enforces: the receiver
        // drops content longer than MAX_CONTENT_LENGTH, so without this check
        // a too-long message would "send" locally but silently never arrive
        if (!P2PManager.isValidContent(content)) return false
        val contact = _contacts.value[peerId]
        val s = sessions[peerId]?.takeIf { it.alive }
        if (s == null && contact == null) return false
        val msg = ChatMessage(
            id = UUID.randomUUID().toString(),
            content = content,
            timestamp = System.currentTimeMillis(),
            senderId = myId,
            senderName = myName,
            isFromMe = true,
            pending = s == null
        )
        // show the message locally right away (pending until delivered)
        appendMessage(peerId, msg)
        if (s != null) {
            s.sendQueue.put(NetworkPacket(type = "chat", message = msg))
        } else if (contact != null) {
            enqueuePending(peerId, contact, msg)
        }
        return true
    }

    /** Park a message for a currently-offline peer and start the redial loop. */
    private fun enqueuePending(peerId: String, contact: Contact, msg: ChatMessage) {
        val q = outbox.getOrPut(peerId) { mutableListOf() }
        val first: Boolean
        synchronized(q) {
            first = q.isEmpty()
            q.add(msg)
        }
        chatEndpoints[peerId] = "${contact.ip}:${contact.port}"
        if (first) {
            _events.tryEmit("对方未在线，消息将在对方上线后自动发送")
        }
        ensureRedialLoop(peerId)
        // race heal: a session may have come up between the alive check and
        // the enqueue — flush inline so the message is never stranded (the
        // flush only enqueues onto the session's sender queue; no socket I/O
        // happens on the caller's, possibly main, thread)
        sessions[peerId]?.takeIf { it.alive }?.let { s ->
            flushOutbox(peerId, s)
        }
    }

    /** Keep dialing a peer while messages wait in its outbox, with growing
     *  backoff. Deliberately quiet: the UI already shows the offline state,
     *  so failures only reach the log (a loud toast every few seconds would
     *  be noise, not information). */
    private fun ensureRedialLoop(peerId: String) {
        synchronized(redialGuard) {
            if (!redialLoops.add(peerId)) return
        }
        thread(name = "direct-redial-$peerId") {
            try {
                val start = System.currentTimeMillis()
                var backoff = REDIAL_BACKOFF_MS
                while (System.currentTimeMillis() - start < REDIAL_LIFETIME_MS) {
                    val q = outbox[peerId]
                    if (q == null || synchronized(q) { q.isEmpty() }) break
                    val alive = sessions[peerId]?.takeIf { it.alive }
                    if (alive != null) {
                        flushOutbox(peerId, alive)
                        break
                    }
                    // the contact may have been removed meanwhile — then there
                    // is no address left to dial and the loop must stop
                    val contact = _contacts.value[peerId] ?: break
                    runCatching {
                        startChatSync(
                            Peer(contact.id, contact.name, contact.ip, contact.port),
                            quiet = true
                        )
                    }
                    val stillQueued = outbox[peerId]?.let { synchronized(it) { it.isNotEmpty() } } == true
                    if (!stillQueued || sessions[peerId]?.alive == true) break
                    Thread.sleep(backoff)
                    backoff = (backoff * 2).coerceAtMost(REDIAL_MAX_BACKOFF_MS)
                }
            } finally {
                synchronized(redialGuard) {
                    redialLoops.remove(peerId)
                    // a message enqueued between the loop's last check and the
                    // flag removal must not strand the queue: relaunch at once
                    val queued = outbox[peerId]?.let { synchronized(it) { it.isNotEmpty() } } == true
                    if (queued && sessions[peerId]?.alive != true &&
                        _contacts.value[peerId] != null
                    ) {
                        ensureRedialLoop(peerId)
                    }
                }
            }
        }
    }

    /** Deliver every queued message for [peerId] over [s], in order, then
     * flip each one's local state to delivered. */
    private fun flushOutbox(peerId: String, s: Session) {
        val q = outbox[peerId] ?: return
        val toSend: List<ChatMessage>
        synchronized(q) {
            if (q.isEmpty()) return
            toSend = q.toList()
            q.clear()
        }
        for (msg in toSend) {
            s.sendQueue.put(NetworkPacket(type = "chat", message = msg))
            markPendingDelivered(peerId, msg.id)
        }
    }

    private fun markPendingDelivered(peerId: String, messageId: String) {
        messageStates[peerId]?.update { list ->
            list.map { if (it.id == messageId) it.copy(pending = false) else it }
        }
        refreshLastMessage(peerId)
    }

    /** Merge one chat key's in-memory state (list + outbox) into another key:
     *  used when a handshake reveals that an "ip:..." alias and a real device
     *  id are the same member. */
    private fun mergeChatState(fromKey: String, toKey: String) {
        if (fromKey == toKey) return
        val fromState = messageStates.remove(fromKey) ?: return
        messageStates.getOrPut(toKey) { MutableStateFlow(emptyList()) }.update { current ->
            val ids = current.mapTo(HashSet()) { it.id }
            (current + fromState.value.filter { it.id !in ids }).sortedBy { it.timestamp }
        }
        val q = outbox.remove(fromKey)
        if (q != null && q.isNotEmpty()) {
            val toQ = outbox.getOrPut(toKey) { mutableListOf() }
            synchronized(toQ) { toQ.addAll(q) }
        }
        refreshLastMessage(toKey)
        _lastMessages.update { it - fromKey }
    }

    /** A session was established for [peer]: move every alias key that points
     *  at the same endpoint (manually added "ip:..." placeholders) over to
     *  the real device id, so queued messages and open chat views re-key. */
    private fun migrateAliasChats(peer: Peer) {
        migrateAliasesFor("${peer.ipAddress}:${peer.port}", peer.id)
    }

    /** Re-key every chat alias recorded for [endpoint] to [realId]. Endpoint
     *  matching is the only safe signal for alias identity — the endpoint a
     *  handshake reveals vs. the one recorded at queue time must AGREE, so
     *  mismatched advertisement (multi-homed peers advertise a different
     *  local IP than the dialed one) simply skips the migration rather than
     *  merging two different members' chats. */
    private fun migrateAliasesFor(endpoint: String, realId: String) {
        val aliases = (chatEndpoints.keys + outbox.keys)
            .filter { it != realId && chatEndpoints[it] == endpoint }
        for (alias in aliases) {
            mergeChatState(alias, realId)
            chatEndpoints.remove(alias)
            onChatMigrated?.invoke(alias, realId)
        }
    }

    /** Record the endpoint a chat key refers to (called when the chat is
     *  opened or a message is queued): a later handshake uses it to migrate
     *  alias keys to the revealed real device id. */
    fun openChat(contact: Contact) {
        chatEndpoints[contact.id] = "${contact.ip}:${contact.port}"
    }

    /** Re-queue messages persisted as pending by a previous process; called
     *  at startup for each chat with undelivered messages. */
    fun restorePending(peerId: String, messages: List<ChatMessage>) {
        if (messages.isEmpty()) return
        seedMessages(peerId, messages)
        val q = outbox.getOrPut(peerId) { mutableListOf() }
        synchronized(q) { q.addAll(messages) }
        val contact = _contacts.value[peerId]
        val endpoint = contact?.let { "${it.ip}:${it.port}" }
            // placeholder ids encode their endpoint themselves
            ?: peerId.removePrefix("ip:").takeIf { it != peerId }
        if (endpoint != null) chatEndpoints[peerId] = endpoint
        if (contact != null) ensureRedialLoop(peerId)
    }

    /**
     * Offer a file to a direct-chat member. The bytes are NOT sent over the
     * message stream: this opens a short-lived download server on a random
     * port and sends a file_message carrying [FileInfo] (incl. the download
     * address). The receiver connects back to download the file (shared
     * [FileTransfer] protocol).
     */
    fun sendFile(
        peerId: String,
        fileName: String,
        resolver: ContentResolver,
        uri: Uri,
        fileSize: Long
    ): Boolean {
        // same rule as message content: the receiver drops an invalid file
        // name silently, so reject it on the sending side
        if (!P2PManager.isValidContent(fileName)) return false
        if (fileSize > FileTransfer.MAX_DOWNLOAD_BYTES) return false
        val s = sessions[peerId] ?: return false
        val fileId = UUID.randomUUID().toString()
        val server = try {
            ServerSocket(0)
        } catch (e: Exception) {
            Log.w(TAG, "failed to open file server", e)
            return false
        }
        val port = server.localPort
        // per-file random key: travels INSIDE the encrypted message channel
        // and protects the raw download stream
        val fileKey = Crypto.randomBytes(Crypto.KEY_LEN)
        // refresh the advertised IP at offer time (same rule as the handshake's
        // myPeer()): myIp was set at app start and may be stale after a
        // network change, which would make the download host unreachable
        val advertised = P2PManager.getLocalIpAddress().ifBlank { myIp }
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
        appendMessage(peerId, msg)
        s.sendQueue.put(NetworkPacket(type = "file_message", message = msg))
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
        return true
    }

    /** Download a file offered via [fileInfo] into [out]. Blocking; call from
     *  a background thread. */
    fun downloadFile(fileInfo: FileInfo, out: OutputStream): FileTransfer.DownloadResult =
        FileTransfer.download(fileInfo, out)

    /** Delete a message in a direct chat: the sender broadcasts it, everyone
     *  (including the sender) removes it locally. A still-queued (pending)
     *  message is dropped from the outbox instead of being broadcast. */
    fun deleteMessage(peerId: String, messageId: String, senderId: String) {
        val s = sessions[peerId]
        if (senderId == myId) {
            val wasPending = outbox[peerId]?.let { q ->
                synchronized(q) { q.removeAll { it.id == messageId } }
            } == true
            if (s != null && !wasPending) {
                s.sendQueue.put(
                    NetworkPacket(type = "delete_message", messageId = messageId, senderId = myId)
                )
            }
        }
        removeMessage(peerId, messageId)
    }

    fun closeChat(peerId: String) {
        sessions.remove(peerId)?.let { s ->
            s.alive = false
            closeSocket(s.socket)
        }
    }

    fun isChatAlive(peerId: String): Boolean = sessions[peerId]?.alive == true

    // ------------------------------------------------------------- internals

    private fun onEstablished(socket: Socket, wire: Wire, peer: Peer) {
        // learn the real member identity: dedupe-by-endpoint replaces a
        // manually added "ip:..." placeholder contact with the real one
        addContact(Contact(peer.id, peer.name, peer.ipAddress, peer.port))
        // re-key any alias chat state (queued under a placeholder id) to the
        // real device id BEFORE flushing, so the outbox drains under it
        migrateAliasChats(peer)
        val session = Session(peer.id, peer.name, socket, wire)
        // A reconnect with the same peer id replaces the OLD session: the old
        // socket is closed, and its read-loop finally must not tear down the
        // freshly established one (guarded via conditional map removal).
        val previous = sessions.put(peer.id, session)
        if (previous != null && previous !== session) {
            previous.alive = false
            closeSocket(previous.socket)
        }
        _aliveSessions.update { it + peer.id }
        _events.tryEmit("已连接 ${peer.name}")
        onSessionEstablished?.invoke(peer.id)
        // deliver everything that piled up while the peer was offline
        flushOutbox(peer.id, session)
        thread(name = "direct-send-${peer.id}") { sendLoop(session) }
        thread(name = "direct-read-${peer.id}") { readLoop(session) }
        thread(name = "direct-ping-${peer.id}") { pingLoop(session) }
    }

    private fun readLoop(s: Session) {
        try {
            s.socket.soTimeout = READ_TIMEOUT_MS
            while (s.alive && !s.socket.isClosed) {
                val packet = s.wire.recvPacket() ?: break
                when (packet.type) {
                    "chat", "file_message" -> packet.message?.let { msg ->
                        // identity + content validation, matching the host
                        // relay: only the session peer may speak as itself
                        if (msg.senderId != s.peerId || !P2PManager.isValidContent(msg.content)) {
                            Log.w(TAG, "drop chat on session ${s.peerId}: senderId=${msg.senderId} len=${msg.content.length}")
                        } else if (messageStates[s.peerId]?.value?.any { it.id == msg.id } == true) {
                            // idempotent redelivery (e.g. the sender died
                            // between flushing its outbox and writing the
                            // pending=0 flag, then re-queued on restart):
                            // drop instead of duplicating the bubble
                            Log.i(TAG, "drop duplicate message ${msg.id} on session ${s.peerId}")
                        } else {
                            appendMessage(s.peerId, P2PManager.markFromMe(msg, myId))
                        }
                    }
                    "delete_message" -> {
                        val id = packet.messageId ?: continue
                        val sender = packet.senderId ?: continue
                        if (sender != s.peerId) continue // forged senderId
                        // only the original sender can delete
                        messageStates[s.peerId]?.value?.find { it.id == id }?.let { target ->
                            if (target.senderId == sender) removeMessage(s.peerId, id)
                        }
                    }
                    in CALL_PACKET_TYPES -> {
                        // 1:1 session: call signaling must involve THIS member
                        // and the packet's sender role must match the linked
                        // peer (parity with the Windows _read_loop checks).
                        val call = packet.call ?: continue
                        if (call.callerId != myId && call.calleeId != myId) continue
                        when (packet.type) {
                            "call_offer" ->
                                if (call.callerId != s.peerId || call.calleeId != myId) continue
                            "call_answer", "call_reject", "call_failed" ->
                                if (call.calleeId != s.peerId || call.callerId != myId) continue
                            "call_hangup" ->
                                if (call.callerId != s.peerId && call.calleeId != s.peerId) continue
                        }
                        onCallSignal?.invoke(packet)
                    }
                    "ping" -> runCatching { s.wire.sendPacket(NetworkPacket(type = "pong")) }
                    "pong" -> {}
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "direct read loop ended", e)
        } finally {
            s.alive = false
            // only when THIS session is still the registered one: a reconnect
            // may have replaced it, and the old loop's cleanup must not mark
            // the fresh session offline or end a call riding it
            if (sessions.remove(s.peerId, s)) {
                _aliveSessions.update { it - s.peerId }
                closeSocket(s.socket)
                _events.tryEmit("与 ${s.peerName} 的直聊连接已断开")
                onSessionClosed?.invoke(s.peerId)
            } else {
                closeSocket(s.socket)
            }
        }
    }

    private fun pingLoop(s: Session) {
        try {
            while (s.alive && !s.socket.isClosed) {
                Thread.sleep(PING_INTERVAL_MS)
                if (!s.alive) break
                runCatching { s.wire.sendPacket(NetworkPacket(type = "ping")) }
            }
        } catch (e: InterruptedException) {
            // closing
        }
    }

    private fun closeSocket(socket: Socket) {
        runCatching { socket.close() }
    }
}
