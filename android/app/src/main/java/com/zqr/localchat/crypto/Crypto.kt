package com.zqr.localchat.crypto

import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec

/**
 * Cryptographic primitives for all LocalChat transports. Pure JCE so the same
 * code runs on-device (API 24+, no Android-specific classes — Base64 is
 * implemented locally because java.util.Base64 needs API 26) and in JVM unit
 * tests.
 *
 * Security properties provided elsewhere (see network/SecureWire.kt):
 *  - confidentiality + integrity: AES-256-GCM per line / frame / chunk
 *  - key agreement: ephemeral ECDH over P-256
 *  - group/mesh authentication: the shared group password is folded into the
 *    key derivation and confirmed with HMACs (defeets passive sniffing and,
 *    given a high-entropy generated group password, MITM and offline
 *    dictionary attacks)
 *  - direct/call authentication: long-term EC identity keys signing the
 *    ephemeral transcript (TOFU + comparable fingerprints)
 */
object Crypto {

    private val random = SecureRandom()

    const val GCM_NONCE_LEN = 12
    const val GCM_TAG_BITS = 128
    const val KEY_LEN = 32

    // -------------------------------------------------------------- random

    fun randomBytes(n: Int): ByteArray = ByteArray(n).also { random.nextBytes(it) }

    /** Password alphabet for generated group passwords: alphanumeric minus
     *  visually ambiguous characters (0/O, 1/l/I), so they survive manual
     *  typing and being read aloud. */
    private const val PASSWORD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"

    fun randomPassword(length: Int): String = buildString {
        repeat(length) { append(PASSWORD_CHARS[random.nextInt(PASSWORD_CHARS.length)]) }
    }

    // --------------------------------------------------------------- hashes

    fun sha256(data: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(data)

    fun hmacSha256(key: ByteArray, data: ByteArray): ByteArray =
        Mac.getInstance("HmacSHA256")
            .apply { init(SecretKeySpec(key, "HmacSHA256")) }
            .doFinal(data)

    /** HKDF-SHA256 (RFC 5869, extract-then-expand). */
    fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, outLen: Int): ByteArray {
        val prk = hmacSha256(if (salt.isEmpty()) ByteArray(32) else salt, ikm)
        var t = ByteArray(0)
        val okm = ByteArray(outLen)
        var pos = 0
        var counter = 1
        while (pos < outLen) {
            t = hmacSha256(prk, t + info + byteArrayOf(counter.toByte()))
            val n = minOf(t.size, outLen - pos)
            System.arraycopy(t, 0, okm, pos, n)
            pos += n
            counter++
        }
        return okm
    }

    /**
     * PBKDF2-HMAC-SHA1. SHA1 (not SHA256) deliberately: it is the only PBKDF2
     * PRF guaranteed on every Android version down to API 24, so both peers
     * always derive identical keys. Iterations compensate for SHA1's speed on
     * GPUs; combined with an 8-char generated password (~47.6 bits) an offline
     * dictionary attack on a recorded handshake stays out of reach.
     */
    fun pbkdf2Sha1(password: String, salt: ByteArray, iterations: Int, outLenBytes: Int): ByteArray {
        val spec = PBEKeySpec(password.toCharArray(), salt, iterations, outLenBytes * 8)
        return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1").generateSecret(spec).encoded
    }

    fun constantTimeEquals(a: ByteArray, b: ByteArray): Boolean {
        if (a.size != b.size) return false
        var diff = 0
        for (i in a.indices) diff = diff or (a[i].toInt() xor b[i].toInt())
        return diff == 0
    }

    // ----------------------------------------------------------------- AES

