package com.zqr.localchat.network

import com.zqr.localchat.data.CallInfo
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.data.Peer
import kotlinx.serialization.Serializable

@Serializable
data class GroupInfo(
    val groupName: String,
    val creatorName: String,
    val creatorId: String,
    val memberCount: Int
)

@Serializable
data class NetworkPacket(
    // Decode strictness policy (deliberate asymmetry with the Windows peer):
    // Android decodes leniently (nullable fields + ignoreUnknownKeys) and its
    // handlers ignore what is meaningless, while Windows fails closed — a
    // malformed packet aborts the decode and the connection. See AGENTS.md
    // §2/§5: compat applies to MISSING NEW FIELDS only, never to validation
    // failures; do not change either side without a protocol decision plus a
    // mixed-version E2E run.
    val type: String,
    val groupId: String? = null,
    val peer: Peer? = null,
    val members: List<Peer>? = null,
    val message: ChatMessage? = null,
    val messages: List<ChatMessage>? = null,
    val messageId: String? = null,
    /** Tombstoned message ids carried by a host's join_ack and a mesh
     *  history_reply (offline-member delete convergence): the receiver
     *  removes these messages locally and records tombstones, but never
     *  rebroadcasts (the sender already told everyone). Null is omitted on
     *  the wire (encodeDefaults=false); old peers ignore the unknown field. */
    val deletedIds: List<String>? = null,
    val groupInfo: GroupInfo? = null,
    val senderId: String? = null,
    val errorMessage: String? = null,
    val fileInfo: FileInfo? = null,
    val fileId: String? = null,
    val targetId: String? = null,
    val call: CallInfo? = null,
    /** The group's host (creator), returned by a member-sponsored join so the
     *  newcomer can connect to the host for the relay path. */
    val host: Peer? = null,
    /** Handshake: which kind of secured connection is being set up
     *  (query/join/mesh/direct). */
    val hsMode: String? = null,
    /** Handshake: Base64 ephemeral ECDH public key. */
    val eph: String? = null,
    /** Handshake: Base64 long-term identity public key (direct mode). */
    val ident: String? = null,
    /** Handshake: Base64 HMAC confirmation (password modes). */
    val mac: String? = null,
    /** Handshake: Base64 ECDSA signature over the transcript (direct mode). */
    val sig: String? = null,
    /** file_download request: Base64(HMAC-SHA256(fileKey, "lc-file-dl-v1:"
     *  + fileId)) — proves the downloader knows the per-file key (see
     *  [FileTransfer.downloadToken]). Mandatory on every request. */
    val token: String? = null,
    /** file_download request: bytes the receiver already holds in its ".part"
     *  staging file. Mandatory (resume contract): the sender seeks to this
     *  offset and streams the rest, so 0 is a fresh download and a value equal
     *  to the file size transfers nothing but the meta/EOF. The per-chunk GCM
     *  nonce is fresh and random on every encrypted chunk, so the offset does
     *  not change the wire layout in any way (Windows parity). */
    val offset: Long? = null,
    /** Wire-session sequence number: stamped by [Wire.sendPacket] (per
     *  direction, strictly 1,2,3,...) INSIDE the GCM-protected JSON, so the
     *  receiver can reject replayed/reordered/injected lines. Never set by
     *  application code; null is omitted on the wire. */
    val seq: Long? = null,
    /** read_receipt: the newest message id the sender has read in the
     *  [groupId] scope; [readerId] is the reader's device id (must match the
     *  packet's authenticated sender). Direct chats only — group chats do not
     *  track per-reader receipts (see README). Null is omitted. */
    val upToId: String? = null,
    val readerId: String? = null,
    /** typing: true while the sender is composing in the [groupId] scope,
     *  false once it stopped. Advisory only: receivers also expire an
     *  indicator that received no refresh (see README). Null is omitted. */
    val active: Boolean? = null,
    /** group_update (owner-only): a new display name and/or announcement.
     *  Both optional; null means "unchanged" and is omitted on the wire, so
     *  the bytes match the Windows peer's output. */
    val groupName: String? = null,
    val announcement: String? = null,
    /** edit_message: the author's replacement text for [messageId]. */
    val newContent: String? = null,
    /** reaction: the emoji toggled on/off for [messageId] by [senderId]
     *  (with [active]). Sanitized + length-capped by the receiving paths. */
    val emoji: String? = null,
    /** Group sender identity binding (TOFU, see [GroupAuth]) for packets that
     *  claim authorship WITHOUT a ChatMessage object (delete_message /
     *  edit_message / group_update / kick_member): the author's long-term
     *  identity public key (Base64 SPKI) plus the ECDSA signature over the
     *  packet's signing transcript. Null is omitted on the wire so plain
     *  packets stay byte-identical with older peers; chat/file messages
     *  carry the same pair inside [ChatMessage] instead. */
    val senderPubId: String? = null,
    val senderSig: String? = null
)
