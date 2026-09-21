package com.zqr.localchat.network

import android.util.Log
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.ChatMessage

/**
 * Group sender identity binding (TOFU signatures) — Windows parity:
 * localchat/groupauth.py.
 *
 * Closes the group trust gap the README documents: group chat / delete / edit
 * previously only required holding the group password, so a leaked password
 * meant any member could forge ANY author. Group messages, delete tombstones,
 * edit rewrites and owner management packets (group_update / kick_member)
 * carry the author's device identity signature:
 *
 *   senderPubId  Base64 long-term identity public key (same key + encoding
 *                as the direct-mode handshake "ident" field)
 *   senderSig    Base64 ECDSA-SHA256 (DER) signature over the transcript hash
 *
 * Transcript (byte-identical to the Windows side; parts are length-prefixed
 * so "|" inside ids/content can never split them):
 *
 *   domain   = "lc-group-v1"
 *   part(p)  = utf8(p).size ":" p
 *   input    = domain + "|" + parts.joinToString("|", transform = ::part)
 *   digest   = sha256(utf8(input))
 *   sig      = Sign(deviceIdentityKey, digest)
 *
 *   message:    ["msg", groupId, senderId, messageId, timestamp.toString(),
 *                hex(sha256(utf8(content)))]
 *   delete:     ["del", groupId, senderId, messageId]
 *   edit rewrite: the edit_message senderSig covers the EDITED body's
 *             message transcript (the fields variant [messageParts], built
 *             from the receiver's copy identity + new content), so the
 *             verified signature can be stored on the mesh history copy and
 *             later history pushes pass [verifyMessage]
 *   groupUpdate:["gupd", groupId, senderId, groupName-or-"", announcement-or-""]
 *   kick:       ["kick", groupId, senderId, targetId]
 *
 * TOFU / upgrade policy (mirrors the seq upgrade; the key BINDING persists in
 * [DeviceIdentity], the "signatures required" flag is per process run):
 *
 *   - unsigned packet, sender never signed this run -> accept (old peer)
 *   - unsigned packet, sender already signed this run -> reject
 *   - invalid signature -> reject (never relax)
 *   - valid signature -> TOFU-bind on first sight; a LATER DIFFERENT key for
 *     the same sender is rejected (fingerprint mismatch = possible MITM)
 *
 * EDIT exception: edits rewrite stored history and are verified strictly via
 * [verifyMessageFields] — an unsigned edit is refused on every path (the
 * project is unreleased, no legacy tolerance).
 *
 * Callers must verify BEFORE merging state or firing listener/persistence
 * callbacks (LESSONS 2026-09-19 #3).
 */
object GroupAuth {

    private const val TAG = "GroupAuth"

    /** Transcript domain separator (Windows parity: GROUP_SIGN_DOMAIN). */
    const val GROUP_SIGN_DOMAIN = "lc-group-v1"

    /** Per-process-run enforcement state: composite sender keys that carried
     *  at least one VALID signature. Guarded by itself. */
    private val enforced = HashSet<String>()

    /** Test seam for the persistent TOFU binding: defaults to
     *  [DeviceIdentity.checkPeer]; JVM unit tests install an in-memory fake
     *  (DeviceIdentity needs an Android Context to persist). */
    internal var checkPeerBinding: (key: String, identB64: String) -> Boolean =
        { key, identB64 -> DeviceIdentity.checkPeer(key, identB64) }

    /** Composite DeviceIdentity key for a group member's device binding;
     *  cannot collide with direct-chat peer ids (UUIDs). */
    fun groupSenderKey(groupId: String, senderId: String): String =
        "group|$groupId|$senderId"

    private fun part(p: String): String {
        val bytes = p.toByteArray(Charsets.UTF_8)
        return "${bytes.size}:$p"
    }

    /** sha256 over the length-prefixed transcript (byte-parity with Python:
     *  lengths are UTF-8 byte counts, digest input is UTF-8). */
    fun transcriptHash(parts: List<String>): ByteArray {
        val payload = (listOf(GROUP_SIGN_DOMAIN) + parts.map { part(it) })
            .joinToString("|")
        return Crypto.sha256(payload.toByteArray(Charsets.UTF_8))
    }

    fun contentDigest(content: String): String =
        Crypto.hex(Crypto.sha256(content.toByteArray(Charsets.UTF_8)))

    fun messageParts(groupId: String, msg: ChatMessage): List<String> = messageParts(
        groupId, msg.senderId, msg.id, msg.timestamp, msg.content
    )

    /** Transcript parts for a message known only by its fields: an
     *  edit_message packet carries the author's signature over the EDITED
     *  body's message transcript (no ChatMessage object exists on the wire).
     *  Byte-parity with [messageParts]. */
    fun messageParts(
        groupId: String,
        senderId: String,
        messageId: String,
        timestamp: Long,
        content: String
    ): List<String> = listOf(
        "msg",
        groupId,
        senderId,
        messageId,
        timestamp.toString(),
        contentDigest(content)
    )

    fun deleteParts(groupId: String, senderId: String, messageId: String): List<String> =
        listOf("del", groupId, senderId, messageId)

    fun groupUpdateParts(
        groupId: String,
        senderId: String,
        groupName: String?,
        announcement: String?
    ): List<String> =
        listOf("gupd", groupId, senderId, groupName ?: "", announcement ?: "")

