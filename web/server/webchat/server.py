import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

from . import util as U
from .accounts import AccountRegistry
from .engine import ChatEngine
from .webapp import WebApp

MAX_JSON_BODY = 64 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _port_arg(value, fallback, flag):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed < 1 or parsed > 65535:
        print(
            "\u5ffd\u7565\u65e0\u6548\u53c2\u6570 %s %s\uff0c\u4f7f\u7528\u9ed8\u8ba4\u503c %d"
            % (flag, value, fallback)
        )
        return fallback
    return parsed


def parse_args(argv):
    args = {
        "http_port": 8090,
        "http_host": "127.0.0.1",
        "data": os.path.join(_WEB_DIR, "data"),
        "public_dir": os.path.join(_WEB_DIR, "public"),
        "open_browser": False,
    }
    items = list(argv)
    i = 0
    while i < len(items):
        a = items[i]
        if a == "--http" and i + 1 < len(items):
            args["http_port"] = _port_arg(items[i + 1], args["http_port"], a)
            i += 2
        elif a == "--http-host" and i + 1 < len(items):
            args["http_host"] = items[i + 1]
            i += 2
        elif a == "--data" and i + 1 < len(items):
            args["data"] = os.path.abspath(items[i + 1])
            i += 2
        elif a == "--public" and i + 1 < len(items):
            args["public_dir"] = os.path.abspath(items[i + 1])
            i += 2
        elif a == "--open":
            args["open_browser"] = True
            i += 1
        else:
            i += 1
    return SimpleNamespace(**args)


