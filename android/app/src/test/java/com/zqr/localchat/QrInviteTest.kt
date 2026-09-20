package com.zqr.localchat

import com.zqr.localchat.network.QrInvite
import com.zqr.localchat.network.QrPayloadException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * QR invite payload codec. The string samples were produced by the Windows
 * codec (localchat/qrshare.py): both platforms must encode and decode the
 * exact same bytes, so a Windows-shown QR scans on Android and vice versa.
 */
class QrInviteTest {

    // ------------------------------------------------- Windows-produced bytes

    @Test
    fun `contact encoding matches the Windows codec byte for byte`() {
        val payload = QrInvite.encodeContactInvite(
            name = "王五",
            fingerprint = "0123456789ABCDEF",
            ip = "192.168.1.5"
        )
        assertEquals(
            "localchat://contact?v=1&n=%E7%8E%8B%E4%BA%94&f=0123456789ABCDEF&ip=192.168.1.5",
            payload
        )
    }

    @Test
    fun `contact encoding omits the default port`() {
        assertEquals(
            "localchat://contact?v=1&ip=192.168.1.5&p=12345",
            QrInvite.encodeContactInvite("", "", "192.168.1.5", 12345)
        )
    }

    @Test
    fun `group encoding matches the Windows codec byte for byte`() {
        assertEquals(
            "localchat://group?v=1&g=12345678&n=%E5%AE%B6%E5%BA%AD&ip=192.168.1.9&r=192.168.1.2%3A8000",
            QrInvite.encodeGroupInvite(
                groupId = "12345678",
                name = "家庭",
                ip = "192.168.1.9",
                port = 9999,
                relay = "192.168.1.2:8000"
            )
        )
    }

    // ------------------------------------------------------------- round trip

    @Test
    fun `windows contact payload parses back`() {
        val invite = QrInvite.parse(
            "localchat://contact?v=1&n=%E7%8E%8B%E4%BA%94&f=0123456789ABCDEF&ip=192.168.1.5"
        )
        assertTrue(invite is com.zqr.localchat.network.ContactInvite)
        invite as com.zqr.localchat.network.ContactInvite
        assertEquals("王五", invite.name)
        assertEquals("0123456789ABCDEF", invite.fingerprint)
        assertEquals("192.168.1.5", invite.ip)
        assertEquals(9999, invite.port)
    }

    @Test
    fun `windows group payload parses back with relay`() {
        val invite = QrInvite.parse(
            "localchat://group?v=1&g=12345678&n=%E5%AE%B6%E5%BA%AD&ip=192.168.1.9&r=192.168.1.2%3A8000"
        )
        assertTrue(invite is com.zqr.localchat.network.GroupInvite)
        invite as com.zqr.localchat.network.GroupInvite
        assertEquals("12345678", invite.groupId)
        assertEquals("家庭", invite.name)
        assertEquals("192.168.1.9", invite.ip)
        assertEquals(9999, invite.port)
        assertEquals("192.168.1.2:8000", invite.relay)
    }

    @Test
    fun `percent-encoded space from Windows decodes like the plus form`() {
        // Python quote() emits %20, our URLEncoder emits +: both decode to a space
        val pct = QrInvite.parse("localchat://contact?v=1&n=a%20b&ip=192.168.1.5")
            as com.zqr.localchat.network.ContactInvite
        assertEquals("a b", pct.name)

        val plus = QrInvite.parse("localchat://contact?v=1&n=a+b&ip=192.168.1.5")
            as com.zqr.localchat.network.ContactInvite
        assertEquals("a b", plus.name)
    }

    @Test
    fun `round trip survives non-ascii names`() {
        val payload = QrInvite.encodeGroupInvite(
            groupId = "87654321", name = "测试 群组!", ip = "10.0.0.2", port = 1234
        )
        val invite = QrInvite.parse(payload) as com.zqr.localchat.network.GroupInvite
        assertEquals("87654321", invite.groupId)
        assertEquals("测试 群组!", invite.name)
        assertEquals("10.0.0.2", invite.ip)
        assertEquals(1234, invite.port)
        assertEquals("", invite.relay)
    }

    // -------------------------------------------------------------- rejection

    @Test
    fun `non localchat text is rejected`() {
        assertThrows(QrPayloadException::class.java) { QrInvite.parse("https://example.com") }
        assertThrows(QrPayloadException::class.java) { QrInvite.parse("") }
    }

    @Test
    fun `unknown payload version is rejected`() {
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://contact?v=2&ip=192.168.1.5")
        }
    }

    @Test
    fun `unknown payload kind is rejected`() {
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://wifi?v=1&ip=192.168.1.5")
        }
    }

    @Test
    fun `contact payload without a valid address is rejected`() {
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://contact?v=1")
        }
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://contact?v=1&ip=not-an-address")
        }
    }

    @Test
    fun `group payload needs eight digits`() {
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://group?v=1&g=1234567&ip=192.168.1.5")
        }
    }

    @Test
    fun `bad fingerprint shape is rejected`() {
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://contact?v=1&f=XYZ&ip=192.168.1.5")
        }
    }

    @Test
    fun `bad relay shape is rejected`() {
        assertThrows(QrPayloadException::class.java) {
            QrInvite.parse("localchat://group?v=1&g=12345678&r=no-port-here")
        }
    }

    @Test
    fun `a password-looking parameter is ignored not trusted`() {
        // the group password NEVER travels in a QR: a stray parameter must
        // neither break parsing nor smuggle a secret into the join flow
        val invite = QrInvite.parse(
            "localchat://group?v=1&g=12345678&password=leaked&ip=192.168.1.5"
        ) as com.zqr.localchat.network.GroupInvite
        assertEquals("12345678", invite.groupId)
        assertEquals("192.168.1.5", invite.ip)
    }
}
