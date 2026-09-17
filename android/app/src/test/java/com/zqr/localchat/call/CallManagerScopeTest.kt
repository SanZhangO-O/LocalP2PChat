package com.zqr.localchat.call

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** JVM tests for the CallManager media replay guard ([CallManager.NonceLru]). */
class CallManagerScopeTest {

    /** Distinct 12-byte nonce built from [v]; + a dummy ciphertext tail. */
    private fun frame(v: Int): ByteArray = ByteArray(12) { i -> ((v * 7 + i) and 0xFF).toByte() } + ByteArray(8)

    @Test
    fun `first sight is not a replay, repeat is`() {
        val lru = CallManager.NonceLru()
        assertFalse(lru.isReplay(frame(1)))
        assertTrue(lru.isReplay(frame(1)))
    }

    @Test
    fun `different nonces are independent`() {
        val lru = CallManager.NonceLru()
        for (v in 1..3) assertFalse("v=$v", lru.isReplay(frame(v)))
        for (v in 1..3) assertTrue("v=$v", lru.isReplay(frame(v)))
    }

    @Test
    fun `lru evicts the oldest unrefreshed entry`() {
        val lru = CallManager.NonceLru(capacity = 3)
        assertFalse(lru.isReplay(frame(1)))
        assertFalse(lru.isReplay(frame(2)))
        assertFalse(lru.isReplay(frame(3)))
        // refresh nonce 1: nonce 2 becomes the oldest
        assertTrue(lru.isReplay(frame(1)))
        assertFalse(lru.isReplay(frame(4)))
        // survivors first: every isReplay() call RECORDS the nonce, so a
        // miss-check on the evicted entry would re-insert it and evict
        // someone else
        assertTrue("refreshed entry survives", lru.isReplay(frame(1)))
        assertTrue(lru.isReplay(frame(3)))
        assertTrue(lru.isReplay(frame(4)))
        // nonce 2 was evicted when 4 arrived: seeing it again is a first sight
        assertFalse("evicted entry is forgotten", lru.isReplay(frame(2)))
    }

    @Test
    fun `only the leading 12 bytes identify a frame`() {
        val lru = CallManager.NonceLru()
        val nonce = frame(5)
        val sameNonceDifferentTail = nonce.copyOfRange(0, 12) + ByteArray(8) { 9 }
        assertFalse(lru.isReplay(nonce))
        assertTrue(lru.isReplay(sameNonceDifferentTail))
    }

    @Test
    fun `capacity bound holds after many inserts`() {
        val lru = CallManager.NonceLru(capacity = 2)
        assertFalse(lru.isReplay(frame(1)))
        assertFalse(lru.isReplay(frame(2)))
        for (v in 3..6) assertFalse(lru.isReplay(frame(v)))
        // capacity 2: only nonces 5 and 6 remain — verify the survivors
        // BEFORE an evicted nonce, because a miss-check records the nonce
        // again (and evicts someone else)
        assertTrue(lru.isReplay(frame(6)))
        assertTrue(lru.isReplay(frame(5)))
        assertFalse(lru.isReplay(frame(1)))
    }
}
