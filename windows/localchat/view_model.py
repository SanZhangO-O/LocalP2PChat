import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .call import CallManager
from .crypto import KEY_LEN, random_bytes, random_password, to_b64
from .download_state import FileResumeStore
from .group_call import GroupCallManager
from .hardware import get_hardware_id, get_local_ip_address
from .models import (
    FILE_KIND_AUDIO,
    FILE_KIND_IMAGE,
    MAX_FOLDER_FILES,
    MEDIA_AUDIO,
    MEDIA_VIDEO,
    TCP_PORT,
    ChatMessage,
    ContactRequest,
    FileInfo,
    ForwardedInfo,
    GroupFileInfo,
    GroupInfo,
    NetworkPacket,
    Peer,
    detect_media_kind,
    sanitize_file_name,
    sanitize_relative_path,
)
from . import network as network_module
from . import groupauth
from .network import DirectChatListener, DirectChatManager, P2PListener, P2PManager, Protocol
from .punch import DEFAULT_SIGNALING_PORT, parse_server_endpoint
from .qrshare import (
    ContactInvite,
    GroupInvite,
    QrPayloadError,
    encode_contact_invite,
    encode_group_invite,
    parse_invite,
)
from .securewire import DeviceIdentity
from .storage import (
    ChatStore,
    SavedCallLog,
    SavedGroup,
    SavedGroupFile,
    to_saved_message,
)

logger = logging.getLogger(__name__)

# Display-name cap for nicknames, group names and contact remarks — Android
# parity (20 chars). Enforced by truncation here plus QLineEdit.maxLength in
# the UI; truncation (not rejection) because persisted names may predate the
# cap and must keep working.
MAX_NAME_LENGTH = 20


@dataclass
class GroupMeta:
    group_id: str
    group_name: str
    is_host: bool
    host_ip: str = ""
    host_port: int = 0
    my_name: str = ""
    member_count: int = 1
    last_message: str = ""
    last_message_time: int = 0
    unread_count: int = 0
    connected: bool = False
    # The group owner's published announcement (group_update), shown as a
    # banner in the group lobby and persisted so it survives restarts.
    announcement: str = ""


