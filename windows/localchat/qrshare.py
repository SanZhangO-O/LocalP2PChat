"""QR invite payloads: contact cards and group invites as compact URLs.

The payload is plain text (a URL) so both a QR image and copy/paste carry the
same bytes, and both platforms (Windows / Android) encode and decode it
identically:

    localchat://contact?v=1&n=<name>&f=<security code>&ip=<addr>&p=<port>
    localchat://group?v=1&g=<8-digit id>&n=<group name>&ip=<addr>&p=<port>&r=<relay>

Rules (must stay in sync with network/QrInvite.kt on Android):
- ``v`` is the payload format version; unknown versions are rejected.
- Values are UTF-8 percent-encoded (``quote``/``unquote_plus`` semantics, so a
  "+" round-trips as a space either way).
- contact: ``ip`` required; ``f`` (the peer's 安全码 fingerprint) and ``n``
  are optional; ``p`` defaults to the standard port.
- group: ``g`` required (digits only, 8 digits); ``ip``/``p`` (a member's LAN
  endpoint) and ``r`` (relay server host:port) are optional.
- The group PASSWORD is NEVER part of the payload: scanning still requires
  the joiner to type the group password (the password never leaves the
  handshake path). Any password-looking parameter is ignored on decode.

This module is Qt-free and only imports its optional imaging dependencies
lazily, so the codec works (and is unit-tested) without them.
"""

import ipaddress
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote, unquote_plus

from .models import TCP_PORT

SCHEME = "localchat"
KIND_CONTACT = "contact"
KIND_GROUP = "group"

PAYLOAD_VERSION = "1"

# Fingerprint length: DeviceIdentity fingerprints are 16 uppercase hex chars
# (sha256 prefix). The payload accepts exactly this shape.
_FINGERPRINT_CHARS = set("0123456789ABCDEFabcdef")


class QrPayloadError(ValueError):
    """A QR payload string that is not a valid LocalChat invite."""


@dataclass
class ContactInvite:
    """A scanned contact card: who to add and how to reach them."""

    name: str = ""
    fingerprint: str = ""
    ip: str = ""
    port: int = TCP_PORT


@dataclass
class GroupInvite:
    """A scanned group invite: numeric join id plus an optional endpoint
    (any member can be the join entry point) and optional relay server."""

    group_id: str = ""
    name: str = ""
    ip: str = ""
    port: int = TCP_PORT
    relay: str = ""


def encode_contact_invite(
    name: str, fingerprint: str, ip: str, port: int = TCP_PORT
) -> str:
    parts = [f"v={PAYLOAD_VERSION}"]
    if name:
        parts.append("n=" + quote(name, safe=""))
    if fingerprint:
        parts.append("f=" + quote(fingerprint, safe=""))
    if ip:
        parts.append("ip=" + quote(ip, safe=""))
    if port and port != TCP_PORT:
        parts.append(f"p={int(port)}")
    return f"{SCHEME}://{KIND_CONTACT}?" + "&".join(parts)


def encode_group_invite(
    group_id: str,
    name: str = "",
    ip: str = "",
    port: int = TCP_PORT,
    relay: str = "",
) -> str:
    digits = "".join(ch for ch in str(group_id) if ch.isdigit())
    parts = [f"v={PAYLOAD_VERSION}", f"g={digits}"]
    if name:
        parts.append("n=" + quote(name, safe=""))
    if ip:
        parts.append("ip=" + quote(ip, safe=""))
    if port and port != TCP_PORT:
        parts.append(f"p={int(port)}")
    if relay:
        parts.append("r=" + quote(relay, safe=""))
    return f"{SCHEME}://{KIND_GROUP}?" + "&".join(parts)


def _parse_query(query: str) -> dict:
    fields = {}
    for chunk in query.split("&"):
        if not chunk:
            continue
        if "=" in chunk:
            key, _, value = chunk.partition("=")
        else:
            key, value = chunk, ""
        # unquote_plus: mirrors quote on encode and accepts the "+"-encoded
        # spaces a URL-encoder (URLEncoder on Android) may have produced
        fields[unquote_plus(key)] = unquote_plus(value)
    return fields


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _valid_port(port) -> bool:
    return isinstance(port, int) and 1 <= port <= 65535


def _valid_relay(relay: str) -> bool:
    host, _, port_text = relay.rpartition(":")
    if not host or not port_text.isdigit():
        return False
    try:
        return 1 <= int(port_text) <= 65535
    except ValueError:
        return False


