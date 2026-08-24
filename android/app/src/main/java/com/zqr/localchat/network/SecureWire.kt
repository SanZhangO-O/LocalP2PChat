package com.zqr.localchat.network

import android.content.Context
import android.util.Log
import com.zqr.localchat.crypto.Crypto
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.BufferedReader
import java.io.InputStream
import java.io.PrintWriter
import java.security.KeyPair

/**
 * Wire-protocol security layer.
 *
 * Every TCP connection (group join/query, host relay, mesh link, direct chat,
 * call media) starts with a handshake of PLAINTEXT JSON lines:
 *
 *   password modes (query/join/mesh):
 *     C -> S: hs_start {hsMode, groupId, eph}
 *     S -> C: hs_ack  {eph}
 *     C -> S: hs_confirm {mac}        (omitted when no password is known)
 *     S -> C: hs_ok   {mac} | hs_reject {errorMessage}
 *   direct mode (identity-based, used by direct chats and call media):
 *     C -> S: hs_start  {hsMode="direct", eph, ident}
 *     S -> C: hs_ack    {eph, ident, sig}
 *     C -> S: hs_confirm {sig}
 *
 * After a successful handshake EVERY subsequent line on the connection is
 * Base64(nonce || AES-256-GCM(json)) instead of plaintext JSON. Legacy
 * plaintext packets are rejected (no downgrade).
 *
 * Key derivation (password modes):
 *   transcript  = mode|groupId|ephClient|ephServer
 *   salt        = sha256(transcript)
 *   pwKey       = PBKDF2-SHA1(password, salt, 210k, 32)
 *   sessionKey  = HKDF(ECDH(ephC, ephS) ++ pwKey, salt, "localchat-session-v1")
 *   clientMac   = HMAC(pwKey, "lc-client|transcript")
 *   serverMac   = HMAC(pwKey, "lc-server|transcript")
 * The MACs authenticate BOTH endpoints' knowledge of the group password and
 * bind the ephemeral keys to it: a passive sniffer cannot decrypt (ECDH) or
 * verify password guesses cheaply (PBKDF2); an active MITM cannot substitute
 * its own ephemerals without failing the MACs.
 *
 * Direct mode:
 *   transcriptHash = sha256("lc-direct-v1|ephClient|ephServer")
 *   sig            = Sign(identityKey, transcriptHash)  (both sides)
 *   sessionKey     = HKDF(ECDH(ephC, ephS), sha256(transcript), "lc-direct-v1")
 * Long-term EC identity keys are persisted per device (see [DeviceIdentity]);
 * peers' keys are remembered on first contact (TOFU) and a later change is
 * treated as a possible MITM and rejected. Users can additionally compare the
 * short fingerprints ("安全码") shown in the settings screen.
 */
object Protocol {
    const val HS_START = "hs_start"
    const val HS_ACK = "hs_ack"
    const val HS_CONFIRM = "hs_confirm"
    const val HS_OK = "hs_ok"
    const val HS_REJECT = "hs_reject"

    const val MODE_QUERY = "query"
    const val MODE_JOIN = "join"
    const val MODE_MESH = "mesh"
    const val MODE_DIRECT = "direct"

    /** Direct chats / call media: the inner packet identifying the dialer. */
    const val DIRECT_HELLO = "direct_hello"
    const val DIRECT_ACK = "direct_ack"

    /** The listener parked the dialer's request in its contact-request
     *  message box instead of accepting: the dialer surfaces "waiting for
     *  confirmation" instead of a failure. */
    const val DIRECT_PENDING = "direct_pending"
}

class WireException(message: String, cause: Throwable? = null) : Exception(message, cause)

/** Line input abstraction: buffered for line-only sockets, raw (no
 *  read-ahead) for sockets that switch to binary framing after the handshake
 *  (call media) — a BufferedReader there would swallow the first frames. */
fun interface LineIn {
    fun readLine(): String?
}

class BufferedReaderLineIn(private val reader: BufferedReader) : LineIn {
    override fun readLine(): String? = P2PManager.readLineLimited(reader)
}

class RawLineIn(private val input: InputStream) : LineIn {
    override fun readLine(): String? {
        val buffer = StringBuilder(128)
        try {
            while (buffer.length <= P2PManager.MAX_LINE_LENGTH) {
                val b = input.read()
                if (b == -1) return if (buffer.isEmpty()) null else buffer.toString()
                if (b == '\n'.code) return buffer.toString().removeSuffix("\r")
                buffer.append(b.toChar())
            }
        } catch (e: Exception) {
            return null
        }
        return null
    }
}

