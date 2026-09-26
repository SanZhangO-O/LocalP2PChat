import hashlib
import hmac
import json
import os
import re
import secrets
import threading

from . import util as U

SCRYPT_KEYLEN = 64
SESSION_TTL_MS = 7 * 24 * 3600 * 1000
LOGIN_WINDOW_MS = 5 * 60 * 1000
LOGIN_MAX_ATTEMPTS = 10
REGISTER_WINDOW_MS = 5 * 60 * 1000
REGISTER_MAX_ATTEMPTS = 20


def hash_password(password, salt):
    return hashlib.scrypt(
        str(password).encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=16384,
        r=8,
        p=1,
        dklen=SCRYPT_KEYLEN,
        maxmem=32 * 1024 * 1024,
    ).hex()


def verify_password(password, salt, expected_hash):
    actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected_hash)


def valid_username(name):
    return isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5]{1,32}", name) is not None


def valid_password(pw):
    return isinstance(pw, str) and 6 <= len(pw) <= 128


class AccountRegistry:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.file_path = os.path.join(data_dir, "accounts.json")
        self.accounts = {}
        self.sessions = {}
        self.login_attempts = {}
        self.register_attempts = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
        except (OSError, ValueError):
            data = []
        if not isinstance(data, list):
            data = []
        for rec in data:
            if isinstance(rec, dict) and rec.get("id"):
                self.accounts[rec["id"]] = rec

    def save(self):
        with self._lock:
            data = list(self.accounts.values())
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, self.file_path)

    @property
    def first_run(self):
        return len(self.accounts) == 0

    def get(self, account_id):
        return self.accounts.get(account_id)

    def create(self, username, password):
        if not valid_username(username):
            return {"ok": False, "message": "\u7528\u6237\u540d\u9700 1-32 \u4f4d\uff08\u4e2d\u82f1\u6587\u3001\u6570\u5b57\u3001_- \uff09"}
        if not valid_password(password):
            return {"ok": False, "message": "\u5bc6\u7801\u81f3\u5c11 6 \u4f4d"}
        exists = any(
            rec["username"].lower() == username.lower() for rec in self.accounts.values()
        )
        if exists:
            return {"ok": False, "message": "\u7528\u6237\u540d\u5df2\u5b58\u5728"}
        salt = secrets.token_bytes(16).hex()
        record = {
            "id": U.uuid(),
            "username": username,
            "salt": salt,
            "passHash": hash_password(password, salt),
            "nickname": username,
            "createdAt": U.now_ms(),
        }
        self.accounts[record["id"]] = record
        self.save()
        return {"ok": True, "record": record}

    def find_by_username(self, username):
        # duplicate registration is case-insensitive, so login must match
        # names the same way ("Alice" logs into the "alice" account)
        want = str(username or "").lower()
        for rec in self.accounts.values():
            if rec["username"].lower() == want:
                return rec
        return None

    def verify(self, username, password):
        rec = self.find_by_username(username)
        if rec is None:
            hash_password(password or "", "00")
            return None
        if not verify_password(password or "", rec["salt"], rec["passHash"]):
            return None
        return rec

    def create_session(self, account_id):
        token = secrets.token_bytes(32).hex()
        with self._lock:
            self.sessions[token] = {"accountId": account_id, "expires": U.now_ms() + SESSION_TTL_MS}
            self._sweep_sessions()
        return token

    def session_account(self, token):
        if not token:
            return None
        with self._lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            if U.now_ms() > session["expires"]:
                del self.sessions[token]
                return None
            return self.accounts.get(session["accountId"])

    def drop_session(self, token):
        if token:
            with self._lock:
                self.sessions.pop(token, None)

    def _sweep_sessions(self):
        now = U.now_ms()
        expired = [t for t, s in self.sessions.items() if now > s["expires"]]
        for token in expired:
            self.sessions.pop(token, None)

    def allow_login(self, ip):
        if not ip:
            return True
        now = U.now_ms()
        entry = self.login_attempts.get(ip)
        if entry is None or now > entry["resetAt"]:
            entry = {"count": 0, "resetAt": now + LOGIN_WINDOW_MS}
            self.login_attempts[ip] = entry
        if entry["count"] >= LOGIN_MAX_ATTEMPTS:
            return False
        return True

    def note_login_failure(self, ip):
        if not ip:
            return
        entry = self.login_attempts.get(ip)
        if entry is not None:
            entry["count"] += 1

    def allow_register(self, ip):
        if not ip:
            return True
        now = U.now_ms()
        entry = self.register_attempts.get(ip)
        if entry is None or now > entry["resetAt"]:
            entry = {"count": 0, "resetAt": now + REGISTER_WINDOW_MS}
            self.register_attempts[ip] = entry
        entry["count"] += 1
        return entry["count"] <= REGISTER_MAX_ATTEMPTS