def open_in_browser(url):
    try:
        if sys.platform == "win32":
            os.startfile(url)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _session_cookie(token):
    from .accounts import SESSION_TTL_MS

    return (
        "lc_session=%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=%d"
        % (token, SESSION_TTL_MS // 1000)
    )


_CLEAR_COOKIE = "lc_session=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"


class ChatServer:
    def __init__(self, args):
        self.args = args
        self.data_dir = args.data
        os.makedirs(args.data, exist_ok=True)
        self.registry = AccountRegistry(args.data)
        self.engine = ChatEngine(self, args.data)
        self.online_counts = {}
        self.lock = threading.RLock()
        self.web = WebApp(
            public_dir=args.public_dir,
            port=args.http_port,
            host=args.http_host,
            on_auth=self._auth_account_id,
            on_ws_open=self.on_ws_open,
            on_ws_close=self.on_ws_close,
            on_ws_message=self.on_ws_message,
            api_get=self.handle_api_get,
            api_post=self.handle_api_post,
        )

    def is_online(self, account_id):
        return self.online_counts.get(account_id, 0) > 0

    def on_ws_open(self, conn):
        with self.lock:
            self.online_counts[conn.account_id] = self.online_counts.get(conn.account_id, 0) + 1
            self.engine.presence_changed()
        conn.send(json.dumps({"snapshot": self.engine.snapshot_for(conn.account_id)}, separators=(",", ":"), ensure_ascii=False))

    def on_ws_close(self, conn):
        with self.lock:
            left = self.online_counts.get(conn.account_id, 1) - 1
            if left <= 0:
                self.online_counts.pop(conn.account_id, None)
            else:
                self.online_counts[conn.account_id] = left
            self.engine.presence_changed()

    def start(self):
        port = self.web.start()
        print("LocalChat Web \u670d\u52a1\u5668\u5df2\u542f\u52a8")
        print("  \u672c\u673a\u8bbf\u95ee:   http://localhost:%d" % port)
        lan = U.lan_addresses()
        if self.args.http_host == "0.0.0.0" and lan:
            print("  \u5c40\u57df\u7f51\u5185\u5176\u4ed6\u7528\u6237\u7528\u6d4f\u89c8\u5668\u6253\u5f00:")
            for ip in lan:
                print("    http://%s:%d" % (ip, port))
        else:
            print("  \u754c\u9762\u5730\u5740:   http://%s:%d" % (self.args.http_host, port))
        print(
            "  \u6570\u636e\u76ee\u5f55:   %s (%d \u4e2a\u8d26\u53f7)"
            % (self.data_dir, len(self.registry.accounts))
        )
        print("  \u9996\u6b21\u4f7f\u7528\u5728\u9875\u9762\u4e0a\u521b\u5efa\u7b2c\u4e00\u4e2a\u8d26\u53f7\uff1bCtrl+C \u505c\u6b62\u670d\u52a1")
        if self.args.open_browser:
            open_in_browser("http://localhost:%d" % port)
        return port

    def stop(self):
        self.web.stop()

    def on_ws_message(self, conn, text):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if not conn.account_id:
            return
        action = msg.get("action")
        try:
            with self.lock:
                self.engine.handle_action(conn, action, msg)
        except Exception as e:
            doc = {"error": str(e) or repr(e)}
            if "action" in msg:
                doc["action"] = msg["action"]
            try:
                conn.send(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
            except Exception:
                pass

    def _session_token(self, headers):
        header = headers.get("Cookie") or ""
        for part in header.split(";"):
            idx = part.find("=")
            if idx < 0:
                continue
            if part[:idx].strip() == "lc_session":
                return part[idx + 1:].strip()
        return None

    def _pick_token(self, headers, params):
        header = headers.get("X-LC-Token")
        if header:
            return str(header)
        if params:
            query_token = params.get("token")
            if query_token:
                return query_token
        return self._session_token(headers)

    def _client_ip(self, handler):
        try:
            return handler.client_address[0] or ""
        except Exception:
            return ""

    def _auth_account_id(self, handler, params):
        token = self._pick_token(handler.headers, params)
        rec = self.registry.session_account(token)
        return rec["id"] if rec is not None else None

    def _read_json_body(self, handler):
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_JSON_BODY:
            raise RuntimeError("body too large")
        data = handler.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(data.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def handle_api_get(self, pathname, params, res, account_id):
        if pathname == "/api/whoami":
            record = self.registry.get(account_id) if account_id else None
            return {
                "authenticated": bool(record),
                "firstRun": self.registry.first_run,
                "username": record["username"] if record else None,
            }
        if not account_id:
            res.json(401, {"error": "\u672a\u767b\u5f55"})
            return None
        if pathname.startswith("/files/"):
            self.engine.serve_file(res, pathname[len("/files/"):])
            return None
        return {"ok": True}

    def handle_api_post(self, pathname, params, res, account_id):
        if pathname == "/api/login":
            body = self._read_json_body(res.handler)
            with self.lock:
                if not self.registry.allow_login(self._client_ip(res.handler)):
                    res.json(429, {"ok": False, "message": "\u5c1d\u8bd5\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5"})
                    return None
                record = self.registry.verify(body.get("username"), body.get("password"))
                if record is None:
                    self.registry.note_login_failure(self._client_ip(res.handler))
                    res.json(401, {"ok": False, "message": "\u7528\u6237\u540d\u6216\u5bc6\u7801\u9519\u8bef"})
                    return None
                token = self.registry.create_session(record["id"])
            res.json(
                200,
                {"ok": True, "username": record["username"], "token": token},
                [("Set-Cookie", _session_cookie(token))],
            )
            return None
        if pathname == "/api/register":
            body = self._read_json_body(res.handler)
            with self.lock:
                if not self.registry.allow_register(self._client_ip(res.handler)):
                    res.json(429, {"ok": False, "message": "\u5c1d\u8bd5\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5"})
                    return None
                result = self.registry.create(
                    str(body.get("username") or ""), str(body.get("password") or "")
                )
                if not result["ok"]:
                    res.json(400, result)
                    return None
                is_authed = bool(account_id)
                token = self.registry.create_session(result["record"]["id"])
                cookie_headers = [] if is_authed else [("Set-Cookie", _session_cookie(token))]
                self.engine.push_snapshot_all()
            res.json(
                200,
                {"ok": True, "username": result["record"]["username"], "token": token},
                cookie_headers,
            )
            return None
        if pathname == "/api/logout":
            cookie_token = self._session_token(res.handler.headers)
            token = self._pick_token(res.handler.headers, params)
            self.registry.drop_session(token)
            if not cookie_token or token == cookie_token:
                res.json(200, {"ok": True}, [("Set-Cookie", _CLEAR_COOKIE)])
            else:
                res.json(200, {"ok": True})
            return None
        if not account_id:
            res.json(401, {"error": "\u672a\u767b\u5f55"})
            return None
        if pathname == "/api/upload":
            self.handle_upload(params, res)
            return None
        return {"ok": False}

    def handle_upload(self, params, res):
        name = U.sanitize_file_name(params.get("name") or "file")
        upload_id = "%s_%s" % (U.uuid(), name)
        target = self.engine._upload_path(upload_id)
        responded = []

        def respond(code, obj):
            if responded:
                return
            responded.append(True)
            res.json(code, obj)

        try:
            length = int(res.handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        size = 0
        rejected = False
        try:
            with open(target, "wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = res.handler.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        if not rejected:
                            rejected = True
                            respond(413, {"error": "file too large"})
                    elif not rejected:
                        out.write(chunk)
        except OSError:
            respond(500, {"error": "upload failed"})
            return
        if not responded:
            respond(200, {"uploadId": upload_id, "size": size})
