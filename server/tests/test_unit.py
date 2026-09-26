import pytest

from webchat import crypto as C
from webchat import util as U
from webchat.accounts import hash_password
from webchat.store import SecretBox, Store

from helpers import make_tmp_dir

NODE_SCRYPT_HASH = "dd273a4649477c14a923b478c9412f25377d2533976934d7e39684c823ceea001d3701d5eabe78e5bd464edfc60d6aaca76f522a153a1882648d5545dd003f2c"
NODE_GCM_BLOB = (
    "000102030405060708090a0bf2a515d3e5f4eddaa6580e9052bd055b4a8b0ba0"
    "2e0ca556ec324f6a4345d079"
)
NODE_GCM_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


def test_aes_gcm_roundtrip_produces_nonce_ct_tag_layout():
    key = C.random_bytes(32)
    plain = b"hello localchat"
    blob = C.aes_gcm_encrypt(key, plain)
    assert len(blob) == 12 + len(plain) + 16
    assert C.aes_gcm_decrypt(key, blob) == plain
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(Exception):
        C.aes_gcm_decrypt(key, bytes(tampered))
    with pytest.raises(Exception):
        C.aes_gcm_decrypt(key, bytes(10))


def test_node_written_data_stays_readable():
    assert hash_password("\u5bc6\u7801abc123", "aabbccdd00112233") == NODE_SCRYPT_HASH
    key = bytes.fromhex(NODE_GCM_KEY)
    blob = bytes.fromhex(NODE_GCM_BLOB)
    assert C.aes_gcm_decrypt(key, blob).decode("utf-8") == "\u4f60\u597d localchat"
    assert len(C.aes_gcm_encrypt(key, b"x")) == 12 + 1 + 16


def test_secret_box_encrypts_at_rest_with_enc1_prefix_and_roundtrips():
    directory = make_tmp_dir("lc-box-")
    box = SecretBox(directory)
    secret = "\u79d8\u5bc6\u6d88\u606f secret"
    stored = box.protect(secret)
    assert stored.startswith("enc1:")
    assert stored != secret
    assert box.unprotect(stored) == secret
    reloaded = SecretBox(directory)
    assert reloaded.unprotect(stored) == secret
    assert box.unprotect("plain") == "plain"
    assert box.unprotect("enc1:garbage") == ""


def test_store_persists_chats_encrypted_and_reloads_decrypted():
    directory = make_tmp_dir("lc-store-")
    store = Store(directory)
    store.save_chat(
        "direct:a|b",
        [{"id": "m1", "content": "\u4f60\u597d", "timestamp": 1, "senderId": "a", "senderName": "A"}],
    )
    with open(store.chat_path("direct:a|b"), "r", encoding="utf-8") as f:
        raw = f.read()
    assert "enc1:" in raw
    assert "\u4f60\u597d" not in raw
    loaded = store.load_chat_decrypted("direct:a|b")
    assert len(loaded) == 1
    assert loaded[0]["content"] == "\u4f60\u597d"
    store.delete_chat("direct:a|b")
    assert store.load_chat("direct:a|b") == []


def test_sanitizers_match_cross_end_behaviour():
    assert U.sanitize_file_name("..\\a\\b\\file.txt ") == "file.txt"
    assert U.sanitize_file_name("") == "file"
    assert len(U.code_points_of(U.sanitize_emoji("\u00e9\u4f60\u597d"))) == 3
    assert "\u0000" not in U.code_points_of(U.sanitize_emoji("a\u0000b"))
    assert U.detect_media_kind("clip.MP4") == "video"
    assert U.detect_media_kind("song.ogg") == "audio"
    assert U.detect_media_kind("pic.webp") == "image"
    assert U.detect_media_kind("doc.pdf") == "file"


def test_content_validation_counts_code_points_not_utf16_units():
    astral_heavy = "\U0001F600" * U.MAX_CONTENT_LENGTH
    assert U.is_valid_content(astral_heavy) is True
    assert U.is_valid_content(astral_heavy + "x") is False
    assert U.is_valid_content("   ") is False
    assert U.is_valid_content("hi") is True


def test_direct_conversation_keys_are_order_independent():
    assert U.direct_key("b", "a") == U.direct_key("a", "b")
    assert U.direct_key("a", "b").startswith("direct:")
