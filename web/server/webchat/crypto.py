import base64
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

GCM_NONCE_LEN = 12
GCM_TAG_LEN = 16
KEY_LEN = 32


def random_bytes(n):
    return os.urandom(n)


def aes_gcm_encrypt(key, plaintext):
    nonce = os.urandom(GCM_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def aes_gcm_decrypt(key, blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) <= GCM_NONCE_LEN + GCM_TAG_LEN:
        raise ValueError("ciphertext too short")
    nonce = bytes(blob[:GCM_NONCE_LEN])
    # cryptography's AESGCM expects ciphertext WITH the 16-byte tag appended
    # (encrypt returns ct||tag), so the tag must NOT be stripped here.
    return AESGCM(key).decrypt(nonce, bytes(blob[GCM_NONCE_LEN:]), None)


def to_b64(data):
    return base64.b64encode(data).decode("ascii")


def from_b64(s):
    text = re.sub(r"\s+", "", str(s))
    return base64.b64decode(text + "=" * (-len(text) % 4))
