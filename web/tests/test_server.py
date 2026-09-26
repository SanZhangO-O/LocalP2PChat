import os

import pytest
from types import SimpleNamespace

from webchat.server import ChatServer

from helpers import WsClient, http_request, make_tmp_dir, wait_for

THUMB = "\U0001F44D"

PUBLIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "web",
    "public",
)


def start_server(data_dir):
    args = SimpleNamespace(
        data=data_dir,
        public_dir=PUBLIC_DIR,
        http_port=0,
        http_host="127.0.0.1",
        open_browser=False,
    )
    server = ChatServer(args)
    port = server.start()
    return server, port


def cookie_of(res):
    set_cookie = res["headers"].get("Set-Cookie")
    assert set_cookie, "login must set a session cookie"
    return set_cookie.split(";")[0]


def register(port, username, password, cookie=None):
    return http_request(port, "POST", "/api/register", {"username": username, "password": password}, cookie)


def login_ws(port, username, password):
    res = http_request(port, "POST", "/api/login", {"username": username, "password": password})
    assert res["status"] == 200, "login %s must succeed" % username
    ws = WsClient.connect(port, cookie_of(res))
    ws.next(lambda doc: doc.get("snapshot"))
    return ws


def latest_snapshot(client):
    snaps = [d for d in client.docs if d.get("snapshot")]
    return snaps[-1]["snapshot"] if snaps else None


def snapshot_chat(doc, chat_key):
    snap = doc.get("snapshot")
    if not snap:
        return None
    return snap["chats"].get(chat_key)