class ChatViewModel(QObject, P2PListener, DirectChatListener):
    groups_changed = pyqtSignal()
    active_group_changed = pyqtSignal()
    active_peers_changed = pyqtSignal()
    active_messages_changed = pyqtSignal()
    active_server_error_changed = pyqtSignal()
    active_connection_lost_changed = pyqtSignal()
    query_state_changed = pyqtSignal()
    join_ui_state_changed = pyqtSignal()
    rejoin_state_changed = pyqtSignal()
    join_successful = pyqtSignal()
    create_failed = pyqtSignal(str)
    status_message = pyqtSignal(str)
    # (file_id, ok, message) after a download finishes (emitted on the main
    # thread; the download itself runs on a worker thread).
    file_download_finished = pyqtSignal(str, bool, str)
    # An own sent image finished copying into the media dir: the chat pages
    # re-layout so the sender's own bubble flips to the inline render.
    media_ready = pyqtSignal()
    # (file_id, received, total) while a download runs, throttled to at most
    # one emission per 250ms or 64KB (whichever comes first).
    file_progress = pyqtSignal(str, int, int)
    # Folder transfer: (folder_id, completed_entries, total_entries) as a
    # folder's files are saved one by one, and (folder_id, ok, message) when
    # the whole folder download finishes (emitted on the main thread).
    folder_progress = pyqtSignal(str, int, int)
    folder_download_finished = pyqtSignal(str, bool, str)
    # An async folder offer (send_folder / send_direct_folder) finished: ok is
    # False when nothing could be sent (not connected / unreadable folder).
    # The walk + per-entry offers run on a worker thread so a large folder
    # never freezes the UI; this is emitted on the main thread.
    folder_send_finished = pyqtSignal(bool)
    # The folder had more sendable files than MAX_FOLDER_FILES and the offer
    # was truncated; carries the folder display name.
    folder_send_truncated = pyqtSignal(str)
    # Emitted from network threads with (group_id | "direct:<peer_id>",
    # sender_name, body); the aggregator slot runs on the main thread (queued
    # connection).
    raw_tray = pyqtSignal(str, str, str)
    # Aggregated (group_id, title, body); emitted on the main thread.
    tray_notification = pyqtSignal(str, str, str)
    # Direct member chats (queued from the direct-chat worker threads).
    direct_contacts_signal = pyqtSignal()
    direct_messages_signal = pyqtSignal(str)
    # The contact-request message box changed (parked / accepted / ignored).
    direct_requests_signal = pyqtSignal()
    # A direct session forwarded call signaling (NetworkPacket) or closed
    # (peer_id). Emitted from network threads; the slots run on the main
    # thread (queued connection) and route into the CallManager.
    direct_call_signal = pyqtSignal(object)
    direct_session_closed = pyqtSignal(str)
    # A direct chat's key moved from an "ip:..." placeholder id to the member's
    # real device id (revealed by a handshake): the UI re-keys the open chat.
    direct_chat_migrated = pyqtSignal(str, str)
    # A peer's typing indicator changed (direct chat) / a group member's did
    # (group chat). Emitted from network threads; the slots run on the main
    # thread (queued connection) and update the typing state + page labels.
    direct_typing_signal = pyqtSignal(str, bool)
    group_typing_signal = pyqtSignal(str, str, bool)
    # The typing indicator shown by a page changed (appeared/refreshed/expired):
    # the chat pages re-render their label.
    typing_state_changed = pyqtSignal()
    # A conversation's local call log changed (one finished call recorded):
    # carries the peer id so an open direct chat re-renders its system rows.
    call_logs_changed = pyqtSignal(str)
    # A conversation's message-experience state changed (a reaction / pin /
    # group read receipt was applied or removed): carries the conversation
    # key (group id or "direct:<peer_id>") so the open page re-renders.
    extras_changed = pyqtSignal(str)
    # A group's shared-file index (群文件) changed (a file was shared, removed
    # or convergence applied): carries the group id so the share page reloads.
    group_files_changed = pyqtSignal(str)

    # Burst window in ms: incoming notifications within this span are merged
    # into a single tray bubble instead of one popup per message.
    TRAY_AGGREGATE_MS = 2000

    # Download progress throttling: emit file_progress when EITHER 250ms
    # elapsed since the last emission OR 64KB of new bytes arrived.
    FILE_PROGRESS_MIN_INTERVAL = 0.25
    FILE_PROGRESS_MIN_DELTA = 64 * 1024

    # Typing indicator (Android parity): while the local user keeps typing,
    # refresh at most once per TYPING_ACTIVE_INTERVAL; TYPING_STOP_DELAY of
    # silence sends active=false. The receiver expires an indicator that saw
    # no refresh for TYPING_TIMEOUT (a path can die mid-typing).
    TYPING_ACTIVE_INTERVAL = 2.0
    TYPING_STOP_DELAY = 4.0
    TYPING_TIMEOUT = 6.0

    def __init__(self, store: ChatStore, data_dir: str = "."):
        super().__init__()
        self.store = store
        self.data_dir = data_dir
        self._lock = threading.RLock()
        # set by shutdown(): afterwards every main-thread slot becomes a
        # no-op, so a queued signal delivered by a LATER event pump (a
        # subsequent test, a stale singleton callback) can never re-arm this
        # ViewModel's timers or touch the closed store
        self._shutdown_done = False
        # Persisted resume state for interrupted downloads (file_id -> staging
        # path / received bytes / the offer's address+key). Kept in the
        # encrypted settings blob so a paused download can continue after an
        # app restart (see download_state.py).
        self._resume_store = FileResumeStore(store)
        self._resume_store.drop_missing_parts()

        # Long-term device identity for direct chats and call media (loaded
        # once; the private key never leaves this machine's data dir).
        DeviceIdentity.ensure_loaded(data_dir)

        self.groups: List[GroupMeta] = []
        self.group_p2p_map: Dict[str, P2PManager] = {}
        self.active_group_id: Optional[str] = None
        self.active_group_name: str = ""
        self.active_is_host: bool = False
        self.active_my_name: str = ""
        self.active_group_password: str = ""

        self.removed_group_ids: set = set()
        self.persisted_message_ids: Dict[str, set] = {}
        self.persisted_peer_counts: Dict[str, int] = {}
        self.persisted_my_names: Dict[str, str] = {}
        # Per group: the newest message id a group read receipt was sent for
        # (dedup — only send again when a newer message arrives).
        self._group_receipt_sent: Dict[str, str] = {}
        # Per group: the P2PManager whose saved-history replay finished (or,
        # for a fresh host group, the one that had nothing to replay). Only
        # THAT instance may mirror deletes into the database: a stale
        # connection's in-flight callback carries a message list where the
        # rows the fresh instance has not loaded yet look deleted (Android
        # parity: replayDone[groupId] = p2p). An entry is absent while no
        # replay has completed for the current instance, which also blocks
        # mirroring mid-replay.
        self.replay_done: Dict[str, P2PManager] = {}

        self.setup_p2p: Optional[P2PManager] = None
        self.pending_p2p: Optional[P2PManager] = None
        self.pending_host_ip: str = ""
        self.pending_host_port: Optional[int] = None
        self.pending_group_id: Optional[str] = None
        self.rejoin_in_progress: bool = False
        self.rejoin_failed: bool = False

        self.window_active: bool = True
        self.nickname: str = self.store.get_setting("nickname", "")

        # The whole program uses ONE port (default 9999, configurable in the
        # settings dialog). A single shared host server listens on it and
        # serves every host group (see network.HostGroupServer).
        try:
            self.port: int = int(
                self.store.get_setting("port", "") or network_module.TCP_PORT
            )
        except (TypeError, ValueError):
            # a corrupted / hand-edited port setting must not crash startup:
            # fall back to the default instead
            self.port = network_module.TCP_PORT
        self.host_server = network_module.HostGroupServer(self.port)

        # Video/audio call engine (created on the GUI thread; it owns the
        # QAudioSource/QAudioSink and all call state).
        self.call_manager = CallManager(self)
        # Every finished call is recorded to the local call log (per 1:1
        # conversation with the other participant); missed incoming calls also
        # surface as a system row in that conversation (Android parity).
        self.call_manager.call_finished.connect(self._on_call_finished)
        # Group voice conference engine (audio only, star topology: the
        # initiator hosts and mixes). Kept separate from CallManager so the
        # 1:1 call path is untouched; the two are mutually exclusive because
        # they share the microphone (busy_check / the start_call guards).
        self.group_call = GroupCallManager(self)
        self.group_call.busy_check = lambda: self.call_manager.state != "idle"

        # Direct member chats: members are first-class — the shared listener
        # must be reachable even with no host group, and the identity must
        # match the group identity so contacts unify.
        self.direct = network_module.DirectChatManager()
        self.direct.attach(self)
        self.host_server.direct_manager = self.direct
        self.host_server.ensure_running()
        # re-serve group files this device uploaded before a restart
        self._restore_shared_files()
        self._direct_persisted_ids: Dict[str, set] = {}
        self._direct_pending: Dict[str, Dict[str, bool]] = {}
        # Own direct messages' persisted read state (peer read_receipts):
        # message id -> read. Mirrors the _direct_pending bookkeeping.
        self._direct_read: Dict[str, Dict[str, bool]] = {}
        self._direct_last: Dict[str, Optional[ChatMessage]] = {}
        # Running file downloads: (group_id|peer_id, message_id) ->
        # (cancel Event, [download socket]). cancel_download sets the event
        # and shuts the socket down so a blocked read aborts immediately.
        self._download_registry: Dict[tuple, tuple] = {}

        # Typing indicators (main thread only): peer id -> monotonic deadline
        # of the received indicator, group id -> {sender id: (name, deadline)},
        # and the outbound "stop typing" timers per conversation scope.
        self._direct_typing: Dict[str, float] = {}
        self._group_typing: Dict[str, Dict[str, tuple]] = {}
        self._typing_out: Dict[str, dict] = {}
        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(1000)
        self._typing_timer.timeout.connect(self._on_typing_tick)

        # Group mesh: member-to-member links so chat survives the host going
        # offline, plus history backfill on connect.
        self.mesh = network_module.GroupMeshManager()
        self.mesh.attach(self)
        # history pushes carry the group's delete tombstones so members that
        # were offline during a delete converge instead of resurrecting it
        self.mesh.deleted_ids_provider = self.store.get_deleted_ids
        # group file share area (群文件): history pushes carry the group's
        # share index and removal tombstones so an offline member converges
        self.mesh.group_files_provider = self.store.get_group_files
        self.mesh.removed_file_ids_provider = self.store.get_removed_group_file_ids
        # owner management packets (group_update / kick_member) are only
        # accepted over a mesh link when senderId is the group's creator
        self.mesh.creator_id_provider = lambda gid: self.store.get_setting(
            f"group_creator_id_{gid}", ""
        )
        self.host_server.mesh_manager = self.mesh
        # Resolve the group password for an incoming handshake on the shared
        # listener: host groups by numeric join id, member groups (join
        # sponsors) by their saved password, and mesh groups by their internal
        # id (Android parity — only members who know the password can link and
        # read history over the mesh).
        self.host_server.password_lookup = self._password_lookup
        # Any member can be the join entry point: query/join packets targeting
        # a group we belong to as a MEMBER are answered here, so newcomers
        # only need the IP of SOME member, not the creator's.
        self.host_server.member_group_handler = self._handle_member_group_request
        # Cross-NAT joins (optional): when a signaling/relay server is
        # configured, hosted groups are announced there and members on other
        # NAT segments can join by the numeric id alone (punch or relay).
        self._signaling_server: str = ""
        # Server access secret (challenge-response HMAC; stored encrypted).
        self._signaling_secret: str = self.store.get_secret("signaling_secret", "")
        self._apply_signaling_setting(
            self.store.get_setting("signaling_server", "") or "", persist=False
        )
        saved_contacts = self._load_direct_contacts()
        # honor contact removals from previous processes BEFORE announcing:
        # a peer that keeps presenting itself must not resurrect a contact
        # the user deleted (marks carry id + endpoint + removal time)
        removed_ids, removed_endpoints = self._load_direct_removed_marks()
        self.direct.restore_removed_marks(removed_ids, removed_endpoints)
        # unanswered contact requests survive a restart too: they are the
        # user's pending decisions, not transient state (Android parity)
        self.direct.restore_contact_requests(
            self._load_direct_contact_requests()
        )
        self.direct.configure(
            self._device_id(),
            self.nickname or "用户",
            get_local_ip_address(),
            self.port,
            saved_contacts,
        )
        # Direct calls ride the session socket, not the host relay (Android
        # parity): forward call packets and session-closed events into the
        # CallManager with the direct session as the signaling channel.
        self.direct.on_call_signal = self._emit_direct_call_signal
        self.direct.on_session_closed = self._emit_direct_session_closed
        # A session established by the OTHER side must be persisted too:
        # without this, messages received in a chat the local user never
        # opened exist only in memory and vanish when the process dies.
        self.direct.on_session_established = self._seed_direct_history
        # A handshake revealed a placeholder "ip:..." contact's real device
        # id: move that chat's observer, persisted rows and open screen over.
        self.direct.on_chat_migrated = self._on_direct_chat_migrated
        # Removal marks changed (contact removed / re-added): persist them so
        # a restart keeps honoring the removals (ChatStore is thread-safe).
        self.direct.removed_marks_changed = self._save_direct_removed_marks
        # The request box changed (parked / accepted / ignored): persist it
        # and let the member page re-render (queued to the main thread).
        self.direct.contact_requests_changed = self._direct_contact_requests_changed
        # Surface transient direct-chat events (connected, offline notices,
        # security warnings) as toasts.
        self.direct.on_event = lambda text: self.status_message.emit(text)
        self.direct_contacts_signal.connect(self._on_direct_contacts_changed)
        self.direct_messages_signal.connect(self._on_direct_messages_changed)
        self.direct_call_signal.connect(self._on_direct_call_signal)
        self.direct_session_closed.connect(self._on_direct_session_closed)
        self.direct_chat_migrated.connect(self._on_direct_chat_migrated_slot)
        self.direct_typing_signal.connect(self._on_direct_typing)
        self.group_typing_signal.connect(self._on_group_typing)

        # Tray-notification aggregation state (main thread only).
        self._tray_timer = QTimer(self)
        self._tray_timer.setSingleShot(True)
        self._tray_timer.timeout.connect(self._flush_tray)
        self._tray_accum: Optional[dict] = None
        self.raw_tray.connect(self._on_raw_tray)

        self._load_persisted_groups()
        self._restore_direct_summaries()

    def _device_id(self) -> str:
        """Stable per-device identity persisted in settings: a reconnect then
        looks like the same member to the host (message attribution, delete
        rights and the member list all key off the peer id)."""
        device_id = self.store.get_setting("device_id", "")
        if not device_id:
            device_id = str(uuid.uuid4())
            self.store.set_setting("device_id", device_id)
        return device_id

    def _hardware_fingerprint(self) -> str:
        """Stable per-device fingerprint persisted in settings: normally the
        Windows MachineGuid, but persisted once so the fallback (a generated
        value on machines without a MachineGuid) never changes across
        restarts. The numeric group ID is derived from this, so it must be
        stable for members to rejoin."""
        fp = self.store.get_setting("hardware_fingerprint", "")
        if not fp:
            fp = get_hardware_id()
            self.store.set_setting("hardware_fingerprint", fp)
        return fp

    def set_window_active(self, active: bool) -> None:
        was = self.window_active
        self.window_active = active
        if active and not was:
            # returning to the window: sessions may have died (sleep/resume,
            # network change) -- re-announce every dead contact session now
            # instead of waiting for the presence sweep
            self.direct.announce_online()

    @property
    def security_code(self) -> str:
        """Short fingerprint of this device's long-term identity key (安全码):
        compare it with the peer's code out-of-band (e.g. read it aloud) to
        rule out a man-in-the-middle on the first direct chat / call."""
        return DeviceIdentity.fingerprint()

    def parse_qr_invite(self, text: str):
        """Decode a scanned QR payload into a ContactInvite / GroupInvite,
        or None when it is not a valid (current-format) invite."""
        try:
            return parse_invite(text)
        except QrPayloadError:
            return None

    def qr_contact_payload(self) -> str:
        """The contact QR this device shows: name, 安全码 and LAN endpoint.
        The fingerprint in it lets the scanner pin TOFU before dialing."""
        return encode_contact_invite(
            name=self.nickname or "用户",
            fingerprint=self.security_code,
            ip=get_local_ip_address(),
            port=self.port,
        )

    def qr_group_invite_payload(self) -> Optional[str]:
        """The group invite QR for the active (hosted) group: numeric join id
        + host endpoint + configured relay. The group password is NEVER part
        of it — joiners still type the password themselves."""
        if not self.active_is_host or self.active_group_id is None:
            return None
        group_id = self.active_group_numeric_id()
        if not group_id:
            return None
        return encode_group_invite(
            group_id=group_id,
            name=self.active_group_name,
            ip=get_local_ip_address(),
            port=self.port,
            relay=self.signaling_server,
        )

    def _password_lookup(self, mode: str, group_id: Optional[str]) -> Optional[str]:
        """Resolve the group password for an incoming handshake on the shared
        listener (runs on socket threads, so it must not race group
        registration). Returns None when this device knows no such group for
        that mode, "" for a known group without a password."""
        if not group_id:
            return None
        if mode == Protocol.MODE_MESH:
            return self.mesh.password_for(group_id)
        p2p = self.host_server.resolve_group(group_id)
        if p2p is not None:
            return p2p.group_password
        with self._lock:
            candidates = list(self.group_p2p_map.values())
        for candidate in candidates:
            if not candidate.is_host and candidate.join_id == group_id:
                return self.store.get_group_password(candidate.current_group_id)
        return None

    def _on_raw_tray(self, gid: str, sender_name: str, body: str) -> None:
        """Aggregate raw tray notifications arriving within the burst window.

        Runs on the main thread via the queued signal connection, so the
        QTimer is only ever touched from the main thread."""
        if self._shutdown_done:
            return
        if self._tray_accum is not None and self._tray_accum["gid"] != gid:
            # another group arrived mid-window: flush the accumulated bubble
            # first so its count/preview never bleed into the new group's
            self._flush_tray()
        if self._tray_accum is None:
            self._tray_accum = {
                "gid": gid,
                "title": sender_name,
                "body": body,
                "count": 1,
            }
        else:
            self._tray_accum["gid"] = gid
            self._tray_accum["body"] = body
            self._tray_accum["count"] += 1
            self._tray_accum["title"] = (
                f"{sender_name} 等 {self._tray_accum['count']} 条新消息"
            )
        self._tray_timer.start(self.TRAY_AGGREGATE_MS)

    def _flush_tray(self) -> None:
        acc = self._tray_accum
        self._tray_accum = None
        if acc is not None:
            self.tray_notification.emit(acc["gid"], acc["title"], acc["body"])

    @property
    def local_ip(self) -> str:
        return get_local_ip_address()

    @property
    def local_port(self) -> int:
        """The single program-wide port used by every host group."""
        return self.port

    def set_nickname(self, name: str) -> None:
        """Persist the display nickname and apply it to every live session so
        new direct chats, calls and group joins immediately use the new name."""
        nick = name.strip()[:MAX_NAME_LENGTH]
        if not nick:
            return
        self.nickname = nick
        self.store.set_setting("nickname", nick)
        self.direct.configure(
            self._device_id(), nick, get_local_ip_address(), self.port,
            saved_contacts=self.direct.contacts_list(),
        )
        for p2p in self.group_p2p_map.values():
            p2p.my_name = nick
        for p2p in (self.pending_p2p, self.setup_p2p):
            if p2p is not None:
                p2p.my_name = nick
        if self.active_group_id is not None:
            self.active_my_name = nick
        # Keep persisted per-group display names aligned too.
        for meta in self.groups:
            self.store.upsert_group(
                SavedGroup(
                    group_id=meta.group_id,
                    group_name=meta.group_name,
                    is_host=meta.is_host,
                    host_ip=meta.host_ip,
                    host_port=meta.host_port,
                    my_name=nick,
                    member_count=meta.member_count,
                    last_message=meta.last_message,
                    last_message_time=meta.last_message_time,
                )
            )

    def can_create_group(self) -> bool:
        # multiple groups are supported; creating is always allowed
        return True

    def set_port(self, port: int) -> None:
        """Change the program-wide port and rebind the shared host server.
        Existing member connections keep working; new joins use the new port."""
        if port < 1 or port > 65535:
            self.status_message.emit("端口必须在 1-65535 之间")
            return
        self.port = port
        self.store.set_setting("port", str(port))
        # Every live/queued group manager advertises the local listening port;
        # update all of them so join_ack/mesh advertisements never carry the
        # stale pre-change port.
        for p2p in self.group_p2p_map.values():
            p2p.port = port
        for p2p in (self.pending_p2p, self.setup_p2p):
            if p2p is not None:
                p2p.port = port
        for gid, p2p in self.group_p2p_map.items():
            if not p2p.is_host:
                self.mesh.update_local_port(gid, port)
        # Persist the new host port immediately (otherwise a restart reverts
        # host groups to the old value loaded from the database).
        for meta in self.groups:
            if meta.is_host:
                meta.host_port = port
                self.store.upsert_group(
                    SavedGroup(
                        group_id=meta.group_id,
                        group_name=meta.group_name,
                        is_host=True,
                        host_ip=meta.host_ip or get_local_ip_address(),
                        host_port=port,
                        my_name=meta.my_name,
                        member_count=meta.member_count,
                        last_message=meta.last_message,
                        last_message_time=meta.last_message_time,
                    )
                )
        # rebind for host groups AND direct member chats (every device listens)
        self.host_server.restart(port)
        self.direct.configure(
            self._device_id(), self.nickname or "用户", get_local_ip_address(), port,
            saved_contacts=self.direct.contacts_list(),
        )
        self.groups_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()
        self.status_message.emit(f"本机端口已改为 {port}")

    # ------------------------------------------------------ direct member chat

    # Full-width punctuation / digits a Chinese IME produces (：．。０-９...):
    # typed addresses must be normalized to their ASCII equivalents or the
    # contact is saved with an endpoint that can never connect (the row looks
    # fine in the member list, then silently never reaches the peer).
    _FULLWIDTH_MAP = str.maketrans(
        {
            "：": ":",
            "．": ".",
            "。": ".",
            "，": ",",
            "　": " ",
            "０": "0",
            "１": "1",
            "２": "2",
            "３": "3",
            "４": "4",
            "５": "5",
            "６": "6",
            "７": "7",
            "８": "8",
            "９": "9",
        }
    )

    # hostname label: alnum, inner hyphens, not starting/ending with '-'
    _HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")

    def _load_direct_contacts(self) -> list:
        raw = self.store.get_setting("direct_contacts", "")
        if not raw:
            return []
        try:
            return [Peer.from_dict(d) for d in json.loads(raw)]
        except Exception:
            return []

    def _save_direct_contacts(self) -> None:
        try:
            self.store.set_setting(
                "direct_contacts",
                json.dumps([c.to_dict() for c in self.direct.contacts_list()]),
            )
        except Exception:
            pass

    def _load_direct_removed_marks(self) -> tuple:
        """Removed-contact marks persisted by a previous process: (ids,
        endpoints) -> removal time, so a peer that keeps announcing cannot
        resurrect a contact the user deleted."""
        raw = self.store.get_setting("direct_removed_marks", "")
        if not raw:
            return {}, {}
        try:
            data = json.loads(raw)
            return dict(data.get("ids", {})), dict(data.get("endpoints", {}))
        except Exception:
            return {}, {}

    def _save_direct_removed_marks(self) -> None:
        try:
            ids, endpoints = self.direct.removed_marks()
            self.store.set_setting(
                "direct_removed_marks",
                json.dumps({"ids": ids, "endpoints": endpoints}),
            )
        except Exception:
            pass

    def _load_direct_contact_requests(self) -> list:
        """Contact-request box persisted by a previous process: unanswered
        requests must still be answerable after a restart."""
        raw = self.store.get_setting("direct_contact_requests", "")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [ContactRequest.from_dict(d) for d in data]
        except Exception:
            return []

    def _direct_contact_requests_changed(self) -> None:
        try:
            self.store.set_setting(
                "direct_contact_requests",
                json.dumps(
                    [r.to_dict() for r in self.direct.contact_requests()]
                ),
            )
        except Exception:
            pass
        self.direct_requests_signal.emit()

    def direct_requests_list(self) -> list:
        return self.direct.contact_requests()

    def accept_contact_request(self, request_id: str) -> None:
        self.direct.accept_contact_request(request_id)

    def ignore_contact_request(self, request_id: str) -> None:
        self.direct.ignore_contact_request(request_id)

    def direct_contacts_list(self) -> list:
        return self.direct.contacts_list()

    def direct_messages(self, peer_id: str) -> list:
        return self.direct.messages_for(peer_id)

    def direct_chat_alive(self, peer_id: str) -> bool:
        return self.direct.is_chat_alive(peer_id)

    # ---------------------------------------------------- typing indicators

    def direct_peer_typing(self, peer_id: str) -> bool:
        """True while the peer's typing indicator is live (direct chat)."""
        with self._lock:
            deadline = self._direct_typing.get(peer_id)
        return deadline is not None and time.monotonic() < deadline

    def group_typing_names(self, group_id: str) -> list:
        """Display names of members currently typing in [group_id]."""
        with self._lock:
            entries = dict(self._group_typing.get(group_id, {}))
        now = time.monotonic()
        return [name for name, deadline in entries.values() if now < deadline]

    def notify_direct_typing(self, peer_id: str) -> None:
        """The local user typed in the direct chat: refresh our indicator
        (throttled) and arm the stop timer. Call on every input change."""
        if peer_id:
            self._typing_activity(
                "direct:" + peer_id,
                lambda active, pid=peer_id: self.direct.send_typing(pid, active),
            )

    def notify_group_typing(self) -> None:
        """The local user typed in the active group: refresh our indicator."""
        gid = self.active_group_id
        if gid:
            self._typing_activity(
                gid, lambda active, g=gid: self._send_group_typing(g, active)
            )

    def end_direct_typing(self, peer_id: str) -> None:
        """The user left the direct chat: our indicator is stale now."""
        if peer_id:
            self._end_active_typing("direct:" + peer_id)

    def _send_group_typing(self, group_id: str, active: bool) -> None:
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.send_typing(active)
        # host-offline path: the mesh carries the indicator too
        self.mesh.broadcast_typing(group_id, self.direct.my_id_value, active)

    def _typing_activity(self, scope_key: str, send) -> None:
        """Throttle an outgoing typing indicator: active=true at most once per
        TYPING_ACTIVE_INTERVAL, and active=false after TYPING_STOP_DELAY with
        no further activity. [send] receives the boolean active flag (kept in
        the state so the delayed stop reaches the same channel)."""
        if self._shutdown_done:
            return
        state = self._typing_out.get(scope_key)
        if state is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            # zero-parameter closure: a default-arg lambda connected to a
            # signal is arity-fragile across PyQt's slot introspection (the
            # "missing 1 required positional argument: 'key'" phantom)
            timer.timeout.connect(lambda: self._stop_typing(scope_key))
            state = {"last": 0.0, "timer": timer, "send": send}
            self._typing_out[scope_key] = state
        else:
            state["send"] = send
        now = time.monotonic()
        if now - state["last"] >= self.TYPING_ACTIVE_INTERVAL:
            state["last"] = now
            send(True)
        state["timer"].start(int(self.TYPING_STOP_DELAY * 1000))

    def _stop_typing(self, scope_key: str) -> None:
        state = self._typing_out.pop(scope_key, None)
        if state is None:
            return
        state["send"](False)

    def _end_active_typing(self, scope_key: str) -> None:
        """Our message just went out (or the chat closed): the indicator is
        stale immediately, no need to wait for the stop delay."""
        if scope_key in self._typing_out:
            self._stop_typing(scope_key)

    def _on_direct_typing(self, peer_id: str, active: bool) -> None:
        """Main-thread slot: a peer's typing indicator changed."""
        with self._lock:
            if active:
                self._direct_typing[peer_id] = (
                    time.monotonic() + self.TYPING_TIMEOUT
                )
            else:
                self._direct_typing.pop(peer_id, None)
        self._ensure_typing_timer()
        self.typing_state_changed.emit()

    def _on_group_typing(self, group_id: str, sender_id: str, active: bool) -> None:
        """Main-thread slot: a group member's typing indicator changed."""
        if not group_id or sender_id == self.direct.my_id_value:
            return
        with self._lock:
            entries = self._group_typing.setdefault(group_id, {})
            if active:
                name = sender_id
                p2p = self.group_p2p_map.get(group_id)
                if p2p is not None:
                    peer = p2p.peers.get(sender_id)
                    if peer is not None and peer.name:
                        name = peer.name
                entries[sender_id] = (name, time.monotonic() + self.TYPING_TIMEOUT)
            else:
                entries.pop(sender_id, None)
                if not entries:
                    self._group_typing.pop(group_id, None)
        self._ensure_typing_timer()
        self.typing_state_changed.emit()

    def _ensure_typing_timer(self) -> None:
        """Run the 1s expiry tick only while some indicator is live."""
        if self._shutdown_done:
            return
        with self._lock:
            has_any = bool(self._direct_typing) or bool(self._group_typing)
        if has_any and not self._typing_timer.isActive():
            self._typing_timer.start()
        elif not has_any and self._typing_timer.isActive():
            self._typing_timer.stop()

    def _on_typing_tick(self) -> None:
        now = time.monotonic()
        changed = False
        with self._lock:
            for peer_id in [p for p, d in self._direct_typing.items() if now >= d]:
                self._direct_typing.pop(peer_id, None)
                changed = True
            for gid in list(self._group_typing.keys()):
                entries = self._group_typing[gid]
                for sender_id in [
                    s for s, (_, deadline) in entries.items() if now >= deadline
                ]:
                    entries.pop(sender_id, None)
                    changed = True
                if not entries:
                    self._group_typing.pop(gid, None)
        if changed:
            self.typing_state_changed.emit()
        self._ensure_typing_timer()

    def direct_last_message(self, peer_id: str) -> Optional[ChatMessage]:
        """Last message of a direct chat, for the home-page preview."""
        return self._direct_last.get(peer_id)

    def search_history(
        self, keyword: str, scope_group_id: Optional[str] = None
    ) -> List[dict]:
        """Keyword search over group + direct-chat history (newest match
        first, see ChatStore.search_messages). Resolves each hit's
        conversation display name so the search page can list
        会话名 + 发送者 + 摘要 + 时间 without touching the network.

        Each result dict carries: message (SavedMessage), conversation_id
        (group id or "direct:<peerId>"), conversation (display name) and
        is_direct."""
        hits = self.store.search_messages(keyword, scope_group_id)
        contacts = {c.id: c for c in self.direct_contacts_list()}
        groups = {g.group_id: g for g in self.groups}
        results = []
        for m in hits:
            gid = m.group_id
            is_direct = gid.startswith("direct:")
            if is_direct:
                peer_id = gid[len("direct:"):]
                contact = contacts.get(peer_id)
                name = contact.name if contact is not None else peer_id
            else:
                meta = groups.get(gid)
                if meta is not None:
                    name = meta.group_name
                else:
                    saved = self.store.get_group(gid)
                    name = saved.group_name if saved is not None else gid
            results.append(
                {
                    "message": m,
                    "conversation_id": gid,
                    "conversation": name,
                    "is_direct": is_direct,
                }
            )
        return results

    def search_scopes(self) -> List[dict]:
        """Scope entries for the search page's range selector: every known
        conversation (groups first, then direct contacts), each as
        {"id": <group_id or "direct:<peerId>">, "name": <display>}.
        The implicit first entry 全部 is prepended by the UI."""
        scopes = [
            {"id": g.group_id, "name": f"群聊：{g.group_name}"} for g in self.groups
        ]
        seen = set()
        for c in self.direct_contacts_list():
            if c.id in seen:
                continue
            seen.add(c.id)
            scopes.append({"id": f"direct:{c.id}", "name": f"成员：{c.name}"})
        return scopes

    def open_direct_chat(self, contact: Peer) -> Optional[str]:
        """Open a 1:1 chat with a member WITHOUT requiring the peer to be
        online: persisted history loads immediately, messages sent while
        offline queue as pending, and a background dial keeps trying to
        connect (flushes the outbox when it succeeds). Returns the chat key
        (the contact's id; if a handshake later reveals a different real
        device id, direct_chat_migrated re-keys the open page)."""
        self.direct.open_chat(contact)
        self._seed_direct_history(contact.id)
        if not self.direct.is_chat_alive(contact.id):
            # best-effort connect in the background; failure surfaces as a
            # toast but the chat (with its history) stays open and usable
            def run():
                self.direct.start_chat(contact, quiet=False)

            threading.Thread(target=run, daemon=True).start()
        return contact.id

    def _seed_direct_history(self, peer_id: str) -> None:
        saved = self.store.get_messages_for_group("direct:" + peer_id)
        msgs = [
            ChatMessage(
                id=m.id,
                content=m.content,
                timestamp=m.timestamp,
                sender_id=m.sender_id,
                sender_name=m.sender_name,
                is_from_me=m.is_from_me,
                file_info=self._restored_file_info(m),
                pending=m.pending,
                reply_to=m.reply_to or None,
                reply_preview=m.reply_preview or None,
                reply_sender=m.reply_sender or None,
                read=m.read,
                edited=m.edited,
                mentions=json.loads(m.mentions) if m.mentions else None,
            )
            for m in saved
        ]
        self.direct.seed_messages(peer_id, msgs)
        self._direct_persisted_ids[peer_id] = {m.id for m in saved}
        self._direct_read[peer_id] = {m.id: m.read for m in saved}
        self._direct_last[peer_id] = msgs[-1] if msgs else None

    @staticmethod
    def _restored_file_info(m) -> Optional[FileInfo]:
        """Rebuild a FileInfo from a persisted row. Every restored offer is
        shown as expired: the download address was captured in a previous
        session, and the sender's download server dies with its process — once
        EITHER side restarted, a stale address only produces a failed
        download. The bubble renders it as "已过期" instead of offering a
        download that can no longer succeed (Android parity). The kind is
        preserved so image/video messages keep rendering inline (from their
        local media copy) across restarts."""
        if m.file_size <= 0 and not m.download_host:
            return None
        # blank the address for every restored offer, own or received
        return FileInfo(
            m.id,
            m.content,
            m.file_size,
            "",
            0,
            kind=m.kind,
            folder_id=m.folder_id,
            folder_name=m.folder_name,
            relative_path=m.relative_path,
            folder_total=m.folder_total,
        )

    def send_direct_message(
        self,
        peer_id: str,
        content: str,
        reply_to: Optional[str] = None,
        reply_preview: Optional[str] = None,
        reply_sender: Optional[str] = None,
        forwarded: Optional[ForwardedInfo] = None,
    ) -> bool:
        # peers are first-class: a message may queue as pending while the peer
        # is offline and deliver automatically once it comes online (Android
        # parity). False only when there is no contact and no session.
        sent = self.direct.send_message(
            peer_id,
            content,
            reply_to=reply_to,
            reply_preview=reply_preview,
            reply_sender=reply_sender,
            forwarded=forwarded,
        )
        if sent:
            # the message supersedes any "typing" we were showing
            self._end_active_typing("direct:" + peer_id)
        return sent

    def delete_direct_message(self, peer_id: str, message_id: str, sender_id: str) -> None:
        self.direct.delete_message(peer_id, message_id, sender_id)

    def add_direct_contact(
        self, ip_port: str, name: str, expected_fingerprint: str = ""
    ) -> bool:
        ip, parsed_port = self._parse_host_port(ip_port)
        # validate: a syntactically broken endpoint (mangled IP, bad port)
        # used to be accepted silently — the member row then appeared in the
        # list but could never connect, which looks like "adding by IP has
        # no effect". Reject it here; the dialog surfaces 地址无效.
        if not ip or not self._is_valid_host(ip) or not 1 <= parsed_port <= 65535:
            return False
        if expected_fingerprint:
            # scanned QR: pin the 安全码 the QR declared so the first
            # handshake with this endpoint must present exactly that key
            self.direct.set_qr_expected_fingerprint(
                ip, parsed_port, expected_fingerprint
            )
        contact = Peer(
            id=f"ip:{ip}:{parsed_port}",
            name=name.strip()[:MAX_NAME_LENGTH] or ip,
            ip_address=ip,
            port=parsed_port,
        )
        self.direct.add_contact(contact)
        # add_contact keeps the REAL-id contact when this endpoint is already
        # known (a manual placeholder must not clobber it): dial the stored
        # contact, not the placeholder, so the session keys under the real id
        effective = next(
            (
                c
                for c in self.direct.contacts_list()
                if c.ip_address == ip and c.port == parsed_port
            ),
            contact,
        )

        # A manual add is an explicit user action: dial LOUD right away
        # (quiet=False). The presence sweep also picks the new contact up,
        # but it is deliberately silent — without this loud dial, adding an
        # unreachable member gives NO feedback at all (the dialog just
        # closes). Success: "已连接 X" toast; failure: the reason toast
        # (not running / different network / removed on the peer side).
        def run():
            if self.direct.is_chat_alive(effective.id):
                # already connected (re-adding an existing member): the
                # dial short-circuits with no event, so surface it here —
                # without this, the second add silently does nothing and
                # looks like "only the first add works"
                self.status_message.emit(f"已连接 {effective.name}")
                return
            self.direct.start_chat(effective, quiet=False)

        threading.Thread(target=run, daemon=True).start()
        return True

    def remove_direct_contact(self, contact_id: str) -> None:
        # end the session first (Android parity: removeDirectContact closes
        # the chat before dropping the contact)
        self.direct.close_chat(contact_id)
        self.direct.remove_contact(contact_id)
        # a removed member's live typing indicator must not linger
        self._on_direct_typing(contact_id, False)

    # ------------------------------------------ direct calls and file transfer

    def _emit_direct_call_signal(self, packet) -> None:
        """Network-thread bridge for call packets forwarded by a direct
        session; hops to the main thread via the queued signal."""
        self.direct_call_signal.emit(packet)

    def _emit_direct_session_closed(self, peer_id: str) -> None:
        """Network-thread bridge; hops to the main thread via the queued
        signal."""
        self.direct_session_closed.emit(peer_id)

    def _direct_channel(self):
        """Signaling channel backed by the direct session socket."""
        return lambda pid, pkt: self.direct.send_packet(pid, pkt)

    def _on_direct_call_signal(self, packet) -> None:
        """A direct session forwarded call signaling: route it into the
        CallManager with the direct session as the reply channel."""
        caller_ip = ""
        call = packet.call
        if call is not None:
            contact = next(
                (c for c in self.direct.contacts_list() if c.id == call.caller_id), None
            )
            if contact is not None:
                caller_ip = contact.ip_address
        self.call_manager.handle_direct_signal(
            channel_send=self._direct_channel(),
            identity=self.direct,
            packet=packet,
            caller_ip=caller_ip,
            my_id=self.direct.my_id_value,
            my_name=self.direct.my_name_value,
        )

    def _on_direct_session_closed(self, peer_id: str) -> None:
        # a call riding this session cannot continue without signaling; the
        # peer id narrows the match so an unrelated session closing never
        # kills a call on another session
        self.call_manager.end_if_on(self.direct, "连接已断开", peer_id=peer_id)

    def start_direct_call(self, peer_id: str, media: str = MEDIA_VIDEO) -> None:
        """Start a video/audio call with a direct-chat member: signaling rides
        the direct session socket, media over the usual TCP connection.
        [media] is "video" (default) or "audio"."""
        contact = next(
            (c for c in self.direct.contacts_list() if c.id == peer_id), None
        )
        if contact is None:
            self.status_message.emit("无法发起通话：成员不在线")
            return
        if not self.direct.is_chat_alive(peer_id):
            self.status_message.emit("未连接到该成员，无法发起通话")
            return
        if self.group_call.state != "idle":
            self.status_message.emit("语音会议进行中，请先挂断会议")
            return
        self.call_manager.start_direct_call(
            channel_send=self._direct_channel(),
            identity=self.direct,
            peer=contact,
            my_id=self.direct.my_id_value,
            my_name=self.direct.my_name_value,
            media=media,
        )

    # ---------------------------------------------------------- call log
    # Local-only call history (never sent over the wire, never synced): one
    # row per finished call keyed to the 1:1 conversation with the other
    # participant. The UI interleaves these as system-style rows.

    def _on_call_finished(self, info) -> None:
        """CallManager finished a call: persist its local log row."""
        if not isinstance(info, dict):
            return
        peer_id = str(info.get("peer_id") or "")
        if not peer_id:
            return
        try:
            self.store.add_call_log(
                SavedCallLog(
                    id=str(info.get("call_id") or uuid.uuid4()),
                    conversation_key="direct:" + peer_id,
                    peer_id=peer_id,
                    peer_name=str(info.get("peer_name") or ""),
                    direction=str(info.get("direction") or "outgoing"),
                    result=str(info.get("result") or "failed"),
                    media=str(info.get("media") or MEDIA_VIDEO),
                    start_time=int(info.get("started_at") or 0),
                    duration=int(info.get("duration") or 0),
                )
            )
        except Exception:
            return
        # refresh an open direct chat's merged message + call-log flow
        self.call_logs_changed.emit(peer_id)

    def direct_call_logs(self, peer_id: str) -> list:
        """Newest call logs of the 1:1 conversation with [peer_id], oldest
        first (render order)."""
        try:
            return self.store.get_call_logs("direct:" + peer_id)
        except Exception:
            return []

    def send_direct_file(self, peer_id: str, path: str) -> bool:
        """Offer a local file over a direct session (download server + shared
        file_message protocol). Returns False when it cannot be served."""
        msg = self.direct.send_file(peer_id, path)
        if msg is None:
            return False
        self._mirror_own_media(msg, path)
        return True

    def send_direct_folder(self, peer_id: str, path: str) -> None:
        """Offer a folder over a direct session as one file_message per entry
        (each carrying folder_id/folder_name/relative_path/folder_total). Old
        peers still see ordinary file offers; new peers group them into one
        folder card. The walk + offers run on a worker thread so a large
        folder never blocks the caller; folder_send_finished reports the
        outcome."""
        threading.Thread(
            target=self._send_direct_folder_worker, args=(peer_id, path), daemon=True
        ).start()

    def _send_direct_folder_worker(self, peer_id: str, path: str) -> None:
        entries, folder_name, truncated = self._collect_folder_entries(path)
        if not entries:
            self.folder_send_finished.emit(False)
            return
        if truncated:
            self.folder_send_truncated.emit(folder_name)
        folder_id = str(uuid.uuid4())
        sent = 0
        for relative_path, abs_path in entries:
            msg = self.direct.send_file(
                peer_id,
                abs_path,
                folder_id=folder_id,
                folder_name=folder_name,
                relative_path=relative_path,
                folder_total=len(entries),
            )
            if msg is not None:
                sent += 1
        self.folder_send_finished.emit(sent > 0)

    @staticmethod
    def _collect_folder_entries(path: str) -> tuple:
        """Walk [path] and return ([(relative_posix_path, abs_path), ...],
        folder_name, truncated), sorted for a stable order. Entries that cannot
        be sanitized to a safe relative path or that exceed the per-file size
        cap are skipped; the list is capped at MAX_FOLDER_FILES and truncated
        is True when sendable files existed beyond that cap."""
        if not path or not os.path.isdir(path):
            return [], "", False
        folder_name = sanitize_file_name(os.path.basename(os.path.normpath(path)))
        entries = []
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                abs_path = os.path.join(root, name)
                rel = os.path.relpath(abs_path, path).replace(os.sep, "/")
                rel = sanitize_relative_path(rel)
                if not rel:
                    continue
                try:
                    if os.path.getsize(abs_path) > network_module.MAX_DOWNLOAD_BYTES:
                        continue
                except OSError:
                    continue
                if len(entries) >= MAX_FOLDER_FILES:
                    return entries, folder_name, True
                entries.append((rel, abs_path))
        return entries, folder_name, False

    def _mirror_own_media(self, msg: ChatMessage, source_path: str) -> None:
        """After an own IMAGE or VOICE message goes out, copy it into the
        media dir on a worker thread so the sender's own bubble renders/plays
        inline like a received one — and keeps doing so after a restart.
        Videos are NOT copied: they are far larger and the placeholder card
        already opens fine for the sender. Failures are ignored: the copy is
        a rendering convenience, the offer itself was already delivered."""
        fi = msg.file_info
        if fi is None or fi.kind not in (FILE_KIND_IMAGE, FILE_KIND_AUDIO):
            return

        def run():
            try:
                target = self.media_target_path(fi.file_id, fi.file_name)
                os.makedirs(self.media_dir, exist_ok=True)
                if not os.path.isfile(target):
                    # .part + atomic replace so a half-copied image is never
                    # visible to the UI's media path probe
                    tmp = target + ".part"
                    shutil.copyfile(source_path, tmp)
                    os.replace(tmp, target)
                self.media_ready.emit()
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _make_file_progress(self, file_id: str):
        """Throttled progress callback for a download worker thread: forwards
        (received, total) into file_progress at most every 250ms or 64KB —
        whichever comes first — so huge files don't flood the Qt event loop."""
        state = {"t": 0.0, "bytes": 0}

        def report(received: int, total: int) -> None:
            now = time.monotonic()
            with self._lock:
                due = (
                    now - state["t"] >= self.FILE_PROGRESS_MIN_INTERVAL
                    or received - state["bytes"] >= self.FILE_PROGRESS_MIN_DELTA
                )
                if due:
                    state["t"] = now
                    state["bytes"] = received
            if due:
                self.file_progress.emit(file_id, received, total)

        return report

    def _register_download(self, key: tuple) -> tuple:
        """Create the cancel entry for a download: (Event, [socket holder])."""
        entry = (threading.Event(), [])
        with self._lock:
            self._download_registry[key] = entry
        return entry

    def _finish_download(self, key: tuple) -> None:
        with self._lock:
            self._download_registry.pop(key, None)

    # ------------------------------------------------------ download resume

    def resume_info(self, file_id: str) -> Optional[dict]:
        """Persisted resume position for [file_id], or None when there is no
        usable staging file. The chat card renders a paused state from this
        (also after an app restart)."""
        entry = self._resume_store.get(file_id)
        if not entry:
            return None
        target = str(entry.get("target") or "")
        if not target:
            return None
        try:
            received = os.path.getsize(target + ".part")
        except OSError:
            return None
        if received <= 0:
            return None
        total = int(entry.get("total") or 0)
        percent = min(99, int(received * 100 / total)) if total > 0 else 0
        return {
            "target": target,
            "received": received,
            "total": total,
            "percent": percent,
        }

    def folder_resume_info(self, folder_id: str) -> Optional[dict]:
        """Destination and entry counts of a paused folder save, or None. Lets
        the folder card resume without asking for the directory again."""
        entries = self._resume_store.folder_entries(folder_id)
        if not entries:
            return None
        dest = next(
            (str(e.get("destDir")) for e in entries if e.get("destDir")), ""
        )
        if not dest:
            return None
        total = max(int(e.get("folderTotal") or 0) for e in entries) or len(entries)
        # the persisted counter only knows files this app actually saved;
        # never present more than exist
        done = min(total, max(0, self._resume_store.folder_done_count(folder_id)))
        return {"target": dest, "done": done, "total": total}

    def _merged_offer(self, file_info: FileInfo) -> FileInfo:
        """Restore the address/per-file key of a paused offer. Offers rebuilt
        from the database blank those fields (a previous session's server may
        be gone), but a resume entry recorded when the download was interrupted
        still carries them, so the next tap can continue the transfer."""
        if file_info.download_host and file_info.download_port > 0 and file_info.file_key:
            return file_info
        entry = self._resume_store.get(file_info.file_id)
        if not entry:
            return file_info
        host = str(entry.get("host") or file_info.download_host)
        try:
            port = int(entry.get("port") or file_info.download_port or 0)
        except (TypeError, ValueError):
            port = 0
        key = str(entry.get("key") or file_info.file_key)
        if not host or port <= 0 or not key:
            return file_info
        return FileInfo(
            file_info.file_id,
            file_info.file_name,
            file_info.file_size,
            host,
            port,
            file_key=key,
            kind=file_info.kind,
            folder_id=file_info.folder_id,
            folder_name=file_info.folder_name,
            relative_path=file_info.relative_path,
            folder_total=file_info.folder_total,
        )

    def _resume_offset(self, file_id: str, target_path: str) -> int:
        resume = self.resume_info(file_id)
        if resume is None:
            return 0
        if resume["target"] == target_path:
            return int(resume["received"])
        # a different save location was chosen: the old staging file is stale
        self._discard_resume(file_id)
        return 0

    def _sync_resume(
        self,
        file_info: FileInfo,
        target_path: str,
        folder_id: str = "",
        folder_total: int = 0,
        dest_dir: str = "",
        keep_empty: bool = False,
    ) -> None:
        """Record (or clear) the staging state after an attempt: a surviving
        ".part" becomes a resumable pause; a renamed-away one means success and
        the entry is dropped. [keep_empty] records the entry even before any
        byte arrived, so a process death mid-transfer still leaves the target
        and address available for the next start."""
        try:
            received = os.path.getsize(target_path + ".part")
        except OSError:
            received = 0
        if received <= 0 and not keep_empty:
            self._resume_store.remove(file_info.file_id)
            return
        self._resume_store.put(
            file_info.file_id,
            target=target_path,
            received=max(0, int(received)),
            total=int(file_info.file_size),
            host=file_info.download_host,
            port=int(file_info.download_port),
            key=file_info.file_key,
            folderId=folder_id,
            folderTotal=int(folder_total),
            destDir=dest_dir,
        )

    def _discard_resume(self, file_id: str) -> None:
        """Drop a resume entry and its staging file (save location changed or
        the part can never complete)."""
        entry = self._resume_store.get(file_id)
        self._resume_store.remove(file_id)
        if entry and entry.get("target"):
            try:
                os.remove(str(entry["target"]) + ".part")
            except OSError:
                pass

    def cancel_download(self, group_id: str, message_id: str) -> None:
        """Pause a running download: set its cancel event and shut the held
        socket down (a blocked read returns at once). The worker reports
        "下载已取消" via file_download_finished, and the ".part" staging file
        plus the resume entry are KEPT so the next tap continues from the
        received offset (Android parity: 取消 = 保留断点)."""
        key = (group_id, message_id)
        with self._lock:
            entry = self._download_registry.pop(key, None)
        if entry is None:
            return
        event, socks = entry
        event.set()
        for s in tuple(socks):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def download_direct_file(self, peer_id: str, file_id: str, target_path: str) -> None:
        """Download a direct-chat file offer by file_id on a worker thread;
        file_download_finished(file_id, ok, message) fires on completion. A
        paused download resumes from its staged offset; after an app restart
        the address and per-file key come from the persisted resume entry."""
        msg = next(
            (m for m in self.direct.messages_for(peer_id) if m.id == file_id), None
        )
        if msg is None or msg.file_info is None:
            self.file_download_finished.emit(file_id, False, "文件消息不存在")
            return
        file_info = self._merged_offer(msg.file_info)
        if not file_info.download_host or file_info.download_port <= 0:
            self.file_download_finished.emit(file_id, False, "文件已过期，请对方重新发送")
            return
        # media targets live in the app media dir; ensure it exists before the
        # worker thread opens the output file
        try:
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        except OSError:
            self.file_download_finished.emit(file_id, False, "无法创建保存目录")
            return
        offset = self._resume_offset(file_id, target_path)
        # cancel entry + throttled progress for the worker thread
        event, socks = self._register_download((peer_id, file_id))
        progress = self._make_file_progress(file_id)

        def run() -> None:
            # remember the target/address before the first byte: a process
            # death mid-transfer then still leaves a resumable entry
            self._sync_resume(file_info, target_path, keep_empty=True)
            ok, message = self.direct.download_file(
                file_info,
                target_path,
                progress=progress,
                cancel=event,
                sock_holder=socks,
                offset=offset,
            )
            self._sync_resume(file_info, target_path)
            self._finish_download((peer_id, file_id))
            self.file_download_finished.emit(file_id, ok, message)

        threading.Thread(target=run, daemon=True).start()

    # DirectChatListener (called from direct-chat worker threads): hop to the
    # main thread via queued signals.
    def direct_contacts_changed(self) -> None:
        self.direct_contacts_signal.emit()

    def direct_messages_changed(self, peer_id: str) -> None:
        self.direct_messages_signal.emit(peer_id)

    def direct_connect_failed(self, peer, reason: str) -> None:
        self.status_message.emit(
            f"无法连接成员 {peer.name}（{peer.ip_address}:{peer.port}）：{reason}"
        )

    def direct_typing_changed(self, peer_id: str, active: bool) -> None:
        # network thread -> main thread via the queued signal
        self.direct_typing_signal.emit(peer_id, active)

    # GroupMeshListener (called from mesh worker threads; hop to the main
    # thread via queued signals).
    def group_mesh_message(self, group_id: str, msgs) -> None:
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.merge_incoming(msgs)

    def group_mesh_links_changed(self, group_id: str) -> None:
        # a mesh link came up: flush any ops staged while fully offline
        if self.mesh.has_links(group_id):
            self._replay_pending_ops(group_id)
        if group_id == self.active_group_id:
            self.active_connection_lost_changed.emit()

    def group_mesh_delete(self, group_id: str, message_id: str, sender_id: str) -> None:
        """A delete arrived over the group mesh (host-offline path): remove the
        message from the owning group's list. The _messages collector mirrors
        the removal to the database exactly like relay-delivered deletes."""
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.remove_local_message(message_id, sender_id)

    def group_mesh_deleted_ids(self, group_id: str, deleted_ids) -> None:
        """history_reply carried tombstone convergence data: drop the copies
        this member still holds and record every tombstone — without
        rebroadcasting (convergence is not a new delete event)."""
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.apply_deleted_ids(deleted_ids)
        with self._lock:
            self.store.record_deleted_messages(group_id, deleted_ids)
        if group_id == self.active_group_id:
            self.active_messages_changed.emit()

    def group_mesh_typing(self, group_id: str, sender_id: str, active: bool) -> None:
        """A linked member's typing indicator (host-offline path); hop to the
        main thread via the queued signal like the other mesh events."""
        self.group_typing_signal.emit(group_id, sender_id, active)

    def typing_changed(self, p2p: P2PManager, sender_id: str, active: bool) -> None:
        """A member's typing indicator (host relay path); hop to the main
        thread via the queued signal."""
        self.group_typing_signal.emit(p2p.current_group_id or "", sender_id, active)

    # -------------------------------------------------------------- message
    # experience (edit / reactions / pins / group read receipts): network
    # threads persist through the (thread-safe) store and hop to the main
    # thread via signals.

    def message_edited(self, p2p: P2PManager, message_id: str, new_content: str, sender_id: str, message_sig: Optional[str] = None) -> None:
        """A relayed edit_message arrived (host relay path). The manager's
        in-memory copy is already updated; persist and refresh. [message_sig]
        mirrors the edit into the mesh history copy (LESSONS 2026-09-21 #1)."""
        gid = p2p.current_group_id
        if gid:
            self._apply_group_edit(gid, message_id, new_content, sender_id, message_sig=message_sig)

    def reaction_changed(self, p2p: P2PManager, message_id: str, emoji: str, sender_id: str, active: bool) -> None:
        gid = p2p.current_group_id
        if gid:
            self._apply_reaction(gid, message_id, emoji, sender_id, active)

    def pin_changed(self, p2p: P2PManager, message_id: str, sender_id: str, active: bool) -> None:
        gid = p2p.current_group_id
        if gid:
            self._apply_pin(gid, message_id, sender_id, active)

    def group_read_receipt(self, p2p: P2PManager, reader_id: str, up_to_id: str) -> None:
        gid = p2p.current_group_id
        if gid:
            self._apply_group_read_receipt(gid, reader_id, up_to_id)

    def group_mesh_edit(self, group_id: str, message_id: str, new_content: str, sender_id: str, message_sig: Optional[str] = None) -> None:
        self._apply_group_edit(group_id, message_id, new_content, sender_id, message_sig=message_sig)

    def group_mesh_reaction(self, group_id: str, message_id: str, emoji: str, sender_id: str, active: bool) -> None:
        self._apply_reaction(group_id, message_id, emoji, sender_id, active)

    def group_mesh_pin(self, group_id: str, message_id: str, sender_id: str, active: bool) -> None:
        self._apply_pin(group_id, message_id, sender_id, active)

    def group_mesh_read_receipt(self, group_id: str, reader_id: str, up_to_id: str) -> None:
        self._apply_group_read_receipt(group_id, reader_id, up_to_id)

    # ------------------------------------------------- group file share (群文件)
    # The relay/mesh layers already bound the claimed senderId to the
    # authenticated connection; these callbacks persist the convergence and
    # refresh the UI. Removal authorization (uploader or group owner) is
    # re-checked here against the STORED entry, and the return value gates the
    # relay's rebroadcast (LESSONS 2026-09-19 #3: a rejected packet must
    # neither persist nor propagate).

    def group_file_add_received(self, p2p: P2PManager, entry: GroupFileInfo) -> None:
        gid = p2p.current_group_id
        if gid:
            self._apply_group_file_add(gid, entry)

    def group_mesh_group_file_add(self, group_id: str, entry: GroupFileInfo) -> None:
        self._apply_group_file_add(group_id, entry)

    def group_file_remove_received(
        self, p2p: P2PManager, file_id: str, sender_id: str
    ) -> bool:
        gid = p2p.current_group_id
        return bool(gid) and self._apply_group_file_remove(gid, file_id, sender_id)

    def group_mesh_group_file_remove(
        self, group_id: str, file_id: str, sender_id: str
    ) -> bool:
        return self._apply_group_file_remove(group_id, file_id, sender_id)

    def group_files_received(self, p2p: P2PManager, entries, removed_ids) -> None:
        gid = p2p.current_group_id
        if gid:
            self._apply_group_files_convergence(gid, entries, removed_ids)

    def group_mesh_group_files(self, group_id: str, entries, removed_ids) -> None:
        self._apply_group_files_convergence(group_id, entries, removed_ids)

    @staticmethod
    def _saved_group_file(group_id: str, entry: GroupFileInfo) -> SavedGroupFile:
        """Wire entry -> storage row (no local source: receivers never serve)."""
        return SavedGroupFile(
            group_id=group_id,
            file_id=entry.file_id,
            name=entry.name,
            size=entry.size,
            sender_id=entry.sender_id,
            sender_name=entry.sender_name,
            ts=entry.ts,
            download_host=entry.download_host,
            download_port=entry.download_port,
            file_key=entry.file_key,
        )

    def _group_creator_id(self, group_id: str) -> str:
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None and p2p.is_host:
            return p2p.my_id
        return self.store.get_setting(f"group_creator_id_{group_id}", "")

    def _apply_group_file_add(self, group_id: str, entry: GroupFileInfo) -> None:
        with self._lock:
            self.store.upsert_group_file(self._saved_group_file(group_id, entry))
        self.group_files_changed.emit(group_id)

    def _apply_group_file_remove(self, group_id: str, file_id: str, sender_id: str) -> bool:
        with self._lock:
            entry = self.store.get_group_file(group_id, file_id)
        if entry is None:
            # unknown / already removed: never authorize a no-op rebroadcast
            return False
        if not network_module.can_remove_group_file(
            sender_id, entry.sender_id, self._group_creator_id(group_id)
        ):
            return False
        with self._lock:
            self.store.record_removed_group_files(group_id, [file_id])
        # our own share: stop serving the bytes too
        if entry.local_path:
            try:
                self.host_server.unregister_shared_file(file_id)
            except Exception:
                pass
        self.group_files_changed.emit(group_id)
        return True

    def _apply_group_files_convergence(self, group_id: str, entries, removed_ids) -> None:
        """join_ack / history_reply share-area convergence: apply the removal
        tombstones FIRST, then the index entries (a tombstoned id stays hidden
        so a removed file never resurrects). No rebroadcast."""
        removed = [str(i) for i in removed_ids or [] if i]
        with self._lock:
            if removed:
                self.store.record_removed_group_files(group_id, removed)
            for entry in entries or []:
                self.store.upsert_group_file(self._saved_group_file(group_id, entry))
        self.group_files_changed.emit(group_id)

    def direct_message_edited(self, peer_id: str, message_id: str, new_content: str) -> None:
        # the network layer only reports an edit its author check accepted;
        # the store write re-checks the author in SQL anyway (defense in
        # depth: a forged edit can never rewrite another author's text)
        with self._lock:
            self.store.update_message_content(
                "direct:" + peer_id, message_id, new_content, sender_id=peer_id
            )

    def direct_reaction_changed(self, peer_id: str, message_id: str, emoji: str, active: bool) -> None:
        key = "direct:" + peer_id
        with self._lock:
            if active:
                self.store.add_reaction(key, message_id, emoji, peer_id)
            else:
                self.store.remove_reaction(key, message_id, emoji, peer_id)
        self.extras_changed.emit(key)

    def direct_pin_changed(self, peer_id: str, message_id: str, active: bool) -> None:
        key = "direct:" + peer_id
        with self._lock:
            self.store.set_message_pinned(key, message_id, active, pinned_by=peer_id)
        self.extras_changed.emit(key)

    def _apply_group_edit(self, group_id: str, message_id: str, new_content: str, sender_id: str, message_sig: Optional[str] = None) -> None:
        """Persist a received edit and mirror it into the owning group's
        in-memory list (the mesh path has updated its own state already; the
        relay paths updated the P2PManager copy — apply_edit_local is
        idempotent either way). [message_sig] is the verified edit packet
        signature (the edited body's message transcript): mirroring it into
        the mesh history copy keeps this member's own later history pushes
        carrying the edited text instead of reverting it for rejoining
        members (LESSONS 2026-09-21 #1); the history-merge path passes None
        because it already rewrote its mesh copy. The store write is
        author-gated in SQL, so a forged edit can never rewrite stored
        history even if an upstream caller skipped the authorization, and
        the UI only refreshes when a row actually changed."""
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.apply_edit_local(message_id, new_content, sender_id)
        if message_sig:
            self.mesh.update_mesh_message(
                group_id, message_id, new_content, sender_id,
                sender_sig=message_sig,
            )
        with self._lock:
            changed = self.store.update_message_content(
                group_id, message_id, new_content, sender_id=sender_id
            )
        if changed and group_id == self.active_group_id:
            self.active_messages_changed.emit()

    def _apply_reaction(self, group_id: str, message_id: str, emoji: str, sender_id: str, active: bool) -> None:
        with self._lock:
            if active:
                self.store.add_reaction(group_id, message_id, emoji, sender_id)
            else:
                self.store.remove_reaction(group_id, message_id, emoji, sender_id)
        self.extras_changed.emit(group_id)

    def _apply_pin(self, group_id: str, message_id: str, sender_id: str, active: bool) -> None:
        with self._lock:
            self.store.set_message_pinned(
                group_id, message_id, active, pinned_by=sender_id
            )
        self.extras_changed.emit(group_id)

    def _apply_group_read_receipt(self, group_id: str, reader_id: str, up_to_id: str) -> None:
        """A member read the group up to [up_to_id]: record the reader on every
        OWN message covered (same cut-off rule as the direct chat: timestamp
        <= the receipt's message). Two targeted SQL lookups — never a full
        decrypted table read (a receipt arrives for every member and every
        new message)."""
        with self._lock:
            target_ts = self.store.get_message_timestamp(group_id, up_to_id)
            if target_ts is None:
                return
            covered = self.store.get_own_message_ids_upto(group_id, target_ts)
            if not covered:
                return
            inserted = self.store.record_group_reads(group_id, covered, reader_id)
        if inserted:
            # a repeat receipt (nothing new recorded) must not trigger a
            # rebuild: N members x M messages would otherwise rebuild the
            # whole conversation N*M times
            self.extras_changed.emit(group_id)

    def deleted_ids_received(self, p2p: P2PManager, deleted_ids) -> None:
        """join_ack carried tombstone convergence data: the still-present
        copies were already dropped by the network layer; record every id so
        later history backfills cannot resurrect them."""
        gid = p2p.current_group_id
        if not gid or not deleted_ids:
            return
        with self._lock:
            self.store.record_deleted_messages(gid, deleted_ids)

    # Group management (owner-only group_update / kick_member; callbacks may
    # arrive on relay or mesh worker threads).

    def group_info_changed(self, p2p: P2PManager) -> None:
        """The owner published a new name/announcement (group_update) or this
        host applied its own: refresh the list entry and persist the change."""
        gid = p2p.current_group_id
        if not gid:
            return
        with self._lock:
            meta = self._find_group(gid)
            name = p2p.current_group_name
            if meta is not None:
                meta.group_name = name
                meta.announcement = p2p.group_announcement
            self.store.upsert_group(
                SavedGroup(
                    group_id=gid,
                    group_name=name,
                    is_host=p2p.is_host,
                    host_ip=meta.host_ip if meta is not None else "",
                    host_port=meta.host_port if meta is not None else 0,
                    my_name=p2p.my_name,
                    member_count=meta.member_count if meta is not None else 1,
                    last_message=meta.last_message if meta is not None else "",
                    last_message_time=meta.last_message_time if meta is not None else 0,
                    announcement=p2p.group_announcement,
                )
            )
            if gid == self.active_group_id:
                self.active_group_name = name
        self.groups_changed.emit()
        if gid == self.active_group_id:
            self.active_group_changed.emit()

    def kicked_from_group(self, p2p: P2PManager) -> None:
        """The owner removed this device: tear down every connection of the
        group, keep the local history, hide the group and tell the user."""
        gid = p2p.current_group_id
        if not gid:
            return
        with self._lock:
            if self.group_p2p_map.get(gid) is not p2p:
                return
            meta = self._find_group(gid)
            name = meta.group_name if meta is not None else p2p.current_group_name
            self.call_manager.end_if_on(p2p, "已离开群组")
            self.group_call.end_if_on(p2p, "已离开群组")
            self.group_p2p_map.pop(gid, None)
            self.persisted_peer_counts.pop(gid, None)
            self.replay_done.pop(gid, None)
            if meta is not None:
                meta.connected = False
            self.groups = [g for g in self.groups if g.group_id != gid]
            if self.active_group_id == gid:
                self.active_group_id = None
                self.active_group_name = ""
                self.active_my_name = ""
                self.active_is_host = False
            # the row (and its message history) stays; kicked hides it
            self.store.set_group_kicked(gid, True)
        p2p.stop()
        self._teardown_group_mesh(gid)
        self.status_message.emit(f"你已被移出群组「{name}」")
        self.groups_changed.emit()
        self.active_group_changed.emit()
        self.active_peers_changed.emit()
        self.active_messages_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()

    def group_mesh_admin(self, group_id: str, packet) -> None:
        """An owner management packet arrived over a mesh link (already
        validated against the group's creator id by the mesh layer)."""
        if packet.type == "group_update":
            p2p = self.group_p2p_map.get(group_id)
            if p2p is not None:
                new_name = (packet.group_name or "").strip()
                if new_name and new_name != p2p.group_name:
                    p2p.group_name = new_name
                if packet.announcement is not None:
                    p2p.group_announcement = packet.announcement
                self.group_info_changed(p2p)
            else:
                with self._lock:
                    meta = self._find_group(group_id)
                    if meta is not None:
                        if packet.group_name:
                            meta.group_name = packet.group_name
                        if packet.announcement is not None:
                            meta.announcement = packet.announcement
                        self.store.upsert_group(
                            SavedGroup(
                                group_id=group_id,
                                group_name=meta.group_name,
                                is_host=False,
                                host_ip=meta.host_ip,
                                host_port=meta.host_port,
                                member_count=meta.member_count,
                                last_message=meta.last_message,
                                last_message_time=meta.last_message_time,
                                announcement=meta.announcement,
                            )
                        )
                self.groups_changed.emit()
                if group_id == self.active_group_id:
                    self.active_group_changed.emit()
        elif packet.type == "kick_member":
            target = packet.target_id
            if not target:
                return
            p2p = self.group_p2p_map.get(group_id)
            if p2p is not None and target == p2p.my_id:
                self.kicked_from_group(p2p)
                return
            if p2p is not None:
                with self._lock:
                    p2p.peers.pop(target, None)
                self._save_group_peers(group_id, list(p2p.peers.values()))
                self.peers_changed(p2p)

    # ------------------------------------------------------------ group mesh

    def _load_group_peers(self, group_id: str) -> list:
        raw = self.store.get_setting(f"group_peers_{group_id}", "")
        if not raw:
            return []
        try:
            return [Peer.from_dict(d) for d in json.loads(raw)]
        except Exception:
            return []

    def _save_group_peers(self, group_id: str, peers) -> None:
        try:
            self.store.set_setting(
                f"group_peers_{group_id}",
                json.dumps([p.to_dict() for p in peers]),
            )
        except Exception:
            pass

    def _setup_group_mesh(self, group_id: str, p2p: P2PManager) -> None:
        """Enter the mesh for a member group: link to every other member and
        seed the mesh with the persisted history. The host relays to everyone,
        so the host itself does not mesh. The group password authenticates
        mesh handshakes: only members who know it may link and read the
        group's history."""
        if p2p.is_host:
            return
        my_peer = Peer(p2p.my_id, p2p.my_name or "用户", get_local_ip_address(), p2p.port)
        seen = {}
        for peer in list(p2p.peers.values()) + self._load_group_peers(group_id):
            seen[peer.id] = peer
        # the group password authenticates mesh handshakes: only members who
        # know it may link and read the group's history
        password = p2p.group_password or self.store.get_group_password(group_id)
        self.mesh.enter_group(
            group_id, my_peer, list(seen.values()), list(p2p.messages), password
        )

    def _teardown_group_mesh(self, group_id: str) -> None:
        self.mesh.leave_group(group_id)

    def _attach_admin_hooks(self, p2p: P2PManager) -> None:
        """Wire one manager's group-management hooks: the creator (owner) id
        used to validate incoming group_update / kick_member, and the mesh
        rebroadcast used to forward relayed owner packets to members whose host
        connection is down. The creator id is resolved lazily because a client
        only learns its group id once the join completes."""
        def creator() -> str:
            gid = p2p.current_group_id
            if not gid:
                return ""
            if p2p.is_host:
                return p2p.my_id
            return self.store.get_setting(f"group_creator_id_{gid}", "")

        p2p.creator_id_provider = creator

        # group file share area (群文件): join_ack carries this group's share
        # index and removal tombstones so a rejoining member converges
        p2p.group_files_provider = self.store.get_group_files
        p2p.removed_file_ids_provider = self.store.get_removed_group_file_ids

        def forward(packet) -> None:
            gid = packet.group_id or p2p.current_group_id or ""
            if gid:
                self.mesh.broadcast_admin(gid, packet)

        p2p.admin_rebroadcast = forward

    def active_group_announcement(self) -> str:
        gid = self.active_group_id
        if gid is None:
            return ""
        meta = self._find_group(gid)
        return meta.announcement if meta is not None else ""

    def update_group_info(
        self, group_name: str = "", announcement: Optional[str] = None
    ) -> bool:
        """Owner action: publish a new group name and/or announcement. No-op
        (returns False) for a non-owner or a disconnected group."""
        p2p = self._active_p2p()
        if p2p is None or not p2p.is_host:
            return False
        name = (group_name or "").strip()[:MAX_NAME_LENGTH]
        ann = None if announcement is None else announcement.strip()
        if not name and ann is None:
            return False
        if not p2p.send_group_update(name, ann):
            return False
        self.status_message.emit("群信息已更新")
        return True

    def kick_active_member(self, peer_id: str) -> bool:
        """Owner action: remove a member from the active group."""
        p2p = self._active_p2p()
        if p2p is None or not p2p.is_host:
            return False
        # take the display name BEFORE the kick: kick_member pops the peer
        peer = p2p.peers.get(peer_id)
        name = peer.name if peer is not None else peer_id
        if not p2p.kick_member(peer_id):
            return False
        self.status_message.emit(f"已将 {name} 移出群组")
        return True

    # ------------------------------------------- member-sponsored join entry

    @staticmethod
    def _send_line_simple(sock, line: str) -> None:
        try:
            sock.sendall((line + "\n").encode("utf-8"))
        except OSError:
            pass

    @staticmethod
    def _safe_close_simple(sock) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _find_host_peer(self, p2p: P2PManager) -> Optional[Peer]:
        meta = self._find_group(p2p.current_group_id)
        if meta is None or not meta.host_ip:
            return None
        ip, port = self._parse_host_port(meta.host_ip)
        if meta.host_port:
            port = meta.host_port
        for p in p2p.peers.values():
            if p.ip_address == ip and p.port == port:
                return p
        return Peer("host", p2p.group_name, ip, port)

    def _handle_member_group_request(self, packet, sock, wire) -> bool:
        """Answer query / join for a group this device belongs to as a MEMBER,
        so the target IP only has to be in the group — not the creator. The
        newcomer gets the member list and the host's address and is announced
        over the mesh (works even when the host is offline). The wire is
        already secured: the password was verified during the handshake, so no
        password check is needed here (Android parity)."""
        id_or_name = packet.group_id
        if not id_or_name:
            return False
        p2p = None
        for candidate in self.group_p2p_map.values():
            if not candidate.is_host and candidate.join_id == id_or_name:
                p2p = candidate
                break
        if p2p is None:
            return False
        gid = p2p.current_group_id
        try:
            if packet.type == Protocol.MODE_QUERY:
                info = GroupInfo(p2p.group_name, p2p.my_name, p2p.my_id, len(p2p.peers) + 1)
                wire.send_packet(NetworkPacket(type="group_info", group_info=info))
            elif packet.type == Protocol.MODE_JOIN:
                peer = packet.peer
                if peer is None:
                    return False
                # exclude the host from the mesh member list: it relays to
                # everyone, linking to it would be redundant
                host = self._find_host_peer(p2p)
                members = [p for p in p2p.peers.values() if host is None or p.id != host.id]
                wire.send_packet(
                    NetworkPacket(
                        type="join_ack",
                        group_id=gid,
                        members=members,
                        host=host,
                        # the owner's current announcement (the sponsor mirrors
                        # it, like the host itself would)
                        announcement=p2p.group_announcement or None,
                        # tombstone convergence: omitted when empty
                        deleted_ids=self.store.get_deleted_ids(gid) or None,
                    )
                )
                self._safe_close_simple(sock)
                # tell every member about the newcomer so the mesh links up
                self.mesh.announce_peer(gid, peer)
            else:
                return False
        except Exception:
            return False
        finally:
            self._safe_close_simple(sock)
        return True

    def _on_direct_contacts_changed(self) -> None:
        self._save_direct_contacts()

    def _on_direct_messages_changed(self, peer_id: str) -> None:
        msgs = self.direct.messages_for(peer_id)
        self._direct_last[peer_id] = msgs[-1] if msgs else None
        stored = self._direct_persisted_ids.setdefault(
            peer_id,
            {m.id for m in self.store.get_messages_for_group("direct:" + peer_id)},
        )
        pending_map = self._direct_pending.setdefault(peer_id, {})
        read_map = self._direct_read.setdefault(peer_id, {})
        current = {m.id for m in msgs}
        removed = stored - current
        if removed:
            stored.difference_update(removed)
            for mid in removed:
                pending_map.pop(mid, None)
                read_map.pop(mid, None)
                self.store.delete_message("direct:" + peer_id, mid)
        new = [m for m in msgs if m.id not in stored]
        peer = next((c for c in self.direct.contacts_list() if c.id == peer_id), None)
        new_incoming = [m for m in new if not m.is_from_me]
        if new_incoming and not self.window_active:
            # window hidden/minimized: pop a tray bubble for the 1:1 chat too.
            # The "direct:" prefix identifies the conversation for the tray
            # aggregator and for the click-to-open handler (Android parity:
            # background message notifications cover direct chats).
            self.raw_tray.emit(
                "direct:" + peer_id,
                peer.name if peer else peer_id,
                new_incoming[-1].content,
            )
        if new:
            # Direct chats live under a synthetic "direct:<peerId>" key in the
            # messages table, which has a foreign key to saved_groups — ensure
            # the placeholder group row exists so the insert never violates it
            self.store.upsert_group(
                SavedGroup(
                    group_id="direct:" + peer_id,
                    group_name=(peer.name if peer else peer_id),
                    is_host=False,
                )
            )
            stored.update(m.id for m in new)
            for m in new:
                pending_map[m.id] = m.pending
                read_map[m.id] = m.read
            self.store.insert_messages(
                [to_saved_message("direct:" + peer_id, m) for m in new]
            )
        # pending -> delivered flips (outbox flush) are plain updates
        flag_changes = [
            m for m in msgs
            if m.is_from_me and m.id in pending_map and pending_map[m.id] != m.pending
        ]
        if flag_changes:
            for m in flag_changes:
                pending_map[m.id] = m.pending
                self.store.update_message_pending("direct:" + peer_id, m.id, m.pending)
        # peer read_receipts flip own messages to 已读: persist like pending
        read_changes = [
            m for m in msgs
            if m.is_from_me and m.id in read_map and read_map[m.id] != m.read
        ]
        if read_changes:
            for m in read_changes:
                read_map[m.id] = m.read
                self.store.update_message_read("direct:" + peer_id, m.id, m.read)

    def _on_direct_chat_migrated(self, from_id: str, to_id: str) -> None:
        """A direct chat's key moved from a "ip:..." placeholder to the real
        device id: move persisted rows and in-memory observer state, and tell
        the UI to re-key the open screen."""
        if from_id == to_id:
            return
        self._direct_persisted_ids[to_id] = self._direct_persisted_ids.pop(
            from_id, self._direct_persisted_ids.get(to_id, set())
        )
        self._direct_pending[to_id] = self._direct_pending.pop(
            from_id, self._direct_pending.get(to_id, {})
        )
        self._direct_read[to_id] = self._direct_read.pop(
            from_id, self._direct_read.get(to_id, {})
        )
        if from_id in self._direct_last:
            self._direct_last[to_id] = self._direct_last.pop(from_id)
        saved = self.store.get_messages_for_group("direct:" + to_id)
        if not saved:
            self._seed_direct_history(to_id)
        # saved_messages has a FK to saved_groups; ensure the destination
        # placeholder row exists before moving rows, or move_messages aborts.
        target_contact = next(
            (c for c in self.direct.contacts_list() if c.id == to_id), None
        )
        self.store.upsert_group(
            SavedGroup(
                group_id="direct:" + to_id,
                group_name=(target_contact.name if target_contact else to_id),
                is_host=False,
            )
        )
        self.store.move_messages("direct:" + from_id, "direct:" + to_id)
        self.store.move_call_logs("direct:" + from_id, "direct:" + to_id)
        self.store.delete_group("direct:" + from_id)
        self.direct_chat_migrated.emit(from_id, to_id)

    def _on_direct_chat_migrated_slot(self, from_id: str, to_id: str) -> None:
        pass

    def _restore_direct_summaries(self) -> None:
        """Restore each persisted direct chat's LAST message after a process
        restart, so the home-page previews are populated without reconnecting
        to every member. Messages persisted as still pending (offline sends
        from the previous process) are additionally re-queued into the outbox
        so they deliver once the peer is reachable (Android parity)."""
        for sg in self.store.get_all_groups():
            if not sg.group_id.startswith("direct:"):
                continue
            peer_id = sg.group_id.removeprefix("direct:")
            saved = self.store.get_messages_for_group(sg.group_id)
            if not saved:
                continue
            msgs = [
                ChatMessage(
                    id=m.id,
                    content=m.content,
                    timestamp=m.timestamp,
                    sender_id=m.sender_id,
                    sender_name=m.sender_name,
                    is_from_me=m.is_from_me,
                    file_info=self._restored_file_info(m),
                    pending=m.pending,
                )
                for m in saved
            ]
            self._direct_last[peer_id] = msgs[-1]
            self._direct_persisted_ids[peer_id] = {m.id for m in msgs}
            self._direct_pending[peer_id] = {m.id: m.pending for m in msgs}
            # previews live in the DirectChatManager's in-memory state
            self.direct.seed_last_message(peer_id, msgs[-1])
            pending = [m for m in msgs if m.pending]
            if pending:
                self.direct.restore_pending(peer_id, pending)
        # also remove any orphaned placeholder group rows (their messages
        # migrated away); harmless if none exist
        for gid in [g.group_id for g in self.groups if g.group_id.startswith("direct:")]:
            self.groups = [g for g in self.groups if g.group_id != gid]

    def _find_group(self, group_id: str) -> Optional[GroupMeta]:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        return None

    def _parse_host_port(self, host: str) -> tuple:
        """Parse "ip" / "ip:port" input. Normalizes the full-width
        punctuation and digits a Chinese IME emits (：．。０-９) to ASCII
        first, so an address typed with the IME in Chinese/full-width
        punctuation mode still yields a connectable endpoint."""
        host = host.translate(self._FULLWIDTH_MAP).strip()
        if ":" in host:
            head, _, tail = host.rpartition(":")
            tail = tail.strip()
            # isascii() first: str.isdigit() alone is True for superscripts
            # ("²") and other unicode digits, which int() then rejects with
            # ValueError (a crash, not a validation failure)
            if tail.isascii() and tail.isdigit():
                return head.strip(), int(tail)
        return host, network_module.TCP_PORT

    @classmethod
    def _is_valid_host(cls, host: str) -> bool:
        """A plausible IPv4 dotted quad or DNS hostname. All-digit but
        non-IPv4 inputs ("127001", "999", "1.2.3") are rejected: they are
        what a mangled IP entry looks like and would only ever fail to
        connect."""
        if not host or len(host) > 253:
            return False
        parts = host.split(".")
        if all(p.isascii() and p.isdigit() for p in parts):
            return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
        return all(parts) and all(cls._HOST_LABEL_RE.match(p) for p in parts)

    def _active_p2p(self) -> Optional[P2PManager]:
        if self.active_group_id is None:
            return None
        return self.group_p2p_map.get(self.active_group_id)

    def groups_list(self) -> List[GroupMeta]:
        return list(self.groups)

    def active_peers(self) -> Dict[str, Peer]:
        p2p = self._active_p2p()
        return dict(p2p.peers) if p2p is not None else {}

    def group_member_verified(self, group_id: Optional[str], peer_id: str) -> bool:
        """True when the member has a TOFU-bound device identity in this group
        (it has sent at least one validly signed packet). UI only."""
        if not group_id or not peer_id:
            return False
        return groupauth.member_verified(group_id, peer_id)

    def group_member_fingerprint(self, group_id: Optional[str], peer_id: str) -> str:
        """The member's bound 安全码 ("" when unbound). UI only."""
        if not group_id or not peer_id:
            return ""
        return groupauth.member_fingerprint(group_id, peer_id)

    def active_host_peer(self) -> Optional[Peer]:
        """The active group's host (creator) as a member entry: for a host
        group that is this device itself (never inside active_peers); for a
        client group the host the connection was established to."""
        p2p = self._active_p2p()
        if p2p is None:
            return None
        if p2p.is_host:
            return Peer(p2p.my_id, p2p.my_name or "用户", get_local_ip_address(), p2p.port)
        return p2p.connected_host

    def active_group_numeric_id(self) -> str:
        """The active host group's numeric join id (for display / sharing)."""
        p2p = self._active_p2p()
        if p2p is None or not p2p.is_host:
            return ""
        return p2p.numeric_group_id

    def active_messages(self) -> List[ChatMessage]:
        p2p = self._active_p2p()
        return list(p2p.messages) if p2p is not None else []

    def active_server_error(self) -> Optional[str]:
        p2p = self._active_p2p()
        return p2p.server_error if p2p is not None else None

    def active_connection_lost(self) -> bool:
        p2p = self._active_p2p()
        if p2p is None:
            return False
        # still usable while the group mesh links are alive: the host may be
        # gone but members can keep chatting directly
        if not p2p.connection_lost:
            return False
        gid = p2p.current_group_id
        return not (gid and self.mesh.has_links(gid))

    def queried_group_info(self) -> Optional[GroupInfo]:
        return self.setup_p2p.queried_group_info if self.setup_p2p is not None else None

    def query_error(self) -> Optional[str]:
        return self.setup_p2p.query_error if self.setup_p2p is not None else None

    def is_querying_group(self) -> bool:
        return bool(self.setup_p2p and self.setup_p2p.is_querying)

    def is_joining(self) -> bool:
        return bool(self.setup_p2p and self.setup_p2p.is_joining)

    def connection_result(self) -> Optional[tuple]:
        return self.setup_p2p.connection_result if self.setup_p2p is not None else None

    def _load_persisted_groups(self) -> None:
        saved = self.store.get_all_groups()
        for sg in saved:
            if sg.group_id in self.removed_group_ids:
                continue
            # direct chats live under a synthetic "direct:..." key in the same
            # table; never surface them as groups
            if sg.group_id.startswith("direct:"):
                continue
            # a group this member was kicked out of keeps its history row but
            # must never appear in the group list again
            if sg.kicked:
                continue
            self.persisted_my_names[sg.group_id] = sg.my_name
            self.groups.append(
                GroupMeta(
                    group_id=sg.group_id,
                    group_name=sg.group_name,
                    is_host=sg.is_host,
                    host_ip=sg.host_ip,
                    host_port=sg.host_port,
                    my_name=sg.my_name,
                    member_count=sg.member_count,
                    last_message=sg.last_message,
                    last_message_time=sg.last_message_time,
                    announcement=sg.announcement or "",
                )
            )
        # Host groups now always use the single program-wide port; normalize
        # any previously persisted per-group ports.
        for meta in self.groups:
            if meta.is_host and meta.host_port != self.port:
                meta.host_port = self.port
                self.store.upsert_group(
                    SavedGroup(
                        group_id=meta.group_id,
                        group_name=meta.group_name,
                        is_host=True,
                        host_ip=meta.host_ip or get_local_ip_address(),
                        host_port=self.port,
                        my_name=meta.my_name,
                        member_count=meta.member_count,
                        last_message=meta.last_message,
                        last_message_time=meta.last_message_time,
                    )
                )

    def _load_and_replay_messages(self, group_id: str, p2p: P2PManager) -> None:
        # open the replay window: until it closes, no instance may mirror-delete
        # (the rows are about to be replayed into this p2p's message list)
        self.replay_done.pop(group_id, None)
        try:
            saved = self.store.get_messages_for_group(group_id)
            # converge with recorded tombstones BEFORE replaying: a delete that
            # happened while this member was offline must not resurrect here
            tombstones = self.store.get_deleted_ids(group_id)
            if tombstones:
                doomed = set(tombstones) & {m.id for m in saved}
                for mid in doomed:
                    self.store.delete_message(group_id, mid)
                if doomed:
                    saved = [m for m in saved if m.id not in doomed]
            self.persisted_message_ids[group_id] = {m.id for m in saved}
            if saved:
                p2p.replay_saved_messages(
                    [
                        ChatMessage(
                            id=m.id,
                            content=m.content,
                            timestamp=m.timestamp,
                            sender_id=m.sender_id,
                            sender_name=m.sender_name,
                            is_from_me=m.is_from_me,
                            file_info=self._restored_file_info(m),
                            reply_to=m.reply_to or None,
                            reply_preview=m.reply_preview or None,
                            reply_sender=m.reply_sender or None,
                            edited=m.edited,
                            mentions=json.loads(m.mentions) if m.mentions else None,
                        )
                        for m in saved
                    ]
                )
        finally:
            # close the window even on failure: a raised replay must not leave
            # the group permanently unable to persist deletes/tombstones
            if self.group_p2p_map.get(group_id) is p2p:
                self.replay_done[group_id] = p2p
            self.active_messages_changed.emit()

    def create_group(self, user_name: str, group_name: str) -> None:
        nick = user_name.strip()[:MAX_NAME_LENGTH]
        name = group_name.strip()[:MAX_NAME_LENGTH]
        if not nick or not name:
            return

        self.nickname = nick
        self.store.set_setting("nickname", nick)

        # Crypto-random group password (8 chars ≈ 47.6 bits): high enough
        # entropy that the PBKDF2-bound handshake cannot be brute-forced
        # offline from a recorded exchange (Android parity).
        password = random_password(8)
        p2p = P2PManager(
            self,
            port=self.port,
            host_server=self.host_server,
            device_id=self._device_id(),
            hardware_id=self._hardware_fingerprint(),
        )
        self.call_manager.attach(p2p)
        self.group_call.attach(p2p)
        # join_acks carry the group's delete tombstones (convergence)
        p2p.deleted_ids_provider = self.store.get_deleted_ids
        p2p.initialize_as_host(nick, name, password)
        group_id = p2p.current_group_id
        # Freeze the numeric join id at creation: a later owner rename
        # (group_update) must not change the id members saved to rejoin.
        p2p.set_join_id(p2p.numeric_group_id)
        self.store.set_setting(f"group_join_id_{group_id}", p2p.join_id)
        self._attach_admin_hooks(p2p)
        # The same group name on this device derives the SAME group id: stop
        # the previous instance instead of leaking its sockets/heartbeats, and
        # drop its row (a duplicate id would also break the list's keys).
        old = self.group_p2p_map.pop(group_id, None)
        if old is not None:
            self.call_manager.end_if_on(old, "通话已结束")
            self.group_call.end_if_on(old, "通话已结束")
            old.stop()
        self.store.set_group_password(group_id, password)
        # a re-created same-id group is a fresh one: no announcement, and a
        # previous kick flag (if any) must not hide it
        self.store.set_group_announcement(group_id, "")
        self.store.set_group_kicked(group_id, False)
        self.group_p2p_map[group_id] = p2p
        # a fresh group has no saved history to replay, so this instance may
        # mirror deletes right away (replay_done holds "the instance whose
        # replay finished": nothing to replay == finished)
        self.replay_done[group_id] = p2p
        self.groups = [
            GroupMeta(group_id, name, True, host_port=self.port, connected=True)
        ] + [g for g in self.groups if g.group_id != group_id]
        self._persist_group(group_id, p2p)

        self.active_group_id = group_id
        self.active_group_name = name
        self.active_my_name = nick
        self.active_is_host = True
        self.active_group_password = password
        p2p.start_as_host()
        self.groups_changed.emit()
        self.active_group_changed.emit()
        self.active_peers_changed.emit()
        self.active_messages_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()
        self.status_message.emit("群组已创建，等待其他设备加入")

    def group_name_exists(self, name: str) -> bool:
        """True if a live or persisted group already carries this display
        name. Creating again with the same name derives the SAME group id and
        silently replaces the old instance (create_group stops the previous
        manager), so the UI asks for confirmation first."""
        wanted = name.strip()
        if not wanted:
            return False
        if any(meta.group_name == wanted for meta in self.groups):
            return True
        try:
            saved = self.store.get_all_groups()
        except Exception:
            return False
        # "direct:..." rows are direct chats stored in the same table; they
        # are never groups in the list sense
        return any(
            sg.group_name == wanted and not sg.group_id.startswith("direct:")
            for sg in saved
        )

    def query_group(
        self,
        user_name: str,
        group_id: str,
        host_ip: str,
        port: Optional[int] = None,
        password: Optional[str] = None,
    ) -> None:
        """Query a group by its numeric join id: the id is the join identifier
        (the group name is only a display label, learned from the host)."""
        join_id = "".join(ch for ch in group_id.strip() if ch.isdigit())
        raw_ip = host_ip.strip()
        ip, parsed_port = self._parse_host_port(raw_ip)
        if port is None:
            port = parsed_port
        # Validate the whole endpoint BEFORE the nickname check (same style
        # as add_direct_contact): a wrong-length join id, mangled host or
        # out-of-range port used to be accepted silently and the query then
        # just timed out with no clue why. The numeric join id is exactly 8
        # digits (network.numeric_group_id_of zfills to 8), so anything else
        # can never match a group. Endpoint errors win over a blank nickname
        # so the user always gets the "地址无效" toast for the actual fault.
        if (
            not join_id
            or not raw_ip
            or len(join_id) != 8
            or not self._is_valid_host(ip)
            or not 1 <= port <= 65535
        ):
            self.status_message.emit("地址无效")
            return
        nick = user_name.strip()[:MAX_NAME_LENGTH]
        if not nick:
            # never persist an empty nickname (create_group behaves the same)
            return
        self.nickname = nick
        self.store.set_setting("nickname", nick)
        self._stop_pending_p2p()
        self.rejoin_in_progress = False
        self.rejoin_failed = False
        p2p = P2PManager(
            self, port=self.port, device_id=self._device_id(), hardware_id=self._hardware_fingerprint()
        )
        self.call_manager.attach(p2p)
        self.group_call.attach(p2p)
        p2p.initialize_as_client(nick, "", password)
        p2p.set_join_id(join_id)
        self._attach_admin_hooks(p2p)
        self.pending_p2p = p2p
        self.setup_p2p = p2p
        self.pending_host_ip = ip
        self.pending_host_port = port
        self.pending_group_id = None
        p2p.clear_query_state()
        p2p.clear_join_result()
        p2p.query_group(ip, port)

    def confirm_join(self, port: Optional[int] = None) -> None:
        p2p = self.pending_p2p
        if p2p is None or not self.pending_host_ip:
            return
        if port is None:
            port = self.pending_host_port or network_module.TCP_PORT
        p2p.confirm_join(self.pending_host_ip, port)

    # ------------------------------------------------------------ signaling

    @property
    def signaling_server(self) -> str:
        """The configured signaling/relay server endpoint ('' = off)."""
        return self._signaling_server

    @property
    def signaling_secret(self) -> str:
        """The server access secret paired with [signaling_server]."""
        return self._signaling_secret

    def set_signaling_server(self, text: str, secret: str = "") -> bool:
        """Enable/disable cross-NAT joining. Returns False (with a status
        toast) when the endpoint is malformed; the previous setting stays
        active in that case. [secret] is the deployment's server access
        secret — required when the server was started with one."""
        text = (text or "").strip()
        if text:
            host, port = parse_server_endpoint(text)
            if host is None:
                self.status_message.emit("服务器地址无效（格式：IP或域名:端口）")
                return False
        self._apply_signaling_setting(text, secret=(secret or "").strip())
        self.status_message.emit(
            "已启用中继服务器" if text else "已关闭中继服务器"
        )
        return True

    def _apply_signaling_setting(
        self, text: str, persist: bool = True, secret: Optional[str] = None
    ) -> None:
        text = (text or "").strip()
        if persist:
            self.store.set_setting("signaling_server", text)
        if secret is not None:
            self._signaling_secret = secret
            # the access secret is a credential: encrypted at rest
            self.store.set_secret("signaling_secret", secret)
        self._signaling_server = text
        if not text:
            self.host_server.disable_signaling()
            return
        host, port = parse_server_endpoint(text)
        if host is None:
            self.host_server.disable_signaling()
            return
        self.host_server.enable_signaling(host, port, secret=self._signaling_secret)

    def join_via_server(
        self,
        user_name: str,
        group_id: str,
        server: str,
        password: Optional[str] = None,
    ) -> None:
        """Join a group through the signaling server by its numeric id alone:
        punch a direct hole to the host (or fall back to the encrypted relay)
        instead of needing a reachable host IP."""
        join_id = "".join(ch for ch in group_id.strip() if ch.isdigit())
        host, port = parse_server_endpoint(server)
        if len(join_id) != 8:
            self.status_message.emit("群组数字ID必须是8位")
            return
        if host is None:
            self.status_message.emit("服务器地址无效（格式：IP或域名:端口）")
            return
        nick = user_name.strip()[:MAX_NAME_LENGTH]
        if not nick:
            return
        self.nickname = nick
        self.store.set_setting("nickname", nick)
        self._stop_pending_p2p()
        self.rejoin_in_progress = False
        self.rejoin_failed = False
        p2p = P2PManager(
            self, port=self.port, device_id=self._device_id(), hardware_id=self._hardware_fingerprint()
        )
        self.call_manager.attach(p2p)
        self.group_call.attach(p2p)
        p2p.initialize_as_client(nick, "", password)
        p2p.set_join_id(join_id)
        self._attach_admin_hooks(p2p)
        self.pending_p2p = p2p
        self.setup_p2p = p2p
        # no host address is known on this path: the group row is saved
        # without one and LAN rejoins fall back to a manual server join
        self.pending_host_ip = ""
        self.pending_host_port = None
        self.pending_group_id = None
        p2p.clear_query_state()
        p2p.clear_join_result()
        p2p.confirm_join_via_server(host, port, secret=self._signaling_secret)

    def cancel_join(self) -> None:
        self._stop_pending_p2p()
        self.setup_p2p = None
        self.pending_host_ip = ""
        self.join_ui_state_changed.emit()

    def clear_join_state(self) -> None:
        self.setup_p2p = None

    def _stop_pending_p2p(self) -> None:
        if self.pending_p2p is not None:
            self.pending_p2p.stop()
        self.pending_p2p = None
        self.pending_group_id = None
        self.pending_host_port = None

    def switch_to_group(self, group_id: str) -> None:
        if self.pending_group_id is not None and self.pending_group_id != group_id:
            self._stop_pending_p2p()
            self.setup_p2p = None
            self.rejoin_in_progress = False
            self.rejoin_failed = False
        self._clear_unread(group_id)
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            self._setup_group_mesh(group_id, p2p)
            self._set_active(group_id, p2p)
            return
        meta = self._find_group(group_id)
        if meta is None:
            return
        if meta.is_host:
            nick = meta.my_name or "用户"
            password = self.store.get_group_password(group_id) or None
            new_p2p = P2PManager(
                self,
                port=self.port,
                host_server=self.host_server,
                device_id=self._device_id(),
                hardware_id=self._hardware_fingerprint(),
            )
            # join_acks carry the group's delete tombstones (convergence)
            new_p2p.deleted_ids_provider = self.store.get_deleted_ids
            # re-host under the ORIGINAL group id and numeric join id: a
            # rename (group_update) changed only the display name
            new_p2p.initialize_as_host(
                nick, meta.group_name, password, group_id=group_id
            )
            stored_join_id = self.store.get_setting(f"group_join_id_{group_id}", "")
            new_p2p.set_join_id(stored_join_id or new_p2p.numeric_group_id)
            if not stored_join_id:
                self.store.set_setting(f"group_join_id_{group_id}", new_p2p.join_id)
            new_p2p.group_announcement = meta.announcement or ""
            self._attach_admin_hooks(new_p2p)
            self.call_manager.attach(new_p2p)
            self.group_p2p_map[group_id] = new_p2p
            meta.host_port = self.port
            self._persist_group(group_id, new_p2p)
            self._load_and_replay_messages(group_id, new_p2p)
            new_p2p.start_as_host()
            self._set_active(group_id, new_p2p)
        else:
            self._set_active_meta(group_id, meta)
            self._rejoin_group(group_id)

    def _rejoin_group(self, group_id: str) -> None:
        if group_id in self.group_p2p_map:
            return
        if self.pending_p2p is not None:
            return
        sg = self.store.get_group(group_id)
        if sg is None or sg.is_host or not sg.host_ip:
            self.rejoin_in_progress = False
            self.rejoin_failed = True
            self.rejoin_state_changed.emit()
            return
        join_id = self.store.get_setting(f"group_join_id_{group_id}", "")
        if not join_id:
            # the numeric id is the ONLY addressable join identifier (a host
            # resolves a handshake by id, never by name), so a group whose id
            # was not remembered cannot be rejoined automatically: fail with a
            # clear message instead of dialing with a name that gets rejected
            self.rejoin_in_progress = False
            self.rejoin_failed = True
            self.rejoin_state_changed.emit()
            self.status_message.emit("该群组缺少数字 ID，请重新查询加入")
            return
        p2p = P2PManager(
            self, port=self.port, device_id=self._device_id(), hardware_id=self._hardware_fingerprint()
        )
        self.call_manager.attach(p2p)
        self.group_call.attach(p2p)
        p2p.initialize_as_client(
            sg.my_name or "用户",
            sg.group_name,
            self.store.get_group_password(group_id) or None,
        )
        p2p.set_join_id(join_id)
        self._attach_admin_hooks(p2p)
        self.pending_p2p = p2p
        self.setup_p2p = p2p
        self.pending_host_ip = sg.host_ip
        self.pending_group_id = group_id
        self.rejoin_in_progress = True
        self.rejoin_failed = False
        self.rejoin_state_changed.emit()
        p2p.confirm_join(sg.host_ip, sg.host_port or network_module.TCP_PORT)

    def reconnect_active_group(self) -> None:
        gid = self.active_group_id
        if gid is None or self.pending_p2p is not None:
            return
        old = self.group_p2p_map.pop(gid, None)
        if old is not None:
            self.call_manager.end_if_on(old, "连接已断开")
            self.group_call.end_if_on(old, "连接已断开")
            old.stop()
        meta = self._find_group(gid)
        if meta is not None:
            meta.connected = False
        self.groups_changed.emit()
        self._rejoin_group(gid)

    def leave_active_group(self) -> None:
        gid = self.active_group_id
        if gid is None:
            return
        if self.pending_group_id == gid:
            self._stop_pending_p2p()
            self.setup_p2p = None
            self.pending_host_ip = ""
        meta = self._find_group(gid)
        p2p = self.group_p2p_map.pop(gid, None)
        if p2p is not None:
            self.call_manager.end_if_on(p2p, "通话已结束")
            self.group_call.end_if_on(p2p, "通话已结束")
            p2p.stop()
        self._teardown_group_mesh(gid)
        # Leaving stops this group's server; the group stays in the list and
        # re-hosts on the same port when re-entered.
        self.rejoin_in_progress = False
        self.rejoin_failed = False
        if meta is not None:
            meta.connected = False
        self.active_group_id = None
        self.active_group_name = ""
        self.active_my_name = ""
        self.active_is_host = False
        self.groups_changed.emit()
        self.active_group_changed.emit()
        self.active_peers_changed.emit()
        self.active_messages_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()

    def remove_group(self, group_id: str) -> None:
        if self.pending_group_id == group_id:
            self._stop_pending_p2p()
            self.setup_p2p = None
            self.pending_host_ip = ""
            self.rejoin_in_progress = False
            self.rejoin_failed = False
        meta = self._find_group(group_id)
        p2p = self.group_p2p_map.pop(group_id, None)
        if p2p is not None:
            self.call_manager.end_if_on(p2p, "通话已结束")
            self.group_call.end_if_on(p2p, "通话已结束")
            p2p.stop()
        self._teardown_group_mesh(group_id)
        self.removed_group_ids.add(group_id)
        self.persisted_message_ids.pop(group_id, None)
        self.replay_done.pop(group_id, None)
        self.persisted_peer_counts.pop(group_id, None)
        self.persisted_my_names.pop(group_id, None)
        self.groups = [g for g in self.groups if g.group_id != group_id]
        if self.active_group_id == group_id:
            self.active_group_id = None
            self.active_group_name = ""
            self.active_my_name = ""
            self.active_is_host = False
        self.store.delete_group(group_id)
        self.removed_group_ids.discard(group_id)
        self.groups_changed.emit()
        self.active_group_changed.emit()
        self.active_peers_changed.emit()
        self.active_messages_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()

    def send_message(
        self,
        content: str,
        reply_to: Optional[str] = None,
        reply_preview: Optional[str] = None,
        reply_sender: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        forwarded: Optional[ForwardedInfo] = None,
    ) -> bool:
        if not content.strip():
            return False
        gid = self.active_group_id
        if gid is None:
            return False
        p2p = self.group_p2p_map.get(gid)
        # messages can go out over the host relay OR the group mesh (host
        # offline) — either path suffices
        if p2p is None or (p2p.connection_lost and not self.mesh.has_links(gid)):
            return False
        msg = p2p.send_message(
            content,
            reply_to=reply_to,
            reply_preview=reply_preview,
            reply_sender=reply_sender,
            mentions=mentions,
            forwarded=forwarded,
        )
        if msg is not None:
            self.mesh.broadcast(gid, msg)
            # the message supersedes any "typing" we were showing
            self._end_active_typing(gid)
        return True

    def send_message_to_group(
        self, group_id: str, content: str, forwarded: Optional[ForwardedInfo] = None
    ) -> bool:
        if not content.strip():
            return False
        p2p = self.group_p2p_map.get(group_id)
        if p2p is None or (p2p.connection_lost and not self.mesh.has_links(group_id)):
            return False
        msg = p2p.send_message(content, forwarded=forwarded)
        if msg is not None:
            self.mesh.broadcast(group_id, msg)
            self._end_active_typing(group_id)
        return True

    def delete_message(self, message_id: str) -> None:
        gid = self.active_group_id
        if gid is None:
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            return
        if p2p.remove_message(message_id):
            # host-offline path: the relay may be unreachable, so also push
            # the delete over the group mesh so every member converges
            self.mesh.broadcast_delete(gid, message_id)
            self.store.delete_message(gid, message_id)
            # the delete cascades reactions/pins out of the store: refresh
            # the reaction cache and pinned banner instead of leaving a
            # banner pointing at a message that no longer exists
            self.extras_changed.emit(gid)

    # ------------------------------------------------------- message experience

    def _stage_pending_op(
        self,
        scope: str,
        kind: str,
        message_id: str,
        emoji: str = "",
        active: bool = False,
        content: str = "",
    ) -> None:
        """Park one offline message-experience op (edit/reaction/pin) in the
        durable log; replayed in order once the conversation is reachable
        again. Idempotent by (scope, kind, messageId, emoji) in the store."""
        try:
            with self._lock:
                self.store.stage_pending_op(
                    scope, kind, message_id, emoji=emoji, active=active, content=content
                )
        except Exception:
            logger.warning("stage pending op failed", exc_info=True)

    def _replay_pending_ops(self, group_id: str) -> None:
        """Replay a group's staged ops in staging order now that it is
        reachable. Each op is dropped after the attempt: applied+handed off,
        or permanently dead (target gone) — it must never loop forever."""
        p2p = self.group_p2p_map.get(group_id)
        if p2p is None:
            return
        if p2p.connection_lost and not self.mesh.has_links(group_id):
            return
        try:
            ops = self.store.get_pending_ops(group_id)
        except Exception:
            return
        for op in ops:
            ok = False
            try:
                if op.kind == "edit":
                    ok = p2p.edit_message(op.message_id, op.content)
                    if ok:
                        self.mesh.broadcast_edit(group_id, op.message_id, op.content)
                elif op.kind == "reaction":
                    ok = p2p.send_reaction(op.message_id, op.emoji, op.active)
                    if ok:
                        self.mesh.broadcast_reaction(
                            group_id, op.message_id, op.emoji, op.active
                        )
                elif op.kind == "pin":
                    ok = p2p.send_pin(op.message_id, op.active)
                    if ok:
                        self.mesh.broadcast_pin(group_id, op.message_id, op.active)
            except Exception:
                ok = False
            try:
                with self._lock:
                    self.store.delete_pending_op(op.op_id)
            except Exception:
                pass

    def edit_message(self, message_id: str, new_content: str) -> bool:
        """Edit one of OUR text messages in the active group: apply locally,
        relay, and mirror over the mesh (a member offline now converges via
        the mesh history push, whose merge accepts author-consistent
        rewrites). With no live path at all the edit is staged and replayed
        on reconnect."""
        gid = self.active_group_id
        if gid is None:
            return False
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            return False
        if not p2p.edit_message(message_id, new_content):
            return False
        self.mesh.broadcast_edit(gid, message_id, new_content)
        with self._lock:
            self.store.update_message_content(gid, message_id, new_content)
        if p2p.connection_lost and not self.mesh.has_links(gid):
            self._stage_pending_op(gid, "edit", message_id, content=new_content)
        return True

    def toggle_group_reaction(self, message_id: str, emoji: str, active: bool) -> bool:
        """Toggle OUR emoji reaction on a group message (relay + mesh mirror,
        both advisory). Local store follows immediately. An op that cannot be
        handed to any live path is staged and replayed on reconnect
        (idempotent: re-applying the same active state is a no-op)."""
        gid = self.active_group_id
        if gid is None:
            return False
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            return False

        def on_failed() -> None:
            self._stage_pending_op(
                gid, "reaction", message_id, emoji=emoji, active=active
            )

        sent = p2p.send_reaction(message_id, emoji, active, on_failed=on_failed)
        self.mesh.broadcast_reaction(gid, message_id, emoji, active)
        self._apply_reaction(gid, message_id, emoji, p2p.my_id, active)
        if not sent and not self.mesh.has_links(gid):
            on_failed()
        return sent or self.mesh.has_links(gid)

    def toggle_group_pin(self, message_id: str, active: bool) -> bool:
        """Pin/unpin a group message (any member; relay + mesh mirror). Same
        offline staging contract as toggle_group_reaction."""
        gid = self.active_group_id
        if gid is None:
            return False
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            return False

        def on_failed() -> None:
            self._stage_pending_op(gid, "pin", message_id, active=active)

        sent = p2p.send_pin(message_id, active, on_failed=on_failed)
        self.mesh.broadcast_pin(gid, message_id, active)
        self._apply_pin(gid, message_id, p2p.my_id, active)
        if not sent and not self.mesh.has_links(gid):
            on_failed()
        return sent or self.mesh.has_links(gid)

    def notify_group_read_receipt(self) -> None:
        """The open group chat shows the newest message: report the read
         receipt (relay + mesh). Deduped per group until the newest id
        changes; only ever sent for the OPEN group (that is what "read"
        means)."""
        gid = self.active_group_id
        if gid is None or not self.window_active:
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            return
        msgs = p2p.messages
        if not msgs:
            return
        up_to = msgs[-1].id
        with self._lock:
            if self._group_receipt_sent.get(gid) == up_to:
                return
            self._group_receipt_sent[gid] = up_to
        p2p.send_group_read_receipt(up_to)
        self.mesh.broadcast_read_receipt(gid, up_to)

    def toggle_direct_reaction(self, peer_id: str, message_id: str, emoji: str, active: bool) -> bool:
        """Toggle OUR emoji reaction in a direct chat (live session only)."""
        key = "direct:" + peer_id
        sent = self.direct.send_direct_reaction(peer_id, message_id, emoji, active)
        with self._lock:
            if active:
                self.store.add_reaction(key, message_id, emoji, self.direct.my_id_value)
            else:
                self.store.remove_reaction(key, message_id, emoji, self.direct.my_id_value)
        self.extras_changed.emit(key)
        return sent

    def toggle_direct_pin(self, peer_id: str, message_id: str, active: bool) -> bool:
        """Pin/unpin a direct-chat message (local + live session mirror)."""
        key = "direct:" + peer_id
        sent = self.direct.send_direct_pin(peer_id, message_id, active)
        with self._lock:
            self.store.set_message_pinned(
                key, message_id, active, pinned_by=self.direct.my_id_value
            )
        self.extras_changed.emit(key)
        return sent or not self.direct.is_chat_alive(peer_id)

    def edit_direct_message(self, peer_id: str, message_id: str, new_content: str) -> bool:
        """Edit one of OUR direct-chat messages (live session only, Android
        parity: when the peer is offline NOTHING changes — no local rewrite,
        no staged replay — so the local copy and the peer's copy can never
        disagree). The local store follows only an accepted edit."""
        sent = self.direct.edit_message(peer_id, message_id, new_content)
        if sent:
            with self._lock:
                self.store.update_message_content(
                    "direct:" + peer_id, message_id, new_content
                )
        return sent

    # ------------------------------------------------- message experience read model

    def reactions_for(self, conversation_key: str) -> dict:
        """{msg_id: [(emoji, actor_id), …]} of one conversation (UI render)."""
        with self._lock:
            return self.store.get_reactions(conversation_key)

    def pinned_for(self, conversation_key: str) -> list:
        """[(msg_id, pinned_at, pinned_by)] oldest first (banner = last)."""
        with self._lock:
            return self.store.get_pinned_messages(conversation_key)

    def group_readers_for(self, conversation_key: str) -> dict:
        """{msg_id: [reader_id, …]} recorded group read receipts."""
        with self._lock:
            return self.store.get_group_readers(conversation_key)

    @property
    def my_device_id(self) -> str:
        """This device's stable id (mention matching, reaction ownership)."""
        return self.direct.my_id_value

    def send_file(self, path: str) -> bool:
        """Offer a local file to the active group. The offer reaches members
        over the host relay AND the mesh; both paths dedup by message id.
        Sending depends only on the SENDER being online — a live mesh link is
        enough, so the host going offline never blocks it."""
        if not path:
            return False
        gid = self.active_group_id
        if gid is None:
            return False
        p2p = self.group_p2p_map.get(gid)
        if p2p is None or (p2p.connection_lost and not self.mesh.has_links(gid)):
            return False
        msg = p2p.send_file(path)
        if msg is None:
            return False
        # p2p.send_file relays the offer to the group when the host is up; the
        # mesh delivers it to every linked member either way (receivers dedup
        # by message id)
        self.mesh.broadcast(gid, msg)
        self._mirror_own_media(msg, path)
        return True

    # ------------------------------------------------- group file share (群文件)

    def group_files(self, group_id: Optional[str] = None) -> List[SavedGroupFile]:
        """The group's shared-file index (newest first) for the UI/history
        providers. Tombstoned entries are already filtered out."""
        gid = self.active_group_id if group_id is None else group_id
        if not gid:
            return []
        return self.store.get_group_files(gid)

    def can_remove_shared_file(self, entry: SavedGroupFile) -> bool:
        """True when THIS device may remove [entry] (uploader or group owner);
        the UI hides the action otherwise."""
        return network_module.can_remove_group_file(
            self.my_device_id, entry.sender_id, self._group_creator_id(entry.group_id)
        )

    def _restore_shared_files(self) -> None:
        """Startup: re-register the share-area files this device uploaded so
        they stay downloadable after a restart, and refresh their advertised
        address/port (the shared listener port and the local IP can change)."""
        try:
            entries = self.store.get_local_group_files()
        except Exception:
            return
        if not entries:
            return
        host = get_local_ip_address()
        for entry in entries:
            path = entry.local_path
            if not path or not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                self.host_server.register_shared_file(
                    entry.file_id, path, size, entry.file_key
                )
            except Exception:
                continue
            if host and (entry.download_host != host or entry.download_port != self.port):
                try:
                    with self._lock:
                        self.store.upsert_group_file(
                            replace(
                                entry,
                                size=size,
                                download_host=host,
                                download_port=self.port,
                            )
                        )
                except Exception:
                    pass

    def share_group_file(self, path: str) -> bool:
        """Share a local file into the active group's share area (群文件).
        The bytes are served from the shared listener (stable port, unlike the
        ephemeral chat-file servers), so an index entry stays downloadable
        while this app runs. Returns False when offline or the file is
        unusable."""
        gid = self.active_group_id
        if gid is None or not path or not os.path.isfile(path):
            return False
        p2p = self.group_p2p_map.get(gid)
        if p2p is None or (p2p.connection_lost and not self.mesh.has_links(gid)):
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size <= 0 or size > network_module.MAX_DOWNLOAD_BYTES:
            return False
        name = sanitize_file_name(os.path.basename(path))
        if not name:
            return False
        file_id = str(uuid.uuid4())
        file_key = to_b64(random_bytes(KEY_LEN))
        host = get_local_ip_address() or getattr(p2p, "my_ip_address", "") or ""
        try:
            self.host_server.register_shared_file(file_id, path, size, file_key)
        except Exception:
            return False
        ts = int(time.time() * 1000)
        with self._lock:
            self.store.upsert_group_file(
                SavedGroupFile(
                    group_id=gid,
                    file_id=file_id,
                    name=name,
                    size=size,
                    sender_id=self.my_device_id,
                    sender_name=self.nickname,
                    ts=ts,
                    download_host=host,
                    download_port=self.port,
                    file_key=file_key,
                    local_path=path,
                )
            )
        packet = NetworkPacket(
            type="group_file_add",
            group_id=gid,
            file_id=file_id,
            name=name,
            size=size,
            sender_id=self.my_device_id,
            sender_name=self.nickname,
            ts=ts,
            file_info=FileInfo(
                file_id,
                name,
                size,
                host,
                self.port,
                file_key,
            ),
        )
        # dual delivery like a chat send: host relay + mesh (receivers dedup
        # by fileId and the mesh layer binds senderId to the link identity)
        p2p.send_group_packet(packet)
        self.mesh.broadcast_admin(gid, packet)
        self.group_files_changed.emit(gid)
        return True

    def remove_group_file(self, file_id: str) -> bool:
        """Remove one entry from the active group's share area. Authorized for
        the uploader or the group owner; the tombstone is persisted so an
        offline member converges instead of resurrecting the entry."""
        gid = self.active_group_id
        if gid is None:
            return False
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            return False
        with self._lock:
            entry = self.store.get_group_file(gid, file_id)
        if entry is None or not self.can_remove_shared_file(entry):
            return False
        packet = NetworkPacket(
            type="group_file_remove",
            group_id=gid,
            file_id=file_id,
            sender_id=self.my_device_id,
            ts=int(time.time() * 1000),
        )
        p2p.send_group_packet(packet)
        self.mesh.broadcast_admin(gid, packet)
        with self._lock:
            self.store.record_removed_group_files(gid, [file_id])
        if entry.local_path:
            try:
                self.host_server.unregister_shared_file(file_id)
            except Exception:
                pass
        self.group_files_changed.emit(gid)
        return True

    def download_group_file(self, file_id: str, target_path: str) -> None:
        """Download one shared file of the active group to [target_path] on a
        worker thread; file_download_finished(file_id, ok, message) fires on
        completion and file_progress reports bytes (same contract as
        download_file)."""
        gid = self.active_group_id
        if gid is None:
            self.file_download_finished.emit(file_id, False, "未连接到群组")
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            self.file_download_finished.emit(file_id, False, "未连接到群组")
            return
        with self._lock:
            entry = self.store.get_group_file(gid, file_id)
        if entry is None:
            self.file_download_finished.emit(file_id, False, "文件不存在")
            return
        if not entry.download_host or entry.download_port <= 0:
            self.file_download_finished.emit(
                file_id, False, "文件已过期，请上传者重新分享"
            )
            return
        file_info = FileInfo(
            entry.file_id,
            entry.name,
            entry.size,
            entry.download_host,
            entry.download_port,
            entry.file_key,
        )
        try:
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        except OSError:
            self.file_download_finished.emit(file_id, False, "无法创建保存目录")
            return
        offset = self._resume_offset(file_id, target_path)
        event, socks = self._register_download((gid, file_id))
        progress = self._make_file_progress(file_id)

        def run() -> None:
            self._sync_resume(file_info, target_path, keep_empty=True)
            ok, message = p2p.download_file(
                file_info,
                target_path,
                progress=progress,
                cancel=event,
                sock_holder=socks,
                offset=offset,
            )
            self._sync_resume(file_info, target_path)
            self._finish_download((gid, file_id))
            self.file_download_finished.emit(file_id, ok, message)

        threading.Thread(target=run, daemon=True).start()

    def send_folder(self, path: str) -> None:
        """Offer a folder to the active group as one file_message per entry
        (each carrying folder_id/folder_name/relative_path/folder_total) so
        receivers can group them and rebuild the tree. Old peers still see
        ordinary file offers. The walk + offers run on a worker thread so a
        large folder never blocks the caller; folder_send_finished reports the
        outcome."""
        gid = self.active_group_id
        p2p = self.group_p2p_map.get(gid) if gid is not None else None
        if p2p is None or (p2p.connection_lost and not self.mesh.has_links(gid)):
            self.folder_send_finished.emit(False)
            return
        threading.Thread(
            target=self._send_group_folder_worker, args=(gid, p2p, path), daemon=True
        ).start()

    def _send_group_folder_worker(self, gid: str, p2p, path: str) -> None:
        entries, folder_name, truncated = self._collect_folder_entries(path)
        if not entries:
            self.folder_send_finished.emit(False)
            return
        if truncated:
            self.folder_send_truncated.emit(folder_name)
        folder_id = str(uuid.uuid4())
        sent = 0
        for relative_path, abs_path in entries:
            msg = p2p.send_file(
                abs_path,
                folder_id=folder_id,
                folder_name=folder_name,
                relative_path=relative_path,
                folder_total=len(entries),
            )
            if msg is None:
                continue
            # same dual delivery as a single file: relay + mesh, dedup by id
            self.mesh.broadcast(gid, msg)
            sent += 1
        self.folder_send_finished.emit(sent > 0)

    def download_file(self, file_id: str, target_path: str) -> None:
        """Download a file offer by file_id to target_path on a worker thread;
        file_download_finished(file_id, ok, message) fires on completion. A
        paused download resumes from its staged offset; after an app restart
        the address and per-file key come from the persisted resume entry."""
        gid = self.active_group_id
        if gid is None:
            self.file_download_finished.emit(file_id, False, "未连接到群组")
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            self.file_download_finished.emit(file_id, False, "未连接到群组")
            return
        msg = next((m for m in p2p.messages if m.id == file_id), None)
        if msg is None or msg.file_info is None:
            self.file_download_finished.emit(file_id, False, "文件消息不存在")
            return
        file_info = self._merged_offer(msg.file_info)
        if not file_info.download_host or file_info.download_port <= 0:
            self.file_download_finished.emit(file_id, False, "文件已过期，请对方重新发送")
            return
        # media targets live in the app media dir; ensure it exists before the
        # worker thread opens the output file
        try:
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        except OSError:
            self.file_download_finished.emit(file_id, False, "无法创建保存目录")
            return
        offset = self._resume_offset(file_id, target_path)
        # cancel entry + throttled progress for the worker thread
        event, socks = self._register_download((gid, file_id))
        progress = self._make_file_progress(file_id)

        def run() -> None:
            # remember the target/address before the first byte: a process
            # death mid-transfer then still leaves a resumable entry
            self._sync_resume(file_info, target_path, keep_empty=True)
            ok, message = p2p.download_file(
                file_info,
                target_path,
                progress=progress,
                cancel=event,
                sock_holder=socks,
                offset=offset,
            )
            self._sync_resume(file_info, target_path)
            self._finish_download((gid, file_id))
            self.file_download_finished.emit(file_id, ok, message)

        threading.Thread(target=run, daemon=True).start()

    def download_folder(self, folder_id: str, dest_dir: str) -> None:
        """Save every entry of a group folder offer under dest_dir/<folderName>/
        (or dest_dir when the sender gave no name). One worker thread downloads
        the entries sequentially, each reusing the hardened single-file
        download; folder_progress / folder_download_finished report progress
        and the outcome."""
        gid = self.active_group_id
        if gid is None:
            self.folder_download_finished.emit(folder_id, False, "未连接到群组")
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None:
            self.folder_download_finished.emit(folder_id, False, "未连接到群组")
            return
        self._start_folder_download(
            p2p.download_file, list(p2p.messages), folder_id, dest_dir, (gid, folder_id)
        )

    def download_direct_folder(self, peer_id: str, folder_id: str, dest_dir: str) -> None:
        """Save every entry of a direct-chat folder offer (see download_folder)."""
        self._start_folder_download(
            self.direct.download_file,
            list(self.direct.messages_for(peer_id)),
            folder_id,
            dest_dir,
            (peer_id, folder_id),
        )

    def _start_folder_download(self, downloader, messages, folder_id, dest_dir, key) -> None:
        threading.Thread(
            target=self._run_folder_download,
            args=(downloader, messages, folder_id, dest_dir, key),
            daemon=True,
        ).start()

    def _run_folder_download(self, downloader, messages, folder_id, dest_dir, key) -> None:
        entries = []
        seen = set()
        for m in messages:
            fi = m.file_info
            if fi is None or fi.folder_id != folder_id or m.id in seen:
                continue
            seen.add(m.id)
            entries.append(m)
        if not entries:
            self.folder_download_finished.emit(folder_id, False, "文件夹消息不存在")
            return
        entries.sort(key=lambda m: (m.file_info.relative_path or m.file_info.file_name))
        root_name = next(
            (m.file_info.folder_name for m in entries if m.file_info.folder_name), ""
        )
        root = os.path.join(dest_dir, sanitize_file_name(root_name)) if root_name else dest_dir
        root_abs = os.path.abspath(root)
        total = len(entries)
        # a FRESH run counts completions from zero; a resume run (pending
        # entries for this folder exist) keeps the persisted count — its
        # already-saved files were counted by the run that saved them
        resuming = bool(self._resume_store.folder_entries(folder_id))
        if not resuming:
            self._resume_store.reset_folder_done(folder_id)
        event, socks = self._register_download(key)
        ok_all = True
        message = ""
        for index, m in enumerate(entries):
            if event.is_set():
                ok_all = False
                message = "下载已取消"
                break
            fi = self._merged_offer(m.file_info)
            rel = fi.relative_path or fi.file_name
            target = os.path.abspath(os.path.join(root_abs, *rel.split("/")))
            try:
                # defense in depth: relative_path is already sanitized, but a
                # crafted path must never write outside the chosen directory
                if os.path.commonpath([root_abs, target]) != root_abs:
                    ok_all = False
                    message = "无效的文件路径"
                    continue
            except ValueError:
                ok_all = False
                message = "无效的文件路径"
                continue
            # a resumed folder run skips entries that already landed complete
            try:
                complete = (
                    fi.file_size > 0
                    and os.path.isfile(target)
                    and os.path.getsize(target) == fi.file_size
                )
            except OSError:
                complete = False
            if complete:
                self._resume_store.remove(m.id)
                if not resuming:
                    # a fresh run found the file already on disk: count it
                    # (a resume run counted it in the run that saved it)
                    self._resume_store.bump_folder_done(folder_id)
                self.folder_progress.emit(folder_id, index + 1, total)
                continue
            if not fi.download_host or fi.download_port <= 0:
                ok_all = False
                message = "部分文件已过期，请对方重新发送"
                break
            try:
                os.makedirs(os.path.dirname(target) or root_abs, exist_ok=True)
            except OSError:
                ok_all = False
                message = "无法创建保存目录"
                continue
            offset = self._resume_offset(m.id, target)
            progress = self._make_file_progress(m.id)
            # record before the first byte: a process death mid-entry still
            # leaves a resumable state for this folder run
            self._sync_resume(
                fi,
                target,
                folder_id=folder_id,
                folder_total=total,
                dest_dir=dest_dir,
                keep_empty=True,
            )
            ok, entry_message = downloader(
                fi,
                target,
                progress=progress,
                cancel=event,
                sock_holder=socks,
                offset=offset,
            )
            # keep/clear the per-entry resume state ("<target>.part")
            self._sync_resume(
                fi,
                target,
                folder_id=folder_id,
                folder_total=total,
                dest_dir=dest_dir,
            )
            if not ok:
                ok_all = False
                message = entry_message or "下载失败"
                break
            self._resume_store.bump_folder_done(folder_id)
            self.folder_progress.emit(folder_id, index + 1, total)
        self._finish_download(key)
        self.folder_download_finished.emit(folder_id, ok_all, root_abs if ok_all else message)

    # ------------------------------------------------- media (image / video)

    @property
    def media_dir(self) -> str:
        """App-media directory for downloaded image/video messages. Media is
        stored here (NOT wherever the user points a save dialog) so the
        conversation can render it inline, including after a restart."""
        return os.path.join(self.data_dir, "media")

    def media_target_path(self, file_id: str, file_name: str) -> str:
        """Deterministic download target for one media message: keyed by the
        file id so same-named files from different messages never collide,
        with the sanitized display name kept for readability. Pure path
        math (called from paint-time probes too); the download entry points
        create the directory."""
        safe = sanitize_file_name(file_name or "")
        return os.path.join(self.media_dir, f"{file_id}_{safe}")

    def downloaded_media_path(self, file_id: str, file_name: str) -> Optional[str]:
        """Path of an already-downloaded media copy, or None. The UI uses this
        to render image/video messages inline after a process restart."""
        path = self.media_target_path(file_id, file_name)
        return path if os.path.isfile(path) else None

    # ------------------------------------------------------------- video call

    def start_call(self, peer_id: str, media: str = MEDIA_VIDEO) -> None:
        """Start a video/audio call with a member of the active group.
        [media] is "video" (default) or "audio"."""
        gid = self.active_group_id
        if gid is None:
            self.status_message.emit("请先进入一个群组")
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None or p2p.connection_lost:
            self.status_message.emit("未连接到群组，无法发起通话")
            return
        if self.group_call.state != "idle":
            self.status_message.emit("语音会议进行中，请先挂断会议")
            return
        self.call_manager.start_call(p2p, peer_id, media=media)

    def accept_call(self) -> None:
        self.call_manager.accept_call()

    def reject_call(self) -> None:
        self.call_manager.reject_call()

    def hangup_call(self) -> None:
        self.call_manager.hangup()

    def toggle_audio_muted(self, muted: bool) -> None:
        self.call_manager.set_audio_muted(muted)

    def toggle_video_muted(self, muted: bool) -> None:
        self.call_manager.set_video_muted(muted)

    # -------------------------------------------------- group voice meeting

    def start_group_call(self) -> None:
        """Start a group voice conference in the active group (this device
        becomes the meeting host / mixer)."""
        gid = self.active_group_id
        if gid is None:
            self.status_message.emit("请先进入一个群组")
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None or p2p.connection_lost:
            self.status_message.emit("未连接到群组，无法发起语音会议")
            return
        self.group_call.start_meeting(p2p)

    def accept_group_call(self) -> None:
        self.group_call.accept_invite()

    def decline_group_call(self) -> None:
        self.group_call.decline_invite()

    def hangup_group_call(self) -> None:
        self.group_call.leave_meeting()

    def toggle_group_call_muted(self, muted: bool) -> None:
        self.group_call.set_audio_muted(muted)

    def clear_unread(self, group_id: str) -> None:
        meta = self._find_group(group_id)
        if meta is not None and meta.unread_count > 0:
            meta.unread_count = 0
            self.groups_changed.emit()

    def _clear_unread(self, group_id: str) -> None:
        meta = self._find_group(group_id)
        if meta is not None and meta.unread_count > 0:
            meta.unread_count = 0

    def _set_active(self, group_id: str, p2p: P2PManager) -> None:
        self.active_group_id = group_id
        self.active_group_name = p2p.current_group_name
        self.active_is_host = p2p.is_host
        self.active_my_name = p2p.my_name
        self.active_group_password = (
            self.store.get_group_password(group_id) if p2p.is_host else ""
        )
        meta = self._find_group(group_id)
        if meta is not None:
            meta.connected = not p2p.connection_lost
        self.groups_changed.emit()
        self.active_group_changed.emit()
        self.active_peers_changed.emit()
        self.active_messages_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()

    def _set_active_meta(self, group_id: str, meta: GroupMeta) -> None:
        self.active_group_id = group_id
        self.active_group_name = meta.group_name
        self.active_is_host = meta.is_host
        self.active_my_name = meta.my_name or "用户"
        self.active_group_password = ""
        self.groups_changed.emit()
        self.active_group_changed.emit()
        self.active_peers_changed.emit()
        self.active_messages_changed.emit()
        self.active_connection_lost_changed.emit()
        self.active_server_error_changed.emit()

    def _persist_group(self, group_id: str, p2p: P2PManager) -> None:
        meta = self._find_group(group_id)
        self.store.upsert_group(
            SavedGroup(
                group_id=group_id,
                group_name=p2p.current_group_name,
                is_host=p2p.is_host,
                host_ip=self.pending_host_ip if not p2p.is_host else get_local_ip_address(),
                host_port=p2p.port if p2p.is_host else (self.pending_host_port or 0),
                my_name=p2p.my_name,
                member_count=meta.member_count if meta else 1,
                last_message=meta.last_message if meta else "",
                last_message_time=meta.last_message_time if meta else 0,
            )
        )

    def _persist_peer_count(self, group_id: str, p2p: P2PManager, count: int) -> None:
        if self.persisted_peer_counts.get(group_id) == count:
            return
        self.persisted_peer_counts[group_id] = count
        meta = self._find_group(group_id)
        self.store.upsert_group(
            SavedGroup(
                group_id=group_id,
                group_name=p2p.current_group_name,
                is_host=p2p.is_host,
                host_port=p2p.port if p2p.is_host else (meta.host_port if meta else 0),
                member_count=count,
                last_message=meta.last_message if meta else "",
                last_message_time=meta.last_message_time if meta else 0,
            )
        )

    def _persist_last_message(self, group_id: str, p2p: P2PManager) -> None:
        meta = self._find_group(group_id)
        self.store.upsert_group(
            SavedGroup(
                group_id=group_id,
                group_name=p2p.current_group_name,
                is_host=p2p.is_host,
                host_port=p2p.port if p2p.is_host else (meta.host_port if meta else 0),
                member_count=meta.member_count if meta else 1,
                last_message=meta.last_message if meta else "",
                last_message_time=meta.last_message_time if meta else 0,
            )
        )

    def peers_changed(self, p2p: P2PManager) -> None:
        gid = p2p.current_group_id
        if not gid:
            return
        # every member seen in a group becomes a contact: the member list is
        # the universal address book for direct chats
        for peer in p2p.peers.values():
            self.direct.add_contact(peer)
        # keep the group mesh in sync and persist peers so links survive the
        # host going offline. Losing the host clears the peer map; syncing
        # that emptiness would tear down the mesh and wipe the persisted peer
        # list exactly when the members need them most (host-offline
        # chatting) — keep the last known members while disconnected (Android
        # parity).
        if not p2p.is_host and not (p2p.connection_lost and not p2p.peers):
            self.mesh.sync_peers(gid, list(p2p.peers.values()))
            self._save_group_peers(gid, list(p2p.peers.values()))
        with self._lock:
            count = len(p2p.peers) + 1
            meta = self._find_group(gid)
            if meta is not None:
                meta.member_count = count
            self._persist_peer_count(gid, p2p, count)
        self.groups_changed.emit()
        if gid == self.active_group_id:
            self.active_peers_changed.emit()

    def messages_changed(self, p2p: P2PManager) -> None:
        gid = p2p.current_group_id
        if not gid:
            return
        with self._lock:
            msgs = list(p2p.messages)
            last = msgs[-1] if msgs else None
            meta = self._find_group(gid)
            if meta is not None:
                meta.last_message = last.content if last else ""
                meta.last_message_time = last.timestamp if last else 0
            persisted = self.persisted_message_ids.setdefault(gid, set())
            if (
                persisted
                and self.group_p2p_map.get(gid) is p2p
                and self.replay_done.get(gid) is p2p
            ):
                # mirror deletes relayed from the group into the database —
                # but only from the CURRENT manager whose saved-history replay
                # finished: an old connection's in-flight callback (its message
                # list is missing rows the fresh instance has not loaded yet)
                # must neither mirror-delete rows nor mint delete tombstones
                # for peers. Every mirrored delete also becomes a tombstone so
                # offline members converge instead of resurrecting it.
                current_ids = {m.id for m in msgs}
                removed_ids = persisted - current_ids
                if removed_ids:
                    persisted.difference_update(removed_ids)
                    for mid in removed_ids:
                        self.store.delete_message(gid, mid)
                    self.store.record_deleted_messages(gid, list(removed_ids))
            new_messages = [m for m in msgs if m.id not in persisted]
            if new_messages:
                # Keep the mesh history state complete even when the message
                # arrived over the host relay: later mesh backfills must include
                # it for members that were offline at send time.
                for m in new_messages:
                    self.mesh.note_message(gid, m)
                incoming = [m for m in new_messages if not m.is_from_me]
                if incoming and gid != self.active_group_id:
                    # unread counts only accrue off the open group
                    if meta is not None:
                        meta.unread_count += len(incoming)
                if incoming and not self.window_active:
                    # window hidden/minimized: notify for EVERY group,
                    # including the one currently open -- the user cannot see
                    # the open chat (Android parity: notifyNewMessages runs
                    # for the active group when the app is not foreground).
                    # send to the aggregator; it emits the merged bubble
                    self.raw_tray.emit(gid, new_messages[0].sender_name, incoming[-1].content)
                persisted.update(m.id for m in new_messages)
                self.store.insert_messages([to_saved_message(gid, m) for m in new_messages])
                if last is not None:
                    self._persist_last_message(gid, p2p)
        self.groups_changed.emit()
        if gid == self.active_group_id:
            self.active_messages_changed.emit()

    def connection_lost(self, p2p: P2PManager) -> None:
        gid = p2p.current_group_id
        # a call riding this connection cannot continue without signaling
        self.call_manager.end_if_on(p2p, "连接已断开")
        self.group_call.end_if_on(p2p, "连接已断开")
        if gid:
            with self._lock:
                meta = self._find_group(gid)
                if meta is not None:
                    meta.connected = False
            self.groups_changed.emit()
        if gid == self.active_group_id:
            self.active_connection_lost_changed.emit()
            self.active_peers_changed.emit()

    def server_error(self, p2p: P2PManager, message: str) -> None:
        """Shared host-server state changes (message=None means the listener
        is back up). The group's P2PManager is never stopped — with the
        program-wide server, retry only rebinds the shared listener."""
        gid = p2p.current_group_id
        with self._lock:
            is_host_group = bool(
                gid and p2p.is_host and self.group_p2p_map.get(gid) is p2p
            )
            if is_host_group:
                meta = self._find_group(gid)
                if meta is not None:
                    meta.connected = message is None
        if is_host_group:
            self.groups_changed.emit()
            self.active_connection_lost_changed.emit()
            self.active_server_error_changed.emit()
            if message is None:
                self.status_message.emit("已重新开始监听")
            else:
                self.status_message.emit(message)
        elif p2p is self._active_p2p():
            self.active_server_error_changed.emit()

    def retry_host_listening(self) -> None:
        """Rebind the shared host server after a bind failure (e.g. the port
        became free again). Groups stay registered; no rebuild needed."""
        self.host_server.restart()
        self.status_message.emit("已重新开始监听")

    def query_result_changed(self, p2p: P2PManager) -> None:
        self.query_state_changed.emit()

    def join_state_changed(self, p2p: P2PManager) -> None:
        result = p2p.connection_result
        if result is not None and p2p is self.pending_p2p:
            success, message = result
            if success:
                self.rejoin_in_progress = False
                self.rejoin_failed = False
                rejoined_group_id = self.pending_group_id
                gid = p2p.current_group_id
                # When the join went through a member sponsor, the ack
                # revealed the real host: persist THAT address (not the
                # sponsor the user typed), so a later rejoin connects to the
                # host and never fails just because the sponsor is offline
                # (Android parity).
                host_peer = p2p.connected_host
                if host_peer is not None:
                    host_ip = host_peer.ip_address
                    host_port = host_peer.port
                else:
                    host_ip = self.pending_host_ip
                    host_port = self.pending_host_port or 0
                # Remember the creator (group owner) device id so incoming
                # group_update / kick_member packets can be validated against
                # it. A member-sponsored join reveals the host in the ack; a
                # direct join has the host at the address we dialed; the query
                # response carries creatorId when it was answered by the host.
                creator_id = ""
                if host_peer is not None and host_peer.id:
                    creator_id = host_peer.id
                else:
                    info = p2p.queried_group_info
                    if info is not None and info.creator_id:
                        creator_id = info.creator_id
                    else:
                        dial_port = self.pending_host_port or network_module.TCP_PORT
                        for peer in p2p.peers.values():
                            if (
                                peer.ip_address == self.pending_host_ip
                                and peer.port == dial_port
                            ):
                                creator_id = peer.id
                                break
                if creator_id:
                    self.store.set_setting(f"group_creator_id_{gid}", creator_id)
                self.pending_p2p = None
                self.pending_group_id = None
                self.group_p2p_map[gid] = p2p
                meta = self._find_group(gid)
                if meta is None:
                    meta = GroupMeta(
                        group_id=gid,
                        group_name=p2p.current_group_name,
                        is_host=False,
                        host_ip=host_ip,
                        host_port=host_port,
                        my_name=p2p.my_name,
                        connected=True,
                        announcement=p2p.group_announcement,
                    )
                    self.groups.insert(0, meta)
                else:
                    meta.is_host = False
                    meta.connected = True
                    meta.host_ip = host_ip
                    meta.host_port = host_port
                    meta.my_name = p2p.my_name
                    meta.announcement = p2p.group_announcement
                self.store.upsert_group(
                    SavedGroup(
                        group_id=gid,
                        group_name=p2p.current_group_name,
                        is_host=False,
                        host_ip=host_ip,
                        host_port=host_port,
                        my_name=p2p.my_name,
                        member_count=len(p2p.peers) + 1,
                        announcement=p2p.group_announcement,
                    )
                )
                if p2p.group_password:
                    self.store.set_group_password(gid, p2p.group_password)
                if p2p.join_id:
                    self.store.set_setting(f"group_join_id_{gid}", p2p.join_id)
                self._load_and_replay_messages(gid, p2p)
                # Enter the mesh AFTER replaying persisted history so the mesh
                # state seeds with the full local history and can backfill it
                # to members that come online later.
                self._setup_group_mesh(gid, p2p)
                # the join is the (re)connection point: flush ops staged while
                # the group was unreachable
                self._replay_pending_ops(gid)
                self.setup_p2p = None
                self.groups_changed.emit()
                self.status_message.emit("已成功加入群组")
                self._set_active(gid, p2p)
                if rejoined_group_id is None:
                    self.join_successful.emit()
                self.rejoin_state_changed.emit()
            else:
                if self.rejoin_in_progress:
                    self.rejoin_in_progress = False
                    self.rejoin_failed = True
                    self.rejoin_state_changed.emit()
                    self.status_message.emit(f"连接失败: {message}")
                self._stop_pending_p2p()
        self.join_ui_state_changed.emit()

    def shutdown(self) -> None:
        self._shutdown_done = True
        self.call_manager.hangup()
        self.group_call.leave_meeting()
        if self.pending_p2p is not None:
            self.pending_p2p.stop()
        for p2p in self.group_p2p_map.values():
            p2p.stop()
        self.group_p2p_map.clear()
        self.direct.shutdown()
        self.mesh.shutdown()
        self.host_server.shutdown()
        # stop every timer this ViewModel owns: a ViewModel that outlives its
        # window (a test teardown that drops it, a future leak) must never
        # have its slots fire from a later event pump in the same process
        self._typing_timer.stop()
        for state in self._typing_out.values():
            state["timer"].stop()
        self._typing_out.clear()
        self._direct_typing.clear()
        self._group_typing.clear()
        self._tray_timer.stop()
        self._tray_accum = None
        try:
            self.store.close()
        except Exception:
            pass