    /** AES-256-GCM: returns nonce || ciphertext || tag with a fresh random nonce. */
    fun aesGcmEncrypt(key: ByteArray, plaintext: ByteArray, nonce: ByteArray = randomBytes(GCM_NONCE_LEN)): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(GCM_TAG_BITS, nonce))
        return nonce + cipher.doFinal(plaintext)
    }

    /** Inverse of [aesGcmEncrypt]; throws on tampering or wrong key. */
    fun aesGcmDecrypt(key: ByteArray, blob: ByteArray): ByteArray {
        require(blob.size > GCM_NONCE_LEN) { "ciphertext too short" }
        val nonce = blob.copyOfRange(0, GCM_NONCE_LEN)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(GCM_TAG_BITS, nonce))
        return cipher.doFinal(blob, GCM_NONCE_LEN, blob.size - GCM_NONCE_LEN)
    }

    // ------------------------------------------------------------------ EC

    fun generateEcKeyPair(): KeyPair =
        KeyPairGenerator.getInstance("EC")
            .apply { initialize(ECGenParameterSpec("secp256r1"), random) }
            .genKeyPair()

    fun encodePub(key: PublicKey): String = toB64(key.encoded)
    fun encodePriv(key: PrivateKey): String = toB64(key.encoded)

    fun decodePub(b64: String): PublicKey =
        KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(fromB64(b64)))

    fun decodePriv(b64: String): PrivateKey =
        KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(fromB64(b64)))

    /**
     * ECDH shared secret (the x-coordinate of the shared point). Providers
     * disagree on leading zero bytes, so both sides canonicalize to the
     * minimal encoding before it is hashed into the session key.
     */
    fun ecdh(privateKey: PrivateKey, peerPub: PublicKey): ByteArray {
        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(privateKey)
        ka.doPhase(peerPub, true)
        return ka.generateSecret().dropWhile { it.toInt() == 0 }.toByteArray()
    }

    fun sign(privateKey: PrivateKey, data: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA")
            .apply { initSign(privateKey) }
            .let { it.update(data); it.sign() }

    fun verify(publicKey: PublicKey, data: ByteArray, sig: ByteArray): Boolean = try {
        Signature.getInstance("SHA256withECDSA")
            .apply { initVerify(publicKey) }
            .let { it.update(data); it.verify(sig) }
    } catch (e: Exception) {
        false
    }

    // -------------------------------------------------------------- Base64

    // Local implementation: java.util.Base64 requires API 26 (minSdk is 24)
    // and android.util.Base64 does not exist in plain JVM unit tests.

    private const val B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    private val B64_DECODE: IntArray = IntArray(128) { -1 }.also { table ->
        B64_ALPHABET.forEachIndexed { i, c -> table[c.code] = i }
    }

    fun toB64(bytes: ByteArray): String {
        val out = StringBuilder((bytes.size * 4 / 3) + 4)
        var i = 0
        while (i + 2 < bytes.size) {
            val n = ((bytes[i].toInt() and 0xFF) shl 16) or
                ((bytes[i + 1].toInt() and 0xFF) shl 8) or
                (bytes[i + 2].toInt() and 0xFF)
            out.append(B64_ALPHABET[n ushr 18 and 63])
            out.append(B64_ALPHABET[n ushr 12 and 63])
            out.append(B64_ALPHABET[n ushr 6 and 63])
            out.append(B64_ALPHABET[n and 63])
            i += 3
        }
        when (bytes.size - i) {
            1 -> {
                val n = (bytes[i].toInt() and 0xFF) shl 16
                out.append(B64_ALPHABET[n ushr 18 and 63])
                out.append(B64_ALPHABET[n ushr 12 and 63])
                out.append("==")
            }
            2 -> {
                val n = ((bytes[i].toInt() and 0xFF) shl 16) or
                    ((bytes[i + 1].toInt() and 0xFF) shl 8)
                out.append(B64_ALPHABET[n ushr 18 and 63])
                out.append(B64_ALPHABET[n ushr 12 and 63])
                out.append(B64_ALPHABET[n ushr 6 and 63])
                out.append('=')
            }
        }
        return out.toString()
    }

    fun fromB64(s: String): ByteArray? {
        val vals = ArrayList<Int>(s.length)
        for (ch in s) {
            when (ch) {
                '\r', '\n', ' ' -> {}
                '=' -> vals.add(-2)
                else -> {
                    if (ch.code >= 128) return null
                    val v = B64_DECODE[ch.code]
                    if (v < 0) return null
                    vals.add(v)
                }
            }
        }
        // strip padding
        while (vals.isNotEmpty() && vals.last() == -2) vals.removeAt(vals.size - 1)
        val out = ByteArray(vals.size * 3 / 4)
        var oi = 0
        var buffer = 0
        var bits = 0
        for (v in vals) {
            buffer = (buffer shl 6) or v
            bits += 6
            if (bits >= 8) {
                bits -= 8
                if (oi < out.size) out[oi++] = (buffer ushr bits).toByte()
            }
        }
        return out
    }

    // ----------------------------------------------------------------- hex

    fun hex(bytes: ByteArray): String = buildString(bytes.size * 2) {
        for (b in bytes) {
            append("0123456789abcdef"[(b.toInt() shr 4) and 0xF])
            append("0123456789abcdef"[b.toInt() and 0xF])
        }
    }
}
