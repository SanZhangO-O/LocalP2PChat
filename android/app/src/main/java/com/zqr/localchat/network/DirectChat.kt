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
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Direct member-to-member chat: the management unit is the member, not the
 * group. Picking a member immediately pulls up a 1:1 chat over a direct TCP
 * connection; a KNOWN member (by device id or endpoint) is auto-accepted as
 * long as the app is running and listening on the program-wide port. A FIRST
 * CONTACT is never silently dropped OR silently accepted: the request lands
 * in the contact-request message box ([contactRequests]) for the user to
 * accept or ignore, and the dialer is told so via "direct_pending".
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
 *
 * Presence: "the app is running" IS "online". At start (and on return to the
 * foreground / a network change) the manager announces itself to every saved
 * contact by dialing the ones without a live session — the dial IS the
 * notification: it rides the identity handshake, so the peer flips us to
 * online with no polling and both sides' outboxes flush. Between events a
 * low-frequency sweep repairs sessions that died mid-air; deterministic
 * dialing (the member with the smaller id dials, placeholder contacts
 * always dial) keeps simultaneous announces from both apps from racing into
 * mismatched session pairs. A contact the local user removed is MARKED
 * (id + endpoint): the removed peer's announces must not resurrect it.
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

    /** Presence sweep cadence: how often a dead contact session is retried
     *  between announce events (app start, foreground return, network
     *  change). Quiet and low-frequency by design — this is link REPAIR,
     *  not status polling. */
    private const val PRESENCE_SWEEP_MS = 60_000L

    /** How long a removed-contact mark survives: the mark blocks the removed
     *  peer's presence dials from resurrecting the contact, but a stale
     *  endpoint mark (DHCP gave the address to a new device) must not block
     *  a legitimate first contact forever. */
    private val REMOVED_MARK_TTL_MS = 30L * 24 * 3600 * 1000

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

    // -------------------------------------------------------------- presence

    /** Wakes the presence loop between sweeps (announce events coalesce). */
    private val presenceWake = Semaphore(1)
    private val presenceGuard = Any()
    @Volatile
    private var presenceStarted = false

    /** Contacts a presence dial is currently in flight for (dedupes sweeps
     *  and concurrent announce events). */
    private val presenceDialing = ConcurrentHashMap.newKeySet<String>()

    /** Per-peer dial serialization: the presence sweep, the outbox redial
     *  loop and an explicit open-chat can all dial the same member at once,
     *  and two concurrent handshakes interleave into MISMATCHED session
     *  pairs — each side's "replace the old session" then lands on a
     *  different one of the two connections, so writes go to a socket the
     *  peer already closed and messages silently never arrive. */
    private val dialLocks = ConcurrentHashMap<String, Any>()

    /** Removed-contact marks: ids (and endpoints) the LOCAL user deleted,
     *  with the removal time. A peer still announcing to us must not
     *  resurrect a deleted contact; [addContact] clears the marks (explicit
     *  re-add, group sync, or a handshake from a re-added address). The
     *  id->endpoint link makes re-adding clear BOTH marks even when the
     *  peer's address changed between removal and re-add (DHCP churn). */
    private val removedIds = ConcurrentHashMap<String, Long>()
    private val removedEndpoints = ConcurrentHashMap<String, Long>()
    private val removedEndpointById = ConcurrentHashMap<String, String>()

    /** Marks changed (contact removed / re-added): the ViewModel persists
     *  them so a restart keeps honoring the removals. May fire on any
     *  thread. */
    @Volatile
    var onRemovedMarksChanged: (() -> Unit)? = null

    // --------------------------------------------------------- request box

    /** An incoming contact request parked for the user to accept or ignore.
     *  [fromRemoved] marks a request from a member the local user removed
     *  (its re-add attempts must stay visible too — nothing is dropped
     *  silently). */
    @Serializable
    data class ContactRequest(
        val id: String,
        val name: String,
        val ip: String,
        val port: Int,
        val fromRemoved: Boolean = false,
        val timestamp: Long = 0L
    )

    /** Box capacity: a LAN scanner hammering the port must not be able to
     *  grow the box without bound. Oldest entries fall off. */
    private const val REQUEST_BOX_MAX = 50

    private val _contactRequests = MutableStateFlow<List<ContactRequest>>(emptyList())

    /** Pending contact requests, newest last. The ViewModel persists this
     *  list; the member page renders it as the request message box. */
    val contactRequests: StateFlow<List<ContactRequest>> = _contactRequests.asStateFlow()

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

    /** Emit a transient user-facing event (toast). Used by the ViewModel
     *  for surface-level confirmations (e.g. "已连接 X" on a re-add). */
    fun emitEvent(text: String) {
        _events.tryEmit(text)
    }

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
        // identity + saved contacts ready: the app is ONLINE the moment it
        // starts, so announce to every saved contact right away
        announceOnline()
    }

    /** TEST-ONLY: wipe all in-memory state. The JVM unit tests share this
     *  singleton process, so each test must start from a clean slate. */
    fun resetForTest() {
        myId = ""; myName = ""; myIp = ""; myPort = 0
        _contacts.value = emptyMap()
        _contactRequests.value = emptyList()
        _aliveSessions.value = emptySet()
        _lastMessages.value = emptyMap()
        removedIds.clear()
        removedEndpoints.clear()
        removedEndpointById.clear()
        chatEndpoints.clear()
        outbox.clear()
        redialLoops.clear()
        messageStates.clear()
        fileServers.values.forEach { runCatching { it.close() } }
        fileServers.clear()
        sessions.values.forEach { runCatching { closeSocket(it.socket) } }
        sessions.clear()
        presenceDialing.clear()
        // stop a lingering presence loop (it re-checks the flag every sweep)
        // and drain wake permits so the next test's announce starts fresh
        synchronized(presenceGuard) {
            presenceStarted = false
        }
        while (presenceWake.tryAcquire()) {
        }
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
        // a contact (re-)added is no longer "removed": clear the marks so
        // the peer's announces are accepted again
        unmark(contact.id, contact.ip.takeIf { it.isNotBlank() }?.let { "$it:${contact.port}" })
        _contacts.update { map ->
            // dedupe by endpoint: a manually added placeholder (id from ip)
            // is replaced by the real contact once a handshake reveals the
            // id. The reverse must NOT happen: a manual "ip:..." add of an
            // endpoint already known under its REAL device id keeps the real
            // contact — clobbering it would orphan the chat history keyed by
            // the real id ("re-adding my contact by IP wiped it").
            var next = map
            for (old in map.values) {
                if (old.ip == contact.ip && old.port == contact.port && old.id != contact.id) {
                    if (contact.id.startsWith("ip:") && !old.id.startsWith("ip:")) {
                        // keep the known real contact; the re-add already
                        // cleared the removal marks above
                        return@update map
                    }
                    next = next - old.id
                }
            }
            next + (contact.id to contact)
        }
    }

    fun removeContact(id: String) {
        val c = _contacts.value[id]
        _contacts.update { it - id }
        // remember the removal: the peer's app keeps announcing (its
        // presence dials forever), and an incoming hello must not
        // resurrect the contact the user just deleted
        markRemoved(id, c?.takeIf { it.ip.isNotBlank() }?.let { "${it.ip}:${it.port}" })
    }

    /** Restore removal marks persisted by a previous process (entries past
     *  the TTL are dropped). Runs at startup, before [announceOnline]. */
    fun restoreRemovedMarks(ids: Map<String, Long>, endpoints: Map<String, Long>) {
        val now = System.currentTimeMillis()
        ids.forEach { (k, t) -> if (now - t < REMOVED_MARK_TTL_MS) removedIds[k] = t }
        endpoints.forEach { (k, t) -> if (now - t < REMOVED_MARK_TTL_MS) removedEndpoints[k] = t }
    }

    /** Snapshot of the removal marks (expired entries filtered), for
     *  persistence by the ViewModel. */
    fun removedMarks(): Pair<Map<String, Long>, Map<String, Long>> {
        val now = System.currentTimeMillis()
        fun fresh(m: Map<String, Long>) = m.filterValues { now - it < REMOVED_MARK_TTL_MS }
        return fresh(removedIds.toMap()) to fresh(removedEndpoints.toMap())
    }

    private fun markRemoved(id: String, endpoint: String?) {
        val now = System.currentTimeMillis()
        removedIds[id] = now
        if (endpoint != null) {
            removedEndpoints[endpoint] = now
            removedEndpointById[id] = endpoint
        }
        onRemovedMarksChanged?.invoke()
    }

    private fun unmark(id: String, endpoint: String?) {
        var changed = removedIds.remove(id) != null
        // clear the endpoint recorded WITH the id mark: the peer's address
        // may have changed between removal and re-add, so clearing only the
        // newly advertised endpoint would leave the stale one blocking the
        // peer's announces
        removedEndpointById.remove(id)?.let { linked ->
            if (removedEndpoints.remove(linked) != null) changed = true
        }
        if (endpoint != null && removedEndpoints.remove(endpoint) != null) changed = true
        if (changed) onRemovedMarksChanged?.invoke()
    }

    /** True when [peer] is a contact the local user removed (id match, or
     *  endpoint match for a contact removed under an "ip:..." placeholder
     *  whose real device id was never learned). */
    private fun isRemoved(peer: Peer): Boolean {
        val now = System.currentTimeMillis()
        if (removedIds[peer.id]?.let { now - it < REMOVED_MARK_TTL_MS } == true) return true
        if (peer.ipAddress.isNotEmpty()) {
            val ep = "${peer.ipAddress}:${peer.port}"
            if (removedEndpoints[ep]?.let { now - it < REMOVED_MARK_TTL_MS } == true) return true
        }
        return false
    }

    /** Park an incoming request in the message box. Deduped by device id AND
     *  by endpoint (a placeholder-era peer re-requesting from a new address
     *  must not stack two rows); only a NEW entry raises the user-facing
     *  event, so a peer's presence sweep re-dialing every minute cannot
     *  toast in a loop. */
    fun recordContactRequest(peer: Peer, fromRemoved: Boolean) {
        val entry = ContactRequest(
            id = peer.id,
            name = peer.name,
            ip = peer.ipAddress,
            port = peer.port,
            fromRemoved = fromRemoved,
            timestamp = System.currentTimeMillis()
        )
        fun sameBoxSlot(other: ContactRequest) =
            other.id == entry.id || (other.ip == entry.ip && other.port == entry.port)
        // CAS loop: the "is this NEW?" decision must be atomic with the box
        // update (two concurrent identical dials could otherwise both pass
        // the check and double-toast; the box itself would stay sane).
        while (true) {
            val current = _contactRequests.value
            val isNew = current.none(::sameBoxSlot)
            val next = (current.filterNot(::sameBoxSlot) + entry).takeLast(REQUEST_BOX_MAX)
            if (_contactRequests.compareAndSet(current, next)) {
                if (isNew) {
                    val suffix = if (fromRemoved) "（已移除的成员）" else ""
                    _events.tryEmit("${peer.name} 请求添加你为成员$suffix")
                }
                return
            }
        }
    }

    /** The user accepted a request: add the member (clearing any removal
     *  marks — acceptance is the explicit un-block) and announce right away;
     *  the presence sweep dials the fresh contact immediately (quiet). A
     *  failed dial just means the peer went offline — the sweep retries and
     *  both outboxes flush on the next session. */
    fun acceptContactRequest(requestId: String) {
        val req = _contactRequests.value.firstOrNull { it.id == requestId } ?: return
        _contactRequests.update { list -> list.filterNot { it.id == requestId } }
        addContact(Contact(req.id, req.name, req.ip, req.port))
        _events.tryEmit("已添加 ${req.name}")
        announceOnline()
    }

    /** The user ignored a request: drop the entry. The peer is never
     *  auto-added, so no removal mark is needed; if it insists, its next
     *  dial simply re-parks the (single, deduped) entry. */
    fun ignoreContactRequest(requestId: String) {
        _contactRequests.update { list -> list.filterNot { it.id == requestId } }
    }

    /** Restore the request box persisted by a previous process (startup).
     *  Deduped by id AND endpoint — the same slot semantics the live box
     *  uses — against BOTH the live rows and the saved list, so a stale
     *  saved row cannot sit beside its live replacement. */
    fun restoreContactRequests(saved: List<ContactRequest>) {
        if (saved.isEmpty()) return
        _contactRequests.update { list ->
            val live = list
            var kept = emptyList<ContactRequest>()
            for (r in saved) {
                val taken = live.any { it.id == r.id || (it.ip == r.ip && it.port == r.port) } ||
                    kept.any { it.id == r.id || (it.ip == r.ip && it.port == r.port) }
                if (!taken) kept = kept + r
            }
            (live + kept).takeLast(REQUEST_BOX_MAX)
        }
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
        // serialize dials per peer (see dialLocks); a concurrent dial may
        // have established the session while we waited for the lock
        val dialLock = dialLocks.computeIfAbsent(peer.id) { Any() }
        synchronized(dialLock) {
            sessions[peer.id]?.let { if (it.alive) return peer.id }
            return dialPeer(peer, quiet)
        }
    }

    /** The actual dial + secured handshake (runs under the peer's dial
     *  lock). Returns the member's REAL device id, or null on failure. */
    private fun dialPeer(peer: Peer, quiet: Boolean): String? {
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
            if (ack.type == Protocol.DIRECT_PENDING) {
                // The peer parked our request in its message box: this is
                // NOT a failure. Tell the user (loud dials) their request
                // awaits confirmation instead of a refused/unreachable
                // reason; the session comes up when they accept.
                if (!quiet) {
                    _events.tryEmit("已向 ${peer.name} 发送连接请求，等待对方在其设备上确认")
                }
                return null // finally closes the socket
            }
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
                    else -> if (e.message == "no direct_ack" || e.message == "bad direct_ack") {
                        // the handshake SUCCEEDED but the peer hung up
                        // instead of acking: the classic signature of a peer
                        // that dropped the hello on purpose (removed
                        // contact) — or died mid-handshake
                        "对方未接受连接（应用刚退出、不在同一网络，或已被对方移除）"
                    } else e.message ?: "未知错误"
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

    /** Listener side: a secured direct_hello arrived on the shared port.
     *  A KNOWN member (device id or endpoint) is auto-accepted — the
     *  handshake already authenticated the dialer's identity key. A FIRST
     *  CONTACT (unknown id AND endpoint) or a member the local user REMOVED
     *  is never silently dropped: the request is parked in the message box
     *  ([contactRequests]) for the user to accept or ignore, and the dialer
     *  is told via "direct_pending" so its side shows "waiting for
     *  confirmation" instead of a failure. */
    fun handleDirectHello(socket: Socket, wire: Wire, hello: NetworkPacket, peerIdent: String?) {
        val peer = hello.peer
        if (peer == null || peer.id == myId) {
            closeSocket(socket)
            return
        }

        // First contact / removed member -> the request box. Nothing here is
        // dropped silently: the box entry is visible (deduped, one row per
        // peer, one event per NEW entry) and the dialer gets a definitive
        // "pending" answer instead of a hung-up connection.
        val removed = isRemoved(peer)
        val known = _contacts.value.containsKey(peer.id) ||
            _contacts.value.any { (_, c) -> c.ip == peer.ipAddress && c.port == peer.port }
        if (removed || !known) {
            recordContactRequest(peer, fromRemoved = removed)
            runCatching { wire.sendPacket(NetworkPacket(type = Protocol.DIRECT_PENDING)) }
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

    // -------------------------------------------------------------- presence

    /**
     * The app is up (start, return to foreground, network change, contact
     * added): announce to every contact by dialing the ones without a live
     * session. The dial IS the notification — it rides the identity
     * handshake, so the peer flips us to online with no polling and both
     * sides' outboxes flush. Wakes the reconnect sweep immediately; between
     * events the sweep alone repairs dead sessions ([PRESENCE_SWEEP_MS]).
     */
    fun announceOnline() {
        if (myId.isEmpty()) return
        synchronized(presenceGuard) {
            if (!presenceStarted) {
                presenceStarted = true
                thread(isDaemon = true, name = "direct-presence") { presenceLoop() }
            }
        }
        presenceWake.release()
    }

    private fun presenceLoop() {
        while (true) {
            if (!presenceStarted) return  // resetForTest stopped the loop
            try {
                dialDeadContacts()
            } catch (e: Exception) {
                Log.w(TAG, "presence sweep failed", e)
            }
            try {
                // wake early on announce events; otherwise the
                // low-frequency fallback sweep
                presenceWake.tryAcquire(PRESENCE_SWEEP_MS, TimeUnit.MILLISECONDS)
            } catch (_: InterruptedException) {
                return
            }
            // coalesce piled-up wakes into one pass
            while (presenceWake.tryAcquire()) {
            }
        }
    }

    /** Dial every contact without a live session. Deterministic dialer:
     *  with real ids on both sides only the member with the SMALLER id
     *  dials, so a simultaneous announce from both apps cannot race into
     *  mismatched session pairs (same rule as the group mesh). A
     *  placeholder "ip:..." contact always dials — the other side does not
     *  know this device yet, so nobody else would establish the session. */
    private fun dialDeadContacts() {
        if (myId.isEmpty()) return
        for (contact in _contacts.value.values) {
            if (contact.ip.isBlank() || contact.port <= 0) continue
            if (sessions[contact.id]?.alive == true) continue
            if (!contact.id.startsWith("ip:") && myId >= contact.id) continue
            // a redial loop already hammers this peer for the outbox
            if (contact.id in redialLoops) continue
            if (!presenceDialing.add(contact.id)) continue
            thread(isDaemon = true, name = "direct-announce-${contact.id}") {
                try {
                    startChatSync(
                        Peer(contact.id, contact.name, contact.ip, contact.port),
                        quiet = true
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "presence dial to ${contact.id} failed", e)
                } finally {
                    presenceDialing.remove(contact.id)
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
        // The endpoint now belongs to [realId]: drop a manually added
        // "ip:<endpoint>" placeholder CONTACT for it. addContact's dedupe
        // cannot do this when the peer advertises a DIFFERENT local IP
        // (multi-homed / DHCP churn) — the placeholder then survived every
        // handshake as a duplicate member row that dialed forever.
        if (_contacts.value.containsKey("ip:$endpoint")) {
            _contacts.update { it - "ip:$endpoint" }
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
