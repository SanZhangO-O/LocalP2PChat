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
    /** Wire-session sequence number: stamped by [Wire.sendPacket] (per
     *  direction, strictly 1,2,3,...) INSIDE the GCM-protected JSON, so the
     *  receiver can reject replayed/reordered/injected lines. Never set by
     *  application code; null is omitted on the wire. */
    val seq: Long? = null,
    /** group_update (owner-only): a new display name and/or announcement.
     *  Both optional; null means "unchanged" and is omitted on the wire, so
     *  the bytes match the Windows peer's output. */
    val groupName: String? = null,
    val announcement: String? = null
)
