import json
import os

from . import crypto as C


def atomic_write_json(file_path, obj):
    tmp = file_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    os.replace(tmp, file_path)


def read_json(file_path, fallback):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return fallback


class SecretBox:
    def __init__(self, data_dir):
        self.file_path = os.path.join(data_dir, "secret_key.json")
        doc = read_json(self.file_path, None)
        self.key = None
        if isinstance(doc, dict) and doc.get("k"):
            try:
                self.key = C.from_b64(doc["k"])
            except Exception:
                self.key = None
        if not isinstance(self.key, (bytes, bytearray)) or len(self.key) != C.KEY_LEN:
            self.key = C.random_bytes(C.KEY_LEN)
            atomic_write_json(self.file_path, {"scheme": "plain", "k": C.to_b64(self.key)})
        self.key = bytes(self.key)

    def protect(self, text):
        if not text:
            return text
        try:
            return "enc1:" + C.to_b64(C.aes_gcm_encrypt(self.key, str(text).encode("utf-8")))
        except Exception:
            return text

    def unprotect(self, text):
        if not text or not str(text).startswith("enc1:"):
            return text
        try:
            return C.aes_gcm_decrypt(self.key, C.from_b64(str(text)[5:])).decode("utf-8")
        except Exception:
            return ""


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.chats_dir = os.path.join(data_dir, "chats")
        os.makedirs(self.chats_dir, exist_ok=True)
        self.secret_box = SecretBox(data_dir)

    def _path(self, name):
        return os.path.join(self.data_dir, name)

    def load(self, name, fallback):
        return read_json(self._path(name), fallback)

    def save(self, name, obj):
        atomic_write_json(self._path(name), obj)

    def chat_path(self, key):
        safe = key.encode("utf-8").hex()
        return os.path.join(self.chats_dir, safe + ".json")

    def load_chat(self, key):
        doc = read_json(self.chat_path(key), None)
        if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
            return []
        return doc["messages"]

    def save_chat(self, key, messages):
        msgs = []
        for m in messages:
            out = dict(m)
            content = out.get("content")
            if content is not None and not (
                isinstance(content, str) and content.startswith("enc1:")
            ):
                out["content"] = self.secret_box.protect(content)
            if out.get("replyPreview"):
                out["replyPreview"] = self.secret_box.protect(out["replyPreview"])
            msgs.append(out)
        atomic_write_json(self.chat_path(key), {"messages": msgs})

    def load_chat_decrypted(self, key):
        out = []
        for m in self.load_chat(key):
            item = dict(m)
            content = item.get("content")
            if isinstance(content, str) and content.startswith("enc1:"):
                item["content"] = self.secret_box.unprotect(content)
            preview = item.get("replyPreview")
            if isinstance(preview, str) and preview.startswith("enc1:"):
                item["replyPreview"] = self.secret_box.unprotect(preview)
            out.append(item)
        return out

    def delete_chat(self, key):
        try:
            os.unlink(self.chat_path(key))
        except OSError:
            pass

    def protect(self, text):
        return self.secret_box.protect(text)

    def unprotect(self, text):
        return self.secret_box.unprotect(text)
