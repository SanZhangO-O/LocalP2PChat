import base64
import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import time
from urllib.parse import quote


def free_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    return port


def make_tmp_dir(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def wait_for(fn, timeout_ms=10000, step_ms=25):
    deadline = time.time() + timeout_ms / 1000.0
    while True:
        if fn():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(step_ms / 1000.0)


def http_request(port, method, req_path, body=None, cookie=None, token=None):
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["X-LC-Token"] = token
    payload = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            payload = bytes(body)
        else:
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, req_path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    finally:
        conn.close()
    text = data.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return {"status": resp.status, "headers": resp.headers, "json": parsed, "text": text}


def _read_handshake(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during ws handshake")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    status_line = head.decode("latin-1").split("\r\n")[0]
    status = int(status_line.split()[1])
    return status, rest


class WsClient:
    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buffer = bytearray(initial)
        self.docs = []
        self.closed = False
        self._cond = threading.Condition()
        # Frames may have arrived together with the 101 handshake (single TCP
        # segment): parse the pre-buffered bytes now, because _read_loop only
        # parses on newly recv'd data.
        self._feed(b"")
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    @classmethod
    def connect(cls, port, auth=None):
        opts = auth if isinstance(auth, dict) else {"cookie": auth}
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        if opts.get("token"):
            path = "/?token=%s" % quote(str(opts["token"]), safe="")
        else:
            path = "/"
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: 127.0.0.1:%d" % port,
            "Connection: Upgrade",
            "Upgrade: websocket",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        if opts.get("cookie"):
            lines.append("Cookie: %s" % opts["cookie"])
        if opts.get("token"):
            lines.append("X-LC-Token: %s" % opts["token"])
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        status, rest = _read_handshake(sock)
        if status != 101:
            sock.close()
            raise RuntimeError("upgrade rejected with %d" % status)
        return cls(sock, rest)

    def _read_loop(self):
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                self._feed(data)
        except OSError:
            pass
        finally:
            with self._cond:
                self.closed = True
                self._cond.notify_all()

    def _feed(self, data):
        self.buffer.extend(data)
        while True:
            frame = self._parse_frame()
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 0x8:
                self.close()
                return
            if opcode != 0x1:
                continue
            try:
                doc = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            with self._cond:
                self.docs.append(doc)
                self._cond.notify_all()

    def _parse_frame(self):
        buf = self.buffer
        if len(buf) < 2:
            return None
        opcode = buf[0] & 0x0F
        length = buf[1] & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = struct.unpack("!H", bytes(buf[2:4]))[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = struct.unpack("!Q", bytes(buf[2:10]))[0]
            offset = 10
        if len(buf) < offset + length:
            return None
        payload = bytes(buf[offset:offset + length])
        del buf[:offset + length]
        return opcode, payload

    def send(self, obj):
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def seen(self, predicate):
        with self._cond:
            return next((d for d in self.docs if predicate(d)), None)

    def next(self, predicate, timeout_ms=5000):
        deadline = time.time() + timeout_ms / 1000.0
        with self._cond:
            while True:
                hit = next((d for d in self.docs if predicate(d)), None)
                if hit is not None:
                    return hit
                if self.closed:
                    raise RuntimeError("ws closed while waiting for message")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError(
                        "timeout waiting for ws message; docs: %s"
                        % json.dumps(self.docs, ensure_ascii=False)
                    )
                self._cond.wait(min(remaining, 0.2))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