/**
 * One TCP connection: plaintext handshake lines first, then authenticated
 * encryption of every packet line. All methods may be called from any thread;
 * the underlying PrintWriter serializes whole-line writes.
 */
class Wire(val lineIn: LineIn, val writer: PrintWriter) {

    // Volatile: activate() runs on the handshake thread while senders on
    // other threads (heartbeat, broadcast, call signaling) read it — without
    // the guarantee a sender could see a stale null and drop early packets.
    @Volatile
    private var key: ByteArray? = null

    fun activate(sessionKey: ByteArray) {
        key = sessionKey
    }

    val sessionKey: ByteArray?
        get() = key

    val isSecure: Boolean
        get() = key != null

    fun sendPacket(packet: NetworkPacket) {
        val k = key ?: throw WireException("wire not secured yet")
        val json = wireJson.encodeToString(packet)
        val line = Crypto.toB64(Crypto.aesGcmEncrypt(k, json.toByteArray(Charsets.UTF_8)))
        if (line.length > P2PManager.MAX_LINE_LENGTH) throw WireException("encrypted line exceeds cap")
        writer.println(line)
        writer.flush()
    }

    /** Decrypted packet, or null at stream end. Throws [WireException] on
     *  tampering / wrong key — callers must treat that as a dead connection. */
    fun recvPacket(): NetworkPacket? {
        val line = lineIn.readLine() ?: return null
        if (line.isEmpty()) return null
        val k = key ?: throw WireException("wire not secured yet")
        val blob = Crypto.fromB64(line) ?: throw WireException("malformed encrypted line")
        val plain = try {
            Crypto.aesGcmDecrypt(k, blob)
        } catch (e: Exception) {
            throw WireException("decrypt failed (tampered or wrong key)", e)
        }
        return runCatching { wireJson.decodeFromString<NetworkPacket>(plain.toString(Charsets.UTF_8)) }
            .getOrElse { throw WireException("malformed packet JSON", it) }
    }

    // ---- handshake-phase plaintext IO (never used once activate() ran) ----

    fun sendRaw(packet: NetworkPacket) {
        writer.println(wireJson.encodeToString(packet))
        writer.flush()
    }

    fun sendRawReject(message: String) {
        sendRaw(NetworkPacket(type = Protocol.HS_REJECT, errorMessage = message))
    }

    fun recvRaw(): NetworkPacket? {
        val line = lineIn.readLine() ?: return null
        return runCatching { wireJson.decodeFromString<NetworkPacket>(line) }.getOrNull()
    }

    companion object {
        private val wireJson = Json { ignoreUnknownKeys = true }
    }
}

/** Result of a completed handshake: the secured wire plus dispatch info. */
class SecuredWire(
    val wire: Wire,
    val mode: String,
    val groupId: String?,
    /** Direct mode only: the peer's long-term identity public key (Base64). */
    val peerIdent: String?
)

object Handshake {

    private const val TAG = "Handshake"

    /** PBKDF2 cost for the password binding (~200ms on a mid-range phone). */
    const val PBKDF2_ITERATIONS = 210_000

    private const val INFO_SESSION = "localchat-session-v1"
    private const val INFO_DIRECT = "lc-direct-v1"

    // -------------------------------------------------- password-mode client

