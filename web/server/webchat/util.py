import re
import socket
import time
import uuid as uuidlib

MAX_CONTENT_LENGTH = 5000
MAX_NAME_LENGTH = 64
MAX_ANNOUNCEMENT_LENGTH = 500
MAX_EMOJI_LEN = 16
MAX_MESSAGE_HISTORY = 2000

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"}
AUDIO_EXTENSIONS = {".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".mp3", ".flac"}

FILE_KIND_FILE = "file"
FILE_KIND_IMAGE = "image"
FILE_KIND_VIDEO = "video"
FILE_KIND_AUDIO = "audio"

_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

_JS_WHITESPACE = set(
    "\t\n\x0b\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def uuid():
    return str(uuidlib.uuid4())


def now_ms():
    return int(time.time() * 1000)


def lan_addresses():
    out = []
    seen = set()
    candidates = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.add(info[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            candidates.add(probe.getsockname()[0])
            probe.connect(("8.8.8.8", 80))
            candidates.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    for addr in candidates:
        if addr in seen or addr.startswith("127.") or addr.startswith("169.254."):
            continue
        seen.add(addr)
        out.append(addr)
    return out


def code_points_of(text):
    return list(text)


def is_valid_content(content):
    if not isinstance(content, str) or not content.strip():
        return False
    return len(code_points_of(content)) <= MAX_CONTENT_LENGTH


def is_valid_name(name):
    if not isinstance(name, str):
        return False
    trimmed = name.strip()
    return len(trimmed) > 0 and len(code_points_of(trimmed)) <= MAX_NAME_LENGTH


def detect_media_kind(name):
    idx = name.rfind(".")
    ext = name[idx:].lower() if idx >= 0 else ""
    if ext in IMAGE_EXTENSIONS:
        return FILE_KIND_IMAGE
    if ext in VIDEO_EXTENSIONS:
        return FILE_KIND_VIDEO
    if ext in AUDIO_EXTENSIONS:
        return FILE_KIND_AUDIO
    return FILE_KIND_FILE


def sanitize_file_name(name):
    text = ("" if name is None else str(name)).replace("\\", "/")
    idx = text.rfind("/")
    if idx >= 0:
        text = text[idx + 1:]
    out = []
    for ch in text:
        cp = ord(ch)
        if cp < 32 or (0x7F <= cp < 0xA0):
            continue
        out.append(ch)
    text = "".join(out)
    text = re.sub(r"[ .]+$", "", text)
    if len(text) > 255:
        text = re.sub(r"[ .]+$", "", text[:255])
    return text or "file"


def sanitize_file_id(value):
    out = []
    for ch in "" if value is None else str(value):
        if ch in _ID_CHARS:
            out.append(ch)
        if len(out) >= 64:
            break
    return "".join(out) or "file"


def _is_emoji_only_printable(ch):
    cp = ord(ch)
    if cp < 0x20 or (0x7F <= cp < 0xA0):
        return False
    return ch not in _JS_WHITESPACE


def sanitize_emoji(value):
    text = "" if value is None else str(value)
    out = []
    utf16_len = 0
    for ch in text:
        if _is_emoji_only_printable(ch):
            out.append(ch)
            utf16_len += 2 if ord(ch) > 0xFFFF else 1
        if utf16_len >= MAX_EMOJI_LEN * 2:
            break
    joined = "".join(out)
    return "".join(code_points_of(joined)[:MAX_EMOJI_LEN])


def direct_key(a, b):
    x, y = sorted([str(a), str(b)])
    return "direct:%s|%s" % (x, y)
