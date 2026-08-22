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
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.models import Peer
from localchat.network import DirectChatManager, HostGroupServer
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
        sweep alone must bring up a live session BOTH ways."""
        self.b.add_contact(self.peer_a)
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
        self.a.add_contact(self.peer_b)
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
        must NOT resurrect B in A's contact list nor open a session."""
        self.b.add_contact(self.peer_a)
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

    def test_readding_contact_accepts_announce_again(self):
        """Re-adding clears the removal mark: B's very next announce must
        bring the session back. The re-add deliberately carries a DIFFERENT
        endpoint than the one marked at removal (B advertises its LAN IP;
        removal recorded that, DHCP churn changes it again) — the id->endpoint
        link must clear BOTH marks, else B's announces stay blocked."""
        self.b.add_contact(self.peer_a)
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

    def test_manual_add_loud_when_peer_drops_hello(self):
        """The nastiest silent case: the peer IS running (handshake succeeds)
        but drops the hello because the local user is on ITS removal list.
        The loud dial must surface a reason that mentions removal — before,
        this mapped to a misleading '网络不可达' or vanished silently."""
        failures = []

        from localchat.network import DirectChatListener

        class Rec(DirectChatListener):
            def direct_connect_failed(self, peer, reason):
                failures.append(reason)

        # B marks A's endpoint as removed BEFORE any contact exists between
        # the two (A's id was never learned on B, e.g. A was removed as an
        # "ip:..." placeholder)
        self.b._mark_removed(A_ID, f"127.0.0.1:{self.PORT_A}")
        self.a.attach(Rec())
        result = self.a.start_chat(self.peer_b, quiet=False)
        self.assertIsNone(result, "a dropped hello must fail the dial")
        self.assertTrue(
            wait_until(lambda: len(failures) == 1), "the drop must be surfaced"
        )
        self.assertIn(
            "\u88ab\u5bf9\u65b9\u79fb\u9664",  # 被对方移除
            failures[0],
            f"reason should mention removal: {failures[0]!r}",
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


if __name__ == "__main__":
    unittest.main()
