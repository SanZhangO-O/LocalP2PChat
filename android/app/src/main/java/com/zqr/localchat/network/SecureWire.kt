package com.zqr.localchat.network

import android.content.Context
import android.util.Log
import com.zqr.localchat.crypto.Crypto
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.io.BufferedReader
import java.io.InputStream
import java.io.PrintWriter
import java.security.KeyPair
import java.security.KeyStore
import java.util.LinkedHashMap
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Wire-protocol security layer.
 *
 * Every TCP connection (group join/query, host relay, mesh link, direct chat,
 * call media) starts with a handshake of PLAINTEXT JSON lines:
 *
 *   password modes (query/join/mesh):
 *     C -> S: hs_start {hsMode, groupId, eph}
 *     S -> C: hs_ack  {eph}
 *     C -> S: hs_confirm {mac}        (UNCONDITIONAL — even with an empty
 *                                      password the client sends a MAC of "")
 *     S -> C: hs_ok   {mac} | hs_reject {errorMessage}
 *   direct mode (identity-based, used by direct chats and call media):
 *     C -> S: hs_start  {hsMode="direct", eph, ident}
 *     S -> C: hs_ack    {eph, ident, sig}
 *     C -> S: hs_confirm {sig}
 *
 * After a successful handshake EVERY subsequent line on the connection is
 * Base64(nonce || AES-256-GCM(json)) instead of plaintext JSON. Legacy
 * plaintext packets are rejected (no downgrade). Every encrypted packet
 * carries a per-direction sequence number ("seq", 1,2,3,...) stamped inside
 * the protected JSON: the receiver accepts only the exact next number, so
 * replayed, reordered or injected lines always fail — there is no eviction
 * window a replay could slip through. Relay hops re-stamp on their own
 * outgoing wire.
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

    /** Serializes whole [sendPacket] invocations (stamp + encrypt + write).
     *  [PrintWriter] only synchronizes individual write calls, so without
     *  this lock two senders could interleave inside one line AND stamp seq
     *  out of wire order — the receiver's strict seq check then kills the
     *  connection (Windows parity: Wire._lock covers the whole send). */
    private val sendLock = Any()

    /** Per-direction packet sequence numbers (see class doc): send stamps
     *  1,2,3,... under [sendLock]; recv requires exactly last+1, so any
     *  replayed, reordered or injected encrypted line fails even after the
     *  nonce LRU evicted its nonce. Guarded by [seenNonces]' monitor on
     *  recv (single read loop) and by [sendLock] on send.
     *
     *  Compatibility: peers built BEFORE the seq field exists never stamp it
     *  (README: chat/file/group must keep working across versions). An
     *  unstamped stream is accepted until the first stamped packet proves
     *  the peer seq-capable; from then on a missing seq is treated like a
     *  replay. An honest peer cannot alternate between stamped and
     *  unstamped lines, and an attacker cannot strip seq without breaking
     *  the GCM authentication. */
    private var sendSeq: Long = 0
    private var recvSeq: Long = 0
    private var recvSeqEnforced = false

    /** Nonce replay guard: raw 12-byte nonces already seen on THIS
     *  connection, insertion-ordered, oldest evicted past
     *  [NONCE_CACHE_CAPACITY]. AES-GCM forbids nonce reuse, so a repeated
     *  nonce on one connection is a replay (or a broken peer) — the line is
     *  rejected before decryption and the connection treated as dead,
     *  exactly like a decrypt failure. Defense in depth next to the seq
     *  guard. */
    private val seenNonces = object : LinkedHashMap<String, Boolean>(1024, 0.75f, false) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Boolean>): Boolean =
            size > NONCE_CACHE_CAPACITY
    }

    fun activate(sessionKey: ByteArray) {
        key = sessionKey
    }

    val sessionKey: ByteArray?
        get() = key

    val isSecure: Boolean
        get() = key != null

    fun sendPacket(packet: NetworkPacket) {
        val k = key ?: throw WireException("wire not secured yet")
        // The WHOLE send runs under one lock: seq order == wire order even
        // with several concurrent writers (send queue, ping/pong, heartbeat,
        // relay broadcast all target the same wire). Atomic per line: two
        // senders can never interleave inside one println.
        synchronized(sendLock) {
            sendSeq += 1
            val stamped = packet.copy(seq = sendSeq)
            val json = wireJson.encodeToString(stamped)
            val line = Crypto.toB64(Crypto.aesGcmEncrypt(k, json.toByteArray(Charsets.UTF_8)))
            if (line.length > P2PManager.MAX_LINE_LENGTH) throw WireException("encrypted line exceeds cap")
            writer.println(line)
            writer.flush()
        }
    }

    /** Decrypted packet, or null at stream end. Throws [WireException] on
     *  tampering / wrong key / sequence violation — callers must treat that
     *  as a dead connection. */
    fun recvPacket(): NetworkPacket? {
        val line = lineIn.readLine() ?: return null
        if (line.isEmpty()) return null
        val k = key ?: throw WireException("wire not secured yet")
        val blob = Crypto.fromB64(line) ?: throw WireException("malformed encrypted line")
        if (blob.size > Crypto.GCM_NONCE_LEN) {
            val nonce = Crypto.hex(blob.copyOfRange(0, Crypto.GCM_NONCE_LEN))
            synchronized(seenNonces) {
                if (seenNonces.put(nonce, true) != null) {
                    Log.w(TAG, "replayed nonce rejected, dropping connection")
                    throw WireException("replayed nonce (possible replay attack)")
                }
            }
        }
        val plain = try {
            Crypto.aesGcmDecrypt(k, blob)
        } catch (e: Exception) {
            throw WireException("decrypt failed (tampered or wrong key)", e)
        }
        val packet = runCatching { wireJson.decodeFromString<NetworkPacket>(plain.toString(Charsets.UTF_8)) }
            .getOrElse { throw WireException("malformed packet JSON", it) }
        synchronized(seenNonces) {
            val seq = packet.seq
            if (seq == null) {
                // legacy (pre-seq) peer: accepted until it proves otherwise
                if (recvSeqEnforced) {
                    Log.w(TAG, "packet missing seq after enforcement began")
                    throw WireException(
                        "packet sequence violation (missing seq; replayed, reordered or injected)"
                    )
                }
            } else {
                val expected = recvSeq + 1
                if (seq != expected) {
                    Log.w(TAG, "packet sequence violation (got $seq, want $expected)")
                    throw WireException(
                        "packet sequence violation (replayed, reordered or injected)"
                    )
                }
                recvSeq = seq
                recvSeqEnforced = true
            }
        }
        return packet
    }

    // ---- handshake-phase plaintext IO (never used once activate() ran) ----

    fun sendRaw(packet: NetworkPacket) {
        check(key == null) { "wire already secured: plaintext raw send is forbidden" }
        writer.println(wireJson.encodeToString(packet))
        writer.flush()
    }

    fun sendRawReject(message: String) {
        sendRaw(NetworkPacket(type = Protocol.HS_REJECT, errorMessage = message))
    }

    fun recvRaw(): NetworkPacket? {
        check(key == null) { "wire already secured: plaintext raw receive is forbidden" }
        val line = lineIn.readLine() ?: return null
        return runCatching { wireJson.decodeFromString<NetworkPacket>(line) }.getOrNull()
    }

    companion object {
        private const val TAG = "Wire"

        /** Replay-window size per connection: comfortably above any plausible
         *  in-flight packet count, small enough that memory stays trivial. */
        private const val NONCE_CACHE_CAPACITY = 4096

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
 * private key is stored WRAPPED under an AES-256-GCM key generated in the
 * hardware AndroidKeyStore (alias [WRAP_ALIAS], value format
 * "enc1:" + Base64(iv || ciphertext)), so the plaintext key material never
 * sits in storage. A legacy plaintext value found on first read is wrapped
 * in place (migration). When the KeyStore is unusable on a device, storage
 * falls back to plaintext with a logged warning — identity must still load.
 *
 * TOFU: the first handshake with a peer remembers its identity key; a later
 * change aborts the connection (possible MITM). [fingerprint] gives the user
 * an out-of-band comparable "安全码".
 */
object DeviceIdentity {

    private const val TAG = "DeviceIdentity"
    private const val PREFS = "localchat_identity"
    private const val KEY_PRIV = "identity_private"
    private const val KEY_PUB = "identity_public"
    private const val KEY_PEER_PREFIX = "peer_ident_"

    /** AndroidKeyStore alias of the AES-256-GCM key that wraps the private
     *  identity key. Non-exportable; lives in the device keystore. */
    private const val WRAP_ALIAS = "localchat_identity_wrap"

    /** Marks a wrapped [KEY_PRIV] value ("enc1:" + Base64(iv || ct)); values
     *  without it are legacy plaintext and get migrated on first read. */
    private const val WRAP_PREFIX = "enc1:"

    @Volatile
    var current: KeyPair? = null

    private var appContext: Context? = null

    /** The AndroidKeyStore AES wrapping key, created on first use. Null when
     *  the keystore is unavailable — callers fall back to plaintext storage
     *  (already logged inside). */
    private fun wrapKey(): SecretKey? = try {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(WRAP_ALIAS, null) as? SecretKey) ?: run {
            val generator = KeyGenerator.getInstance("AES", "AndroidKeyStore")
            generator.init(
                KeyGenParameterSpec.Builder(
                    WRAP_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build()
            )
            generator.generateKey()
        }
    } catch (e: Exception) {
        Log.w(TAG, "AndroidKeyStore unavailable: identity key will be stored in plaintext", e)
        null
    }

    /** Wrap a Base64 private key as "enc1:" + Base64(iv || ciphertext);
     *  null when wrapping is impossible (plaintext fallback). */
    private fun wrapPrivateKey(privB64: String): String? {
        val key = wrapKey() ?: return null
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, key)
            val encrypted = cipher.doFinal(privB64.toByteArray(Charsets.UTF_8))
            WRAP_PREFIX + Crypto.toB64(cipher.iv + encrypted)
        } catch (e: Exception) {
            Log.w(TAG, "identity key wrap failed: key will be stored in plaintext", e)
            null
        }
    }

    /** Persisted form of the private key: wrapped under the AndroidKeyStore
     *  key when possible, otherwise plaintext Base64 (fallback, logged). */
    private fun storePrivateKey(privB64: String): String =
        wrapPrivateKey(privB64) ?: privB64

    /** Unwrap a stored [KEY_PRIV] value; legacy plaintext values pass through
     *  unchanged (they are migrated by the caller). Returns null when the
     *  wrapped value cannot be decrypted (keystore key lost etc.). */
    private fun unwrapPrivateKey(stored: String): String? {
        if (!stored.startsWith(WRAP_PREFIX)) return stored
        return try {
            val key = wrapKey() ?: return null
            val blob = Crypto.fromB64(stored.removePrefix(WRAP_PREFIX))
                ?: return null
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(
                Cipher.DECRYPT_MODE, key,
                GCMParameterSpec(Crypto.GCM_TAG_BITS, blob, 0, Crypto.GCM_NONCE_LEN)
            )
            String(
                cipher.doFinal(blob, Crypto.GCM_NONCE_LEN, blob.size - Crypto.GCM_NONCE_LEN),
                Charsets.UTF_8
            )
        } catch (e: Exception) {
            Log.w(TAG, "stored identity unwrap failed, regenerating", e)
            null
        }
    }

    /** Load (or generate once) the device identity. Call at app start. */
    fun ensureLoaded(context: Context): KeyPair {
        current?.let { return it }
        synchronized(this) {
            current?.let { return it }
            appContext = context.applicationContext
            val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val pub = prefs.getString(KEY_PUB, null)
            val stored = prefs.getString(KEY_PRIV, null)
            val priv = stored?.let { unwrapPrivateKey(it) }
            val pair = if (pub != null && priv != null) {
                try {
                    KeyPair(Crypto.decodePub(pub), Crypto.decodePriv(priv))
                } catch (e: Exception) {
                    Log.w(TAG, "stored identity unreadable, regenerating", e)
                    null
                }
            } else null
            val result = if (pair != null) {
                // legacy plaintext private key on first read: wrap it in
                // place, replacing (removing) the plaintext value
                if (stored != null && !stored.startsWith(WRAP_PREFIX)) {
                    wrapPrivateKey(priv!!)?.let { wrapped ->
                        prefs.edit().putString(KEY_PRIV, wrapped).apply()
                    }
                }
                pair
            } else {
                Crypto.generateEcKeyPair().also {
                    prefs.edit()
                        .putString(KEY_PUB, Crypto.encodePub(it.public))
                        .putString(KEY_PRIV, storePrivateKey(Crypto.encodePriv(it.private)))
                        .apply()
                }
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

    /**
     * The remembered identity key (Base64) for [peerId], null when unknown
     * (pure lookup, no TOFU side effects). The group TOFU UI uses it to show
     * a member's bound 安全码 (GroupAuth stores bindings under composite
     * "group|<groupId>|<senderId>" keys).
     */
    fun peerIdent(peerId: String): String? {
        if (peerId.isBlank()) return null
        val ctx = appContext ?: return null
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_PEER_PREFIX + peerId, null)
    }
}
