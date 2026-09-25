"""Python-side driver for the Node.js web-client interop tests.

Each subcommand plays one end of a LocalChat wire scenario against the
Node.js implementation in web/server. Results are reported as JSON lines
on stdout ({"event": ...}); the Node test parses them. Exit code 0 means
the Python side's own assertions passed.

Run only from web/test/interop.test.js (it spawns this file).
"""

import hashlib
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "windows"))
sys.path.insert(0, os.path.join(ROOT, "windows", "tests"))

from localchat import groupauth  # noqa: E402
from localchat.crypto import generate_ec_key_pair, sha256  # noqa: E402
from localchat.models import ChatMessage, FileInfo, NetworkPacket, Peer  # noqa: E402
from localchat.network import (  # noqa: E402
    GroupMeshManager,
    P2PListener,
    P2PManager,
    _download_file_offer,
    _serve_file_download,
    make_wire,
)
from localchat.securewire import DeviceIdentity, Handshake, Protocol, WireException  # noqa: E402
from fake_peer import FakePeerClient, install_identity, wait_until  # noqa: E402


def emit(obj):
    print(json.dumps(obj), flush=True)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def read_raw_line(sock, timeout=15):
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        try:
            b = sock.recv(1)
        except OSError:
            return None
        if not b:
            return None
        if b == b"\n":
            return bytes(buf).decode("utf-8", "replace").rstrip("\r")
        buf.extend(b)


def expect(cond, message):
    if not cond:
        emit({"event": "fail", "message": message})
        sys.exit(1)


# ---------------------------------------------------------------- scenarios


def password_server(port, password):
    """Accept loop: password handshake, echo each received packet back."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    emit({"event": "listening", "port": srv.getsockname()[1]})

    def handle(conn):
        try:
            conn.settimeout(15)
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            first = read_raw_line(conn)
            if not first:
                return
            start = NetworkPacket.from_json(first)
            wire = make_wire(conn, reader)
            secured = Handshake.accept(wire, start, lambda m, g: password)
            expect(secured is not None, "server handshake failed")
            emit({"event": "secured", "mode": secured.mode})
            while True:
                try:
                    pkt = wire.recv_packet()
                except WireException as e:
                    emit({"event": "wire_error", "message": str(e)})
                    return
                if pkt is None:
                    return
                emit({"event": "packet", "type": pkt.type})
                if pkt.type == "quit":
                    return
                if pkt.type == "chat":
                    expect(pkt.message.content == "hello-from-node", "chat content")
                    expect(pkt.message.senderId == "node-sender-1", "sender id")
                wire.send_packet(pkt)
        except Exception as e:
            emit({"event": "error", "message": str(e)})
        finally:
            try:
                conn.close()
            except OSError:
                pass

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def wrong_password_server(port, password):
    """Reject a client that cannot produce the right MAC."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    emit({"event": "listening", "port": srv.getsockname()[1]})
    conn, _ = srv.accept()
    conn.settimeout(30)
    reader = conn.makefile("r", encoding="utf-8", newline="\n")
    first = read_raw_line(conn)
    start = NetworkPacket.from_json(first)
    wire = make_wire(conn, reader)
    secured = Handshake.accept(wire, start, lambda m, g: password)
    expect(secured is None, "wrong-password handshake must be rejected")
    emit({"event": "rejected"})
    conn.close()


def direct_server(port, scenario):
    install_identity()
    ident = DeviceIdentity.current.public_b64
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    emit({"event": "listening", "port": srv.getsockname()[1], "ident": ident})

    def accept_one():
        conn, _ = srv.accept()
        conn.settimeout(30)
        reader = conn.makefile("r", encoding="utf-8", newline="\n")
        first = read_raw_line(conn)
        start = NetworkPacket.from_json(first)
        wire = make_wire(conn, reader)
        secured = Handshake.accept_direct(wire, start, None)
        expect(secured is not None, "direct handshake failed")
        hello = wire.recv_packet()
        expect(hello is not None and hello.type == Protocol.DIRECT_HELLO, "expected direct_hello")
        return conn, wire, secured, hello

    if scenario == "tofu":
        conn, wire, secured, hello = accept_one()
        expect(hello.peer.id == "node-device-1", "hello peer id")
        expect(
            DeviceIdentity.check_peer("node-device-1", secured.peer_ident),
            "first contact binds the dialer key",
        )
        ack = NetworkPacket(
            type=Protocol.DIRECT_ACK,
            peer=Peer("py-device-1", "Py Peer", "127.0.0.1", port),
        )
        wire.send_packet(ack)
        emit({"event": "bound"})
        conn2, wire2, secured2, hello2 = accept_one()
        expect(hello2.peer.id == "node-device-1", "second hello peer id")
        same = DeviceIdentity.check_peer("node-device-1", secured2.peer_ident, remember=False)
        expect(not same, "a DIFFERENT key for a bound id must be rejected (TOFU)")
        emit({"event": "tofu_rejected"})
        conn2.close()
        conn.close()
        return

    conn, wire, secured, hello = accept_one()
    emit({"event": "secured"})
    expect(hello.peer.id == "node-device-1", "hello peer id")
    expect(hello.peer.name == "Web Node", "hello peer name")
    ack = NetworkPacket(
        type=Protocol.DIRECT_ACK,
        peer=Peer("py-device-1", "Py Peer", "127.0.0.1", port),
    )
    wire.send_packet(ack)
    while True:
        try:
            pkt = wire.recv_packet()
        except WireException:
            return
        if pkt is None or pkt.type == "quit":
            return
        emit({"event": "packet", "type": pkt.type})
        if pkt.type == "chat":
            expect(pkt.message.senderId == "node-device-1", "direct chat sender")
            receipt = NetworkPacket(
                type="read_receipt",
                groupId="direct:py-device-1",
                upToId=pkt.message.id,
                readerId="py-device-1",
            )
            wire.send_packet(receipt)