    /**
     * Client side of the query/join/mesh handshake. The confirm exchange is
     * UNCONDITIONAL (even with an empty password the client sends a MAC
     * derived from ""): a deterministic message flow means a client without
     * the password gets a clean "群组密码错误" rejection instead of a
     * deadlock, and there is no downgrade to an unauthenticated variant.
     * Throws [WireException] on any failure — callers close the socket and
     * surface the message.
     */
    fun initiate(wire: Wire, mode: String, groupId: String?, password: String): Wire {
        val eph = Crypto.generateEcKeyPair()
        val ephC = Crypto.encodePub(eph.public)
        wire.sendRaw(
            NetworkPacket(type = Protocol.HS_START, hsMode = mode, groupId = groupId, eph = ephC)
        )
        val ack = wire.recvRaw() ?: throw WireException("对方无响应")
        if (ack.type == Protocol.HS_REJECT) throw WireException(ack.errorMessage ?: "连接被拒绝")
        if (ack.type != Protocol.HS_ACK || ack.eph.isNullOrBlank()) throw WireException("无效的握手响应")
        val ephS = ack.eph!!
        val peerPub = try {
            Crypto.decodePub(ephS)
        } catch (e: Exception) {
            throw WireException("无效的握手密钥", e)
        }
        val transcript = "$mode|$groupId|$ephC|$ephS"
        val salt = Crypto.sha256(transcript.toByteArray(Charsets.UTF_8))
        val pwKey = Crypto.pbkdf2Sha1(password, salt, PBKDF2_ITERATIONS, Crypto.KEY_LEN)
        val shared = Crypto.ecdh(eph.private, peerPub)
        val clientMac = Crypto.toB64(
            Crypto.hmacSha256(pwKey, "lc-client|$transcript".toByteArray(Charsets.UTF_8))
        )
        wire.sendRaw(NetworkPacket(type = Protocol.HS_CONFIRM, mac = clientMac))
        val ok = wire.recvRaw() ?: throw WireException("对方无响应")
        if (ok.type == Protocol.HS_REJECT) throw WireException(ok.errorMessage ?: "连接被拒绝")
        if (ok.type != Protocol.HS_OK || ok.mac.isNullOrBlank()) throw WireException("握手确认无效")
        val expected = Crypto.toB64(
            Crypto.hmacSha256(pwKey, "lc-server|$transcript".toByteArray(Charsets.UTF_8))
        )
        // 常数时间比较（复用 Crypto.constantTimeEquals）：非常数比较会
        // 泄露 MAC 前缀的匹配长度，辅助针对握手 MAC 的定时侧信道
        val macMatches = Crypto.constantTimeEquals(
            ok.mac!!.toByteArray(Charsets.UTF_8),
            expected.toByteArray(Charsets.UTF_8)
        )
        if (!macMatches) throw WireException("对方密码验证失败")
        wire.activate(
            Crypto.hkdfSha256(shared + pwKey, salt, INFO_SESSION.toByteArray(), Crypto.KEY_LEN)
        )
        return wire
    }

    // -------------------------------------------------- password-mode server

    /**
     * Server side of the query/join/mesh handshake. [start] is the already
     * read hs_start line. [passwordFor] resolves the group's password:
     * null = no such group on this device (rejected), otherwise the password
     * ("" for a group created without one). The confirm + MAC exchange is
     * mandatory, so a wrong or missing password always fails cleanly.
     * Returns null after sending a rejection.
     */
    fun accept(
        wire: Wire,
        start: NetworkPacket,
        passwordFor: (mode: String, groupId: String?) -> String?
    ): SecuredWire? {
        val mode = start.hsMode ?: return reject(wire, "无效的握手").let { null }
        if (start.eph.isNullOrBlank()) return reject(wire, "无效的握手").let { null }
        val clientPub = try {
            Crypto.decodePub(start.eph!!)
        } catch (e: Exception) {
            return reject(wire, "无效的握手密钥").let { null }
        }
        val password = try {
            passwordFor(mode, start.groupId)
        } catch (e: Exception) {
            Log.w(TAG, "password lookup failed", e)
            null
        }
        if (password == null) {
            return reject(wire, "该设备不存在此群组").let { null }
        }
        val eph = Crypto.generateEcKeyPair()
        val ephC = start.eph!!
        val ephS = Crypto.encodePub(eph.public)
        wire.sendRaw(NetworkPacket(type = Protocol.HS_ACK, eph = ephS))
        val transcript = "$mode|${start.groupId}|$ephC|$ephS"
        val salt = Crypto.sha256(transcript.toByteArray(Charsets.UTF_8))
        val pwKey = Crypto.pbkdf2Sha1(password, salt, PBKDF2_ITERATIONS, Crypto.KEY_LEN)
        val confirm = wire.recvRaw()
        if (confirm == null || confirm.type != Protocol.HS_CONFIRM || confirm.mac.isNullOrBlank()) {
            return reject(wire, "需要群组密码").let { null }
        }
        val expected = Crypto.hmacSha256(pwKey, "lc-client|$transcript".toByteArray(Charsets.UTF_8))
        val provided = Crypto.fromB64(confirm.mac!!)
        if (provided == null || !Crypto.constantTimeEquals(provided, expected)) {
            return reject(wire, "群组密码错误").let { null }
        }
        val serverMac = Crypto.toB64(
            Crypto.hmacSha256(pwKey, "lc-server|$transcript".toByteArray(Charsets.UTF_8))
        )
        wire.sendRaw(NetworkPacket(type = Protocol.HS_OK, mac = serverMac))
        val shared = Crypto.ecdh(eph.private, clientPub)
        wire.activate(
            Crypto.hkdfSha256(shared + pwKey, salt, INFO_SESSION.toByteArray(), Crypto.KEY_LEN)
        )
        return SecuredWire(wire, mode, start.groupId, null)
    }

