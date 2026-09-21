"""Group sender identity binding (TOFU) tests.

Covers windows/localchat/groupauth.py and its network.py wiring:

  - the length-prefixed signing transcript must stay byte-identical to the
    Android side (GroupAuthTest.kt pins the SAME SHA-256 vectors, so a
    divergence on either end fails a test instead of silently breaking
    cross-platform verification);
  - the upgrade policy mirrors seq: unsigned packets keep working for a legacy
    peer, but once a sender has carried a VALID signature a stripped copy is
    rejected;
  - an invalid signature or a changed device key after binding is always
    rejected (never relaxed);
  - verification gates the mesh listener, so forged / stripped messages never
    reach history or the UI.

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat import groupauth
from localchat.crypto import generate_ec_key_pair, to_b64
from localchat.models import ChatMessage, Peer
from localchat.network import GroupMeshListener, GroupMeshManager, HostGroupServer
from tests.fake_peer import install_identity, wait_until


GRP = "\u7fa4G"
OTHER_GRP = "\u522b\u7684\u7fa4"
GROUP_PASSWORD = "123456"

# Cross-platform parity vectors. The digest is sha256 over
#   "|".join(["lc-group-v1"] + ["<utf8-byte-len>:<part>", ...])
# Android's GroupAuthTest.kt must produce the same hex for the same parts.
CONTENT_DIGEST_HELLO = "670d9743542cae3ea7ebe36af56bd53648b0a1126162e78d81a32934a711302e"
MSG_VECTOR_DIGEST = "eb0d6f2304bd047533b6e06f1a76f539488a4675db574f72946165b27abbe7aa"
DELETE_VECTOR_DIGEST = "9da5a23be978fbed247c25801ec9d2cadde9b622e0aab8a1d386b0727c3eb61c"
GROUP_UPDATE_VECTOR_DIGEST = (
    "1798e60563138d304e368d2a5fda6075fa796c1ae24bcd2384e0d63404d7d01a"
)
KICK_VECTOR_DIGEST = "9cf3a652186aa6ece811f0d50fb12f20e97594b27d52bc543839a4e34aaae2f0"


def make_msg(content, sender_id, sender_name, mid, timestamp=1700000000000):
    return ChatMessage(
        id=mid,
        content=content,
        timestamp=timestamp,
        sender_id=sender_id,
        sender_name=sender_name,
    )


class TranscriptParityTest(unittest.TestCase):
    """The transcript itself: byte lengths are UTF-8 counts on both ends."""

    def test_content_digest_matches_android_vector(self):
        # content digest is sha256(utf8(content)).hex() ("\u4f60\u597d")
        self.assertEqual(groupauth.content_digest("\u4f60\u597d"), CONTENT_DIGEST_HELLO)

    def test_message_transcript_matches_android_vector(self):
        msg = make_msg("\u4f60\u597d", "aaa-member", "A", "m-1")
        self.assertEqual(
            groupauth.transcript_hash(groupauth.message_parts(GRP, msg)).hex(),
            MSG_VECTOR_DIGEST,
        )

    def test_delete_transcript_matches_android_vector(self):
        self.assertEqual(
            groupauth.transcript_hash(
                groupauth.delete_parts(GRP, "aaa-member", "m-1")
            ).hex(),
            DELETE_VECTOR_DIGEST,
        )

    def test_group_update_transcript_matches_android_vector(self):
        self.assertEqual(
            groupauth.transcript_hash(
                groupauth.group_update_parts(
                    GRP, "aaa-member", "\u65b0\u540d", "\u516c\u544a"
                )
            ).hex(),
            GROUP_UPDATE_VECTOR_DIGEST,
        )

    def test_kick_transcript_matches_android_vector(self):
        self.assertEqual(
            groupauth.transcript_hash(
                groupauth.kick_parts(GRP, "aaa-member", "bbb-member")
            ).hex(),
            KICK_VECTOR_DIGEST,
        )

    def test_parts_are_length_prefixed_so_separators_cannot_be_split(self):
        # "a|b" must not collide with the two-part transcript ["a", "b"]
        joined = groupauth.transcript_hash(["x", "a|b"])
        split = groupauth.transcript_hash(["x", "a", "b"])
        self.assertNotEqual(joined, split)


class PolicyTest(unittest.TestCase):
    """Signature policy: observe first, enforce after the sender signs."""

    def setUp(self):
        groupauth.reset_enforcement_for_tests()
        install_identity()

    def test_unsigned_is_accepted_until_the_sender_signs_then_rejected(self):
        legacy = make_msg("legacy", "aaa-member", "A", "u-1")
        self.assertTrue(groupauth.verify_message(GRP, legacy))
        signed = make_msg("signed", "aaa-member", "A", "u-2")
        groupauth.sign_message(GRP, signed)
        self.assertTrue(signed.sender_sig, "the local identity must sign")
        self.assertTrue(groupauth.verify_message(GRP, signed))
        # the sender is bound now: a stripped copy is a replay-class violation
        self.assertFalse(groupauth.verify_message(GRP, legacy))
        # another sender is still allowed to be legacy
        other = make_msg("legacy too", "bbb-member", "B", "u-3")
        self.assertTrue(groupauth.verify_message(GRP, other))

    def test_enforcement_is_scoped_to_the_group(self):
        signed = make_msg("signed", "aaa-member", "A", "s-1")
        groupauth.sign_message(GRP, signed)
        self.assertTrue(groupauth.verify_message(GRP, signed))
        legacy = make_msg("legacy", "aaa-member", "A", "s-2")
        self.assertFalse(groupauth.verify_message(GRP, legacy))
        self.assertTrue(
            groupauth.verify_message(OTHER_GRP, legacy),
            "another group must keep accepting this sender's legacy traffic",
        )

    def test_tampered_content_fails_signature(self):
        msg = make_msg("honest", "aaa-member", "A", "t-1")
        groupauth.sign_message(GRP, msg)
        msg.content = "forged"
        self.assertFalse(groupauth.verify_message(GRP, msg))

    def test_tampered_timestamp_or_id_fails_signature(self):
        msg = make_msg("honest", "aaa-member", "A", "t-2")
        groupauth.sign_message(GRP, msg)
        self.assertTrue(msg.sender_sig, "the local identity must sign")
        # replay the SAME signature with a tampered timestamp / id: the
        # transcript covers both, so the signature must not verify
        clone = make_msg("honest", "aaa-member", "A", "t-2")
        clone.sender_pub_id = msg.sender_pub_id
        clone.sender_sig = msg.sender_sig
        clone.timestamp = msg.timestamp + 1
        self.assertFalse(groupauth.verify_message(GRP, clone))
        clone2 = make_msg("honest", "aaa-member", "A", "t-3")
        clone2.sender_pub_id = msg.sender_pub_id
        clone2.sender_sig = msg.sender_sig
        clone2.timestamp = msg.timestamp
        self.assertFalse(groupauth.verify_message(GRP, clone2))

    def test_garbage_signature_is_rejected(self):
        msg = make_msg("honest", "aaa-member", "A", "t-4")
        groupauth.sign_message(GRP, msg)
        msg.sender_sig = "AAAA"
        self.assertFalse(groupauth.verify_message(GRP, msg))
        msg.sender_pub_id = "not-base64!!"
        msg.sender_sig = "AAAA"
        self.assertFalse(groupauth.verify_message(GRP, msg))

    def test_identity_change_after_binding_is_rejected(self):
        signed = make_msg("mine", "aaa-member", "A", "i-1")
        groupauth.sign_message(GRP, signed)
        self.assertTrue(groupauth.verify_message(GRP, signed))
        # an attacker holds the group password, knows the message id/content
        # and signs the SAME transcript with its own device key
        attacker = generate_ec_key_pair()
        forged = make_msg("mine", "aaa-member", "A", "i-1")
        forged.sender_pub_id = attacker.public_b64
        forged.sender_sig = to_b64(
            attacker.sign(groupauth.transcript_hash(groupauth.message_parts(GRP, forged)))
        )
        self.assertFalse(
            groupauth.verify_message(GRP, forged),
            "a changed device key for a bound sender must be rejected (MITM)",
        )

    def test_delete_edit_update_and_kick_roundtrip(self):
        pub, sig = groupauth.sign_parts(
            groupauth.delete_parts(GRP, "aaa-member", "d-1")
        )
        self.assertTrue(pub and sig)
        self.assertTrue(
            groupauth.verify_delete(GRP, "aaa-member", "d-1", pub, sig)
        )
        self.assertFalse(
            groupauth.verify_delete(GRP, "aaa-member", "d-2", pub, sig)
        )
        self.assertFalse(
            groupauth.verify_delete(OTHER_GRP, "aaa-member", "d-1", pub, sig)
        )

        pub, sig = groupauth.sign_parts(
            groupauth.edit_parts(GRP, "aaa-member", "e-1", "after")
        )
        self.assertTrue(
            groupauth.verify_edit(GRP, "aaa-member", "e-1", "after", pub, sig)
        )
        self.assertFalse(
            groupauth.verify_edit(GRP, "aaa-member", "e-1", "other", pub, sig)
        )

        pub, sig = groupauth.sign_parts(
            groupauth.group_update_parts(GRP, "aaa-member", "new", "ann")
        )
        self.assertTrue(
            groupauth.verify_group_update(GRP, "aaa-member", "new", "ann", pub, sig)
        )
        self.assertFalse(
            groupauth.verify_group_update(GRP, "aaa-member", "new", "changed", pub, sig)
        )

        pub, sig = groupauth.sign_parts(
            groupauth.kick_parts(GRP, "aaa-member", "bbb-member")
        )
        self.assertTrue(
            groupauth.verify_kick(GRP, "aaa-member", "bbb-member", pub, sig)
        )
        self.assertFalse(
            groupauth.verify_kick(GRP, "aaa-member", "ccc-member", pub, sig)
        )

    def test_unsigned_delete_follows_the_same_upgrade_policy(self):
        self.assertTrue(
            groupauth.verify_delete(GRP, "aaa-member", "d-3", None, None)
        )
        pub, sig = groupauth.sign_parts(
            groupauth.delete_parts(GRP, "aaa-member", "d-4")
        )
        self.assertTrue(groupauth.verify_delete(GRP, "aaa-member", "d-4", pub, sig))
        self.assertFalse(
            groupauth.verify_delete(GRP, "aaa-member", "d-5", None, None),
            "unsigned delete after the author signed must be rejected",
        )

    def test_member_verified_and_fingerprint(self):
        self.assertFalse(groupauth.member_verified(GRP, "aaa-member"))
        self.assertEqual(groupauth.member_fingerprint(GRP, "aaa-member"), "")
        signed = make_msg("hi", "aaa-member", "A", "v-1")
        groupauth.sign_message(GRP, signed)
        self.assertFalse(
            groupauth.member_verified(GRP, "aaa-member"),
            "verification happens on receive, not on send",
        )
        self.assertTrue(groupauth.verify_message(GRP, signed))
        self.assertTrue(groupauth.member_verified(GRP, "aaa-member"))
        fingerprint = groupauth.member_fingerprint(GRP, "aaa-member")
        self.assertEqual(fingerprint, groupauth.DeviceIdentity.fingerprint())
        self.assertEqual(len(fingerprint), 16)

    def test_signing_is_a_noop_without_an_identity(self):
        from localchat.securewire import DeviceIdentity

        old = DeviceIdentity.current
        try:
            DeviceIdentity.current = None
            msg = make_msg("x", "aaa-member", "A", "n-1")
            groupauth.sign_message(GRP, msg)
            self.assertIsNone(msg.sender_pub_id)
            self.assertIsNone(msg.sender_sig)
            self.assertTrue(groupauth.verify_message(GRP, msg))
        finally:
            DeviceIdentity.current = old


class MeshIdentityTest(unittest.TestCase):
    """End-to-end over the real secured mesh: the gate is before the listener."""

    PORT_A = 19741
    PORT_B = 19742

    class Rec(GroupMeshListener):
        def __init__(self):
            self.messages = []  # (group_id, ChatMessage)

        def group_mesh_message(self, group_id, msgs):
            for m in msgs:
                self.messages.append((group_id, m))

    def setUp(self):
        groupauth.reset_enforcement_for_tests()
        install_identity()
        self._old_retry = GroupMeshManager.RETRY_INTERVAL
        GroupMeshManager.RETRY_INTERVAL = 0.05
        self.server_a = HostGroupServer(self.PORT_A)
        self.server_a.ensure_running()
        self.rec_a = self.Rec()
        self.a = GroupMeshManager()
        self.a.attach(self.rec_a)
        self.server_a.mesh_manager = self.a
        self.server_a.password_lookup = lambda mode, gid: self.a.password_for(gid)
        self.server_b = HostGroupServer(self.PORT_B)
        self.server_b.ensure_running()
        self.rec_b = self.Rec()
        self.b = GroupMeshManager()
        self.b.attach(self.rec_b)
        self.server_b.mesh_manager = self.b
        self.server_b.password_lookup = lambda mode, gid: self.b.password_for(gid)
        self.peer_a = Peer("aaa-member", "A", "127.0.0.1", self.PORT_A)
        self.peer_b = Peer("bbb-member", "B", "127.0.0.1", self.PORT_B)
        self.b.enter_group(GRP, self.peer_b, [self.peer_a], [], GROUP_PASSWORD)
        self.a.enter_group(GRP, self.peer_a, [self.peer_b], [], GROUP_PASSWORD)
        self.assertTrue(
            wait_until(lambda: self.a.has_links(GRP) and self.b.has_links(GRP))
        )

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server_a.shutdown()
        self.server_b.shutdown()
        GroupMeshManager.RETRY_INTERVAL = self._old_retry

    def test_signed_mesh_message_binds_the_member_identity(self):
        msg = make_msg("signed hello", "aaa-member", "A", "m-bind-1")
        groupauth.sign_message(GRP, msg)
        self.a.broadcast(GRP, msg)
        self.assertTrue(
            wait_until(lambda: any(m.id == "m-bind-1" for _, m in self.rec_b.messages)),
            "a validly signed message must be accepted",
        )
        self.assertTrue(groupauth.member_verified(GRP, "aaa-member"))
        self.assertEqual(
            groupauth.member_fingerprint(GRP, "aaa-member"),
            groupauth.DeviceIdentity.fingerprint(),
        )

    def test_tampered_message_never_reaches_the_listener(self):
        msg = make_msg("honest", "aaa-member", "A", "m-forge-1")
        groupauth.sign_message(GRP, msg)
        msg.content = "forged after signing"
        self.a.broadcast(GRP, msg)
        time.sleep(0.6)
        self.assertFalse(
            any(m.id == "m-forge-1" for _, m in self.rec_b.messages),
            "a signature that no longer matches the content must be dropped",
        )
        self.assertFalse(groupauth.member_verified(GRP, "aaa-member"))

    def test_stripped_copy_after_enforcement_is_dropped(self):
        signed = make_msg("one", "aaa-member", "A", "m-strip-1")
        groupauth.sign_message(GRP, signed)
        self.a.broadcast(GRP, signed)
        self.assertTrue(
            wait_until(lambda: any(m.id == "m-strip-1" for _, m in self.rec_b.messages))
        )
        stripped = make_msg("two", "aaa-member", "A", "m-strip-2")
        self.a.broadcast(GRP, stripped)
        time.sleep(0.6)
        self.assertFalse(
            any(m.id == "m-strip-2" for _, m in self.rec_b.messages),
            "after the author signed once, an unsigned copy must be rejected",
        )


if __name__ == "__main__":
    unittest.main()
