package com.zqr.localchat.network

import android.util.Log
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.Peer
import com.zqr.localchat.data.withSanitizedExtras
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
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

    /** 每群 mesh peer 数量上限：mesh_announce 携带的 peer 是对端可控
     *  输入，无上限会让 peer 表（以及每 peer 一条的重试线程）无界增长。 */
    private const val MAX_PEERS_PER_GROUP = 64

    /** connectWithRetry 的最多尝试次数：被 announce 的地址可能永远不可达
     *  （或早已下线），无限重试只会永久烧掉线程与流量；退群/停止时循环
     *  也靠 state.connected 立即退出。 */
    private const val MAX_CONNECT_ATTEMPTS = 30

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

    /** A mesh-received delete: (groupId, messageId, senderId), or senderId
     *  = null for a tombstone-synced delete (a peer's history_reply
     *  deletedIds): trusted cleanup with no author validation. The ViewModel
     *  removes the message from the owning group's list so mesh deletes stay in sync
     *  with the relay/history path. Called on mesh worker threads. */
    @Volatile
    var onGroupDelete: ((String, String, String?) -> Unit)? = null

    /** A linked member's typing indicator changed (host-offline path):
     *  (groupId, senderId, active). Advisory; called on mesh worker threads. */
    @Volatile
    var onGroupTyping: ((String, String, Boolean) -> Unit)? = null

    /** Tombstoned message ids per group (offline-member delete convergence),
     *  carried by history_reply so a member that was offline drops what it
     *  missed. Set by the ViewModel (owns the database); empty when none. */
    @Volatile
    var deletedIdsProvider: ((groupId: String) -> List<String>)? = null

    /** The group owner (creator) device id per group: owner management packets
     *  (group_update / kick_member) are only accepted on a link when their
     *  senderId matches it. Unknown creator -> refuse (fail-closed). Set by the
     *  ViewModel (reads its persisted group info). */
    @Volatile
    var creatorIdProvider: ((groupId: String) -> String)? = null

    /** An owner management packet arrived over a mesh link (already validated
     *  against the creator id). The ViewModel applies the change: announcement
     *  / name, or the kicked member's teardown. Never re-forwarded (the mesh
     *  is a complete graph). */
    @Volatile
    var onGroupAdmin: ((String, NetworkPacket) -> Unit)? = null

    /** A member edited its own message (edit_message over a mesh link; the
     *  mesh layer validated the sender against the link AND the author):
     *  (groupId, messageId, newContent, senderId). Mesh worker threads. */
    @Volatile
    var onGroupEdit: ((String, String, String, String) -> Unit)? = null

    /** A member toggled an emoji reaction over a mesh link:
     *  (groupId, messageId, emoji, senderId, active). Mesh worker threads. */
    @Volatile
    var onGroupReaction: ((String, String, String, String, Boolean) -> Unit)? = null

    /** A member pinned/unpinned a message over a mesh link:
     *  (groupId, messageId, senderId, active). Mesh worker threads. */
    @Volatile
    var onGroupPin: ((String, String, String, Boolean) -> Unit)? = null

    /** A linked member reported reading the group up to [upToId] over a mesh
     *  link: (groupId, readerId, upToId). Mesh worker threads. */
    @Volatile
    var onGroupReadReceipt: ((String, String, String) -> Unit)? = null

    /** Shared single writer for the high-frequency advisory broadcasts
     *  (typing / admin relay): one daemon thread instead of one thread per
     *  packet. Sends are serialized, which socket writes tolerate. */
    private val sendExecutor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "mesh-send").apply { isDaemon = true }
    }

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
     *  path) so deletes converge even when the host relay is unreachable.
     *  The sender removes the message from its OWN mesh history too: a later
     *  history_reply (a peer reconnecting) must not resurrect it — same
     *  removal as the receiving side in [handleDeleteIncoming]. */
    fun broadcastDelete(groupId: String, messageId: String) {
        val state = groups[groupId] ?: return
        val myId = state.myPeer?.id ?: return
        synchronized(state) {
            state.messages.value = state.messages.value.filterNot { it.id == messageId }
        }
        val packet = GroupAuth.signPacket(
            NetworkPacket(type = "delete_message", messageId = messageId, senderId = myId),
            GroupAuth.deleteParts(groupId, myId, messageId)
        )
        val links = state.links.values.toList()
        thread(name = "mesh-delete") {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Tell every linked member that [senderId] started/stopped typing
     *  (host-offline path). Advisory: no history/state is touched, and a
     *  member with no link simply misses the indicator. */
    fun broadcastTyping(groupId: String, senderId: String, active: Boolean) {
        val state = groups[groupId] ?: return
        val packet = NetworkPacket(
            type = "typing",
            groupId = groupId,
            senderId = senderId,
            active = active
        )
        val links = state.links.values.toList()
        if (links.isEmpty()) return
        sendExecutor.execute {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Tell every linked member that [messageId]'s author replaced its
     *  content (host-offline path). The packet signature covers the edited
     *  body's message transcript: receivers verify it against their copies
     *  and store it, so this member's later history pushes carry the new
     *  text AND a signature that passes verifyMessage (LESSONS 2026-09-21).
     *  Unsigned (identity missing) edits are not broadcast. */
    fun broadcastEdit(groupId: String, messageId: String, newContent: String) {
        val state = groups[groupId] ?: return
        val myId = state.myPeer?.id ?: return
        var pub: String? = null
        var sig: String? = null
        synchronized(state) {
            val messages = state.messages.value
            val target = messages.firstOrNull { it.id == messageId }
            if (target != null && target.senderId == myId) {
                val signed = GroupAuth.signParts(
                    GroupAuth.messageParts(groupId, myId, messageId, target.timestamp, newContent)
                )
                if (signed != null) {
                    pub = signed.first
                    sig = signed.second
                    val edited = target
                        .copy(content = newContent, edited = true,
                              senderPubId = pub, senderSig = sig)
                    state.messages.value = messages.map {
                        if (it.id == messageId) edited else it
                    }
                }
            }
        }
        if (pub == null || sig == null) return
        val packet = NetworkPacket(
            type = "edit_message",
            groupId = groupId,
            messageId = messageId,
            senderId = myId,
            newContent = newContent,
            senderPubId = pub,
            senderSig = sig
        )
        val links = state.links.values.toList()
        sendExecutor.execute {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Toggle an emoji reaction over every mesh link. Advisory: members with
     *  no link at send time miss it (no offline queue). */
    fun broadcastReaction(groupId: String, messageId: String, emoji: String, active: Boolean) {
        val state = groups[groupId] ?: return
        val myId = state.myPeer?.id ?: return
        val packet = NetworkPacket(
            type = "reaction",
            groupId = groupId,
            messageId = messageId,
            senderId = myId,
            emoji = emoji,
            active = active
        )
        val links = state.links.values.toList()
        sendExecutor.execute {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Pin/unpin a message over every mesh link (host-offline path). */
    fun broadcastPin(groupId: String, messageId: String, active: Boolean) {
        val state = groups[groupId] ?: return
        val myId = state.myPeer?.id ?: return
        val packet = NetworkPacket(
            type = "pin_message",
            groupId = groupId,
            messageId = messageId,
            senderId = myId,
            active = active
        )
        val links = state.links.values.toList()
        sendExecutor.execute {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Tell every linked member we have read the group up to [upToId]
     *  (host-offline path). */
    fun broadcastReadReceipt(groupId: String, upToId: String) {
        val state = groups[groupId] ?: return
        val myId = state.myPeer?.id ?: return
        val packet = NetworkPacket(
            type = "read_receipt",
            groupId = groupId,
            upToId = upToId,
            readerId = myId
        )
        val links = state.links.values.toList()
        sendExecutor.execute {
            links.forEach { link ->
                runCatching { link.wire.sendPacket(packet) }
            }
        }
    }

    /** Apply a verified edit to this member's mesh history copy so a later
     *  history push carries the new text. [senderPubId]/[senderSig] are the
     *  edit packet's author fields: the signature must verify as the EDITED
     *  body's message transcript against this copy's identity fields (the
     *  packet key, or the key already stored on the copy when the caller did
     *  not carry it) — on success it is stored as the copy's signature, so
     *  pushed history passes verifyMessage and the edit converges to members
     *  that were offline (LESSONS 2026-09-21). Strict: a missing or invalid
     *  signature rejects the edit (an unsigned rewrite must never enter
     *  history). */
    fun updateMeshMessage(
        groupId: String,
        messageId: String,
        newContent: String,
        senderId: String,
        senderPubId: String? = null,
        senderSig: String? = null
    ): Boolean {
        val state = groups[groupId] ?: return false
        synchronized(state) {
            val messages = state.messages.value
            val target = messages.firstOrNull { it.id == messageId } ?: return false
            if (target.senderId != senderId) return false
            val pub = senderPubId ?: target.senderPubId
            if (pub == null || !GroupAuth.verifyMessageFields(
                    groupId, senderId, messageId, target.timestamp, newContent,
                    pub, senderSig
                )
            ) {
                return false
            }
            val replacement = target.copy(
                content = newContent, edited = true,
                senderPubId = pub, senderSig = senderSig
            )
            state.messages.value = messages.map {
                if (it.id == messageId) replacement else it
            }
        }
        return true
    }

    /** Relay an owner management packet over every mesh link. Called when a
     *  member received it on the host relay, so members whose own relay is down
     *  still see the owner's command. Receivers validate senderId against the
     *  creator and never forward again. */
    fun broadcastAdmin(groupId: String, packet: NetworkPacket) {
        val state = groups[groupId] ?: return
        val links = state.links.values.toList()
        sendExecutor.execute {
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
        sendDeletedIds(wire, groupId)
    }

    /** Offline-member delete convergence: a dedicated small packet carries
     *  the group's tombstoned ids (up to the cap — attaching them to a
     *  history batch could overflow its size budget). Sent after the batches
     *  and also when the history is empty (everything the peer missed may
     *  have been deleted while it was away). The receiver cleans up locally
     *  and never rebroadcasts (loop prevention). */
    private fun sendDeletedIds(wire: Wire, groupId: String) {
        val deletedIds = deletedIdsProvider?.invoke(groupId).orEmpty()
        if (deletedIds.isEmpty()) return
        runCatching {
            wire.sendPacket(NetworkPacket(type = "history_reply", groupId = groupId, deletedIds = deletedIds))
        }.onFailure { e ->
            Log.w(TAG, "history deletedIds send to $groupId failed (link likely dead)", e)
        }
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
        // endpoint 校验：mesh_announce 的 host:port 是对端可控的，非法
        // 值（空 host / 越界端口）直接忽略，不做任何拨号
        if (peer.ipAddress.isBlank() || peer.port !in 1..65535) {
            Log.w(TAG, "ignore peer ${peer.id} with invalid endpoint ${peer.ipAddress}:${peer.port}")
            return
        }
        // peer 数量上限（同 id 的更新/重连不占新名额）
        if (state.peers.size >= MAX_PEERS_PER_GROUP && !state.peers.containsKey(peer.id)) {
            Log.w(TAG, "peer cap ($MAX_PEERS_PER_GROUP) reached for $groupId, ignoring ${peer.id}")
            return
        }
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
        // 有界重试：最多 MAX_CONNECT_ATTEMPTS 次，防止对被 announce 的
        // 不可达地址无限拨号；state.connected=false（退群/停止）立即退出
        var attempts = 0
        while (state.connected && attempts < MAX_CONNECT_ATTEMPTS) {
            attempts++
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
            // 链路确实建立过：重置尝试计数（与 Windows 端一致），一个
            // 真实但网络不稳的成员不会因累计断连耗尽重拨预算
            attempts = 0
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
                            return@let
                        }
                        // author identity gate (TOFU) BEFORE the message is
                        // merged or any listener fires
                        if (!GroupAuth.verifyMessage(state.groupId, msg)) return@let
                        handleIncoming(state, P2PManager.markFromMe(msg.withSanitizedExtras(), state.myPeer?.id ?: ""))
                    }
                    "delete_message" -> {
                        // a delete arrives on the author's own link; the claimed
                        // sender is validated against the message's author and
                        // its device signature (TOFU) in handleDeleteIncoming
                        val id = packet.messageId
                        val sender = packet.senderId
                        if (id != null && sender != null) {
                            handleDeleteIncoming(state, id, sender, packet.senderPubId, packet.senderSig)
                        }
                    }
                    "history_reply" -> {
                        // 历史里合法包含多个发送者的消息，但每条仍须形状
                        // 合法：senderId 非空白且内容不超限（与 Windows 端
                        // 过滤一致），防止伪造 senderId 注入或超长内容入库；
                        // 每条还须通过作者设备签名校验（TOFU）才可入历史。
                        // 严格模式：验签不过的条目一律丢弃，即使是作者本人
                        // 链路推来的（项目未发布，无 legacy 容忍）。
                        val incoming = packet.messages.orEmpty()
                            .filter {
                                it.senderId.isNotBlank() && P2PManager.isValidContent(it.content) &&
                                    GroupAuth.verifyMessage(state.groupId, it)
                            }
                        // Same rule as the Windows client: a batch entry
                        // reusing a locally-known id with DIFFERENT content is
                        // a forged overwrite UNLESS the author pushes its OWN
                        // message over its OWN link (edit convergence).
                        applyHistoryBatch(state, link, incoming)
                        // tombstone sync: drop messages deleted while this
                        // member was offline (no author check — the link's
                        // password-bound handshake proved group membership).
                        // Sanitized first: one packet cannot make us do
                        // unbounded work or mint tombstones for junk ids.
                        P2PManager.sanitizeDeletedIds(packet.deletedIds).forEach { id ->
                            applyPeerDelete(state, id)
                        }
                    }
                    "mesh_announce" -> packet.peer?.let { peer ->
                        addPeer(state.groupId, peer)
                    }
                    "typing" -> {
                        // only the linked member may claim ITS OWN typing state
                        val sender = packet.senderId
                        val active = packet.active
                        if (sender == link.peerId && active != null) {
                            onGroupTyping?.invoke(state.groupId, sender, active)
                        }
                    }
                    "group_update", "kick_member" -> handleAdminIncoming(state, link, packet)
                    "edit_message" -> handleEditIncoming(state, link, packet)
                    "reaction" -> {
                        // only the linked member may react as itself; sanitize
                        // BEFORE the emptiness check so an all-control-char
                        // payload can never persist as an empty reaction key
                        // (Windows rejects it at decode)
                        val id = packet.messageId
                        val sender = packet.senderId
                        val emoji = com.zqr.localchat.data.sanitizeEmoji(packet.emoji)
                        if (id != null && sender != null && sender == link.peerId &&
                            emoji.isNotEmpty()
                        ) {
                            onGroupReaction?.invoke(
                                state.groupId, id, emoji, sender, packet.active == true
                            )
                        }
                    }
                    "pin_message" -> {
                        val id = packet.messageId
                        val sender = packet.senderId
                        if (id != null && sender != null && sender == link.peerId) {
                            onGroupPin?.invoke(state.groupId, id, sender, packet.active == true)
                        }
                    }
                    "read_receipt" -> {
                        val upTo = packet.upToId
                        val reader = packet.readerId
                        if (upTo != null && reader != null && reader == link.peerId) {
                            onGroupReadReceipt?.invoke(state.groupId, reader, upTo)
                        }
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

    /** Merge one history batch: brand-new ids flow into [handleIncoming];
     *  an id-colliding entry rewrites the local copy ONLY when the author
     *  pushes its OWN message over its OWN link (edit convergence — the
     *  strict rule mirrors the Windows client: everyone else's collision is
     *  a forged overwrite and is dropped). Entries already passed
     *  verifyMessage, so the collision rewrite adopts the entry's signature
     *  and this member's own pushes stay verifyMessage-valid. */
    private fun applyHistoryBatch(state: GroupState, link: Link, incoming: List<ChatMessage>) {
        if (incoming.isEmpty()) return
        val stillNew = ArrayList<ChatMessage>(incoming.size)
        val edits = ArrayList<ChatMessage>()
        synchronized(state) {
            val local = state.messages.value.associateBy { it.id }
            for (msg in incoming) {
                val existing = local[msg.id]
                when {
                    existing == null -> stillNew.add(msg)
                    msg.senderId == link.peerId &&
                        existing.senderId == msg.senderId &&
                        existing.content != msg.content -> {
                        // edit convergence: replace in the mesh state and
                        // report the edit so the ViewModel updates the row;
                        // the entry's (verified) signature is adopted
                        updateMeshMessage(
                            state.groupId, msg.id, msg.content, msg.senderId,
                            msg.senderPubId, msg.senderSig
                        )
                        edits.add(msg)
                    }
                    // identical copy: dedup by id; anything else: forged drop
                }
            }
        }
        if (stillNew.isNotEmpty()) {
            handleIncoming(
                state,
                stillNew.map { P2PManager.markFromMe(it.withSanitizedExtras(), state.myPeer?.id ?: "") }
            )
        }
        for (msg in edits) {
            onGroupEdit?.invoke(state.groupId, msg.id, msg.content, msg.senderId)
        }
    }

    /** Apply a mesh-received edit locally: update this member's mesh history
     *  copy and relay to the ViewModel. Only the linked member may edit as
     *  itself, only its own message (stricter than the mesh delete rule:
     *  content rewrites demand the author on the authoring link), and the
     *  author's device signature must verify (TOFU). No forwarding: the
     *  sender's broadcast already reached every link of the complete graph. */
    private fun handleEditIncoming(state: GroupState, link: Link, packet: NetworkPacket) {
        val id = packet.messageId
        val content = packet.newContent
        val sender = packet.senderId
        if (id == null || content == null || sender == null) return
        if (sender != link.peerId || !P2PManager.isValidContent(content)) {
            Log.w(TAG, "reject mesh edit $id: senderId=$sender on link ${link.peerId}")
            return
        }
        if (!updateMeshMessage(
                state.groupId, id, content, sender,
                packet.senderPubId, packet.senderSig
            )
        ) {
            Log.w(TAG, "reject mesh edit $id: unsigned, not verifiable, message not found or not authored by $sender")
            return
        }
        onGroupEdit?.invoke(state.groupId, id, content, sender)
    }

    /** Apply a mesh-received delete locally: remove the message from this
     *  group's mesh state and relay it to the ViewModel so the owning group's
     *  list + database follow. Only the original sender may delete (same
     *  authorization as the host relay) and its device signature must verify
     *  (TOFU); a duplicate delete for an already removed message is ignored.
     *  No forwarding: the mesh links every member pair directly (the sender's
     *  broadcast already reaches everyone), and relaying would only create a
     *  delete storm through the complete graph. */
    private fun handleDeleteIncoming(
        state: GroupState,
        messageId: String,
        senderId: String,
        senderPubId: String? = null,
        senderSig: String? = null
    ) {
        val target = state.messages.value.firstOrNull { it.id == messageId }
        if (target == null) return
        if (target.senderId != senderId) {
            Log.w(TAG, "reject delete_message $messageId: message senderId=${target.senderId} != claimed $senderId")
            return
        }
        if (!GroupAuth.verifyDelete(state.groupId, senderId, messageId, senderPubId, senderSig)) {
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

    /** Apply a delete learned from a peer's tombstone sync (history_reply
     *  deletedIds): remove the message from the mesh state and forward to the
     *  ViewModel (list + database + tombstone) via [onGroupDelete] with a
     *  null sender — trusted cleanup, no author validation. */
    private fun applyPeerDelete(state: GroupState, messageId: String) {
        synchronized(state) {
            val current = state.messages.value
            if (current.none { it.id == messageId }) return
            state.messages.value = current.filterNot { it.id == messageId }
        }
        val cb = onGroupDelete
        if (cb != null) {
            try {
                cb(state.groupId, messageId, null)
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

    /** Owner management packet over a mesh link: only the group creator may
     *  originate it (senderId must match the persisted creator id, and a
     *  signed packet must verify against the creatorId's remembered device
     *  key — TOFU), otherwise the link is dropped. Applied packets are handed
     *  to the ViewModel and never re-forwarded. */
    private fun handleAdminIncoming(state: GroupState, link: Link, packet: NetworkPacket) {
        val creator = creatorIdProvider?.invoke(state.groupId).orEmpty()
        if (creator.isEmpty() || packet.senderId != creator) {
            Log.w(
                TAG,
                "reject ${packet.type} on mesh link ${link.peerId}: senderId=${packet.senderId} is not the creator ($creator)"
            )
            link.alive = false
            closeSocket(link.socket)
            return
        }
        val verified = if (packet.type == "group_update") {
            GroupAuth.verifyGroupUpdate(
                state.groupId, creator, packet.groupName, packet.announcement,
                packet.senderPubId, packet.senderSig
            )
        } else {
            GroupAuth.verifyKick(
                state.groupId, creator, packet.targetId, packet.senderPubId, packet.senderSig
            )
        }
        if (!verified) {
            link.alive = false
            closeSocket(link.socket)
            return
        }
        if (packet.type == "kick_member") {
            val target = packet.targetId ?: return
            if (target != state.myPeer?.id) {
                state.peers.remove(target)
                state.links.remove(target)?.let { dead ->
                    dead.alive = false
                    closeSocket(dead.socket)
                }
                updateHasLinks(state.groupId)
            }
        }
        val cb = onGroupAdmin
        if (cb != null) {
            try {
                cb(state.groupId, packet)
            } catch (e: Exception) {
                Log.w(TAG, "onGroupAdmin failed", e)
            }
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
