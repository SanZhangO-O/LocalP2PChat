package com.zqr.localchat.crypto

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Log
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import java.security.KeyStore

/**
 * Secret-at-rest protection for high-value strings (group passwords in
 * SharedPreferences, persisted chat bodies / last-message previews in the
 * Room database).
 *
 * Values are encrypted with an AES-256-GCM key that lives in the hardware
 * AndroidKeyStore (non-exportable): "enc1:" + Base64(iv || ciphertext).
 * Nothing secret is ever persisted in plaintext; [unprotect] passes values
 * without the "enc1:" prefix through so data written before this layer
 * existed still reads. On a JVM (unit tests) or a device with an unusable
 * KeyStore the wrap is unavailable and values degrade to plaintext storage
 * (logged) — identity must still load, history must still read.
 */
object StoreCipher {

    private const val TAG = "StoreCipher"
    private const val WRAP_ALIAS = "localchat_store_wrap"
    private const val PREFIX = "enc1:"

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
        Log.w(TAG, "AndroidKeyStore unavailable: secrets will be stored in plaintext", e)
        null
    }

    /** Encrypt [value]; "" passes through unchanged (nothing to protect). */
    fun protect(value: String): String {
        if (value.isEmpty()) return value
        val key = wrapKey() ?: return value
        return try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, key)
            val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
            PREFIX + Crypto.toB64(cipher.iv + encrypted)
        } catch (e: Exception) {
            Log.w(TAG, "secret wrap failed: value stored in plaintext", e)
            value
        }
    }

    /** Inverse of [protect]. A value without the prefix is passed through;
     *  a prefixed value that fails to decrypt (corrupted row, lost keystore
     *  key) degrades to "" instead of surfacing ciphertext. */
    fun unprotect(value: String): String {
        if (!value.startsWith(PREFIX)) return value
        val key = wrapKey() ?: return ""
        return try {
            val blob = Crypto.fromB64(value.removePrefix(PREFIX)) ?: return ""
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
            Log.w(TAG, "secret unwrap failed; dropping the value")
            ""
        }
    }
}