    fun kickParts(groupId: String, senderId: String, targetId: String): List<String> =
        listOf("kick", groupId, senderId, targetId)

    /** Sign a transcript with this device's identity key; null when the local
     *  identity is not initialized (legacy behavior: send unsigned). */
    fun signParts(parts: List<String>): Pair<String, String>? {
        val me = DeviceIdentity.current ?: return null
        return try {
            Pair(Crypto.encodePub(me.public), Crypto.toB64(Crypto.sign(me.private, transcriptHash(parts))))
        } catch (e: Exception) {
            Log.w(TAG, "group sign failed", e)
            null
        }
    }

    /** Attach the author signature to [msg] in place (Kotlin data class: the
     *  caller stores the returned copy). */
    fun signMessage(groupId: String, msg: ChatMessage): ChatMessage {
        val sig = signParts(messageParts(groupId, msg)) ?: return msg
        return msg.copy(senderPubId = sig.first, senderSig = sig.second)
    }

    /** Attach the author signature to a NetworkPacket that claims authorship
     *  without a ChatMessage. */
    fun signPacket(packet: NetworkPacket, parts: List<String>): NetworkPacket {
        val sig = signParts(parts) ?: return packet
        return packet.copy(senderPubId = sig.first, senderSig = sig.second)
    }

    private fun check(
        groupId: String,
        senderId: String,
        pubB64: String?,
        sigB64: String?,
        parts: List<String>
    ): Boolean {
        val key = groupSenderKey(groupId, senderId)
        if (pubB64.isNullOrBlank() || sigB64.isNullOrBlank()) {
            val mustSign = synchronized(enforced) { key in enforced }
            if (mustSign) {
                Log.w(
                    TAG,
                    "reject group packet from $senderId in $groupId: signature " +
                        "missing after the sender began signing"
                )
                return false
            }
            // legacy peer: unsigned is the norm until it signs once
            return true
        }
        val pub = try {
            Crypto.decodePub(pubB64)
        } catch (e: Exception) {
            Log.w(TAG, "reject group packet from $senderId: invalid senderPubId")
            return false
        }
        val sig = Crypto.fromB64(sigB64)
        if (sig == null || !Crypto.verify(pub, transcriptHash(parts), sig)) {
            Log.w(TAG, "reject group packet from $senderId in $groupId: invalid senderSig")
            return false
        }
        // TOFU: first valid signature binds the key; a later different key is
        // a possible MITM / identity theft and must never be accepted.
        if (!checkPeerBinding(key, pubB64)) {
            Log.w(
                TAG,
                "reject group packet from $senderId in $groupId: sender identity " +
                    "changed (TOFU fingerprint mismatch)"
            )
            return false
        }
        synchronized(enforced) { enforced.add(key) }
        return true
    }

    fun verifyMessage(groupId: String, msg: ChatMessage): Boolean =
        check(groupId, msg.senderId, msg.senderPubId, msg.senderSig, messageParts(groupId, msg))

    fun verifyDelete(
        groupId: String,
        senderId: String,
        messageId: String,
        pubB64: String?,
        sigB64: String?
    ): Boolean = check(groupId, senderId, pubB64, sigB64, deleteParts(groupId, senderId, messageId))

    /** [verifyMessage] for the signature carried by an edit_message packet:
     *  the receiver rebuilds the message transcript from its LOCAL copy's
     *  identity fields (id/timestamp/author — immutable) plus the edit's new
     *  content. Strict: an unsigned edit is refused here — an edit rewrites
     *  stored history and is never tolerated without a signature. */
    fun verifyMessageFields(
        groupId: String,
        senderId: String,
        messageId: String,
        timestamp: Long,
        content: String,
        pubB64: String?,
        sigB64: String?
    ): Boolean {
        if (pubB64.isNullOrBlank() || sigB64.isNullOrBlank()) return false
        return check(
            groupId, senderId, pubB64, sigB64,
            messageParts(groupId, senderId, messageId, timestamp, content)
        )
    }

    fun verifyGroupUpdate(
        groupId: String,
        senderId: String,
        groupName: String?,
        announcement: String?,
        pubB64: String?,
        sigB64: String?
    ): Boolean = check(
        groupId, senderId, pubB64, sigB64,
        groupUpdateParts(groupId, senderId, groupName, announcement)
    )

    fun verifyKick(
        groupId: String,
        senderId: String,
        targetId: String?,
        pubB64: String?,
        sigB64: String?
    ): Boolean = check(groupId, senderId, pubB64, sigB64, kickParts(groupId, senderId, targetId ?: ""))

    // ------------------------------------------------------------- TOFU UI

    /** True when the member has a TOFU-bound device identity in this group. */
    fun memberVerified(groupId: String?, senderId: String): Boolean {
        if (groupId.isNullOrEmpty() || senderId.isBlank()) return false
        return DeviceIdentity.hasPeer(groupSenderKey(groupId, senderId))
    }

    /** The bound member's 安全码 (null when unbound). */
    fun memberFingerprint(groupId: String?, senderId: String): String? {
        if (groupId.isNullOrEmpty() || senderId.isBlank()) return null
        val ident = DeviceIdentity.peerIdent(groupSenderKey(groupId, senderId)) ?: return null
        return DeviceIdentity.peerFingerprint(ident)
    }

    /** Test-only: clear the process-run enforcement set. */
    fun resetEnforcementForTests() {
        synchronized(enforced) { enforced.clear() }
    }
}
