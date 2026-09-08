package com.zqr.localchat

import com.zqr.localchat.data.Peer
import com.zqr.localchat.network.DirectChatManager
import com.zqr.localchat.network.DirectChatManager.Contact
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Contact-request message box: first-contact / removed-member dials are no
 * longer silently dropped — they park in the box and the user decides.
 * Accept adds the member and clears removal marks; ignore drops the entry
 * without adding anything; dedupe keeps one row per peer even when its
 * presence sweep dials every minute.
 */
class ContactRequestBoxTest {

    @Before
    fun setUp() {
        DirectChatManager.resetForTest()
    }

    @After
    fun tearDown() {
        DirectChatManager.resetForTest()
    }

    private fun peer(id: String, name: String, ip: String = "192.168.1.5", port: Int = 9999) =
        Peer(id, name, ip, port)

    @Test
    fun recordDedupesByIdAndEndpoint() {
        DirectChatManager.recordContactRequest(peer("dev-1", "Alice"), fromRemoved = false)
        // the same peer's presence sweep re-dials: must NOT stack a row
        DirectChatManager.recordContactRequest(peer("dev-1", "Alice"), fromRemoved = false)
        DirectChatManager.recordContactRequest(peer("dev-2", "Bob", "192.168.1.6"), fromRemoved = false)
        // same endpoint under a different id (placeholder-era re-request):
        // same box slot — the LATEST request replaces the stale one
        DirectChatManager.recordContactRequest(peer("dev-3", "Alice2"), fromRemoved = false)

        assertEquals(2, DirectChatManager.contactRequests.value.size)
        val ids = DirectChatManager.contactRequests.value.map { it.id }
        assertTrue("dev-3" in ids && "dev-2" in ids)
        assertFalse("dev-1" in ids)
    }

    @Test
    fun acceptAddsContactAndClearsRemovalMarks() {
        // the user removed this member earlier (marks persisted)
        DirectChatManager.addContact(Contact("dev-1", "Alice", "192.168.1.5", 9999))
        DirectChatManager.removeContact("dev-1")
        val removed = DirectChatManager.removedMarks()
        assertTrue(removed.first.containsKey("dev-1"))

        // the removed member re-requests: it lands in the box, flagged
        DirectChatManager.recordContactRequest(peer("dev-1", "Alice"), fromRemoved = true)
        assertEquals(1, DirectChatManager.contactRequests.value.size)
        assertTrue(DirectChatManager.contactRequests.value[0].fromRemoved)

        // accepting re-adds the member and un-blocks the marks
        DirectChatManager.acceptContactRequest("dev-1")
        assertTrue(DirectChatManager.contactRequests.value.isEmpty())
        assertNotNull(DirectChatManager.contacts.value["dev-1"])
        val (ids, endpoints) = DirectChatManager.removedMarks()
        assertFalse("accept must clear the id mark", ids.containsKey("dev-1"))
        assertFalse("accept must clear the endpoint mark", endpoints.containsKey("192.168.1.5:9999"))
    }

    @Test
    fun ignoreDropsEntryWithoutAddingContact() {
        DirectChatManager.recordContactRequest(peer("dev-1", "Alice"), fromRemoved = false)
        DirectChatManager.ignoreContactRequest("dev-1")

        assertTrue(DirectChatManager.contactRequests.value.isEmpty())
        assertNull("ignoring never adds the member", DirectChatManager.contacts.value["dev-1"])
    }

    @Test
    fun restoreRoundtrip() {
        val saved = listOf(
            DirectChatManager.ContactRequest("dev-1", "Alice", "192.168.1.5", 9999, false, 123L),
            DirectChatManager.ContactRequest("dev-2", "Bob", "192.168.1.6", 9999, true, 456L)
        )
        DirectChatManager.restoreContactRequests(saved)
        assertEquals(saved, DirectChatManager.contactRequests.value)
    }

    @Test
    fun restoreDedupesBySlotAgainstLiveAndSaved() {
        // a live row occupies the dev-1 slot (id AND endpoint): the stale
        // saved row must not duplicate it — the live (newer) request wins
        DirectChatManager.recordContactRequest(peer("dev-1", "Alice"), fromRemoved = false)
        DirectChatManager.restoreContactRequests(
            listOf(
                DirectChatManager.ContactRequest("dev-1", "Alice", "192.168.1.5", 9999, true, 1L),
                DirectChatManager.ContactRequest("dev-3", "Alice2", "192.168.1.5", 9999, false, 2L),
                DirectChatManager.ContactRequest("dev-2", "Bob", "192.168.1.6", 9999, false, 3L)
            )
        )
        val ids = DirectChatManager.contactRequests.value.map { it.id }
        assertEquals(
            "live dev-1 keeps its slot, saved rows dedupe by endpoint",
            listOf("dev-1", "dev-2"), ids
        )
        assertFalse(
            "the live (newer) request keeps its slot",
            DirectChatManager.contactRequests.value[0].fromRemoved
        )
    }

    @Test
    fun boxIsCapped() {
        // a LAN scanner must not be able to grow the box without bound
        repeat(60) { i ->
            DirectChatManager.recordContactRequest(peer("dev-$i", "Scan$i", "192.168.1.$i"), fromRemoved = false)
        }
        assertTrue(DirectChatManager.contactRequests.value.size <= 50)
    }
}