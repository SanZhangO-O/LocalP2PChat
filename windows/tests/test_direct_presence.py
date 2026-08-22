"""Direct-chat presence tests: "the app is running" IS "online".

Each side announces itself to its contacts the moment it starts (configure
-> announce_online), so sessions come up with NO chat opened on either side,
and both outboxes flush over the first session. A contact the local user
removed is MARKED (id + endpoint): the removed peer keeps announcing forever,
but its dials must not resurrect the deleted contact; re-adding clears the
mark (Android parity).

Id roles are fixed by the deterministic dialer rule (the member with the
SMALLER id dials, same rule as the group mesh): B is "aaa-B" and is therefore
always the dialer, A is "zzz-A" and only accepts. That makes "B keeps
announcing after A removed it" the one-sided traffic the removal mark must
survive.

Two full stacks run over loopback (shared HostGroupServer per side, like
test_direct_chat.py). Both sides share one DeviceIdentity because
DeviceIdentity is process-wide.

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.models import ContactRequest, Peer, NetworkPacket
from localchat.network import (
    DirectChatManager,
    HostGroupServer,
    _read_line_bounded,
    make_wire,
)
from localchat.securewire import Handshake, Protocol
from tests.fake_peer import install_identity

A_ID = "zzz-A"  # larger id: never dials, only accepts
B_ID = "aaa-B"  # smaller id: always the presence dialer
NAME_A = "\u5c0fA"  # 小A
NAME_B = "\u5c0fB"  # 小B


def wait_until(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class DirectPresenceTest(unittest.TestCase):
    PORT_A = 19531
    PORT_B = 19532

    def setUp(self):
        # every direct-session handshake signs with DeviceIdentity.current
        install_identity()
        # shrink the presence sweep so "comes online later" converges fast
        self._old_sweep = DirectChatManager.PRESENCE_SWEEP
        DirectChatManager.PRESENCE_SWEEP = 0.1

        self.server_a = HostGroupServer(self.PORT_A)
        self.server_a.ensure_running()
        self.a = DirectChatManager()
        self.a.configure(A_ID, NAME_A, "127.0.0.1", self.PORT_A)
        self.server_a.direct_manager = self.a

        self.server_b = HostGroupServer(self.PORT_B)
        self.server_b.ensure_running()
        self.b = DirectChatManager()
        self.b.configure(B_ID, NAME_B, "127.0.0.1", self.PORT_B)
        self.server_b.direct_manager = self.b

        self.peer_a = Peer(A_ID, NAME_A, "127.0.0.1", self.PORT_A)
        self.peer_b = Peer(B_ID, NAME_B, "127.0.0.1", self.PORT_B)

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server_a.shutdown()
        self.server_b.shutdown()
        DirectChatManager.PRESENCE_SWEEP = self._old_sweep

    def test_announce_establishes_session_without_chat(self):
        """No chat is ever opened: once B (the dialer) knows A, the presence
        sweep alone must bring up a live session BOTH ways. A knows B too
        (both sides have each other as members — a first contact is parked in
        the request box, not accepted)."""
        self.b.add_contact(self.peer_a)
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(
            wait_until(lambda: self.b.is_chat_alive(A_ID)),
            "B must see A online via presence alone",
        )
        self.assertTrue(
            wait_until(lambda: self.a.is_chat_alive(B_ID)),
            "A must see B online via presence alone",
        )
        # the accepting side learned the contact from the handshake
        self.assertTrue(
            wait_until(lambda: any(c.id == B_ID for c in self.a.contacts_list())),
            "A learned B as a contact from the announce",
        )

    def test_presence_reconnects_after_session_death(self):
        """A session that dies mid-air is repaired by the sweep (link repair,
        not status polling): kill B's socket, no user action, and a NEW
        session must replace it on both sides. The failover can be seamless
        (B re-dials within one sweep), so the assertion is session
        REPLACEMENT, not an observable offline gap."""
        self.b.add_contact(self.peer_a)
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive(A_ID)))
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive(B_ID)))
        with self.b._lock:
            old_b = self.b._sessions.get(A_ID)
        with self.a._lock:
            old_a = self.a._sessions.get(B_ID)

        # shutdown (not close): close() on Windows does not unblock a
        # concurrent recv on the same socket, while shutdown() wakes the
        # local reader AND delivers a real FIN to the peer — exactly a
        # mid-air link death
        old_b["sock"].shutdown(socket.SHUT_RDWR)
        self.assertTrue(
            wait_until(
                lambda: self.b.is_chat_alive(A_ID)
                and self.b._sessions.get(A_ID) is not old_b
            ),
            "the sweep must replace B's dead session",
        )
        self.assertTrue(
            wait_until(
                lambda: self.a.is_chat_alive(B_ID)
                and self.a._sessions.get(B_ID) is not old_a
            ),
            "A must end up on the replacement session too",
        )

    def test_offline_message_flushed_by_peers_announce(self):
        """The delivery gap the presence model fixes: A parks a message while
        B is not running; B comes online MUCH later (long after A's redial
        lifetime would have expired) WITHOUT opening any chat -- B's announce
        dials A, and A's outbox flushes over the session."""
        # A's redial must be dead by the time B appears, and A (larger id)
        # never presence-dials B: B's announce has to do ALL the work
        self.a.REDIAL_BACKOFF = 0.05
        self.a.REDIAL_MAX_BACKOFF = 0.05
        self.a.REDIAL_LIFETIME = 0.4
        # B's app is "not running": its shared listener drops direct hellos
        self.server_b.direct_manager = None
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(
            self.a.send_message(B_ID, "\u665a\u5230\u7684\u95ee\u5019")  # 晚到的问候
        )
        # wait past A's redial lifetime so only B's announce can connect
        time.sleep(0.8)
        self.assertFalse(self.a.is_chat_alive(B_ID))

        # B starts LATE with A as a saved contact and merely runs
        self.server_b.direct_manager = self.b
        self.b.configure(B_ID, NAME_B, "127.0.0.1", self.PORT_B,
                         saved_contacts=[self.peer_a])
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u665a\u5230\u7684\u95ee\u5019"
                    for m in self.b.messages_for(A_ID)
                )
            ),
            "B's announce must flush A's outbox",
        )
        # the pending flag flipped on A's side once actually delivered
        self.assertTrue(
            wait_until(
                lambda: not any(
                    m.pending
                    for m in self.a.messages_for(B_ID)
                    if m.content == "\u665a\u5230\u7684\u95ee\u5019"
                )
            ),
            "the message must flip to delivered on A's side",
        )

    def test_removed_contact_not_resurrected_by_announce(self):
        """A removes B; B (the dialer) keeps announcing forever. The announce
        must NOT resurrect B in A's contact list nor open a session — the
        dial lands in A's contact-request box (flagged as re-adding a
        REMOVED member), never silently resurrected and never silently
        dropped."""
        self.b.add_contact(self.peer_a)
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive(B_ID)))

        # the real removal flow: close the session, then drop the contact
        self.a.close_chat(B_ID)
        self.a.remove_contact(B_ID)
        self.assertTrue(wait_until(lambda: not self.a.is_chat_alive(B_ID)))

        # B keeps sweeping; give it several attempts
        time.sleep(0.8)
        self.assertFalse(
            any(c.id == B_ID for c in self.a.contacts_list()),
            "a removed contact must not be resurrected by the peer's announce",
        )
        self.assertFalse(
            self.a.is_chat_alive(B_ID),
            "no session may exist for a removed contact",
        )
        self.assertTrue(
            any(
                r.id == B_ID and r.from_removed
                for r in self.a.contact_requests()
            ),
            "the removed member's dial must be parked in the request box",
        )

    def test_readding_contact_accepts_announce_again(self):
        """Re-adding clears the removal mark: B's very next announce must
        bring the session back. The re-add deliberately carries a DIFFERENT
        endpoint than the one marked at removal (B advertises its LAN IP;
        removal recorded that, DHCP churn changes it again) — the id->endpoint
        link must clear BOTH marks, else B's announces stay blocked."""
        self.b.add_contact(self.peer_a)
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive(B_ID)))
        with self.a._lock:
            marked_endpoint = self.a._removed_id_endpoint.get(B_ID, "")
        self.a.close_chat(B_ID)
        self.a.remove_contact(B_ID)
        time.sleep(0.5)
        self.assertFalse(self.a.is_chat_alive(B_ID))

        # re-add by an address that differs from the marked endpoint
        self.assertNotEqual(
            marked_endpoint, "10.99.99.99:9999", "test setup: endpoints must differ"
        )
        self.a.add_contact(Peer(B_ID, NAME_B, "10.99.99.99", 9999))
        self.assertTrue(
            wait_until(lambda: self.a.is_chat_alive(B_ID)),
            "re-adding must accept the peer again",
        )
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive(A_ID)))

    def test_manual_placeholder_readd_keeps_real_contact(self):
        """Regression: a manual "ip:..." add of an endpoint already known
        under its REAL device id must keep the real contact. The endpoint
        dedupe used to let the placeholder clobber the real one, orphaning
        the chat history keyed by the real id; with the peer offline nothing
        ever migrated it back."""
        # real contact known, session up (A knows B too: both sides recognize
        # each other, so the re-add tests the contact-record dedupe only)
        self.b.add_contact(self.peer_a)
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive(A_ID)))

        # manual re-add by IP of the endpoint AS STORED (the handshake may
        # have re-pointed it at the peer's advertised address): must be a
        # no-op on the contact record — real survives, no placeholder row
        stored = next(c for c in self.b.contacts_list() if c.id == A_ID)
        endpoint = f"{stored.ip_address}:{stored.port}"
        self.b.add_contact(Peer(f"ip:{endpoint}", "manual",
                                stored.ip_address, stored.port))
        ids = [c.id for c in self.b.contacts_list()]
        self.assertEqual(ids, [A_ID], "real contact must survive the re-add")
        self.assertTrue(self.b.is_chat_alive(A_ID), "session untouched")

    def test_placeholder_at_dialed_endpoint_merges_into_real(self):
        """Regression (duplicate member rows): a peer's ADVERTISED address
        may differ from the address the user typed (multi-homed / DHCP
        churn). The manual "ip:..." placeholder then landed BESIDE the known
        real contact and survived every handshake — a duplicate row dialing
        forever, splitting the chat identity. Once the placeholder's dial
        completes the handshake, the placeholder row must MERGE into the
        real contact (single row, session keyed under the real id)."""
        # B knows A's real id; the session re-points the stored contact at
        # A's ADVERTISED address (the machine's LAN IP, not 127.0.0.1).
        # A knows B too, so the merged dial is accepted on A.
        self.b.add_contact(self.peer_a)
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive(A_ID)))
        stored = next(c for c in self.b.contacts_list() if c.id == A_ID)
        self.assertNotEqual(
            stored.ip_address,
            "127.0.0.1",
            "test setup: advertised address must differ from the dial address",
        )

        # manual add of the peer's OTHER address (loopback): placeholder row
        # appears next to the real one and the sweep dials it
        placeholder_id = f"ip:127.0.0.1:{self.PORT_A}"
        self.b.add_contact(Peer(placeholder_id, "manual", "127.0.0.1", self.PORT_A))
        self.assertIn(placeholder_id, [c.id for c in self.b.contacts_list()])

        # the handshake reveals the real id: the placeholder row must merge
        # away — exactly one row left, keyed under the real id
        self.assertTrue(
            wait_until(
                lambda: placeholder_id
                not in [c.id for c in self.b.contacts_list()]
            ),
            "the placeholder row must merge into the real contact",
        )
        self.assertEqual([c.id for c in self.b.contacts_list()], [A_ID])
        self.assertTrue(self.b.is_chat_alive(A_ID), "session alive under real id")

    def test_handshake_revealed_contact_replaces_placeholder(self):
        """The legitimate placeholder replacement: B first knows A only as a
        manually added "ip:..." placeholder; once a handshake reveals the
        real id, the real contact replaces the placeholder (original dedupe
        intent — must keep working after the re-add fix)."""
        placeholder = Peer(
            f"ip:127.0.0.1:{self.PORT_A}", "manual", "127.0.0.1", self.PORT_A
        )
        self.b.add_contact(placeholder)
        # A recognizes B (the real id appears in the hello's peer field, and
        # A knows that member): the placeholder dial is accepted on A and the
        # handshake merges it into the real contact on B.
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())
        self.assertTrue(
            wait_until(lambda: self.b.is_chat_alive(A_ID)),
            "placeholder dials and the handshake must establish the session",
        )
        self.assertTrue(
            wait_until(
                lambda: [c.id for c in self.b.contacts_list()] == [A_ID]
            ),
            "placeholder replaced by the real id",
        )

    def test_concurrent_dials_serialize_to_one_session(self):
        """Regression (mismatched session pairs): an explicit dial racing the
        presence sweep used to establish TWO sessions whose "replace the old
        session" steps landed on DIFFERENT connections on the two sides —
        writes then went to a socket the peer had already closed and
        messages silently never arrived (seen as a ~20% flaky
        oversized-line / delete-propagates failure). The per-peer dial lock
        serializes dials: N concurrent start_chat calls must converge to
        exactly ONE connection, matched on both sides."""
        import threading

        self.b.add_contact(self.peer_a)  # B's presence sweep starts dialing
        # A already knows B BY ITS ADVERTISED identity (a group sync / manual add
        # stores the LAN address): a loopback-stored contact would trip the
        # first-contact address binding and be refused
        self.a.add_contact(self.b.my_peer())  # A knows B: the dials are accepted
        # hammer concurrent explicit dials on top of the presence dial
        results = []

        def dial():
            results.append(self.b.start_chat(self.peer_a))

        threads = [threading.Thread(target=dial) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(
            all(r == A_ID for r in results), f"all dials must resolve: {results}"
        )
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive(B_ID)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive(A_ID)))
        with self.a._lock:
            sa = self.a._sessions.get(B_ID)
        with self.b._lock:
            sb = self.b._sessions.get(A_ID)
        self.assertIsNotNone(sa)
        self.assertIsNotNone(sb)
        self.assertTrue(sa["alive"] and sb["alive"])
        # the pair must be the SAME TCP connection seen from both ends
        self.assertEqual(
            sa["sock"].getsockname(), sb["sock"].getpeername(), "matched pair (A side)"
        )
        self.assertEqual(
            sa["sock"].getpeername(), sb["sock"].getsockname(), "matched pair (B side)"
        )

    def test_manual_add_dials_loud_on_unreachable_peer(self):
        """Regression (silent add): a manual add is an explicit user action —
        its dial must be LOUD. Against an unreachable peer the failure toast
        data (direct_connect_failed) must surface; the old behavior routed
        the add through the (deliberately quiet) presence sweep, so the
        dialog just closed with NO feedback at all."""
        failures = []

        # attach a listener that records connect failures (what the VM turns
        # into a toast via direct_connect_failed -> status_message)
        from localchat.network import DirectChatListener

        class Rec(DirectChatListener):
            def direct_connect_failed(self, peer, reason):
                failures.append((peer, reason))

        self.a.attach(Rec())
        # the manual-add path: loud dial against a dead port
        result = self.a.start_chat(
            Peer("ip:127.0.0.1:1", NAME_B, "127.0.0.1", 1), quiet=False
        )
        self.assertIsNone(result, "dial to a dead port must fail")
        self.assertTrue(wait_until(lambda: len(failures) == 1), "failure must be surfaced")
        _, reason = failures[0]
        self.assertIn("被拒绝", reason, f"reason should explain refusal: {reason}")

    def test_removed_members_dial_parks_in_request_box(self):
        """A is on B's removal list (B removed A as an "ip:..." placeholder
        whose real id was never learned). A dials B: the request must NOT be
        silently dropped and must NOT be silently accepted — it lands in B's
        contact-request box flagged from_removed, and A is told "等待对方确认"
        (an EVENT, not a connect failure)."""
        failures = []
        events = []
        self.b._mark_removed(A_ID, f"127.0.0.1:{self.PORT_A}")
        self.a.on_event = events.append

        from localchat.network import DirectChatListener

        class Rec(DirectChatListener):
            def direct_connect_failed(self, peer, reason):
                failures.append(reason)

        self.a.attach(Rec())
        result = self.a.start_chat(self.peer_b, quiet=False)
        self.assertIsNone(result, "a parked request is not a session")
        self.assertFalse(failures, "parked must not surface as a failure")
        self.assertTrue(
            wait_until(
                lambda: any(
                    r.id == A_ID and r.from_removed
                    for r in self.b.contact_requests()
                )
            ),
            "the removed member's dial must park in B's request box",
        )
        self.assertTrue(
            wait_until(lambda: any("\u7b49\u5f85\u5bf9\u65b9" in e for e in events)),
            f"dialer must surface the waiting event: {events}",
        )

    def test_first_contact_parks_and_accept_connects(self):
        """Full first-contact flow over a real network: B dials A whom A has
        never met — no session, the request parks in A's box, B is told
        "等待对方确认". When A ACCEPTS, the member is added (no removal marks
        anywhere) and B's presence sweep re-dials into a live session; a
        message A queued meanwhile flushes over it (both outboxes work)."""
        self.b.add_contact(self.peer_a)
        # B's own presence sweep keeps re-dialing A in parallel (quiet): the
        # box dedupes those dials, so they neither stack rows nor re-toast.

        self.assertFalse(
            any(r.id == B_ID for r in self.a.contact_requests()),
            "box starts empty",
        )
        failures = []
        from localchat.network import DirectChatListener

        class Rec(DirectChatListener):
            def direct_connect_failed(self, peer, reason):
                failures.append(reason)

        self.a.attach(Rec())
        result = self.b.start_chat(self.peer_a, quiet=False)
        self.assertIsNone(result, "first contact must not open a session")
        self.assertFalse(self.b.is_chat_alive(A_ID))
        self.assertFalse(failures, "pending must not surface as a failure")
        self.assertTrue(
            wait_until(lambda: any(r.id == B_ID for r in self.a.contact_requests())),
            "the first contact must park in A's request box",
        )
        req = next(
            r for r in self.a.contact_requests() if r.id == B_ID
        )
        self.assertFalse(req.from_removed, "first contact is not a re-add")
        self.assertEqual(req.name, NAME_B)

        self.a.accept_contact_request(B_ID)
        self.assertTrue(
            wait_until(lambda: self.a.contact_requests() == []),
            "accepting clears the box entry",
        )
        self.assertTrue(
            any(c.id == B_ID for c in self.a.contacts_list()),
            "accepting adds the member",
        )
        # A's acceptance announces; B's presence sweep re-dials (quiet) into
        # a KNOWN member and the session comes up
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive(A_ID)))
        self.assertTrue(
            wait_until(lambda: self.a.is_chat_alive(B_ID)),
            "B must see A online after the acceptance",
        )
        # both sides' outboxes work over the accepted session
        self.assertTrue(
            self.a.send_message(B_ID, "\u4f60\u597d\uff0c\u6211\u662fA")  # 你好，我是A
        )
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u4f60\u597d\uff0c\u6211\u662fA"
                    for m in self.b.messages_for(A_ID)
                )
            ),
            "a message must flush over the accepted session",
        )

    def test_dialer_told_request_is_pending_not_failed(self):
        """The Android request box answers a first-contact dial with
        "direct_pending" instead of accepting or hanging up: the dialer must
        surface "等待对方确认" as an EVENT — never as a connect failure (the
        old code mapped it to 'bad direct_ack' and toasted a refusal)."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            conn, _ = srv.accept()
            try:
                reader = conn.makefile("r", encoding="utf-8", newline="\n")
                first_line = _read_line_bounded(reader)
                start = NetworkPacket.from_json(first_line)
                wire = make_wire(conn, reader)
                Handshake.accept_direct(wire, start, None)
                hello = wire.recv_packet()
                if hello.type == Protocol.DIRECT_HELLO:
                    wire.send_packet(NetworkPacket(type=Protocol.DIRECT_PENDING))
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()

        events = []
        failures = []
        self.a.on_event = events.append

        from localchat.network import DirectChatListener

        class Rec(DirectChatListener):
            def direct_connect_failed(self, peer, reason):
                failures.append(reason)

        self.a.attach(Rec())
        result = self.a.start_chat(
            Peer(f"ip:127.0.0.1:{port}", "BoxPeer", "127.0.0.1", port), quiet=False
        )
        self.assertIsNone(result, "a pending request is not a session")
        self.assertFalse(failures, "pending must not surface as a failure")
        self.assertTrue(
            wait_until(lambda: any("\u7b49\u5f85\u5bf9\u65b9" in e for e in events)),
            f"dialer must surface the waiting event: {events}",
        )

    def test_removed_marks_snapshot_roundtrip(self):
        """The persistence round-trip: removed_marks() snapshots id+endpoint
        marks; restore_removed_marks() reinstates them (expired entries are
        dropped) so a restart keeps honoring the removal."""
        self.a.add_contact(self.peer_b)
        self.a.remove_contact(B_ID)
        ids, endpoints = self.a.removed_marks()
        self.assertIn(B_ID, ids, "id mark recorded")
        self.assertIn(f"127.0.0.1:{self.PORT_B}", endpoints, "endpoint mark recorded")

        # an expired mark must not survive the restore
        stale = DirectChatManager()
        stale.restore_removed_marks(
            {"dev-X": time.time() - DirectChatManager.REMOVED_MARK_TTL - 1}, {}
        )
        got_ids, _ = stale.removed_marks()
        self.assertNotIn("dev-X", got_ids, "expired marks are dropped")

    def test_request_box_dedupes_by_id_and_endpoint(self):
        """The box keeps ONE row per peer — by device id AND by endpoint (a
        placeholder-era peer re-requesting from a new address must not stack
        two rows). A NEW entry alone raises the event, so B's presence sweep
        re-dialing every minute cannot toast in a loop."""
        events = []
        self.a.on_event = events.append
        box_peer = Peer("dev-1", "Alice", "192.168.1.5", 9999)
        self.a.record_contact_request(box_peer, from_removed=False)
        self.a.record_contact_request(box_peer, from_removed=False)  # sweep re-dial
        self.a.record_contact_request(
            Peer("dev-2", "Bob", "192.168.1.6", 9999), from_removed=False
        )
        # same endpoint under a different id (placeholder-era re-request):
        # the LATEST request replaces the stale row
        self.a.record_contact_request(
            Peer("dev-3", "Alice2", "192.168.1.5", 9999), from_removed=False
        )

        ids = [r.id for r in self.a.contact_requests()]
        self.assertEqual(sorted(ids), ["dev-2", "dev-3"], ids)
        # dev-3 replaces dev-1's box slot (same endpoint): the replacement is
        # an UPDATE, not a new request — no second toast for it
        self.assertEqual(len(events), 2, "only NEW entries toast")

    def test_request_box_capped(self):
        """A LAN scanner hammering the port must not be able to grow the box
        without bound: oldest entries fall off past REQUEST_BOX_MAX."""
        self.a.record_contact_request(
            Peer("first", "F", "192.168.1.1", 9999), from_removed=False
        )
        for i in range(200):
            self.a.record_contact_request(
                Peer(f"dev-{i}", f"S{i}", f"192.168.1.{i % 250 + 2}", 9999),
                from_removed=False,
            )
        requests = self.a.contact_requests()
        self.assertLessEqual(len(requests), self.a.REQUEST_BOX_MAX)
        self.assertFalse(
            any(r.id == "first" for r in requests), "oldest entries fall off"
        )

    def test_request_box_restore_roundtrip(self):
        """The persistence round-trip: contact_requests() snapshots the box,
        restore_contact_requests() reinstates it, deduping by id AND endpoint
        (the same slot semantics the live box uses)."""
        self.a.record_contact_request(
            Peer("dev-1", "Alice", "192.168.1.5", 9999), from_removed=True
        )
        self.a.record_contact_request(
            Peer("dev-2", "Bob", "192.168.1.6", 9999), from_removed=False
        )
        saved = self.a.contact_requests()

        fresh = DirectChatManager()
        fresh.restore_contact_requests(saved)
        self.assertEqual(
            [r.id for r in fresh.contact_requests()], ["dev-1", "dev-2"]
        )
        self.assertTrue(fresh.contact_requests()[0].from_removed)

        # a stale saved row must not duplicate its live replacement: the
        # live slot wins (it is the newer request), the saved row is skipped
        stale = DirectChatManager()
        stale.record_contact_request(
            Peer("dev-1", "Alice", "192.168.1.5", 9999), from_removed=False
        )
        stale.restore_contact_requests(saved)
        ids = [r.id for r in stale.contact_requests()]
        self.assertEqual(
            ids, ["dev-1", "dev-2"], f"no duplicate row across restore: {ids}"
        )
        self.assertFalse(
            stale.contact_requests()[0].from_removed,
            "the live (newer) request keeps its slot",
        )

        # saved rows themselves dedupe by id AND endpoint: two saved rows
        # sharing an endpoint collapse to one
        fresh2 = DirectChatManager()
        fresh2.restore_contact_requests(
            [
                ContactRequest("dev-1", "Alice", "192.168.1.5", 9999, True, 1),
                ContactRequest("dev-3", "Alice2", "192.168.1.5", 9999, False, 2),
                ContactRequest("dev-2", "Bob", "192.168.1.6", 9999, False, 3),
            ]
        )
        ids2 = [r.id for r in fresh2.contact_requests()]
        self.assertEqual(
            ids2, ["dev-1", "dev-2"], f"endpoint dedupe inside saved rows: {ids2}"
        )

    def test_ignore_request_never_adds_contact(self):
        """Ignoring a request drops the entry without touching contacts; the
        member is never auto-added and no removal mark is created."""
        self.a.record_contact_request(
            Peer("dev-1", "Alice", "192.168.1.5", 9999), from_removed=False
        )
        self.a.ignore_contact_request("dev-1")
        self.assertEqual(self.a.contact_requests(), [])
        self.assertFalse(
            any(c.id == "dev-1" for c in self.a.contacts_list()),
            "ignoring never adds the member",
        )
        self.assertNotIn("dev-1", self.a.removed_marks()[0])


if __name__ == "__main__":
    unittest.main()
