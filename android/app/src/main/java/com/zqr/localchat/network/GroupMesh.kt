package com.zqr.localchat.network

import android.util.Log
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.Peer
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import kotlin.concurrent.thread

/**
 * Group mesh: direct member-to-member links inside a group, so members can
 * keep chatting when the host is offline, and each member auto-backfills the
 * messages they missed while away.
 *
 * A member links to every OTHER member of the group over the shared listener
 * (mesh_hello / mesh_ack, auto-accepted like direct chats). Group messages are
 * broadcast over all links (plus the host relay when it is up; receivers
 * dedup by message id). When a link is established, both sides push their
 * stored history for the group (capped), so a member coming online learns
 * what happened while they were away. The host itself is NOT meshed — it
 * relays to everyone, so linking members to it would be redundant.
 *
 * All socket I/O runs on worker threads; incoming messages are handed to the
 * ViewModel via [onGroupMessage] so they flow into the same message pipeline
 * as host-relayed messages.
 */
object GroupMeshManager {

    private const val TAG = "GroupMesh"
    private const val CONNECT_TIMEOUT_MS = 8_000
    private const val READ_TIMEOUT_MS = 45_000
    private const val PING_INTERVAL_MS = 15_000L
    private const val RETRY_INTERVAL_MS = 10_000L
    private const val HISTORY_CAP = 500

    /** History is pushed in batches whose encoded line stays safely under
     *  P2PManager.MAX_LINE_LENGTH — one 500-message packet would exceed the
     *  read cap and drop the link. The cap accounts for the AES-GCM + Base64
     *  expansion of an encrypted line (~1.4x). */
    private const val HISTORY_CHUNK_BYTES = 36 * 1024

    private class Link(
        val peerId: String,
        val socket: Socket,
        val wire: Wire
    ) {
        @Volatile
        var alive = true
    }

    private class GroupState(
        val groupId: String
    ) {
        @Volatile
        var connected = true // false once the ViewModel left the group
        @Volatile
        var password: String = ""
        val peers = ConcurrentHashMap<String, Peer>()
        val links = ConcurrentHashMap<String, Link>()
        val messages = MutableStateFlow<List<ChatMessage>>(emptyList())
        @Volatile
        var myPeer: Peer? = null
        val connectLocks = ConcurrentHashMap<String, Any>()
    }

    private val groups = ConcurrentHashMap<String, GroupState>()
    private val hasLinksStates = ConcurrentHashMap<String, MutableStateFlow<Boolean>>()

    /** Incoming group messages (mesh or history) routed by the ViewModel into
     *  the owning P2PManager's message list. Called on mesh worker threads;
     *  a whole history batch is delivered in ONE call so the UI and the
     *  database update once instead of once per message. */
    @Volatile
    var onGroupMessage: ((String, List<ChatMessage>) -> Unit)? = null

    /** A mesh-received delete: (groupId, messageId, senderId). The ViewModel
     *  removes the message from the owning group's list so mesh deletes stay
     *  in sync with the relay/history path. Called on mesh worker threads. */
    @Volatile
    var onGroupDelete: ((String, String, String) -> Unit)? = null

    // ------------------------------------------------------------ lifecycle

    /** The ViewModel seeds a group's mesh state and connects to every other
     *  member. [history] is the persisted history for the group; [password]
     *  authenticates mesh handshakes (only members who know the group
     *  password can link and read history). */
    fun enterGroup(
        groupId: String,
        myPeer: Peer,
        peers: List<Peer>,
        history: List<ChatMessage>,
        password: String = ""
    ) {
        val state = groups.getOrPut(groupId) { GroupState(groupId) }
        synchronized(state) {
            state.connected = true
            state.password = password
            state.myPeer = myPeer
            state.messages.value = history.sortedBy { it.timestamp }
        }
        peers.filter { it.id != myPeer.id && it.id.isNotEmpty() }.forEach { peer ->
            addPeer(groupId, peer)
        }
    }