def test_server_chat_accounts_direct_chat_receipts_groups_files_persistence():
    data_dir = make_tmp_dir("lc-e2e-")
    srv1, port = start_server(data_dir)
    try:
        who0 = http_request(port, "GET", "/api/whoami")
        assert who0["json"]["firstRun"] is True
        with pytest.raises(RuntimeError, match="401"):
            WsClient.connect(port, None)

        reg_a = register(port, "alice", "password1")
        assert reg_a["status"] == 200
        alice_cookie = cookie_of(reg_a)
        reg_b = register(port, "bob", "password2", alice_cookie)
        assert reg_b["status"] == 200
        open_reg = register(port, "dave", "password3")
        assert open_reg["status"] == 200
        dave_cookie = cookie_of(open_reg)
        who_dave = http_request(port, "GET", "/api/whoami", None, dave_cookie)
        assert who_dave["json"]["username"] == "dave"
        dup_reg = register(port, "alice", "password9")
        assert dup_reg["status"] == 400
        bad_login = http_request(
            port, "POST", "/api/login", {"username": "alice", "password": "wrong-password"}
        )
        assert bad_login["status"] == 401

        alice = login_ws(port, "alice", "password1")
        bob = login_ws(port, "bob", "password2")

        presence = bob.next(
            lambda doc: doc.get("snapshot")
            and any(u["username"] == "alice" and u["online"] for u in doc["snapshot"]["users"])
        )
        assert presence

        alice.send({"action": "startDirect", "username": "bob"})
        started = alice.next(lambda doc: doc.get("startedDirect"))
        assert started["startedDirect"]["chatKey"].startswith("direct:")
        chat_key = started["startedDirect"]["chatKey"]

        directed = bob.next(
            lambda doc: doc.get("snapshot")
            and any(d["key"] == chat_key for d in doc["snapshot"]["directs"])
        )
        assert directed["snapshot"]["directs"][0]["name"] == "alice"

        alice.send({"action": "sendChat", "chatKey": chat_key, "content": "hello bob"})
        got1 = bob.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(m["content"] == "hello bob" for m in doc["snapshot"]["chats"][chat_key]["messages"])
        )
        msg1 = next(m for m in got1["snapshot"]["chats"][chat_key]["messages"] if m["content"] == "hello bob")
        assert msg1["senderName"] == "alice"
        assert msg1["read"] is False

        bob.send({"action": "sendChat", "chatKey": chat_key, "content": "hi alice"})
        alice.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(m["content"] == "hi alice" for m in doc["snapshot"]["chats"][chat_key]["messages"])
        )

        bob.send({"action": "sendTyping", "chatKey": chat_key, "active": True})
        typing = alice.next(
            lambda doc: doc.get("typing")
            and doc["typing"]["chatKey"] == chat_key
            and doc["typing"]["active"]
        )
        assert typing["typing"]["senderName"] == "bob"
        bob.send({"action": "sendTyping", "chatKey": chat_key, "active": False})

        bob.send({"action": "openChat", "chatKey": chat_key})
        alice.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(
                m["id"] == msg1["id"] and m["read"] for m in doc["snapshot"]["chats"][chat_key]["messages"]
            )
        )

        alice.send(
            {"action": "editMessage", "chatKey": chat_key, "messageId": msg1["id"], "content": "hello bob (edited)"}
        )
        edited = bob.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(
                m["id"] == msg1["id"] and m["edited"] and m["content"] == "hello bob (edited)"
                for m in doc["snapshot"]["chats"][chat_key]["messages"]
            )
        )
        assert edited
        bob.send({"action": "editMessage", "chatKey": chat_key, "messageId": msg1["id"], "content": "forge"})
        rejected = bob.next(lambda doc: doc.get("error") and doc.get("action") == "editMessage")
        assert "\u81ea\u5df1" in rejected["error"]

        bob.send({"action": "react", "chatKey": chat_key, "messageId": msg1["id"], "emoji": THUMB, "active": True})
        alice.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(
                m["id"] == msg1["id"] and len(m["reactions"].get(THUMB) or []) == 1
                for m in doc["snapshot"]["chats"][chat_key]["messages"]
            )
        )
        alice.send({"action": "pin", "chatKey": chat_key, "messageId": msg1["id"], "active": True})
        bob.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(
                m["id"] == msg1["id"] and m["pinned"] for m in doc["snapshot"]["chats"][chat_key]["messages"]
            )
        )

        bob.send({"action": "sendChat", "chatKey": chat_key, "content": "to be removed"})
        rm = alice.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(m["content"] == "to be removed" for m in doc["snapshot"]["chats"][chat_key]["messages"])
        )
        rm_id = next(
            m["id"] for m in rm["snapshot"]["chats"][chat_key]["messages"] if m["content"] == "to be removed"
        )
        bob.send({"action": "deleteMessage", "chatKey": chat_key, "messageId": rm_id})
        assert wait_for(
            lambda: (
                latest_snapshot(alice)
                and latest_snapshot(alice)["chats"].get(chat_key) is not None
                and not any(
                    m["id"] == rm_id for m in latest_snapshot(alice)["chats"][chat_key]["messages"]
                )
            )
        ), "deleted message must disappear for both sides"

        alice.send({"action": "setNickname", "name": "Alice A"})
        bob.next(
            lambda doc: doc.get("snapshot")
            and any(
                u["username"] == "alice" and u["name"] == "Alice A" for u in doc["snapshot"]["users"]
            )
        )

        alice.send({"action": "createGroup", "name": "team", "members": ["bob"]})
        created = alice.next(lambda doc: doc.get("createdGroup"))
        group_id = created["createdGroup"]["groupId"]
        bob.next(
            lambda doc: doc.get("snapshot")
            and any(g["groupId"] == group_id for g in doc["snapshot"]["groups"])
        )

        bob.send({"action": "groupUpdate", "groupId": group_id, "name": "bob team"})
        bob.next(lambda doc: doc.get("error") and doc.get("action") == "groupUpdate")
        alice.send(
            {"action": "groupUpdate", "groupId": group_id, "name": "team2", "announcement": "welcome"}
        )
        bob.next(
            lambda doc: doc.get("snapshot")
            and any(
                g["groupId"] == group_id and g["name"] == "team2" for g in doc["snapshot"]["groups"]
            )
        )

        bob.send({"action": "sendChat", "chatKey": group_id, "content": "group hello"})
        alice.next(
            lambda doc: snapshot_chat(doc, group_id)
            and any(m["content"] == "group hello" for m in doc["snapshot"]["chats"][group_id]["messages"])
        )

        alice.send({"action": "openChat", "chatKey": group_id})
        bob.next(
            lambda doc: snapshot_chat(doc, group_id)
            and any(
                m["content"] == "group hello" and len(m.get("readers") or []) == 1
                for m in doc["snapshot"]["chats"][group_id]["messages"]
            )
        )

        reg_c = register(port, "carol", "password3", alice_cookie)
        assert reg_c["status"] == 200
        carol = login_ws(port, "carol", "password3")
        alice.send({"action": "inviteMember", "groupId": group_id, "username": "carol"})
        carol.next(
            lambda doc: doc.get("snapshot")
            and any(g["groupId"] == group_id for g in doc["snapshot"]["groups"])
        )
        carol_id = next(d for d in carol.docs if d.get("snapshot"))["snapshot"]["profile"]["userId"]
        alice.send({"action": "kickMember", "groupId": group_id, "targetId": carol_id})
        kicked_event = carol.next(lambda doc: isinstance(doc.get("event"), str))
        assert "\u79fb\u51fa" in kicked_event["event"]
        assert wait_for(
            lambda: latest_snapshot(carol) is not None
            and not any(
                g["groupId"] == group_id for g in latest_snapshot(carol)["groups"]
            )
        ), "kicked member must lose the group"

        payload = "file-bytes-\u00e9\u4f60\u597d".encode("utf-8")
        up = http_request(
            port, "POST", "/api/upload?name=notes.txt", payload, alice_cookie
        )
        assert up["status"] == 200
        alice.send({"action": "sendFile", "chatKey": chat_key, "uploadId": up["json"]["uploadId"]})
        file_snap = bob.next(
            lambda doc: snapshot_chat(doc, chat_key)
            and any(m.get("fileInfo") for m in doc["snapshot"]["chats"][chat_key]["messages"])
        )
        file_msg = next(
            m for m in file_snap["snapshot"]["chats"][chat_key]["messages"] if m.get("fileInfo")
        )
        assert file_msg["fileInfo"]["fileName"] == "notes.txt"
        assert file_msg["fileInfo"]["fileSize"] == len(payload)
        dl = http_request(port, "GET", "/files/%s" % file_msg["fileInfo"]["fileId"], None, alice_cookie)
        assert dl["status"] == 200
        assert dl["text"].encode("utf-8") == payload
        dl_anon = http_request(port, "GET", "/files/%s" % file_msg["fileInfo"]["fileId"])
        assert dl_anon["status"] == 401

        alice.close()
        bob.close()
        carol.close()
        srv1.stop()

        srv2, port2 = start_server(data_dir)
        try:
            alice2 = login_ws(port2, "alice", "password1")
            snap = alice2.next(lambda doc: doc.get("snapshot"))["snapshot"]
            assert snap["profile"]["username"] == "alice"
            assert snap["profile"]["name"] == "Alice A"
            direct = snap["chats"].get(chat_key)
            assert direct, "direct conversation must survive restart"
            edited_msg = next(m for m in direct["messages"] if m["id"] == msg1["id"])
            assert edited_msg["content"] == "hello bob (edited)"
            assert edited_msg["edited"] is True
            bob_id = next(u["id"] for u in snap["users"] if u["username"] == "bob")
            assert edited_msg["reactions"][THUMB] == [bob_id]
            assert edited_msg["pinned"] is True
            assert any(m.get("fileInfo") for m in direct["messages"])
            group = next(g for g in snap["groups"] if g["groupId"] == group_id)
            assert group["announcement"] == "welcome"
            assert not any("enc1:" in m["content"] for m in snap["chats"][group_id]["messages"])
            assert any(m["content"] == "group hello" for m in snap["chats"][group_id]["messages"])
            alice2.close()
        finally:
            srv2.stop()

        alice_seen = latest_snapshot(alice)
        assert alice_seen and isinstance(alice_seen["users"], list)
        assert alice_seen["chats"].get(chat_key)
    finally:
        srv1.stop()