    // ---------------------------------------------------- direct-mode client

    /**
     * Initiator side of the identity handshake (direct chats, call media).
     * [expectedPeerId]: when the peer's device id is already known, its
     * remembered identity key is compared (TOFU) and a mismatch aborts.
     */
    fun initiateDirect(
        wire: Wire,
        expectedPeerId: String?,
        onIdentityMismatch: (() -> Unit)? = null
    ): SecuredWire {
        val me = DeviceIdentity.current ?: throw WireException("本机身份未初始化")
        val eph = Crypto.generateEcKeyPair()
        val ephA = Crypto.encodePub(eph.public)
        val identA = Crypto.encodePub(me.public)
        wire.sendRaw(
            NetworkPacket(type = Protocol.HS_START, hsMode = Protocol.MODE_DIRECT, eph = ephA, ident = identA)
        )
        val ack = wire.recvRaw() ?: throw WireException("对方无响应")
        if (ack.type == Protocol.HS_REJECT) throw WireException(ack.errorMessage ?: "连接被拒绝")
        if (ack.type != Protocol.HS_ACK || ack.eph.isNullOrBlank() ||
            ack.ident.isNullOrBlank() || ack.sig.isNullOrBlank()
        ) throw WireException("无效的握手响应")
        val ephB = ack.eph!!
        val identB = ack.ident!!
        val peerIdentPub = try {
            Crypto.decodePub(identB)
        } catch (e: Exception) {
            throw WireException("对方身份密钥无效", e)
        }
        val transcriptHash = Crypto.sha256("lc-direct-v1|$ephA|$ephB".toByteArray(Charsets.UTF_8))
        val theirSig = Crypto.fromB64(ack.sig!!) ?: throw WireException("无效的签名")
        if (!Crypto.verify(peerIdentPub, transcriptHash, theirSig)) {
            throw WireException("对方身份签名验证失败")
        }
        if (expectedPeerId != null && !DeviceIdentity.checkPeer(expectedPeerId, identB)) {
            onIdentityMismatch?.invoke()
            throw WireException("对方身份发生变化，可能存在中间人")
        }
        val sessionKey = Crypto.hkdfSha256(
            Crypto.ecdh(eph.private, Crypto.decodePub(ephB)),
            transcriptHash,
            INFO_DIRECT.toByteArray(),
            Crypto.KEY_LEN
        )
        val mySig = Crypto.toB64(Crypto.sign(me.private, transcriptHash))
        wire.sendRaw(NetworkPacket(type = Protocol.HS_CONFIRM, sig = mySig))
        wire.activate(sessionKey)
        return SecuredWire(wire, Protocol.MODE_DIRECT, null, identB)
    }

    // ---------------------------------------------------- direct-mode server

    /** Acceptor side of the identity handshake; returns null on rejection. */
    fun acceptDirect(
        wire: Wire,
        start: NetworkPacket,
        expectedPeerId: String?,
        onIdentityMismatch: (() -> Unit)? = null
    ): SecuredWire? {
        val me = DeviceIdentity.current
        if (me == null) {
            wire.sendRawReject("对方身份无效")
            return null
        }
        if (start.hsMode != Protocol.MODE_DIRECT || start.eph.isNullOrBlank() || start.ident.isNullOrBlank()) {
            wire.sendRawReject("无效的握手")
            return null
        }
        val eph = Crypto.generateEcKeyPair()
        val ephA = start.eph!!
        val ephB = Crypto.encodePub(eph.public)
        val transcriptHash = Crypto.sha256("lc-direct-v1|$ephA|$ephB".toByteArray(Charsets.UTF_8))
        val sig = Crypto.toB64(Crypto.sign(me.private, transcriptHash))
        wire.sendRaw(
            NetworkPacket(
                type = Protocol.HS_ACK,
                eph = ephB,
                ident = Crypto.encodePub(me.public),
                sig = sig
            )
        )
        val confirm = wire.recvRaw()
        if (confirm == null || confirm.type != Protocol.HS_CONFIRM || confirm.sig.isNullOrBlank()) {
            return null
        }
        val initiatorIdent = try {
            Crypto.decodePub(start.ident!!)
        } catch (e: Exception) {
            return null
        }
        val theirSig = Crypto.fromB64(confirm.sig!!)
        if (theirSig == null || !Crypto.verify(initiatorIdent, transcriptHash, theirSig)) {
            Log.w(TAG, "direct handshake: initiator signature invalid")
            return null
        }
        if (expectedPeerId != null && !DeviceIdentity.checkPeer(expectedPeerId, start.ident!!, remember = false)) {
            onIdentityMismatch?.invoke()
            return null
        }
        val sessionKey = Crypto.hkdfSha256(
            Crypto.ecdh(eph.private, Crypto.decodePub(ephA)),
            transcriptHash,
            INFO_DIRECT.toByteArray(),
            Crypto.KEY_LEN
        )
        wire.activate(sessionKey)
        return SecuredWire(wire, Protocol.MODE_DIRECT, null, start.ident)
    }

