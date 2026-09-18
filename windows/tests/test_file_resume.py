"""Resume support for file downloads: the file_download request carries the
received-byte offset (mandatory), the sender streams from there, and an
interrupted transfer keeps its ".part" staging file so the next attempt
continues instead of starting over.

Covers:
  * offset field roundtrip / missing-or-negative rejection (models)
  * interrupted connection -> resume -> identical bytes (fake sender that
    drops the connection mid-stream)
  * cancel -> .part retained -> resume completes (real download server)
  * offset past the declared size refused by the sender (no meta, no bytes)
  * offset == size with a complete staging file finishes without a byte
  * a staging file larger than the declared size is reset and re-fetched
"""

import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
import unittest

from localchat.crypto import aes_gcm_encrypt, random_bytes, to_b64
from localchat.models import FileInfo, NetworkPacket
from localchat.network import (
    CHUNK_SIZE,
    _download_file_offer,
    _serve_file_download,
    file_download_token,
)

FILE_ID = "resume-file-1"
PAYLOAD = bytes((i * 7 + 13) % 256 for i in range(CHUNK_SIZE * 3 + 1234))


def _read_line(sock) -> str:
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            return bytes(buf).decode("utf-8", "replace")
        if b == b"\n":
            return bytes(buf).decode("utf-8", "replace").rstrip("\r")
        buf.extend(b)


class FakeSender:
    """Scriptable download server: per attempt it streams the payload from the
    requested offset, dropping the connection after N chunks on configured
    attempts (a mid-transfer network break) or sending the EOF marker."""

    def __init__(self, payload: bytes, key: bytes, drop_after: dict) -> None:
        self.payload = payload
        self.key = key
        self.drop_after = dict(drop_after)
        self.attempt = 0
        self.requests = []
        self._stop = threading.Event()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.srv.settimeout(0.5)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            self.attempt += 1
            threading.Thread(
                target=self._serve, args=(conn, self.attempt), daemon=True
            ).start()

    def _serve(self, conn, attempt: int) -> None:
        limit = self.drop_after.get(attempt)
        try:
            conn.settimeout(10)
            line = _read_line(conn)
            req = NetworkPacket.from_json(line)
            self.requests.append(req)
            if req.type != "file_download" or req.file_id != FILE_ID:
                return
            if req.token != file_download_token(self.key, FILE_ID):
                return
            meta = NetworkPacket(
                type="file_meta",
                file_info=FileInfo(FILE_ID, "resume.bin", len(self.payload), "", 0),
            )
            conn.sendall(
                (to_b64(aes_gcm_encrypt(self.key, meta.to_json().encode("utf-8"))) + "\n").encode("utf-8")
            )
            pos = int(req.offset)
            sent = 0
            while pos < len(self.payload):
                if limit is not None and sent >= limit:
                    return  # drop without EOF: a mid-transfer connection break
                chunk = self.payload[pos : pos + CHUNK_SIZE]
                blob = aes_gcm_encrypt(self.key, chunk)
                conn.sendall(struct.pack(">I", len(blob)) + blob)
                pos += len(chunk)
                sent += 1
            if limit is None:
                conn.sendall(struct.pack(">I", 0))
        except Exception:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


class RealSender:
    """Accept loop wired to the production _serve_file_download."""

    def __init__(self, path: str, key: bytes, size: int) -> None:
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.srv.settimeout(0.5)
        self.port = self.srv.getsockname()[1]
        self.path = path
        self.key = key
        self.size = size
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            threading.Thread(
                target=_serve_file_download,
                args=(conn, FILE_ID, self.path, self.size, self.key),
                daemon=True,
            ).start()


class FileResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="lc_resume_")
        self.src = os.path.join(self.tmp, "src.bin")
        with open(self.src, "wb") as f:
            f.write(PAYLOAD)
        self.key = random_bytes(32)
        self.target = os.path.join(self.tmp, "out.bin")

    def tearDown(self) -> None:
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _offer(self, port: int) -> FileInfo:
        return FileInfo(
            FILE_ID, "resume.bin", len(PAYLOAD), "127.0.0.1", port, to_b64(self.key)
        )

    def _part_size(self) -> int:
        try:
            return os.path.getsize(self.target + ".part")
        except OSError:
            return 0

    # ------------------------------------------------------------ protocol

    def test_offset_roundtrip_and_required_validation(self):
        pkt = NetworkPacket(type="file_download", file_id="f1", offset=12345)
        wire = pkt.to_json()
        self.assertEqual(json.loads(wire)["offset"], 12345)
        back = NetworkPacket.from_json(wire)
        self.assertEqual(back.offset, 12345)
        # a plain packet never carries the field
        self.assertNotIn("offset", NetworkPacket(type="ping").to_json())
        # mandatory: a request without it is malformed, a negative one too
        with self.assertRaises(ValueError):
            NetworkPacket.from_json('{"type":"file_download","fileId":"f1"}')
        with self.assertRaises(ValueError):
            NetworkPacket.from_json(
                '{"type":"file_download","fileId":"f1","offset":-1}'
            )
        with self.assertRaises(ValueError):
            NetworkPacket.from_json(
                '{"type":"file_download","fileId":"f1","offset":"1.5"}'
            )

    # --------------------------------------------------------- interruption

    def test_interrupted_download_resumes_to_identical_bytes(self):
        sender = FakeSender(PAYLOAD, self.key, drop_after={1: 2})
        try:
            ok, message = _download_file_offer(self._offer(sender.port), self.target)
            self.assertFalse(ok)
            self.assertEqual(message, "\u6587\u4ef6\u4f20\u8f93\u4e2d\u65ad")  # 文件传输中断
            part = self._part_size()
            self.assertEqual(part, CHUNK_SIZE * 2)
            self.assertFalse(os.path.exists(self.target))
            # the resume entry / ".part" survive: request carries the offset
            ok, message = _download_file_offer(
                self._offer(sender.port), self.target, offset=part
            )
            self.assertTrue(ok, message)
            with open(self.target, "rb") as f:
                self.assertEqual(
                    hashlib.sha256(f.read()).hexdigest(),
                    hashlib.sha256(PAYLOAD).hexdigest(),
                )
            self.assertEqual(self._part_size(), 0)
            self.assertEqual(len(sender.requests), 2)
            self.assertEqual(sender.requests[0].offset, 0)
            self.assertEqual(sender.requests[1].offset, CHUNK_SIZE * 2)
        finally:
            sender.close()

    def test_cancel_keeps_the_part_and_resume_completes(self):
        sender = RealSender(self.src, self.key, len(PAYLOAD))
        cancel = threading.Event()
        seen = []

        def progress(received, total):
            seen.append(received)
            cancel.set()

        try:
            ok, message = _download_file_offer(
                self._offer(sender.port), self.target, progress=progress, cancel=cancel
            )
            self.assertFalse(ok)
            self.assertEqual(message, "\u4e0b\u8f7d\u5df2\u53d6\u6d88")  # 下载已取消
            part = self._part_size()
            self.assertEqual(part, CHUNK_SIZE)
            self.assertTrue(seen)
            # resume: the first progress sample already includes the offset
            seen.clear()
            ok, message = _download_file_offer(
                self._offer(sender.port),
                self.target,
                progress=lambda r, t: seen.append(r),
                offset=part,
            )
            self.assertTrue(ok, message)
            self.assertTrue(seen and seen[0] >= CHUNK_SIZE)
            with open(self.target, "rb") as got, open(self.src, "rb") as want:
                self.assertEqual(got.read(), want.read())
        finally:
            sender.close()

    # ------------------------------------------------------------- offsets

    def test_offset_past_declared_size_is_refused_without_bytes(self):
        sender = RealSender(self.src, self.key, len(PAYLOAD))
        try:
            sock = socket.create_connection(("127.0.0.1", sender.port), timeout=6)
            try:
                req = NetworkPacket(
                    type="file_download",
                    file_id=FILE_ID,
                    token=file_download_token(self.key, FILE_ID),
                    offset=len(PAYLOAD) + 1,
                )
                sock.sendall((req.to_json() + "\n").encode("utf-8"))
                sock.settimeout(2.0)
                buf = bytearray()
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buf.extend(chunk)
                except socket.timeout:
                    pass
                self.assertEqual(
                    bytes(buf), b"", "an out-of-range offset must be refused"
                )
            finally:
                sock.close()
        finally:
            sender.close()

    def test_offset_equal_to_size_finishes_existing_part(self):
        # a full staging file (e.g. cancelled after the last chunk, before the
        # rename): the sender streams zero bytes and the part is finalized
        with open(self.target + ".part", "wb") as f:
            f.write(PAYLOAD)
        sender = RealSender(self.src, self.key, len(PAYLOAD))
        try:
            ok, message = _download_file_offer(
                self._offer(sender.port), self.target, offset=len(PAYLOAD)
            )
            self.assertTrue(ok, message)
            with open(self.target, "rb") as f:
                self.assertEqual(f.read(), PAYLOAD)
            self.assertEqual(self._part_size(), 0)
        finally:
            sender.close()

    def test_oversized_part_is_reset_and_downloaded_again(self):
        with open(self.target + ".part", "wb") as f:
            f.write(b"x" * (len(PAYLOAD) + 4096))
        sender = RealSender(self.src, self.key, len(PAYLOAD))
        try:
            ok, message = _download_file_offer(
                self._offer(sender.port),
                self.target,
                offset=len(PAYLOAD) + 4096,
            )
            self.assertTrue(ok, message)
            with open(self.target, "rb") as got, open(self.src, "rb") as want:
                self.assertEqual(got.read(), want.read())
            self.assertEqual(self._part_size(), 0)
        finally:
            sender.close()


if __name__ == "__main__":
    unittest.main()