def test_group_lifecycle_creator_leaving_dissolves_the_group_for_everyone():
    data_dir = make_tmp_dir("lc-e2e2-")
    srv, port = start_server(data_dir)
    try:
        reg_a = register(port, "alice", "password1")
        alice_cookie = cookie_of(reg_a)
        register(port, "bob", "password2", alice_cookie)
        alice = login_ws(port, "alice", "password1")
        bob = login_ws(port, "bob", "password2")

        alice.send({"action": "createGroup", "name": "temp", "members": ["bob"]})
        created = alice.next(lambda doc: doc.get("createdGroup"))
        group_id = created["createdGroup"]["groupId"]
        bob.next(
            lambda doc: doc.get("snapshot")
            and any(g["groupId"] == group_id for g in doc["snapshot"]["groups"])
        )

        alice.send({"action": "leaveGroup", "groupId": group_id})
        dissolve_event = bob.next(
            lambda doc: isinstance(doc.get("event"), str) and "\u89e3\u6563" in doc["event"]
        )
        assert dissolve_event
        assert wait_for(
            lambda: latest_snapshot(bob) is not None
            and not any(g["groupId"] == group_id for g in latest_snapshot(bob)["groups"])
            and latest_snapshot(bob)["chats"].get(group_id) is None
        ), "dissolved group must disappear from every member"

        alice.send({"action": "createGroup", "name": "keep", "members": ["bob"]})
        created2 = alice.next(
            lambda doc: doc.get("createdGroup") and doc["createdGroup"]["name"] == "keep"
        )
        keep_id = created2["createdGroup"]["groupId"]
        bob.next(
            lambda doc: doc.get("snapshot")
            and any(g["groupId"] == keep_id for g in doc["snapshot"]["groups"])
        )
        bob.send({"action": "leaveGroup", "groupId": keep_id})

        def keep_group_has_single_member():
            snap = latest_snapshot(alice)
            if snap is None:
                return False
            group = next((g for g in snap["groups"] if g["groupId"] == keep_id), None)
            return group is not None and len(group["members"]) == 1

        assert wait_for(keep_group_has_single_member), "group must survive a non-creator leaving"
        alice.close()
        bob.close()
    finally:
        srv.stop()