    /** Leave a group: close every link; retry threads exit. */
    fun leaveGroup(groupId: String) {
        val state = groups.remove(groupId) ?: return
        state.connected = false
        state.links.values.forEach { it.alive = false }
        state.links.values.forEach { closeSocket(it.socket) }
        state.links.clear()
        hasLinksStates.remove(groupId)?.value = false
    }

    /** App teardown: leave every group. */
    fun shutdown() {
        groups.keys.toList().forEach { leaveGroup(it) }
    }

    /** Keep the mesh peer list in sync with what the group reports. */
    fun syncPeers(groupId: String, peers: Collection<Peer>) {
        val state = groups[groupId] ?: return
        val mine = state.myPeer?.id
        peers.filter { it.id != mine && it.id.isNotEmpty() }.forEach { addPeer(groupId, it) }
        // drop peers that left the group
        val keep = peers.map { it.id }.toSet() + mine
        state.peers.keys.filter { it !in keep }.forEach { pid ->
            state.peers.remove(pid)
            state.links.remove(pid)?.let { l ->
                l.alive = false
                closeSocket(l.socket)
            }
        }
    }

    /** The group's password, or null when this device is not in the group —
     *  consumed by the shared listener's mesh-handshake password lookup. */
    fun passwordFor(groupId: String): String? = groups[groupId]?.password

    fun hasLinks(groupId: String): Boolean =
        groups[groupId]?.links?.isNotEmpty() == true

    fun hasLinksFlow(groupId: String): StateFlow<Boolean> =
        hasLinksStates.getOrPut(groupId) { MutableStateFlow(false) }

    private fun updateHasLinks(groupId: String) {
        hasLinksStates[groupId]?.value = groups[groupId]?.links?.isNotEmpty() == true
    }

    // --------------------------------------------------------------- sending

