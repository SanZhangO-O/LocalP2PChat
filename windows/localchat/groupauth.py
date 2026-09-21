"""Group sender identity binding (TOFU signatures) — Android parity: GroupAuth.kt.

Closes the group trust gap that the README documents: group chat / delete /
edit previously only required holding the group password, so a leaked password
meant any member could forge ANY author. Group messages, delete tombstones,
edit rewrites and owner management packets (group_update / kick_member) now
optionally carry the author's device identity signature:

  senderPubId  Base64 long-term identity public key (same key + encoding as
               the direct-mode handshake "ident" field)
  senderSig    Base64 ECDSA-SHA256 (DER) signature over the transcript hash

Transcript (must stay byte-identical to GroupAuth.kt; parts are length-
prefixed so "|" inside ids/content can never split them):

  domain   = "lc-group-v1"
  part(p)  = len(utf8(p)) ":" p
  input    = domain + "|" + "|".join(part(p) for p in parts)
  digest   = sha256(utf8(input))
  sig      = Sign(deviceIdentityKey, digest)      (same as direct mode)

  message (chat/file_message/mesh_chat/history entries):
      parts = ["msg", groupId, senderId, messageId, str(timestamp),
               hex(sha256(utf8(content)))]
  delete tombstone:
      parts = ["del", groupId, senderId, messageId]
  edit rewrite (edit_message senderSig covers the EDITED body's message
  transcript, so the verified signature can be stored on the mesh history
  copy and later history pushes pass verify_message):
      parts = message transcript of the edited body (see message_fields_parts)
  group_update (owner only):
      parts = ["gupd", groupId, senderId, groupName-or-"", announcement-or-""]
  kick_member (owner only):
      parts = ["kick", groupId, senderId, targetId]

TOFU / upgrade policy (mirrors the seq upgrade exactly, except the key
BINDING persists and the "signatures required" flag is per process run):

  - unsigned packet, sender never bound in this run  -> accept (old peer)
  - unsigned packet, sender already signed this run  -> reject (missing
    signature after enforcement began = replay-class violation)
  - signed packet, signature invalid                 -> reject (never relax)
  - signed packet, signature valid:
      first sight  -> TOFU-record the key under "group|<groupId>|<senderId>"
      known key    -> must match the record or the packet is rejected
      (fingerprint mismatch = possible MITM, never accepted)

The persistent TOFU record lives in DeviceIdentity's peer store (identity.json
on Windows, SharedPreferences on Android) under the composite key, so the
安全码 UI can show it. Enforcement ("signed once -> always signed") follows the
seq precedent and lives per process run: persisted message history loses its
signature fields across a restart (the local DB does not store them), so a
receiver that enforced forever would refuse every history backfill from a
restarted peer. The residual window — an unsigned forgery for an already-bound
sender accepted right after THIS device restarts, before any signed packet
from that sender arrives — is the "observe first, enforce then" tradeoff the
README documents; forged SIGNATURES and identity CHANGES are always rejected.

Verification must GATE every listener / persistence callback (LESSONS
2026-09-19 #3): callers drop the packet before notifying the ViewModel.
"""

import logging
import threading

from .crypto import sha256, to_b64, verify_b64
from .securewire import DeviceIdentity

logger = logging.getLogger(__name__)

# Transcript domain separator (Android parity: GroupAuth.GROUP_SIGN_DOMAIN).
GROUP_SIGN_DOMAIN = "lc-group-v1"

# Process-run enforcement state: composite sender keys that have carried at
# least one VALID signature. Once a key is here, unsigned packets from that
# sender are rejected (seq-style upgrade). Guarded by [enforce_lock].
enforce_lock = threading.Lock()
_enforced: set = set()


def group_sender_key(group_id: str, sender_id: str) -> str:
    """Composite DeviceIdentity key for a group member's device binding.
    Cannot collide with direct-chat peer ids (UUIDs)."""
    return f"group|{group_id}|{sender_id}"


def _part(p: str) -> str:
    raw = p.encode("utf-8")
    return f"{len(raw)}:{p}"


def transcript_hash(parts) -> bytes:
    """sha256 over the length-prefixed transcript (byte-parity with Kotlin:
    lengths are UTF-8 byte counts and the digest input is UTF-8)."""
    payload = "|".join([GROUP_SIGN_DOMAIN] + [_part(str(p)) for p in parts])
    return sha256(payload.encode("utf-8"))


def content_digest(content: str) -> str:
    return sha256(str(content).encode("utf-8")).hex()


def message_parts(group_id: str, msg) -> list:
    """Transcript parts for a ChatMessage (content-bound)."""
    return [
        "msg",
        str(group_id),
        str(msg.sender_id),
        str(msg.id),
        str(int(msg.timestamp)),
        content_digest(msg.content),
    ]


def delete_parts(group_id: str, sender_id: str, message_id: str) -> list:
    return ["del", str(group_id), str(sender_id), str(message_id)]


def group_update_parts(
    group_id: str, sender_id: str, group_name: str, announcement: str
) -> list:
    return [
        "gupd",
        str(group_id),
        str(sender_id),
        str(group_name or ""),
        str(announcement or ""),
    ]


def kick_parts(group_id: str, sender_id: str, target_id: str) -> list:
    return ["kick", str(group_id), str(sender_id), str(target_id)]


def _identity():
    return DeviceIdentity.current