def parse_invite(text: str):
    """Parse a QR payload; returns ContactInvite or GroupInvite.

    Raises QrPayloadError on anything that is not a current-format invite.
    Unknown parameters are ignored (forward compatibility); a password-
    looking parameter is ignored too — the password is never carried in QR.
    """
    raw = (text or "").strip()
    prefix = f"{SCHEME}://"
    if not raw.startswith(prefix):
        raise QrPayloadError("不是 LocalChat 二维码内容")
    rest = raw[len(prefix):]
    kind, sep, query = rest.partition("?")
    if not sep:
        query = ""
    kind = kind.strip("/")
    fields = _parse_query(query)
    if fields.get("v", PAYLOAD_VERSION) != PAYLOAD_VERSION:
        raise QrPayloadError("二维码版本不受支持")
    if kind == KIND_CONTACT:
        return _parse_contact(fields)
    if kind == KIND_GROUP:
        return _parse_group(fields)
    raise QrPayloadError("未知二维码类型")


def _parse_contact(fields: dict) -> ContactInvite:
    ip = fields.get("ip", "")
    if not ip or not _valid_host(ip):
        raise QrPayloadError("二维码缺少有效的联系人地址")
    port_text = fields.get("p", "")
    port = int(port_text) if port_text.isdigit() else TCP_PORT
    if not _valid_port(port):
        raise QrPayloadError("二维码端口无效")
    fingerprint = fields.get("f", "").upper()
    if fingerprint and (
        len(fingerprint) != 16 or not set(fingerprint) <= _FINGERPRINT_CHARS
    ):
        raise QrPayloadError("二维码安全码格式无效")
    return ContactInvite(
        name=fields.get("n", ""), fingerprint=fingerprint, ip=ip, port=port
    )


def _parse_group(fields: dict) -> GroupInvite:
    digits = "".join(ch for ch in fields.get("g", "") if ch.isdigit())
    if len(digits) != 8:
        raise QrPayloadError("二维码缺少有效的群组数字ID")
    ip = fields.get("ip", "")
    port_text = fields.get("p", "")
    port = int(port_text) if port_text.isdigit() else TCP_PORT
    if ip:
        if not _valid_host(ip):
            raise QrPayloadError("二维码地址无效")
        if not _valid_port(port):
            raise QrPayloadError("二维码端口无效")
    elif port_text and not _valid_port(port):
        raise QrPayloadError("二维码端口无效")
    relay = fields.get("r", "")
    if relay and not _valid_relay(relay):
        raise QrPayloadError("二维码中继服务器地址无效")
    return GroupInvite(
        group_id=digits, name=fields.get("n", ""), ip=ip, port=port, relay=relay
    )


# --------------------------------------------------------------- QR imaging
# Optional dependencies: generation uses `qrcode` (matrix only, no Pillow);
# recognition uses OpenCV's QRCodeDetector (opencv-python is already a hard
# dependency of the Windows build, but stays optional here so the codec and
# the dialogs degrade gracefully when it cannot be imported).

_qr_lib = None
_cv2_mod = None
_np_mod = None


def qr_matrix(text: str) -> Optional[List[List[bool]]]:
    """QR module matrix for [text], or None when `qrcode` is not installed."""
    global _qr_lib
    if _qr_lib is None:
        try:
            import qrcode as _qrcode_lib

            _qr_lib = _qrcode_lib
        except Exception:
            _qr_lib = False
    if _qr_lib is False:
        return None
    try:
        code = _qr_lib.QRCode(border=2, error_correction=_qr_lib.constants.ERROR_CORRECT_M)
        code.add_data(text)
        code.make(fit=True)
        return [[bool(v) for v in row] for row in code.get_matrix()]
    except Exception:
        return None


def scanner_available() -> bool:
    """True when OpenCV (QRCodeDetector) can be imported for image decoding."""
    global _cv2_mod, _np_mod
    if _cv2_mod is not None and _np_mod is not None:
        return _cv2_mod is not False and _np_mod is not False
    try:
        import cv2 as _cv2

        _cv2_mod = _cv2
    except Exception:
        _cv2_mod = False
    try:
        import numpy as _numpy

        _np_mod = _numpy
    except Exception:
        _np_mod = False
    return _cv2_mod is not False and _np_mod is not False


def decode_qr_image_file(path: str) -> Optional[str]:
    """Decode the FIRST QR code found in an image file; None when nothing
    readable was found (or OpenCV is unavailable). Raises nothing by design:
    a broken image is surfaced as 'no QR found' by the caller."""
    if not scanner_available():
        return None
    try:
        # np.fromfile + imdecode: Windows paths are not always filesystem-
        # encodable (Chinese usernames), cv2.imread would silently fail
        buf = _np_mod.fromfile(path, dtype=_np_mod.uint8)
        image = _cv2_mod.imdecode(buf, _cv2_mod.IMREAD_COLOR)
        if image is None:
            return None
        return decode_qr_array(image)
    except Exception:
        return None


def decode_qr_array(image) -> Optional[str]:
    """Decode a QR code from an OpenCV BGR ndarray (also used by tests that
    synthesize an image straight from qr_matrix)."""
    if not scanner_available():
        return None
    try:
        detector = _cv2_mod.QRCodeDetector()
        text, _, _ = detector.detectAndDecode(image)
        return text or None
    except Exception:
        return None
