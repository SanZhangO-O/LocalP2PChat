package com.zqr.localchat

import com.zqr.localchat.data.CallInfo
import com.zqr.localchat.network.NetworkPacket
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Byte format of the call packets' new `media` field must match the Python
 * side's JSON exactly (compact, camelCase, null omitted). The golden strings
 * below are the same ones the Windows CallInfoSerializationTest asserts.
 */
class CallMediaProtocolTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `audio offer serializes media and matches the Python wire`() {
        val pkt = NetworkPacket(
            type = "call_offer",
            targetId = "b-2",
            call = CallInfo(
                callId = "c1",
                callerId = "a-1",
                callerName = "\u5f20\u4e09",
                calleeId = "b-2",
                mediaPort = 35001,
                media = "audio"
            )
        )
        assertEquals(
            "{\"type\":\"call_offer\",\"targetId\":\"b-2\",\"call\":{" +
                "\"callId\":\"c1\",\"callerId\":\"a-1\",\"callerName\":\"\u5f20\u4e09\"," +
                "\"calleeId\":\"b-2\",\"mediaPort\":35001,\"media\":\"audio\"}}",
            json.encodeToString(NetworkPacket.serializer(), pkt)
        )
    }

    @Test
    fun `video offer omits media (byte-compatible with the old format)`() {
        val pkt = NetworkPacket(
            type = "call_offer",
            targetId = "b-2",
            call = CallInfo("c1", "a-1", "A", "b-2", mediaPort = 35001)
        )
        val wire = json.encodeToString(NetworkPacket.serializer(), pkt)
        // "mediaPort" contains "media": check the exact key
        assertFalse(wire.contains("\"media\""))
        assertEquals(
            "{\"type\":\"call_offer\",\"targetId\":\"b-2\",\"call\":{" +
                "\"callId\":\"c1\",\"callerId\":\"a-1\",\"callerName\":\"A\"," +
                "\"calleeId\":\"b-2\",\"mediaPort\":35001}}",
            wire
        )
    }

    @Test
    fun `python audio offer decodes`() {
        val payload =
            "{\"type\":\"call_offer\",\"targetId\":\"b-2\",\"call\":{\"callId\":\"c9\"," +
                "\"callerId\":\"a-1\",\"callerName\":\"\\u674e\\u56db\",\"calleeId\":\"b-2\"," +
                "\"mediaPort\":42001,\"media\":\"audio\"}}"
        val pkt = json.decodeFromString(NetworkPacket.serializer(), payload)
        assertEquals("call_offer", pkt.type)
        assertEquals("audio", pkt.call?.media)
        assertEquals(42001, pkt.call?.mediaPort)
    }

    @Test
    fun `python video offer decodes with a null media`() {
        val payload =
            "{\"type\":\"call_offer\",\"targetId\":\"b-2\",\"call\":{\"callId\":\"c9\"," +
                "\"callerId\":\"a-1\",\"callerName\":\"A\",\"calleeId\":\"b-2\"," +
                "\"mediaPort\":42001}}"
        val pkt = json.decodeFromString(NetworkPacket.serializer(), payload)
        assertNull(pkt.call?.media)
    }

    @Test
    fun `media field round-trips on answer and hangup packets`() {
        val call = CallInfo("c1", "a-1", "A", "b-2", media = "audio")
        val wire = json.encodeToString(NetworkPacket.serializer(), NetworkPacket(type = "call_hangup", call = call))
        assertTrue(wire.contains("\"media\":\"audio\""))
        val back = json.decodeFromString(NetworkPacket.serializer(), wire)
        assertEquals("audio", back.call?.media)
    }
}
