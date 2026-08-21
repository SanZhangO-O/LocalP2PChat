"""Wire-protocol security layer (Android parity, see SecureWire.kt).

Every TCP connection (group join/query, host relay, mesh link, direct chat,
call media) starts with a handshake of PLAINTEXT JSON lines:

  password modes (query/join/mesh):
    C -> S: hs_start {hsMode, groupId, eph}
    S -> C: hs_ack  {eph}
    C -> S: hs_confirm {mac}        (unconditional — even with an empty
                                     password the client sends a MAC of "")
    S -> C: hs_ok   {mac} | hs_reject {errorMessage}
  direct mode (identity-based, used by direct chats and call media):
    C -> S: hs_start  {hsMode="direct", eph, ident}
    S -> C: hs_ack    {eph, ident, sig}
    C -> S: hs_confirm {sig}

After a successful handshake EVERY subsequent line on the connection is
Base64(nonce || AES-256-GCM(json)) instead of plaintext JSON. Legacy
plaintext packets are rejected (no downgrade).

Key derivation (password modes) — must match Android byte-for-byte:
  transcript  = mode|groupId|ephClient|ephServer   (Kotlin null-rendering
                for a null groupId is the literal "null")
  salt        = sha256(transcript)
  pwKey       = PBKDF2-SHA1(password, salt, 210k, 32)
  sessionKey  = HKDF(ECDH(ephC, ephS) ++ pwKey, salt, "localchat-session-v1")
  clientMac   = HMAC(pwKey, "lc-client|transcript")
  serverMac   = HMAC(pwKey, "lc-server|transcript")

Direct mode:
  transcriptHash = sha256("lc-direct-v1|ephClient|ephServer")
  sig            = Sign(identityKey, transcriptHash)  (both sides, DER)
  sessionKey     = HKDF(ECDH(ephC, ephS), transcriptHash, "lc-direct-v1")

Long-term EC identity keys are persisted per device (see DeviceIdentity);
peers' keys are remembered on first contact (TOFU) and a later change is
treated as a possible MITM and rejected. Users can additionally compare the
short fingerprints ("安全码") shown in the settings screen.
"""

import ctypes
import json
import logging
import os
import sys
import threading
from ctypes import wintypes
from typing import Optional

from .crypto import (
    ECKeyPair,
    KEY_LEN,
    PBKDF2_ITERATIONS,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    constant_time_equals,
    decode_priv,
    decode_pub,
    from_b64,
    generate_ec_key_pair,
    hkdf_sha256,
    hmac_sha256,
    pbkdf2_sha1,
    sha256,
    to_b64,
    verify,
)
from .models import MAX_LINE_LENGTH, NetworkPacket


class Protocol:
    HS_START = "hs_start"
    HS_ACK = "hs_ack"
    HS_CONFIRM = "hs_confirm"
    HS_OK = "hs_ok"
    HS_REJECT = "hs_reject"

    MODE_QUERY = "query"
    MODE_JOIN = "join"
    MODE_MESH = "mesh"
    MODE_DIRECT = "direct"

    # Direct chats / call media: the inner packet identifying the dialer.
    DIRECT_HELLO = "direct_hello"
    DIRECT_ACK = "direct_ack"


INFO_SESSION = b"localchat-session-v1"
INFO_DIRECT = b"lc-direct-v1"

logger = logging.getLogger(__name__)


