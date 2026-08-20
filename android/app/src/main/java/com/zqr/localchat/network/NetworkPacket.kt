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
    val sig: String? = null
)