    /** Broadcast a message to every mesh link (the host path is separate).
     *  The writes run on a background thread — this is called from the UI
     *  thread when the user sends a message. */
    fun broadcast(groupId: String, msg: ChatMessage) {
        val state = groups[groupId] ?: return
        noteMessage(groupId, msg)
        val packet = NetworkPacket(type = "mesh_chat", groupId = groupId, message = msg)
        val links = state.links.values.toList()
        thread(name = "mesh-broadcast") {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Tell every linked member that a message was deleted (host-offline
     *  path) so deletes converge even when the host relay is unreachable. */
    fun broadcastDelete(groupId: String, messageId: String) {
        val state = groups[groupId] ?: return
        val myId = state.myPeer?.id ?: return
        val packet = NetworkPacket(type = "delete_message", messageId = messageId, senderId = myId)
        val links = state.links.values.toList()
        thread(name = "mesh-delete") {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Tell every linked member that [peer] joined the group, so each one
     *  links up with it (used when a member sponsors a join). */
    fun announcePeer(groupId: String, peer: Peer) {
        val state = groups[groupId] ?: return
        addPeer(groupId, peer)
        val packet = NetworkPacket(type = "mesh_announce", groupId = groupId, peer = peer)
        val links = state.links.values.toList()
        thread(name = "mesh-announce") {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Record a locally sent message so it can be shared as history later. */
    fun noteMessage(groupId: String, msg: ChatMessage) {
        val state = groups[groupId] ?: return
        synchronized(state) {
            state.messages.value = mergeMessages(state.messages.value, listOf(msg))
        }
    }

    /** Push history in size-capped batches (see HISTORY_CHUNK_BYTES): the
     *  receiver reads with a bounded line reader, so each packet must stay
     *  under the cap or the link would be dropped. Receivers merge each batch
     *  independently and dedup by message id. */
    private fun sendHistory(wire: Wire, groupId: String, history: List<ChatMessage>) {
        val batch = ArrayList<ChatMessage>(16)
        var estimated = 0
        fun flush() {
            if (batch.isEmpty()) return
            runCatching {
                wire.sendPacket(NetworkPacket(type = "history_reply", groupId = groupId, messages = batch.toList()))
            }.onFailure { e ->
                Log.w(TAG, "history send to $groupId failed (link likely dead)", e)
            }
            batch.clear()
            estimated = 0
        }
        for (msg in history) {
            // Upper bound per message: UTF-8 worst case (4 bytes/char), JSON
            // escaping, the packet envelope, and the AES-GCM + Base64
            // expansion of the encrypted line — stays under the line cap for
            // any realistic content.
            val size = msg.content.length * 4 + 1024
            if (estimated > 0 && estimated + size > HISTORY_CHUNK_BYTES) flush()
            batch.add(msg)
            estimated += size
        }
        flush()
    }

    // ------------------------------------------------------------- listeners

    /** Listener side: a member wants to mesh-link with us. The link arrived
     *  through the shared listener's password-bound handshake, so the caller
     *  already proved it knows the group password (mesh links can read the
     *  group's whole history). */
    fun handleMeshHello(socket: Socket, wire: Wire, hello: NetworkPacket) {
        val groupId = hello.groupId ?: run { closeSocket(socket); return }
        val peer = hello.peer ?: run { closeSocket(socket); return }
        val state = groups[groupId] ?: run { closeSocket(socket); return }
        val my = state.myPeer ?: run { closeSocket(socket); return }
        wire.sendPacket(NetworkPacket(type = "mesh_ack", groupId = groupId, peer = my))
        registerLink(state, peer, socket, wire)
    }

    // ------------------------------------------------------------- internals

    private fun addPeer(groupId: String, peer: Peer) {
        val state = groups[groupId] ?: return
        val mine = state.myPeer?.id
        if (peer.id == mine || peer.id.isEmpty()) return
        state.peers[peer.id] = peer
        // Deterministic linking: only the member with the smaller id connects,
        // the larger one accepts — a simultaneous connect for the same pair
        // could otherwise leave a mismatched socket pair and drop messages.
        if (mine == null || mine >= peer.id) return
        if (state.links[peer.id]?.alive == true) return
        if (state.connectLocks.putIfAbsent(peer.id, Any()) != null) return
        thread(name = "mesh-connect-$groupId-${peer.id}") {
            try {
                connectWithRetry(state, peer)
            } finally {
                state.connectLocks.remove(peer.id)
            }
        }
    }

    private fun connectWithRetry(state: GroupState, peer: Peer) {
        while (state.connected) {
            if (state.links[peer.id]?.alive == true) return
            val link = tryConnect(state, peer)
            if (link == null) {
                if (!state.connected) return
                try { Thread.sleep(RETRY_INTERVAL_MS) } catch (e: InterruptedException) { return }
                continue
            }
            // Atomically install the new link: only a LIVE existing link
            // makes us drop ours — a dead/stale entry must be replaced, not
            // block the fresh connection (otherwise a member could never
            // re-link after its old link died).
            var installed = false
            while (!installed) {
                val existing = state.links[peer.id]
                when {
                    existing != null && existing.alive -> {
                        link.alive = false
                        closeSocket(link.socket)
                        return
                    }
                    existing == null -> {
                        installed = state.links.putIfAbsent(peer.id, link) == null
                    }
                    else -> {
                        installed = state.links.replace(peer.id, existing, link)
                        if (installed) {
                            existing.alive = false
                            closeSocket(existing.socket)
                        }
                    }
                }
            }
            updateHasLinks(state.groupId)
            // push our history so the peer backfills what it missed (both
            // sides push; receivers dedup by id)
            val history = state.messages.value.takeLast(HISTORY_CAP)
            if (history.isNotEmpty()) {
                sendHistory(link.wire, state.groupId, history)
            }
            runLinkLoop(state, link)
            state.links.remove(peer.id, link)
            updateHasLinks(state.groupId)
            if (!state.connected) return
            try { Thread.sleep(RETRY_INTERVAL_MS) } catch (e: InterruptedException) { return }
        }
    }

    private fun tryConnect(state: GroupState, peer: Peer): Link? {
        var sock: Socket? = null
        return try {
            sock = Socket()
            sock!!.tcpNoDelay = true
            sock.connect(InetSocketAddress(peer.ipAddress, peer.port), CONNECT_TIMEOUT_MS)
            sock.soTimeout = CONNECT_TIMEOUT_MS
            // password-bound secured handshake: mesh traffic (chat + history)
            // is encrypted, and both sides prove group membership
            val wire = Wire(
                BufferedReaderLineIn(BufferedReader(InputStreamReader(sock.getInputStream()))),
                PrintWriter(sock.getOutputStream(), true)
            )
            Handshake.initiate(wire, Protocol.MODE_MESH, state.groupId, state.password)
            wire.sendPacket(
                // no password field: the password-bound handshake already
                // proved membership in both directions
                NetworkPacket(
                    type = "mesh_hello",
                    groupId = state.groupId,
                    peer = state.myPeer
                )
            )
            val ack = wire.recvPacket() ?: throw IllegalStateException("no mesh_ack")
            if (ack.type != "mesh_ack" || ack.peer == null) {
                throw IllegalStateException("bad mesh_ack")
            }
            val established = Link(peer.id, sock!!, wire)
            sock = null // ownership transferred to the Link; do not close below
            established
        } catch (e: Exception) {
            Log.w(TAG, "mesh connect to ${peer.ipAddress}:${peer.port} failed", e)
            null
        } finally {
            // any failure path (connect, handshake, bad ack, timeout) must
            // close the socket: the retry loop runs forever, so a leak here
            // burns one FD per 10-second retry
            if (sock != null) closeSocket(sock)
        }
    }

    /** Register a freshly established link (either direction) and exchange
     *  history so both sides backfill what they missed. */
    private fun registerLink(state: GroupState, peer: Peer, socket: Socket, wire: Wire) {
        val link = Link(peer.id, socket, wire)
        state.peers[peer.id] = peer
        // Atomically install: replace a dead/stale link, reject only when a
        // live link already exists. putIfAbsent-alone would leave a NEW link
        // orphaned (its thread running, but invisible to hasLinks()) when a
        // dead entry sat in the map.
        var installed = false
        while (!installed) {
            val existing = state.links[peer.id]
            when {
                existing != null && existing.alive -> {
                    link.alive = false
                    closeSocket(socket)
                    return
                }
                existing == null -> {
                    installed = state.links.putIfAbsent(peer.id, link) == null
                }
                else -> {
                    installed = state.links.replace(peer.id, existing, link)
                    if (installed) {
                        existing.alive = false
                        closeSocket(existing.socket)
                    }
                }
            }
        }
        updateHasLinks(state.groupId)
        // push our history for the group (the peer pushes theirs back)
        val history = state.messages.value.takeLast(HISTORY_CAP)
        if (history.isNotEmpty()) {
            sendHistory(wire, state.groupId, history)
        }
        thread(name = "mesh-read-$state.groupId-${peer.id}") { runLinkLoop(state, link) }
        thread(name = "mesh-ping-$state.groupId-${peer.id}") { pingLoop(link) }
    }

    private fun runLinkLoop(state: GroupState, link: Link) {
        try {
            link.socket.soTimeout = READ_TIMEOUT_MS
            while (link.alive && state.connected && !link.socket.isClosed) {
                val packet = link.wire.recvPacket() ?: break
                when (packet.type) {
                    "mesh_chat", "file_message" -> packet.message?.let { msg ->
                        // only the linked member's own messages travel this
                        // path; a file offer is a message carrying FileInfo and
                        // gets the same sender validation as plain chat
                        if (msg.senderId != link.peerId || !P2PManager.isValidContent(msg.content)) {
                            Log.w(TAG, "drop ${packet.type} on link ${link.peerId}: senderId=${msg.senderId} len=${msg.content.length}")
                        } else {
                            handleIncoming(state, P2PManager.markFromMe(msg, state.myPeer?.id ?: ""))
                        }
                    }
                    "delete_message" -> {
                        // a delete arrives on the author's own link; the claimed
                        // sender is validated against the message's author in
                        // handleDeleteIncoming
                        val id = packet.messageId
                        val sender = packet.senderId
                        if (id != null && sender != null) {
                            handleDeleteIncoming(state, id, sender)
                        }
                    }
                    "history_reply" -> {
                        val incoming = packet.messages.orEmpty().map { P2PManager.markFromMe(it, state.myPeer?.id ?: "") }
                        handleIncoming(state, incoming)
                    }
                    "mesh_announce" -> packet.peer?.let { peer ->
                        addPeer(state.groupId, peer)
                    }
                    "ping" -> runCatching { link.wire.sendPacket(NetworkPacket(type = "pong")) }
                    "pong" -> {}
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "mesh read loop ended", e)
        } finally {
            link.alive = false
            state.links.remove(link.peerId, link)
            updateHasLinks(state.groupId)
            closeSocket(link.socket)
        }
    }

    /** Apply a mesh-received delete locally: remove the message from this
     *  group's mesh state and relay it to the ViewModel so the owning group's
     *  list + database follow. Only the original sender may delete (same
     *  authorization as the host relay); a duplicate delete for an already
     *  removed message is ignored. No forwarding: the mesh links every member
     *  pair directly (the sender's broadcast already reaches everyone), and
     *  relaying would only create a delete storm through the complete graph. */
    private fun handleDeleteIncoming(state: GroupState, messageId: String, senderId: String) {
        val target = state.messages.value.firstOrNull { it.id == messageId }
        if (target == null) return
        if (target.senderId != senderId) {
            Log.w(TAG, "reject delete_message $messageId: message senderId=${target.senderId} != claimed $senderId")
            return
        }
        synchronized(state) {
            state.messages.value = state.messages.value.filterNot { it.id == messageId }
        }
        val cb = onGroupDelete
        if (cb != null) {
            try {
                cb(state.groupId, messageId, senderId)
            } catch (e: Exception) {
                Log.w(TAG, "onGroupDelete failed", e)
            }
        }
    }

    /** Merge incoming messages into the group state and forward the new ones
     *  to the ViewModel in ONE batch (so UI + persistence update once). */
    private fun handleIncoming(state: GroupState, incoming: List<ChatMessage>) {
        val newOnes: List<ChatMessage>
        synchronized(state) {
            val current = state.messages.value
            val ids = current.mapTo(HashSet()) { it.id }
            newOnes = incoming.filter { it.id !in ids }
            if (newOnes.isNotEmpty()) {
                state.messages.value = (current + newOnes).sortedBy { it.timestamp }
            }
        }
        if (newOnes.isEmpty()) return
        val cb = onGroupMessage
        if (cb != null) {
            try {
                cb(state.groupId, newOnes)
            } catch (e: Exception) {
                Log.w(TAG, "onGroupMessage failed", e)
            }
        }
    }

    private fun handleIncoming(state: GroupState, msg: ChatMessage) =
        handleIncoming(state, listOf(msg))

    private fun pingLoop(link: Link) {
        try {
            while (link.alive) {
                Thread.sleep(PING_INTERVAL_MS)
                if (!link.alive) break
                runCatching { link.wire.sendPacket(NetworkPacket(type = "ping")) }
            }
        } catch (e: InterruptedException) {
            // closing
        }
    }

    private fun mergeMessages(current: List<ChatMessage>, incoming: List<ChatMessage>): List<ChatMessage> {
        val ids = current.mapTo(HashSet()) { it.id }
        val fresh = incoming.filter { it.id !in ids }
        return (current + fresh).sortedBy { it.timestamp }
    }

    private fun closeSocket(socket: Socket) {
        runCatching { socket.close() }
    }
}