def _dpapi_call(protect: bool, data: bytes) -> Optional[bytes]:
    """Windows DPAPI CryptProtectData/CryptUnprotectData via ctypes, used to
    encrypt the persisted private identity key at rest (scope: the current
    Windows user — the same protection Chrome/Edge apply to their key files).
    Returns None whenever DPAPI is unavailable or fails; callers then fall
    back to storing the key unencrypted (better than losing the identity,
    which would trigger false MITM warnings on every peer)."""
    if sys.platform != "win32":
        return None

    class _BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _BLOB()
        func = (
            ctypes.windll.crypt32.CryptProtectData
            if protect
            else ctypes.windll.crypt32.CryptUnprotectData
        )
        if not func(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


class WireException(Exception):
    pass


def _render_group_id(group_id: Optional[str]) -> str:
    """Kotlin's "$groupId" renders a null String? as the literal 'null';
    mirror that so both platforms derive identical transcripts."""
    return "null" if group_id is None else group_id


class Wire:
    """One TCP connection: plaintext handshake lines first, then
    authenticated encryption of every packet line. All methods may be called
    from any thread; an internal lock serializes whole-line writes."""

    def __init__(self, read_line, write_line):
        # read_line: () -> Optional[str] (None at stream end)
        # write_line: (str) -> None (raises on failure)
        self._read_line = read_line
        self._write_line = write_line
        self._key: Optional[bytes] = None
        self._lock = threading.Lock()

    def activate(self, session_key: bytes) -> None:
        self._key = session_key

    @property
    def session_key(self) -> Optional[bytes]:
        return self._key

    @property
    def is_secure(self) -> bool:
        return self._key is not None

    def send_packet(self, packet: NetworkPacket) -> None:
        key = self._key
        if key is None:
            raise WireException("wire not secured yet")
        line = to_b64(aes_gcm_encrypt(key, packet.to_json().encode("utf-8")))
        if len(line) > MAX_LINE_LENGTH:
            raise WireException("encrypted line exceeds cap")
        with self._lock:
            self._write_line(line)

    def recv_packet(self) -> Optional[NetworkPacket]:
        """Decrypted packet, or None at stream end. Raises WireException on
        tampering / wrong key — callers must treat that as a dead
        connection."""
        text = self.recv_packet_text()
        if text is None:
            return None
        try:
            return NetworkPacket.from_json(text)
        except Exception as e:
            raise WireException("malformed packet JSON") from e

    def recv_packet_text(self) -> Optional[str]:
        """Decrypted JSON text of the next packet line WITHOUT parsing — lets
        tests assert on the exact wire representation (e.g. that the password
        never appears in a packet). Same failure semantics as recv_packet."""
        line = self._read_line()
        if not line:
            return None
        key = self._key
        if key is None:
            raise WireException("wire not secured yet")
        try:
            blob = from_b64(line)
        except Exception as e:
            raise WireException("malformed encrypted line") from e
        try:
            plain = aes_gcm_decrypt(key, blob)
        except Exception as e:
            raise WireException("decrypt failed (tampered or wrong key)") from e
        return plain.decode("utf-8", "replace")

    def send_raw_encrypted(self, json_str: str) -> None:
        """Send an arbitrary JSON string inside the encrypted envelope — used
        by tests to inject Android-style payloads a NetworkPacket cannot
        represent (unknown keys, extra fields). Same framing and locking as
        send_packet."""
        key = self._key
        if key is None:
            raise WireException("wire not secured yet")
        line = to_b64(aes_gcm_encrypt(key, json_str.encode("utf-8")))
        if len(line) > MAX_LINE_LENGTH:
            raise WireException("encrypted line exceeds cap")
        with self._lock:
            self._write_line(line)

    # ---- handshake-phase plaintext IO (never used once activate() ran) ----

    def send_raw(self, packet: NetworkPacket) -> None:
        with self._lock:
            self._write_line(packet.to_json())

    def send_raw_reject(self, message: str) -> None:
        self.send_raw(NetworkPacket(type=Protocol.HS_REJECT, error_message=message))

    def recv_raw(self) -> Optional[NetworkPacket]:
        line = self._read_line()
        if line is None:
            return None
        try:
            return NetworkPacket.from_json(line)
        except Exception:
            return None


class SecuredWire:
    """Result of a completed handshake: the secured wire plus dispatch info."""

    def __init__(self, wire: Wire, mode: str, group_id: Optional[str], peer_ident: Optional[str]):
        self.wire = wire
        self.mode = mode
        self.group_id = group_id
        self.peer_ident = peer_ident


class Handshake:

    # -------------------------------------------------- password-mode client

    @staticmethod
    def initiate(wire: Wire, mode: str, group_id: Optional[str], password: str) -> Wire:
        """Client side of the query/join/mesh handshake. The confirm exchange
        is UNCONDITIONAL (even with an empty password the client sends a MAC
        derived from ""): a deterministic message flow means a client without
        the password gets a clean "群组密码错误" rejection instead of a
        deadlock, and there is no downgrade to an unauthenticated variant.
        Raises WireException on any failure — callers close the socket and
        surface the message."""
        eph = generate_ec_key_pair()
        eph_c = eph.public_b64
        wire.send_raw(
            NetworkPacket(type=Protocol.HS_START, hs_mode=mode, group_id=group_id, eph=eph_c)
        )
        ack = wire.recv_raw()
        if ack is None:
            raise WireException("对方无响应")
        if ack.type == Protocol.HS_REJECT:
            raise WireException(ack.error_message or "连接被拒绝")
        if ack.type != Protocol.HS_ACK or not ack.eph:
            raise WireException("无效的握手响应")
        eph_s = ack.eph
        try:
            decode_pub(eph_s)
        except Exception as e:
            raise WireException("无效的握手密钥") from e
        transcript = f"{mode}|{_render_group_id(group_id)}|{eph_c}|{eph_s}"
        salt = sha256(transcript.encode("utf-8"))
        pw_key = pbkdf2_sha1(password, salt, PBKDF2_ITERATIONS, KEY_LEN)
        shared = eph.shared(eph_s)
        client_mac = to_b64(
            hmac_sha256(pw_key, f"lc-client|{transcript}".encode("utf-8"))
        )
        wire.send_raw(NetworkPacket(type=Protocol.HS_CONFIRM, mac=client_mac))
        ok = wire.recv_raw()
        if ok is None:
            raise WireException("对方无响应")
        if ok.type == Protocol.HS_REJECT:
            raise WireException(ok.error_message or "连接被拒绝")
        if ok.type != Protocol.HS_OK or not ok.mac:
            raise WireException("握手确认无效")
        expected = hmac_sha256(pw_key, f"lc-server|{transcript}".encode("utf-8"))
        try:
            provided_mac = from_b64(ok.mac)
        except Exception as e:
            raise WireException("无效的握手确认") from e
        if not constant_time_equals(provided_mac, expected):
            raise WireException("对方密码验证失败")
        wire.activate(
            hkdf_sha256(shared + pw_key, salt, INFO_SESSION, KEY_LEN)
        )
        return wire

    # -------------------------------------------------- password-mode server

    @staticmethod
    def accept(wire: Wire, start: NetworkPacket, password_for) -> Optional[SecuredWire]:
        """Server side of the query/join/mesh handshake. [start] is the
        already-read hs_start line. [password_for] resolves the group's
        password: None = no such group on this device (rejected), otherwise
        the password ("" for a group created without one). The confirm + MAC
        exchange is mandatory, so a wrong or missing password always fails
        cleanly. Returns None after sending a rejection."""
        def reject(message: str) -> None:
            try:
                wire.send_raw_reject(message)
            except Exception:
                pass

        mode = start.hs_mode
        if not mode or not start.eph:
            reject("无效的握手")
            return None
        try:
            decode_pub(start.eph)
        except Exception:
            reject("无效的握手密钥")
            return None
        try:
            password = password_for(mode, start.group_id)
        except Exception:
            password = None
        if password is None:
            reject("该设备不存在此群组")
            return None
        eph = generate_ec_key_pair()
        eph_c = start.eph
        eph_s = eph.public_b64
        wire.send_raw(NetworkPacket(type=Protocol.HS_ACK, eph=eph_s))
        transcript = f"{mode}|{_render_group_id(start.group_id)}|{eph_c}|{eph_s}"
        salt = sha256(transcript.encode("utf-8"))
        pw_key = pbkdf2_sha1(password, salt, PBKDF2_ITERATIONS, KEY_LEN)
        confirm = wire.recv_raw()
        if confirm is None or confirm.type != Protocol.HS_CONFIRM or not confirm.mac:
            reject("需要群组密码")
            return None
        expected = hmac_sha256(pw_key, f"lc-client|{transcript}".encode("utf-8"))
        try:
            provided = from_b64(confirm.mac)
        except Exception:
            provided = None
        if provided is None or not constant_time_equals(provided, expected):
            reject("群组密码错误")
            return None
        server_mac = to_b64(
            hmac_sha256(pw_key, f"lc-server|{transcript}".encode("utf-8"))
        )
        wire.send_raw(NetworkPacket(type=Protocol.HS_OK, mac=server_mac))
        shared = eph.shared(eph_c)
        wire.activate(
            hkdf_sha256(shared + pw_key, salt, INFO_SESSION, KEY_LEN)
        )
        return SecuredWire(wire, mode, start.group_id, None)

    # ---------------------------------------------------- direct-mode client

    @staticmethod
    def initiate_direct(
        wire: Wire,
        expected_peer_id: Optional[str],
        on_identity_mismatch=None,
    ) -> SecuredWire:
        """Initiator side of the identity handshake (direct chats, call
        media). [expected_peer_id]: when the peer's device id is already
        known, its remembered identity key is compared (TOFU) and a mismatch
        aborts."""
        me = DeviceIdentity.current
        if me is None:
            raise WireException("本机身份未初始化")
        eph = generate_ec_key_pair()
        eph_a = eph.public_b64
        ident_a = me.public_b64
        wire.send_raw(
            NetworkPacket(
                type=Protocol.HS_START, hs_mode=Protocol.MODE_DIRECT, eph=eph_a, ident=ident_a
            )
        )
        ack = wire.recv_raw()
        if ack is None:
            raise WireException("对方无响应")
        if ack.type == Protocol.HS_REJECT:
            raise WireException(ack.error_message or "连接被拒绝")
        if ack.type != Protocol.HS_ACK or not ack.eph or not ack.ident or not ack.sig:
            raise WireException("无效的握手响应")
        eph_b = ack.eph
        ident_b = ack.ident
        try:
            peer_ident_pub = decode_pub(ident_b)
        except Exception as e:
            raise WireException("对方身份密钥无效") from e
        transcript_hash = sha256(f"lc-direct-v1|{eph_a}|{eph_b}".encode("utf-8"))
        try:
            their_sig = from_b64(ack.sig)
        except Exception:
            raise WireException("无效的签名")
        if not verify(peer_ident_pub, transcript_hash, their_sig):
            raise WireException("对方身份签名验证失败")
        if expected_peer_id and not DeviceIdentity.check_peer(expected_peer_id, ident_b):
            if on_identity_mismatch is not None:
                try:
                    on_identity_mismatch()
                except Exception:
                    pass
            raise WireException("对方身份发生变化，可能存在中间人")
        session_key = hkdf_sha256(
            eph.shared(eph_b), transcript_hash, INFO_DIRECT, KEY_LEN
        )
        my_sig = to_b64(me.sign(transcript_hash))
        wire.send_raw(NetworkPacket(type=Protocol.HS_CONFIRM, sig=my_sig))
        wire.activate(session_key)
        return SecuredWire(wire, Protocol.MODE_DIRECT, None, ident_b)

    # ---------------------------------------------------- direct-mode server

    @staticmethod
    def accept_direct(
        wire: Wire,
        start: NetworkPacket,
        expected_peer_id: Optional[str],
        on_identity_mismatch=None,
    ) -> Optional[SecuredWire]:
        """Acceptor side of the identity handshake; returns None on
        rejection."""
        me = DeviceIdentity.current
        if me is None:
            try:
                wire.send_raw_reject("对方身份无效")
            except Exception:
                pass
            return None
        if start.hs_mode != Protocol.MODE_DIRECT or not start.eph or not start.ident:
            try:
                wire.send_raw_reject("无效的握手")
            except Exception:
                pass
            return None
        eph = generate_ec_key_pair()
        eph_a = start.eph
        eph_b = eph.public_b64
        transcript_hash = sha256(f"lc-direct-v1|{eph_a}|{eph_b}".encode("utf-8"))
        sig = to_b64(me.sign(transcript_hash))
        wire.send_raw(
            NetworkPacket(
                type=Protocol.HS_ACK, eph=eph_b, ident=me.public_b64, sig=sig
            )
        )
        confirm = wire.recv_raw()
        if confirm is None or confirm.type != Protocol.HS_CONFIRM or not confirm.sig:
            return None
        try:
            initiator_ident = decode_pub(start.ident)
        except Exception:
            return None
        try:
            their_sig = from_b64(confirm.sig)
        except Exception:
            return None
        if not verify(initiator_ident, transcript_hash, their_sig):
            return None
        if expected_peer_id and not DeviceIdentity.check_peer(
            expected_peer_id, start.ident, remember=False
        ):
            if on_identity_mismatch is not None:
                try:
                    on_identity_mismatch()
                except Exception:
                    pass
            return None
        session_key = hkdf_sha256(
            eph.shared(eph_a), transcript_hash, INFO_DIRECT, KEY_LEN
        )
        wire.activate(session_key)
        return SecuredWire(wire, Protocol.MODE_DIRECT, None, start.ident)


class DeviceIdentity:
    """Long-term device identity (EC P-256) for direct chats and call media.
    Generated once, stored in a JSON file next to the app data (the private
    key is DPAPI-encrypted for the current Windows user and never leaves this
    machine). TOFU: the first handshake with a peer
    remembers its identity key; a later change aborts the connection
    (possible MITM). [fingerprint] gives the user an out-of-band comparable
    "安全码"."""

    _FILE = "identity.json"

    current: Optional[ECKeyPair] = None

    _lock = threading.Lock()
    _path: Optional[str] = None
    _peers: dict = {}

    @classmethod
    def ensure_loaded(cls, data_dir: str) -> ECKeyPair:
        with cls._lock:
            if cls.current is not None:
                return cls.current
            cls._path = os.path.join(data_dir, cls._FILE)
            pair = None
            try:
                with open(cls._path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                pair = cls._load_pair(doc)
                if pair is None:
                    logger.warning(
                        "identity.json unreadable (corrupt or DPAPI-bound to "
                        "another user); generating a NEW identity — peers that "
                        "knew the old key will see a TOFU mismatch"
                    )
                cls._peers = dict(doc.get("peers") or {})
            except FileNotFoundError:
                pair = None
                cls._peers = {}
            except Exception:
                logger.warning("failed to read identity.json", exc_info=True)
                pair = None
                cls._peers = {}
            if pair is None:
                pair = generate_ec_key_pair()
                cls._save(pair)
            cls.current = pair
            return pair

    @staticmethod
    def _load_pair(doc: dict) -> Optional[ECKeyPair]:
        """Rebuild the key pair from an identity.json document. 'dpapi' keys
        are first unprotected via DPAPI; any failure returns None so the
        caller can decide (regenerate + warn)."""
        scheme = str(doc.get("scheme") or "plain")
        priv_field = doc.get("private")
        pub_field = doc.get("public")
        if not priv_field or not pub_field:
            return None
        try:
            priv_bytes = from_b64(priv_field)
            if scheme == "dpapi":
                priv_bytes = _dpapi_call(False, priv_bytes)
                if priv_bytes is None:
                    return None
            pair = ECKeyPair(decode_priv(to_b64(priv_bytes)))
            if pair.public_b64 != pub_field:
                return None
            return pair
        except Exception:
            return None

    @classmethod
    def install(cls, pair: ECKeyPair) -> None:
        with cls._lock:
            cls.current = pair
            cls._path = None
            cls._peers = {}

    @classmethod
    def _save(cls, pair: ECKeyPair) -> None:
        if cls._path is None:
            return
        try:
            priv_bytes = from_b64(pair.private_b64)
            scheme = "plain"
            protected = _dpapi_call(True, priv_bytes)
            if protected is not None:
                scheme, priv_bytes = "dpapi", protected
            doc = {
                "scheme": scheme,
                "private": to_b64(priv_bytes),
                "public": pair.public_b64,
                "peers": cls._peers,
            }
            tmp = cls._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, cls._path)
        except Exception:
            # A failed save must not be silent: the next start would generate
            # a NEW identity and every peer would flag a TOFU mismatch.
            logger.warning("failed to persist device identity", exc_info=True)

    @classmethod
    def fingerprint(cls) -> str:
        """Short human-comparable fingerprint of the local identity key:
        sha256 of the SPKI DER bytes, first 16 hex chars, uppercase — same
        digest Android computes over key.encoded."""
        cur = cls.current
        if cur is None:
            return ""
        return sha256(from_b64(cur.public_b64)).hex()[:16].upper()

    @classmethod
    def peer_fingerprint(cls, ident_b64: str) -> str:
        try:
            return sha256(from_b64(ident_b64)).hex()[:16].upper()
        except Exception:
            return "????"

    @classmethod
    def forget_peer(cls, peer_id: str) -> None:
        """Drop the remembered identity for [peer_id]. Used when a group member
        list authoritatively changes a peer's endpoint: the old first-contact
        TOFU binding was made against an impostor (or a stale address) and must
        not cause a false MITM rejection when the real member connects.

        TRUST NOTE: the group member list is produced by the group HOST, so
        this deliberately lets the host reset TOFU bindings for member ids.
        A malicious host could use that to un-bind a member before relaying
        its traffic; this is accepted because the host already controls
        routing and addressing for its groups — the device identity store
        only defends against peers OUTSIDE the host's control."""
        if not peer_id:
            return
        with cls._lock:
            if cls._peers.pop(peer_id, None) is not None and cls.current is not None:
                cls._save(cls.current)

    @classmethod
    def has_peer(cls, peer_id: str) -> bool:
        """True when [peer_id] already has a remembered identity key (pure
        lookup, no TOFU side effects). Callers use it to distinguish "the
        handshake proved a KNOWN key" (an address change is then multi-homing
        or DHCP churn, not impersonation) from first contact, where the
        address binding must still be enforced strictly."""
        if not peer_id:
            return False
        with cls._lock:
            return peer_id in cls._peers

    @classmethod
    def check_peer(cls, peer_id: str, ident_b64: str, remember: bool = True) -> bool:
        """TOFU check (and first-contact remember) when [remember] is true;
        when false, an unknown peer is accepted but NOT persisted — callers
        use that to delay binding until an application-level challenge (e.g.
        call_media_hello) has proven the connection belongs to the expected
        call."""
        if not peer_id:
            return True
        with cls._lock:
            known = cls._peers.get(peer_id)
            if known is None:
                if remember:
                    cls._peers[peer_id] = ident_b64
                    if cls.current is not None:
                        cls._save(cls.current)
                    return True
                return True
            return known == ident_b64
