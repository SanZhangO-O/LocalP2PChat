"""Cryptographic primitives for all LocalChat transports (Android parity).

Mirrors app/src/main/java/com/zqr/localchat/crypto/Crypto.kt so the Windows
client interoperates byte-for-byte with the Android app:

 - confidentiality + integrity: AES-256-GCM per line / frame / chunk,
   laid out as nonce || ciphertext || tag
 - key agreement: ephemeral ECDH over P-256 (shared secret canonicalized to
   its minimal encoding — providers disagree on leading zero bytes)
 - group/mesh authentication: the shared group password is folded into the
   key derivation (PBKDF2-HMAC-SHA1, 210k iterations — matching the only
   PRF guaranteed on every Android down to API 24) and confirmed with HMACs
 - direct/call authentication: long-term EC identity keys signing the
   ephemeral transcript (TOFU + comparable fingerprints)
"""

import base64
import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_der_private_key,
    load_der_public_key,
)

GCM_NONCE_LEN = 12
GCM_TAG_BITS = 128
KEY_LEN = 32

# PBKDF2 cost for the password binding (~200ms on a mid-range phone).
PBKDF2_ITERATIONS = 210_000

# Password alphabet for generated group passwords: alphanumeric minus
# visually ambiguous characters (0/O, 1/l/I), so they survive manual typing
# and being read aloud.
_PASSWORD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def random_bytes(n: int) -> bytes:
    return secrets.token_bytes(n)


def random_password(length: int) -> str:
    return "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(length))


# ------------------------------------------------------------------- hashes


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, out_len: int) -> bytes:
    """HKDF-SHA256 (RFC 5869, extract-then-expand) — same as Crypto.kt."""
    prk = hmac_sha256(salt if salt else b"\x00" * 32, ikm)
    t = b""
    okm = bytearray()
    counter = 1
    while len(okm) < out_len:
        t = hmac_sha256(prk, t + info + bytes((counter,)))
        okm.extend(t)
        counter += 1
    return bytes(okm[:out_len])


def pbkdf2_sha1(password: str, salt: bytes, iterations: int, out_len: int) -> bytes:
    """PBKDF2-HMAC-SHA1: SHA1 (not SHA256) deliberately, matching the Android
    side (the only PBKDF2 PRF guaranteed on every Android version down to API
    24, so both peers always derive identical keys)."""
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, iterations, dklen=out_len)


def constant_time_equals(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


# --------------------------------------------------------------------- AES


def aes_gcm_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM: returns nonce || ciphertext || tag with a fresh nonce."""
    nonce = random_bytes(GCM_NONCE_LEN)
    blob = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + blob


def aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    """Inverse of aes_gcm_encrypt; raises on tampering or wrong key."""
    if len(blob) <= GCM_NONCE_LEN:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:GCM_NONCE_LEN], blob[GCM_NONCE_LEN:], None)


# ---------------------------------------------------------------------- EC


class ECKeyPair:
    """P-256 key pair with the encodings SecureWire needs. The underlying
    cryptography objects stay opaque; the wire format is the standard
    X509/SPKI (public) and PKCS#8 (private) DER, Base64-encoded — identical
    to what Java's KeyFactory produces/consumes."""

    def __init__(self, private_key=None):
        self.private_key = private_key if private_key is not None else ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key()

    @property
    def public_b64(self) -> str:
        return to_b64(
            self.public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        )

    @property
    def private_b64(self) -> str:
        return to_b64(
            self.private_key.private_bytes(
                Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
            )
        )

    def shared(self, peer_public_b64: str) -> bytes:
        """ECDH shared secret, canonicalized to the minimal encoding
        (leading zero bytes stripped) exactly like Crypto.ecdh on Android —
        providers disagree on fixed vs minimal length, and the transcript
        hash must match on both platforms."""
        peer = decode_pub(peer_public_b64)
        raw = self.private_key.exchange(ec.ECDH(), peer)
        return raw.lstrip(b"\x00")

    def sign(self, data: bytes) -> bytes:
        """SHA256withECDSA over [data] — DER-encoded signature, the format
        Java's Signature produces and verifies."""
        return self.private_key.sign(data, ec.ECDSA(hashes.SHA256()))


def generate_ec_key_pair() -> ECKeyPair:
    return ECKeyPair()


def decode_pub(b64: str):
    return load_der_public_key(from_b64(b64))


def decode_priv(b64: str):
    return load_der_private_key(from_b64(b64), password=None)


def encode_pub_from_private(priv_b64: str) -> str:
    return ECKeyPair(decode_priv(priv_b64)).public_b64


def verify(public_key, data: bytes, sig: bytes) -> bool:
    try:
        public_key.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def normalize_der_signature(sig: bytes) -> bytes:
    """Re-encode a DER ECDSA signature canonically (decode r/s, re-encode).
    Keeps byte-for-byte comparisons stable across providers."""
    try:
        r, s = decode_dss_signature(sig)
        return encode_dss_signature(r, s)
    except Exception:
        return sig


def verify_b64(public_b64: str, data: bytes, sig_b64: str) -> bool:
    try:
        return verify(decode_pub(public_b64), data, from_b64(sig_b64))
    except Exception:
        return False


# ------------------------------------------------------------------ Base64


def to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def from_b64(s: str) -> bytes:
    # Android's decoder skips embedded whitespace; mirror that tolerance.
    return base64.b64decode("".join(s.split()), validate=False)


# --------------------------------------------------------------------- hex


def hex_str(data: bytes) -> str:
    return data.hex()
