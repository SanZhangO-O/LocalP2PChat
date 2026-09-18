"""Secret-at-rest protection for the Windows client.

Small high-value strings (group passwords, chat message bodies, the last-
message previews) are encrypted with an AES-256-GCM content key before they
touch the SQLite database. The content key itself is randomly generated once,
DPAPI-wrapped (CryptProtectData, scope: the current Windows user) and stored
next to the database — the same layered protection the device identity uses
and Chrome/Edge apply to their key stores. Nothing secret is ever persisted
in plaintext; `unprotect` passes values without the "enc1:" prefix through so
data written before this layer existed still reads.
"""

import base64
import json
import logging
import os
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import KEY_LEN, random_bytes
from .securewire import _dpapi_call

logger = logging.getLogger(__name__)

_PREFIX = "enc1:"
_KEY_FILE = "secret_key.json"
_NONCE_LEN = 12
_GCM_TAG_BITS = 128

_lock = threading.Lock()
_keys: dict = {}  # data_dir -> key bytes (None = unavailable)


def _load_key(data_dir: str):
    """The per-installation content key: created once, DPAPI-wrapped at rest."""
    with _lock:
        if data_dir in _keys:
            return _keys[data_dir]
        path = os.path.join(data_dir, _KEY_FILE)
        key = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            wrapped = base64.b64decode(doc.get("wrapped", ""))
            if doc.get("scheme") == "dpapi" and wrapped:
                raw = _dpapi_call(False, wrapped)
                if raw is not None and len(raw) == KEY_LEN:
                    key = raw
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("unreadable secret key file", exc_info=True)
        if key is None:
            key = random_bytes(KEY_LEN)
            wrapped = _dpapi_call(True, key)
            doc = (
                {"scheme": "dpapi", "wrapped": base64.b64encode(wrapped).decode("ascii")}
                if wrapped is not None
                else {"scheme": "plain", "wrapped": base64.b64encode(key).decode("ascii")}
            )
            if wrapped is None:
                logger.warning(
                    "DPAPI unavailable — secret content key stored UNENCRYPTED"
                )
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, path)
        _keys[data_dir] = key
        return key


def protect(data_dir: str, text: str) -> str:
    """AES-256-GCM encrypt [text] under the installation content key:
    "enc1:" + Base64(nonce || ciphertext || tag). Returns the input unchanged
    (logged) when the key is unavailable — better degraded than lost."""
    if not text:
        return text
    key = _load_key(data_dir)
    if key is None:
        return text
    nonce = random_bytes(_NONCE_LEN)
    blob = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + blob).decode("ascii")


def unprotect(data_dir: str, text: str) -> str:
    """Inverse of [protect]. A value without the prefix is passed through;
    a prefixed value that fails to decrypt (corrupted row, lost DPAPI key)
    degrades to an empty string instead of surfacing ciphertext."""
    if not text or not text.startswith(_PREFIX):
        return text
    key = _load_key(data_dir)
    if key is None:
        return ""
    try:
        blob = base64.b64decode(text[len(_PREFIX) :])
        plain = AESGCM(key).decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], None)
        return plain.decode("utf-8")
    except Exception:
        logger.warning("secret value failed to decrypt; dropping it")
        return ""