def sign_parts(parts) -> tuple:
    """Sign a transcript with this device's identity key. Returns
    (pub_b64, sig_b64), or ("", "") when the local identity is not initialized
    (legacy behavior: send unsigned)."""
    me = _identity()
    if me is None:
        return "", ""
    try:
        return me.public_b64, to_b64(me.sign(transcript_hash(parts)))
    except Exception:
        logger.warning("group sign failed", exc_info=True)
        return "", ""


def sign_message(group_id: str, message) -> None:
    """Attach the author signature to [message] in place (sender fields must
    already be final: id/timestamp/content/sender_id)."""
    pub, sig = sign_parts(message_parts(group_id, message))
    if pub:
        message.sender_pub_id = pub
        message.sender_sig = sig


def sign_packet(packet, parts) -> None:
    """Attach the author signature to a NetworkPacket's senderPubId/senderSig
    (used by packets that claim authorship without a ChatMessage)."""
    pub, sig = sign_parts(parts)
    if pub:
        packet.sender_pub_id = pub
        packet.sender_sig = sig


def _check(group_id: str, sender_id: str, pub_b64, sig_b64, parts) -> bool:
    """Shared verification: upgrade policy + cryptographic check + TOFU.
    Returns True when the packet may be accepted."""
    key = group_sender_key(group_id, sender_id)
    if not pub_b64 or not sig_b64:
        with enforce_lock:
            enforced = key in _enforced
        if enforced:
            logger.warning(
                "reject group packet from %r in %r: signature missing after "
                "the sender began signing (replayed, forged or stripped)",
                sender_id,
                group_id,
            )
            return False
        # legacy peer: unsigned is the norm until it signs once
        return True
    if not verify_b64(pub_b64, transcript_hash(parts), sig_b64):
        logger.warning(
            "reject group packet from %r in %r: invalid senderSig", sender_id, group_id
        )
        return False
    # TOFU: first valid signature binds the key; a later different key is a
    # possible MITM / identity theft and must never be accepted.
    if not DeviceIdentity.check_peer(key, pub_b64):
        logger.warning(
            "reject group packet from %r in %r: sender identity changed "
            "(TOFU fingerprint mismatch)",
            sender_id,
            group_id,
        )
        return False
    with enforce_lock:
        _enforced.add(key)
    return True


def message_fields_parts(
    group_id: str, sender_id: str, message_id: str, timestamp: int, content: str
) -> list:
    """Transcript parts for a message known only by its fields: an
    edit_message packet carries the author's signature over the EDITED body's
    message transcript (no ChatMessage object exists on the wire). Byte-parity
    with message_parts."""
    return [
        "msg",
        str(group_id),
        str(sender_id),
        str(message_id),
        str(int(timestamp)),
        content_digest(content),
    ]


def verify_message(group_id: str, message) -> bool:
    """Full policy check for an inbound group ChatMessage."""
    return _check(
        group_id,
        message.sender_id,
        message.sender_pub_id,
        message.sender_sig,
        message_parts(group_id, message),
    )


def verify_message_fields(
    group_id: str,
    sender_id: str,
    message_id: str,
    timestamp: int,
    content: str,
    pub_b64,
    sig_b64,
) -> bool:
    """verify_message for the signature carried by an edit_message packet: the
    receiver rebuilds the message transcript from its LOCAL copy's identity
    fields (id/timestamp/author — immutable) plus the edit's new content.
    Strict: an unsigned edit is refused here — an edit rewrites stored
    history and is never tolerated without a signature."""
    if not pub_b64 or not sig_b64:
        return False
    return _check(
        group_id,
        sender_id,
        pub_b64,
        sig_b64,
        message_fields_parts(group_id, sender_id, message_id, timestamp, content),
    )


def verify_delete(group_id: str, sender_id: str, message_id: str, pub_b64, sig_b64) -> bool:
    return _check(
        group_id, sender_id, pub_b64, sig_b64, delete_parts(group_id, sender_id, message_id)
    )


def verify_group_update(
    group_id: str, sender_id: str, group_name, announcement, pub_b64, sig_b64
) -> bool:
    return _check(
        group_id,
        sender_id,
        pub_b64,
        sig_b64,
        group_update_parts(group_id, sender_id, group_name, announcement),
    )


def verify_kick(group_id: str, sender_id: str, target_id: str, pub_b64, sig_b64) -> bool:
    return _check(
        group_id, sender_id, pub_b64, sig_b64, kick_parts(group_id, sender_id, target_id)
    )


# ----------------------------------------------------------------- TOFU UI

def member_verified(group_id: str, sender_id: str) -> bool:
    """True when the member has a TOFU-bound device identity in this group
    (it sent at least one validly signed packet we still remember)."""
    if not group_id or not sender_id:
        return False
    return DeviceIdentity.has_peer(group_sender_key(group_id, sender_id))


def member_fingerprint(group_id: str, sender_id: str) -> str:
    """The bound member's 安全码 ("" when unbound)."""
    if not group_id or not sender_id:
        return ""
    ident = DeviceIdentity.peer_ident(group_sender_key(group_id, sender_id))
    if not ident:
        return ""
    return DeviceIdentity.peer_fingerprint(ident)


def reset_enforcement_for_tests() -> None:
    """Test-only: clear the process-run enforcement set (DeviceIdentity peers
    are handled by the tests themselves via DeviceIdentity.install)."""
    with enforce_lock:
        _enforced.clear()
