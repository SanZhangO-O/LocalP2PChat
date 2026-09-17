package com.zqr.localchat

import com.zqr.localchat.ui.screen.DEFAULT_GROUP_PORT
import com.zqr.localchat.ui.screen.hasInvalidPort
import com.zqr.localchat.ui.screen.isValidHost
import com.zqr.localchat.ui.screen.normalizeAddressInput
import com.zqr.localchat.ui.screen.parseHostPort
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression tests for the add-by-IP address input. Before the fix any
 * non-empty junk was accepted as a "contact" that showed in the member list
 * but could never connect — reported as "adding a peer by IP has no effect".
 * Windows parity: ChatViewModel._parse_host_port / _is_valid_host.
 */
class AddressParsingTest {

    @Test
    fun `full-width IME input is normalized to ASCII`() {
        // a Chinese IME emits full-width digits, ideographic dots and a
        // full-width colon in Chinese punctuation mode
        val fullWidth = "１９２。１６８．０。１９１：９９９９"
        assertEquals("192.168.0.191:9999", normalizeAddressInput(fullWidth))

        val parsed = parseHostPort(fullWidth)
        assertEquals("192.168.0.191", parsed.host)
        assertEquals(9999, parsed.port)
    }

    @Test
    fun `ascii input is unchanged`() {
        assertEquals("192.168.0.1:9999", normalizeAddressInput(" 192.168.0.1:9999 "))
        assertEquals("mypc:10000", normalizeAddressInput("mypc:10000"))

        assertEquals("192.168.0.1", parseHostPort("192.168.0.1:9999").host)
        assertEquals(9999, parseHostPort("192.168.0.1:9999").port)
        // a missing port falls back to the default program port
        assertEquals(DEFAULT_GROUP_PORT, parseHostPort("192.168.0.1").port)
        // spaces around host and port are tolerated
        assertEquals(10000, parseHostPort(" 192.168.0.1 : 10000 ").port)
    }

    @Test
    fun `valid hosts are accepted`() {
        assertTrue(isValidHost("192.168.0.1"))
        assertTrue(isValidHost("mypc"))
        assertTrue(isValidHost("my-pc.example.lan"))
        assertTrue(isValidHost("0.0.0.0"))
    }

    @Test
    fun `mangled endpoints are rejected`() {
        assertFalse("dots lost", isValidHost("127001"))
        assertFalse("too few octets", isValidHost("192.168.0"))
        assertFalse("too few labels", isValidHost("1.2.3"))
        assertFalse("octet out of range", isValidHost("192.168.0.300"))
        assertFalse("bare number", isValidHost("999"))
        assertFalse("empty", isValidHost(""))
        assertFalse("space is not a hostname char", isValidHost("host name!"))
    }

    @Test
    fun `unusable trailing ports are flagged`() {
        assertFalse("no port", hasInvalidPort("192.168.0.1"))
        assertFalse("valid port", hasInvalidPort("192.168.0.1:9999"))
        assertFalse("trailing colon only", hasInvalidPort("192.168.0.1:"))
        assertFalse("hostname only", hasInvalidPort("mypc"))
        assertTrue("port zero", hasInvalidPort("192.168.0.1:0"))
        assertTrue("one above range", hasInvalidPort("192.168.0.1:65536"))
        assertTrue("far above range", hasInvalidPort("192.168.0.1:70000"))
        assertTrue("tail too large to parse", hasInvalidPort("192.168.0.1:99999999999999999999"))
    }
}