def group_host():
    """Stand up a P2PManager host the way test_protocol.py does."""
    install_identity()

    class Recorder(P2PListener):
        def __init__(self):
            self.messages = []
            self.peers = []

        def peers_changed(self, p2p):
            self.peers.append(dict(p2p.peers))

        def messages_changed(self, p2p):
            self.messages.append(list(p2p.messages))

    password = "pw12345"
    recorder = Recorder()
    port = free_port()
    host = P2PManager(recorder, port=port, password=password)
    host.initialize_as_host("PyHost", "\u6d4b\u8bd5\u7fa4")
    host.set_join_id(host.numeric_group_id)
    host.start_as_host()
    emit({"event": "host", "port": port, "joinId": host.numeric_group_id})
    time.sleep(0.3)
    emit({"event": "peers", "count": len(host.peers)})
    wait_until(lambda: len(host.peers) > 0, 8)
    time.sleep(0.3)
    host.send_message("\u6765\u81ea Python \u4e3b\u673a\u7684\u6d88\u606f")
    deadline = time.time() + 8
    node_msg = None
    while time.time() < deadline:
        for m in host.messages:
            if m.senderId == "node-device-2" and m.content == "hello group from node":
                node_msg = m
                break
        if node_msg:
            break
        time.sleep(0.05)
    expect(node_msg is not None, "node chat never reached the python host")
    expect(node_msg.senderName == "Web Node", "node sender name")
    host.stop()
    emit({"event": "done"})


def group_member(port, join_id):
    """Join a Node-hosted group as a member and exchange chat."""
    install_identity()

    class Recorder(P2PListener):
        def __init__(self):
            self.messages = []
            self.peers = []
            self.result = None

        def peers_changed(self, p2p):
            self.peers.append(dict(p2p.peers))

        def messages_changed(self, p2p):
            self.messages.append(list(p2p.messages))

        def join_state_changed(self, p2p):
            self.result = p2p.connection_result

    recorder = Recorder()
    member = P2PManager(recorder, port=free_port(), password="pw12345")
    member.initialize_as_client("PyMember", "\u7f51\u7edc\u7fa4")
    member.set_join_id(join_id)
    member.confirm_join("127.0.0.1", port)
    expect(
        wait_until(lambda: recorder.result is not None and recorder.result[0], 10),
        "join did not succeed: %r" % (recorder.result,),
    )
    emit({"event": "joined"})
    wait_until(lambda: len(member.peers) > 0, 8)
    deadline = time.time() + 8
    got = None
    while time.time() < deadline:
        for m in member.messages:
            if m.senderName == "Web Node":
                got = m
                break
        if got:
            break
        time.sleep(0.05)
    expect(got is not None, "node broadcast never reached the python member")
    member.send_message("\u6210\u5458\u6d88\u606f\u6765\u81ea Python")
    deadline = time.time() + 8
    acked = False
    while time.time() < deadline:
        for m in member.messages:
            if m.senderId == member.my_id and m.content == "\u6210\u5458\u6d88\u606f\u6765\u81ea Python":
                acked = True
                break
        if acked:
            break
        time.sleep(0.05)
    expect(acked, "member chat never echoed back")
    member.stop()
    emit({"event": "done"})


