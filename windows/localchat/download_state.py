"""Persistent resume state for interrupted file downloads (Windows side).

A download writes decrypted chunks into "<target>.part" and, when it is
cancelled or the transfer breaks, KEEPS that staging file. This store
remembers, per file id, where the part lives and how many bytes it holds, so
the UI can render "已暂停 xx%（点击续传）" and the next tap resumes from the
exact offset. Everything needed to continue is persisted (address and
per-file key included) because a restarted app no longer has them in memory:
the messages restored from the database deliberately blank the download
address (see ChatViewModel._restored_file_info).

The blob is kept in the ChatStore settings table through get_secret/set_secret
("enc1:..." at rest, encrypted under the installation content key), so a
stolen database alone reveals neither the resume offsets nor the per-file
AES keys.
"""

import json
import os
import threading
import time
from typing import Dict, List, Optional

# Cap on remembered downloads: oldest entries are pruned first, so a long
# history of abandoned parts cannot grow the settings row without bound.
MAX_RESUME_ENTRIES = 200


class FileResumeStore:
    """file_id -> {target, received, total, host, port, key, folderId,...}."""

    SECRET_KEY = "file_download_resume_v1"
    # folder_id -> files known completed (persisted separately: a resume entry
    # is REMOVED when its file lands, so the entry set alone cannot tell how
    # many files of the folder were already saved before an interruption)
    FOLDER_DONE_KEY = "folder_download_done_v1"

    def __init__(self, store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._entries: Dict[str, dict] = self._load()
        self._folder_done: Dict[str, int] = self._load_folder_done()

    # ------------------------------------------------------------- load/save

    def _load(self) -> Dict[str, dict]:
        try:
            raw = self._store.get_secret(self.SECRET_KEY, "")
        except Exception:
            raw = ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        entries: Dict[str, dict] = {}
        for file_id, entry in data.items():
            if isinstance(file_id, str) and isinstance(entry, dict):
                entries[file_id] = entry
        return entries

    def _save_locked(self) -> None:
        if len(self._entries) > MAX_RESUME_ENTRIES:
            ordered = sorted(
                self._entries.items(),
                key=lambda kv: float(kv[1].get("updated") or 0),
                reverse=True,
            )
            self._entries = dict(ordered[:MAX_RESUME_ENTRIES])
        try:
            self._store.set_secret(
                self.SECRET_KEY,
                json.dumps(self._entries, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            # resume state is an optimization: never let a settings write
            # failure break an in-flight download
            pass

    # ---------------------------------------------------------------- lookup

    def get(self, file_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._entries.get(file_id)
            return dict(entry) if entry is not None else None

    def all(self) -> Dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._entries.items()}

    def folder_entries(self, folder_id: str) -> List[dict]:
        if not folder_id:
            return []
        with self._lock:
            return [
                dict(v)
                for v in self._entries.values()
                if v.get("folderId") == folder_id
            ]

    # ---------------------------------------------------------------- update

    def put(self, file_id: str, **fields) -> None:
        if not file_id:
            return
        with self._lock:
            entry = self._entries.get(file_id) or {}
            entry.update(fields)
            entry["updated"] = time.time()
            self._entries[file_id] = entry
            self._save_locked()

    def remove(self, file_id: str) -> None:
        with self._lock:
            if self._entries.pop(file_id, None) is not None:
                self._save_locked()

    def drop_missing_parts(self) -> None:
        """Forget entries whose staging file no longer exists (user cleaned it
        up or the save dialog pointed somewhere that was removed)."""
        with self._lock:
            stale = [
                file_id
                for file_id, entry in self._entries.items()
                if not entry.get("target") or not os.path.exists(
                    str(entry.get("target")) + ".part"
                )
            ]
            for file_id in stale:
                del self._entries[file_id]
            if stale:
                self._save_locked()

    # ------------------------------------------------- folder done counters

    def _load_folder_done(self) -> Dict[str, int]:
        try:
            raw = self._store.get_secret(self.FOLDER_DONE_KEY, "")
        except Exception:
            raw = ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(k): int(v)
            for k, v in data.items()
            if isinstance(v, int) and v >= 0
        }

    def _save_folder_done_locked(self) -> None:
        try:
            self._store.set_secret(
                self.FOLDER_DONE_KEY,
                json.dumps(self._folder_done, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            pass

    def folder_done_count(self, folder_id: str) -> int:
        if not folder_id:
            return 0
        with self._lock:
            return int(self._folder_done.get(folder_id, 0))

    def bump_folder_done(self, folder_id: str, n: int = 1) -> None:
        """Count [n] more files of [folder_id] as saved (persists)."""
        if not folder_id:
            return
        with self._lock:
            self._folder_done[folder_id] = int(self._folder_done.get(folder_id, 0)) + n
            self._save_folder_done_locked()

    def reset_folder_done(self, folder_id: str) -> None:
        """A fresh (non-resume) folder run restarts the completed count."""
        if not folder_id:
            return
        with self._lock:
            if self._folder_done.pop(folder_id, None) is not None:
                self._save_folder_done_locked()
