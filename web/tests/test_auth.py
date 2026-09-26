import os

from webchat.accounts import AccountRegistry, hash_password, verify_password
from webchat.server import parse_args

from helpers import make_tmp_dir


def test_scrypt_password_hash_verify_roundtrip_and_rejection():
    salt = "aabbccdd00112233"
    hash_value = hash_password("\u5bc6\u7801abc123", salt)
    assert len(hash_value) == 128
    assert verify_password("\u5bc6\u7801abc123", salt, hash_value) is True
    assert verify_password("wrong", salt, hash_value) is False
    assert verify_password("\u5bc6\u7801abc123", "00", hash_value) is False


def test_registry_create_unique_usernames_no_protocol_ports():
    directory = make_tmp_dir("lc-registry-")
    registry = AccountRegistry(directory)
    assert registry.first_run is True
    first = registry.create("alice", "password1")
    assert first["ok"] is True
    assert first["record"]["nickname"] == "alice"
    assert "tcpPort" not in first["record"]
    assert registry.first_run is False
    dup = registry.create("Alice", "password2")
    assert dup["ok"] is False
    bad_name = registry.create("a" * 40, "password2")
    assert bad_name["ok"] is False
    bad_pass = registry.create("bob", "123")
    assert bad_pass["ok"] is False
    second = registry.create("bob", "password2")
    assert second["ok"] is True
    assert registry.verify("alice", "password1")["id"] == first["record"]["id"]
    assert registry.verify("alice", "nope") is None
    assert registry.verify("carol", "password1") is None


def test_registry_persists_accounts_and_nickname_edits_sessions_stay_in_memory():
    directory = make_tmp_dir("lc-registry2-")
    a = AccountRegistry(directory)
    rec = a.create("carol", "password9")["record"]
    token = a.create_session(rec["id"])
    assert a.session_account(token)["id"] == rec["id"]
    a.drop_session(token)
    assert a.session_account(token) is None

    b = AccountRegistry(directory)
    assert len(b.accounts) == 1
    assert b.verify("carol", "password9")["id"] == rec["id"]
    assert b.session_account(token) is None
    b.get(rec["id"])["nickname"] = "\u5361\u7f57\u5c14"
    b.save()
    c = AccountRegistry(directory)
    assert c.get(rec["id"])["nickname"] == "\u5361\u7f57\u5c14"


def test_registry_file_layout_stays_inside_the_data_dir():
    directory = make_tmp_dir("lc-registry3-")
    AccountRegistry(directory).create("dave", "password8")
    assert os.path.exists(os.path.join(directory, "accounts.json"))


def test_parse_args_reads_server_flags():
    args = parse_args(["--http", "9000", "--http-host", "0.0.0.0"])
    assert args.http_port == 9000
    assert args.http_host == "0.0.0.0"
    assert getattr(args, "port_base", None) is None