def mesh_peer(port, group_id):
    """A mesh listener: password handshake + mesh_hello, then history push."""
    install_identity()
    mm = GroupMeshManager()
    seen = []

    class Recorder:
        def group_mesh_message(self, group_id, msgs):
            seen.extend(msgs)

        def group_mesh_links_changed(self, group_id):
            pass

        def group_mesh_delete(self, group_id, message_id, sender_id):
            pass

        def group_mesh_deleted_ids(self, group_id, deleted_ids):
            pass

        def group_mesh_typing(self, group_id, sender_id, active):
            pass

        def group_mesh_admin(self, group_id, packet):
            pass

        def group_mesh_edit(self, group_id, message_id, new_content, sender_id, message_sig=None):
            pass

        def group_mesh_reaction(self, group_id, message_id, emoji, sender_id, active):
            pass

        def group_mesh_pin(self, group_id, message_id, sender_id, active):
            pass

        def group_mesh_read_receipt(self, group_id, reader_id, up_to_id):
            pass

        def group_mesh_group_file_add(self, group_id, entry):
            pass

        def group_mesh_group_file_remove(self, group_id, file_id, sender_id):
            return False

        def group_mesh_group_files(self, group_id, entries, removed_ids):
            pass

    mm.attach(Recorder())
    my_peer = Peer("py-mesh-1", "PyMesh", "127.0.0.1", port)
    history_msg = ChatMessage(
        id="py-hist-1",
        content="\u7f51\u72b6\u5386\u53f2\u6d88\u606f",
        timestamp=int(time.time() * 1000) - 5000,
        sender_id="py-mesh-1",
        sender_name="PyMesh",
    )
    groupauth.sign_message("mesh-group-1", history_msg)
    mm.enter_group("mesh-group-1", my_peer, [], [history_msg], password="meshpw")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    emit({"event": "listening", "port": srv.getsockname()[1]})
    conn, _ = srv.accept()
    conn.settimeout(30)
    reader = conn.makefile("r", encoding="utf-8", newline="\n")
    first = read_raw_line(conn)
    start = NetworkPacket.from_json(first)
    wire = make_wire(conn, reader)
    secured = Handshake.accept(wire, start, lambda m, g: "meshpw")
    expect(secured is not None, "mesh handshake failed")
    hello = wire.recv_packet()
    expect(hello is not None and hello.type == "mesh_hello", "expected mesh_hello")
    expect(hello.group_id == "mesh-group-1", "mesh group id")
    mm.handle_mesh_hello(conn, wire, hello)
    deadline = time.time() + 8
    while time.time() < deadline and not any(
        m.senderId == "node-mesh-1" and m.content == "mesh hello from node" for m in seen
    ):
        time.sleep(0.05)
    expect(
        any(m.senderId == "node-mesh-1" and m.content == "mesh hello from node" for m in seen),
        "node mesh_chat never arrived",
    )
    emit({"event": "done"})
    time.sleep(0.3)


def mesh_dial_target(port, group_id):
    """A mesh peer the NODE dials: accepts and pushes history via mesh manager."""
    mesh_peer(port, group_id)


def file_server(port):
    """Serve a temp file with the shared file_download protocol."""
    data = os.urandom(300 * 1024 + 12345)
    path = os.path.join(os.environ.get("LC_TMPDIR", "."), "py_served.bin")
    with open(path, "wb") as f:
        f.write(data)
    file_id = "py-file-1"
    file_key = hashlib.sha256(b"file-key-seed").digest()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    emit({"event": "listening", "port": srv.getsockname()[1], "path": path,
          "size": len(data), "fileId": file_id, "key": file_key.hex(),
          "sha": hashlib.sha256(data).hexdigest()})
    conn, _ = srv.accept()
    _serve_file_download(conn, file_id, path, len(data), file_key)
    emit({"event": "done"})


def file_client(port, tmpdir):
    """Download a file offered BY the node implementation."""
    install_identity()
    path_in = os.path.join(tmpdir, "node_served.bin")
    expect(os.path.isfile(path_in), "node file missing")
    with open(path_in, "rb") as f:
        data = f.read()
    file_id = "node-file-1"
    key_hex = None
    deadline = time.time() + 20
    while key_hex is None and time.time() < deadline:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if line:
            doc = json.loads(line)
            key_hex = doc.get("key")
    expect(key_hex is not None, "no file key received")
    file_key = bytes.fromhex(key_hex)
    info = FileInfo(file_id, "node.bin", len(data), "127.0.0.1", port, key_hex)
    target = os.path.join(tmpdir, "py_downloaded.bin")
    ok, message = _download_file_offer(info, target)
    expect(ok, "download failed: %s" % message)
    with open(target, "rb") as f:
        got = f.read()
    expect(got == data, "downloaded bytes differ")
    emit({"event": "done", "sha256": hashlib.sha256(got).hexdigest()})


def main():
    cmd = sys.argv[1]
    if cmd == "password_server":
        password_server(int(sys.argv[2]), sys.argv[3])
    elif cmd == "wrong_password_server":
        wrong_password_server(int(sys.argv[2]), sys.argv[3])
    elif cmd == "direct_server":
        direct_server(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "basic")
    elif cmd == "group_host":
        group_host()
    elif cmd == "group_member":
        group_member(int(sys.argv[2]), sys.argv[3])
    elif cmd == "mesh_peer":
        mesh_peer(int(sys.argv[2]), sys.argv[3])
    elif cmd == "file_server":
        file_server(int(sys.argv[2]))
    elif cmd == "file_client":
        file_client(int(sys.argv[2]), sys.argv[3])
    else:
        emit({"event": "fail", "message": "unknown command %s" % cmd})
        sys.exit(2)


if __name__ == "__main__":
    main()