def test_multiple_accounts_per_machine_additive_register_and_token_switching():
    data_dir = make_tmp_dir("lc-e2e3-")
    srv, port = start_server(data_dir)
    try:
        reg_a = register(port, "alice", "password1")
        assert reg_a["status"] == 200
        alice_cookie = cookie_of(reg_a)
        token_a = reg_a["json"]["token"]
        assert token_a

        who_cookie = http_request(port, "GET", "/api/whoami", None, alice_cookie)
        assert who_cookie["json"]["username"] == "alice"
        who_token = http_request(port, "GET", "/api/whoami", None, None, token_a)
        assert who_token["json"]["username"] == "alice"

        reg_b = register(port, "bob", "password2", alice_cookie)
        assert reg_b["status"] == 200
        token_b = reg_b["json"]["token"]
        still_alice = http_request(port, "GET", "/api/whoami", None, alice_cookie)
        assert still_alice["json"]["username"] == "alice"
        as_bob = http_request(port, "GET", "/api/whoami", None, None, token_b)
        assert as_bob["json"]["username"] == "bob"

        reg_c = http_request(
            port, "POST", "/api/register", {"username": "carol", "password": "password3"}, None, token_b
        )
        assert reg_c["status"] == 200

        bob_ws = WsClient.connect(port, {"token": token_b})
        snap = bob_ws.next(lambda doc: doc.get("snapshot"))["snapshot"]
        assert snap["profile"]["username"] == "bob"
        bob_ws.close()

        payload = b"multi-account-file"
        up = http_request(
            port, "POST", "/api/upload?name=m.txt", payload, None, token_b
        )
        assert up["status"] == 200

        out = http_request(port, "POST", "/api/logout", None, None, token_b)
        assert out["status"] == 200
        bob_gone = http_request(port, "GET", "/api/whoami", None, None, token_b)
        assert bob_gone["json"]["authenticated"] is False
        alice_stays = http_request(port, "GET", "/api/whoami", None, alice_cookie)
        assert alice_stays["json"]["username"] == "alice"

        relogin = http_request(port, "POST", "/api/login", {"username": "bob", "password": "password2"})
        assert relogin["status"] == 200
        token_b2 = relogin["json"]["token"]
        bob_back = http_request(port, "GET", "/api/whoami", None, None, token_b2)
        assert bob_back["json"]["username"] == "bob"

        limited = False
        for i in range(30):
            if limited:
                break
            r = register(port, "spam%d" % i, "password1")
            if r["status"] == 429:
                limited = True
        assert limited is True
    finally:
        srv.stop()
