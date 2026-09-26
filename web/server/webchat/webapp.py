import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from http import client as http_client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_MESSAGE = 4 * 1024 * 1024
# No inbound frame may exceed the message cap: a single unfragmented frame is
# otherwise read (and handed to json.loads) up to this limit unchecked.
MAX_WS_FRAME = MAX_WS_MESSAGE
WS_PING_INTERVAL = 25.0

PUBLIC_API_PATHS = ["/api/login", "/api/register", "/api/whoami"]

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def accept_key(key):
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _json_text(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class _ConnClosed(Exception):
    pass


class WebSocketConn:
    def __init__(self, sock, rfile):
        self.sock = sock
        self.rfile = rfile
        self.account_id = None
        self.on_message = None
        self.on_close = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._fragments = []
        self._fragment_bytes = 0

    def send(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_frame(self, opcode, payload):
        if self._closed:
            return
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 127]) + struct.pack("!Q", length)
        try:
            with self._write_lock:
                self.sock.sendall(header + payload)
        except OSError:
            self._mark_closed()

    def _read_exact(self, n):
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise _ConnClosed()
        return data

    def _protocol_close(self):
        self._send_frame(0x8, struct.pack("!H", 1002))
        try:
            self.sock.close()
        except OSError:
            pass
        self._mark_closed()

    def _mark_closed(self):
        fire = False
        with self._state_lock:
            if not self._closed:
                self._closed = True
                fire = True
        if fire and self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass

    def reader_loop(self):
        try:
            while not self._closed:
                if not self._read_frame():
                    return
        except (OSError, ValueError, _ConnClosed):
            self._mark_closed()

    def _read_frame(self):
        first, second = self._read_exact(2)
        fin = (first & 0x80) != 0
        rsv = (first & 0x70) != 0
        opcode = first & 0x0F
        masked = (second & 0x80) != 0
        if rsv or not masked:
            self._protocol_close()
            return False
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            big = struct.unpack("!Q", self._read_exact(8))[0]
            if big > MAX_WS_FRAME:
                self._protocol_close()
                return False
            length = big
        if (opcode & 0x8) != 0 and (not fin or length > 125):
            self._protocol_close()
            return False
        mask = self._read_exact(4)
        payload = self._read_exact(length)
        if length:
            repeated = bytes(mask) * ((length + 3) // 4)
            xored = int.from_bytes(payload, "big") ^ int.from_bytes(repeated[:length], "big")
            payload = xored.to_bytes(length, "big")
        if opcode == 0x8:
            self._send_frame(0x8, payload)
            try:
                self.sock.close()
            except OSError:
                pass
            self._mark_closed()
            return False
        if opcode == 0x9:
            self._send_frame(0xA, payload)
            return True
        if opcode == 0xA:
            return True
        if opcode in (0x1, 0x2):
            if length > MAX_WS_MESSAGE:
                self._protocol_close()
                return False
            if fin:
                self._deliver(payload)
            else:
                self._fragments = [payload]
                self._fragment_bytes = len(payload)
                if self._fragment_bytes > MAX_WS_MESSAGE:
                    self._protocol_close()
                    return False
        elif opcode == 0x0:
            self._fragment_bytes += len(payload)
            if self._fragment_bytes > MAX_WS_MESSAGE:
                self._protocol_close()
                return False
            self._fragments.append(payload)
            if fin:
                full = b"".join(self._fragments)
                self._fragments = []
                self._fragment_bytes = 0
                self._deliver(full)
        return True

    def _deliver(self, payload):
        if self.on_message is None:
            return
        try:
            self.on_message(payload.decode("utf-8", "replace"))
        except Exception:
            pass

    def ping(self):
        if not self._closed:
            self._send_frame(0x9, b"")

    def close(self):
        if self._closed:
            return
        self._send_frame(0x8, b"")
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._mark_closed()


class _Res:
    def __init__(self, app, handler):
        self.app = app
        self.handler = handler
        self.head_sent = False

    def raw(self, code, headers, body):
        self.app._respond_raw(self.handler, code, headers, body or b"")

    def json(self, code, obj, extra_headers=None):
        self.app._respond_json(self.handler, code, obj, extra_headers)

    def start_body(self, code, headers):
        self.app._respond_head(self.handler, code, headers, b"")
        self.head_sent = True

    def write_chunk(self, chunk):
        self.handler.connection.sendall(chunk)

    def abort(self):
        try:
            self.handler.connection.close()
        except OSError:
            pass


class WebApp:
    def __init__(
        self,
        public_dir,
        port,
        host="127.0.0.1",
        on_auth=None,
        on_ws_message=None,
        on_ws_open=None,
        on_ws_close=None,
        api_get=None,
        api_post=None,
    ):
        self.public_dir = public_dir
        self.port = port
        self.host = host or "127.0.0.1"
        self.on_auth = on_auth or (lambda handler, params: None)
        self.on_ws_message = on_ws_message or (lambda conn, text: None)
        self.on_ws_open = on_ws_open or (lambda conn: None)
        self.on_ws_close = on_ws_close or (lambda conn: None)
        self.api_get = api_get or (lambda pathname, params, res, account_id: None)
        self.api_post = api_post or (lambda pathname, params, res, account_id: None)
        self.connections = set()
        self._conns_lock = threading.Lock()
        self.httpd = None

    def broadcast(self, obj, account_id=None):
        text = _json_text(obj)
        with self._conns_lock:
            conns = list(self.connections)
        for conn in conns:
            if account_id is not None and conn.account_id != account_id:
                continue
            try:
                conn.send(text)
            except Exception:
                pass

    def start(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 300

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                app.handle_request(self, "GET")

            def do_POST(self):
                app.handle_request(self, "POST")

            def do_PUT(self):
                app.handle_request(self, "PUT")

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.socket.getsockname()[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        threading.Thread(target=self._ping_loop, daemon=True, name="ws-ping").start()
        return self.port

    def _ping_loop(self):
        # Protocol-level pings keep the handler's socket timeout fed for
        # healthy idle connections (browsers answer automatically): without
        # them every quiet tab is killed after 5 minutes and its presence
        # flickers while it reconnects.
        while True:
            time.sleep(WS_PING_INTERVAL)
            with self._conns_lock:
                conns = list(self.connections)
            for conn in conns:
                try:
                    conn.ping()
                except Exception:
                    pass

    def stop(self):
        with self._conns_lock:
            conns = list(self.connections)
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def _ws_closed(self, conn):
        with self._conns_lock:
            self.connections.discard(conn)
        self.on_ws_close(conn)

    def handle_request(self, handler, method):
        try:
            self._handle_request(handler, method)
        except Exception:
            try:
                self._respond_raw(handler, 500, [("Content-Type", "text/plain; charset=utf-8")], b"internal error")
            except Exception:
                pass

    def _handle_request(self, handler, method):
        upgrade = (handler.headers.get("Upgrade") or "").lower()
        if upgrade == "websocket":
            self._handle_upgrade(handler)
            return
        url = urlsplit(handler.path)
        try:
            pathname = unquote(url.path, errors="strict")
        except Exception:
            self._respond_raw(handler, 400, [], b"bad request")
            return
        if pathname.startswith("/api/") or pathname.startswith("/files/"):
            params = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            account_id = self.on_auth(handler, params)
            if not account_id and pathname not in PUBLIC_API_PATHS:
                self._respond_json(handler, 401, {"error": "unauthorized"})
                return
            res = _Res(self, handler)
            if method == "GET":
                result = self.api_get(pathname, params, res, account_id)
            elif method in ("POST", "PUT"):
                result = self.api_post(pathname, params, res, account_id)
            else:
                self._respond_raw(handler, 405, [], b"")
                return
            if result is not None:
                self._respond_json(handler, 200, result)
            return
        self._serve_static(handler, pathname)

    def _handle_upgrade(self, handler):
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            handler.close_connection = True
            try:
                handler.connection.close()
            except OSError:
                pass
            return
        url = urlsplit(handler.path)
        params = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        account_id = self.on_auth(handler, params)
        if not account_id:
            handler.close_connection = True
            try:
                handler.connection.sendall(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
                handler.connection.close()
            except OSError:
                pass
            return
        accept = accept_key(key)
        handler.close_connection = True
        handler.connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
            ).encode("ascii")
        )
        try:
            handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn = WebSocketConn(handler.connection, handler.rfile)
        conn.account_id = account_id
        conn.on_message = lambda text: self.on_ws_message(conn, text)
        conn.on_close = lambda: self._ws_closed(conn)
        with self._conns_lock:
            self.connections.add(conn)
        self.on_ws_open(conn)
        conn.reader_loop()

    def _serve_static(self, handler, pathname):
        rel = "/index.html" if pathname == "/" else pathname
        root = os.path.abspath(self.public_dir)
        file_path = os.path.abspath(os.path.join(root, rel.lstrip("/")))
        if file_path != root and not file_path.startswith(root + os.sep):
            self._respond_raw(handler, 403, [], b"")
            return
        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except OSError:
            self._respond_raw(handler, 404, [("Content-Type", "text/plain; charset=utf-8")], b"not found")
            return
        ext = os.path.splitext(file_path)[1].lower()
        self._respond_raw(
            handler, 200, [("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))], data
        )

    def _respond_head(self, handler, code, headers, body):
        reason = http_client.responses.get(code, "")
        head = "HTTP/1.1 %d %s\r\n" % (code, reason)
        lines = list(headers)
        if not any(k.lower() == "content-length" for k, _ in lines):
            lines.append(("Content-Length", str(len(body))))
        lines.append(("Connection", "close"))
        for k, v in lines:
            head += "%s: %s\r\n" % (k, v)
        head += "\r\n"
        handler.close_connection = True
        handler.connection.sendall(head.encode("latin-1") + body)

    def _respond_raw(self, handler, code, headers, body):
        self._respond_head(handler, code, headers, body)

    def _respond_json(self, handler, code, obj, extra_headers=None):
        body = _json_text(obj).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8")]
        if extra_headers:
            headers.extend(extra_headers)
        self._respond_head(handler, code, headers, body)