    private fun reject(wire: Wire, message: String) {
        wire.sendRawReject(message)
    }
}

/**
 * Long-term device identity (EC P-256) for direct chats and call media.
 * Generated once, stored app-privately (SharedPreferences, Base64). The
 * private key never leaves the app sandbox; hardware-backed AndroidKeyStore
 * KeyAgreement needs API 31+, so plain JCE keys are the portable choice.
 *
 * TOFU: the first handshake with a peer remembers its identity key; a later
 * change aborts the connection (possible MITM). [fingerprint] gives the user
 * an out-of-band comparable "安全码".
 */
object DeviceIdentity {

    private const val PREFS = "localchat_identity"
    private const val KEY_PRIV = "identity_private"
    private const val KEY_PUB = "identity_public"
    private const val KEY_PEER_PREFIX = "peer_ident_"

    @Volatile
    var current: KeyPair? = null

    private var appContext: Context? = null

    /** Load (or generate once) the device identity. Call at app start. */
    fun ensureLoaded(context: Context): KeyPair {
        current?.let { return it }
        synchronized(this) {
            current?.let { return it }
            appContext = context.applicationContext
            val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val pub = prefs.getString(KEY_PUB, null)
            val priv = prefs.getString(KEY_PRIV, null)
            val pair = if (pub != null && priv != null) {
                try {
                    KeyPair(Crypto.decodePub(pub), Crypto.decodePriv(priv))
                } catch (e: Exception) {
                    Log.w("DeviceIdentity", "stored identity unreadable, regenerating", e)
                    null
                }
            } else null
            val result = pair ?: Crypto.generateEcKeyPair().also {
                prefs.edit()
                    .putString(KEY_PUB, Crypto.encodePub(it.public))
                    .putString(KEY_PRIV, Crypto.encodePriv(it.private))
                    .apply()
            }
            current = result
            return result
        }
    }

    /** Install an identity directly (unit tests, no Context). */
    fun install(pair: KeyPair) {
        synchronized(this) {
            current = pair
        }
    }

    /** Short human-comparable fingerprint of the local identity key. */
    fun fingerprint(): String? =
        current?.let { Crypto.hex(Crypto.sha256(it.public.encoded)).take(16).uppercase() }

    fun peerFingerprint(identB64: String): String =
        try {
            Crypto.hex(Crypto.sha256(Crypto.decodePub(identB64).encoded)).take(16).uppercase()
        } catch (e: Exception) {
            "????"
        }

    /**
     * TOFU check (and first-contact remember): true when [identB64] is the
     * remembered key for [peerId] (or was just remembered), false when the
     * peer's identity CHANGED — treat as a possible man-in-the-middle.
     */
    fun checkPeer(peerId: String, identB64: String, remember: Boolean = true): Boolean {
        if (peerId.isBlank()) return true
        val ctx = appContext ?: return true
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val known = prefs.getString(KEY_PEER_PREFIX + peerId, null)
        if (known == null) {
            if (remember) {
                prefs.edit().putString(KEY_PEER_PREFIX + peerId, identB64).apply()
            }
            return true
        }
        return known == identB64
    }

    /**
     * True when [peerId] already has a remembered identity key (pure lookup,
     * no TOFU side effects). Callers use it to distinguish "the handshake
     * proved a KNOWN key" (an address change is then multi-homing or DHCP
     * churn, not impersonation) from first contact, where the address
     * binding must still be enforced strictly.
     */
    fun hasPeer(peerId: String): Boolean {
        if (peerId.isBlank()) return false
        val ctx = appContext ?: return false
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .contains(KEY_PEER_PREFIX + peerId)
    }
}
