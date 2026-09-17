package com.zqr.localchat.network

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 滑动窗口握手限流器（PBKDF2 CPU DoS 防护）的行为测试。 */
class HandshakeRateLimiterTest {

    @Test
    fun `allows up to limit attempts then blocks`() {
        var now = 0L
        val limiter = HandshakeRateLimiter(limit = 3, windowMs = 60_000L) { now }
        repeat(3) { assertTrue(limiter.allow("10.0.0.1")) }
        assertFalse("4th attempt inside the window must be blocked", limiter.allow("10.0.0.1"))
    }

    @Test
    fun `window slides - attempts expire after windowMs`() {
        var now = 0L
        val limiter = HandshakeRateLimiter(limit = 1, windowMs = 1_000L) { now }
        assertTrue(limiter.allow("10.0.0.1"))
        now = 999
        assertFalse("attempt still inside the window", limiter.allow("10.0.0.1"))
        now = 1_000
        assertTrue("attempt expired with the window", limiter.allow("10.0.0.1"))
    }

    @Test
    fun `limits are per source ip`() {
        var now = 0L
        val limiter = HandshakeRateLimiter(limit = 1, windowMs = 60_000L) { now }
        assertTrue(limiter.allow("10.0.0.1"))
        assertTrue("a different ip has its own budget", limiter.allow("10.0.0.2"))
        assertFalse(limiter.allow("10.0.0.1"))
    }

    @Test
    fun `expired ip keys are swept so the ip table stays bounded`() {
        var now = 0L
        val limiter = HandshakeRateLimiter(limit = 5, windowMs = 1_000L) { now }
        // fill the tracker to its key cap (1024 distinct ips)
        repeat(1024) { i -> limiter.allow("10.0.$i.1") }
        now = 60_000 // every tracked attempt is now far outside the window
        assertTrue(
            "sweeping expired keys must free room for a new ip",
            limiter.allow("10.9.9.9")
        )
    }
}
