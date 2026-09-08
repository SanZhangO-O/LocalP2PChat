package com.zqr.localchat

import com.zqr.localchat.crypto.Crypto
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class CryptoTest {

    @Test
    fun `base64 roundtrips arbitrary byte lengths`() {
        for (len in intArrayOf(0, 1, 2, 3, 4, 5, 15, 16, 31, 32, 100, 1000)) {
            val bytes = Crypto.randomBytes(len)
            val encoded = Crypto.toB64(bytes)
            assertArrayEquals("len=$len", bytes, Crypto.fromB64(encoded))
        }
    }

    @Test
    fun `base64 matches known vectors`() {
        assertEquals("", Crypto.toB64(ByteArray(0)))
        assertEquals("AA==", Crypto.toB64(byteArrayOf(0)))
        assertEquals("QUJD", Crypto.toB64("ABC".toByteArray(Charsets.US_ASCII)))
        assertEquals("QUJDRA==", Crypto.toB64("ABCD".toByteArray(Charsets.US_ASCII)))
        assertArrayEquals("ABC".toByteArray(), Crypto.fromB64("QUJD"))
        // invalid input rejected
        assertTrue(Crypto.fromB64("!!!!") == null)
    }

    @Test
    fun `aes-gcm roundtrip and tamper detection`() {
        val key = Crypto.randomBytes(32)
        val plaintext = "你好，LocalChat！".toByteArray(Charsets.UTF_8)
        val blob = Crypto.aesGcmEncrypt(key, plaintext)
        assertArrayEquals(plaintext, Crypto.aesGcmDecrypt(key, blob))
        // nonce is random: two encryptions differ
        assertFalse(blob.contentEquals(Crypto.aesGcmEncrypt(key, plaintext)))

        // flipping any ciphertext byte must fail authentication
        for (i in blob.indices) {
            val tampered = blob.copyOf().also { it[i] = (it[i].toInt() xor 1).toByte() }
            assertThrows("tamper at $i", Exception::class.java) { Crypto.aesGcmDecrypt(key, tampered) }
        }
        // wrong key must fail
        assertThrows(Exception::class.java) { Crypto.aesGcmDecrypt(Crypto.randomBytes(32), blob) }
    }

    @Test
    fun `hkdf-sha256 matches rfc5869 test case 1`() {
        val ikm = ByteArray(22) { 0x0b }
        val salt = byteArrayOf(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
        val info = byteArrayOf(0xf0.toByte(), 0xf1.toByte(), 0xf2.toByte(), 0xf3.toByte(),
            0xf4.toByte(), 0xf5.toByte(), 0xf6.toByte(), 0xf7.toByte(), 0xf8.toByte(), 0xf9.toByte())
        val okm = Crypto.hkdfSha256(ikm, salt, info, 42)
        assertEquals(
            "3cb25f25faacd57a90434f64d0362f2a" +
                "2d2d0a90cf1a5a4c5db02d56ecc4c5bf" +
                "34007208d5b887185865",
            Crypto.hex(okm)
        )
    }

    @Test
    fun `pbkdf2-sha1 matches rfc6070 test case 1`() {
        val dk = Crypto.pbkdf2Sha1("password", "salt".toByteArray(Charsets.US_ASCII), 1, 20)
        assertEquals("0c60c80f961f0e71f3a9b524af6012062fe037a6", Crypto.hex(dk))
    }

    @Test
    fun `ecdh keypairs agree on the shared secret`() {
        val a = Crypto.generateEcKeyPair()
        val b = Crypto.generateEcKeyPair()
        val s1 = Crypto.ecdh(a.private, b.public)
        val s2 = Crypto.ecdh(b.private, a.public)
        assertEquals(Crypto.hex(s1), Crypto.hex(s2))
        // a third party derives something different
        val c = Crypto.generateEcKeyPair()
        assertNotEquals(Crypto.hex(s1), Crypto.hex(Crypto.ecdh(c.private, b.public)))
    }

    @Test
    fun `ecdsa signature verifies and rejects tampering`() {
        val a = Crypto.generateEcKeyPair()
        val data = "transcript".toByteArray(Charsets.UTF_8)
        val sig = Crypto.sign(a.private, data)
        assertTrue(Crypto.verify(a.public, data, sig))
        assertFalse(Crypto.verify(a.public, "tampered".toByteArray(Charsets.UTF_8), sig))
        val other = Crypto.generateEcKeyPair()
        assertFalse(Crypto.verify(other.public, data, sig))
    }

    @Test
    fun `random passwords use the unambiguous alphabet`() {
        val allowed = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789".toSet()
        repeat(50) {
            val pw = Crypto.randomPassword(8)
            assertEquals(8, pw.length)
            assertTrue(pw.all { it in allowed })
        }
        // generated passwords have real entropy
        val seen = HashSet<String>()
        repeat(100) { seen.add(Crypto.randomPassword(8)) }
        assertTrue(seen.size > 90)
    }

    @Test
    fun `constant-time equals behaves`() {
        assertTrue(Crypto.constantTimeEquals(byteArrayOf(1, 2, 3), byteArrayOf(1, 2, 3)))
        assertFalse(Crypto.constantTimeEquals(byteArrayOf(1, 2, 3), byteArrayOf(1, 2, 4)))
        assertFalse(Crypto.constantTimeEquals(byteArrayOf(1, 2), byteArrayOf(1, 2, 3)))
    }
}
