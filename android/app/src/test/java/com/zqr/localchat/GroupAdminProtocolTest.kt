package com.zqr.localchat

import com.zqr.localchat.network.NetworkPacket
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Cross-platform contract for the group-management packets (group_update /
 * kick_member) and the join_ack announcement. The expected JSON strings are
 * exactly what the Windows peer (Python json.dumps with compact separators)
 * emits for the same packet, so both ends stay byte-compatible.
 */
class GroupAdminProtocolTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `group update wire format matches the Windows peer`() {
        val packet = NetworkPacket(
            type = "group_update",
            groupId = "g1",
            senderId = "owner",
            groupName = "NewName",
            announcement = "hello"
        )
        val wire = json.encodeToString(packet)
        assertEquals(
            "{\"type\":\"group_update\",\"groupId\":\"g1\",\"senderId\":\"owner\"," +
                "\"groupName\":\"NewName\",\"announcement\":\"hello\"}",
            wire
        )
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertEquals("owner", decoded.senderId)
        assertEquals("NewName", decoded.groupName)
        assertEquals("hello", decoded.announcement)
    }

    @Test
    fun `group update omits unchanged fields`() {
        val packet = NetworkPacket(
            type = "group_update",
            groupId = "g1",
            senderId = "owner",
            announcement = "only"
        )
        val wire = json.encodeToString(packet)
        assertFalse("null groupName must not be serialized", wire.contains("groupName"))
        assertTrue(wire.contains("\"announcement\":\"only\""))
    }

    @Test
    fun `kick member wire format matches the Windows peer`() {
        val packet = NetworkPacket(
            type = "kick_member",
            groupId = "g1",
            senderId = "owner",
            targetId = "bob"
        )
        val wire = json.encodeToString(packet)
        assertEquals(
            "{\"type\":\"kick_member\",\"groupId\":\"g1\",\"senderId\":\"owner\",\"targetId\":\"bob\"}",
            wire
        )
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertEquals("bob", decoded.targetId)
    }

    @Test
    fun `join ack announcement round-trips and is omitted when empty`() {
        val withAnnouncement = NetworkPacket(
            type = "join_ack",
            groupId = "g1",
            announcement = "notice"
        )
        val wire = json.encodeToString(withAnnouncement)
        assertTrue(wire.contains("\"announcement\":\"notice\""))
        assertEquals("notice", json.decodeFromString<NetworkPacket>(wire).announcement)

        val without = NetworkPacket(type = "join_ack", groupId = "g1")
        assertFalse(json.encodeToString(without).contains("announcement"))
    }

    @Test
    fun `packets without the new fields are tolerated`() {
        // a Windows peer that never sends the fields (or a future field) must
        // still parse: null defaults keep the packet valid
        val decoded = json.decodeFromString<NetworkPacket>(
            """{"type":"group_update","groupId":"g","senderId":"owner"}"""
        )
        assertNull(decoded.groupName)
        assertNull(decoded.announcement)

        val kick = json.decodeFromString<NetworkPacket>(
            """{"type":"kick_member","groupId":"g","senderId":"owner","targetId":"b"}"""
        )
        assertEquals("b", kick.targetId)
    }

    @Test
    fun `windows emitted group update parses`() {
        // literal bytes produced by the Python side (compact separators,
        // ensure_ascii=False): the Chinese announcement must survive
        val wire = "{\"type\":\"group_update\",\"groupId\":\"群@g1\",\"senderId\":\"owner\"," +
            "\"groupName\":\"新办公室\",\"announcement\":\"今天下午开会\"}"
        val decoded = json.decodeFromString<NetworkPacket>(wire)
        assertEquals("新办公室", decoded.groupName)
        assertEquals("今天下午开会", decoded.announcement)
        assertEquals("群@g1", decoded.groupId)
    }
}
