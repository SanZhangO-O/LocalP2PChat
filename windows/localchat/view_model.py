import json
import re
import socket
import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .call import CallManager
from .crypto import random_password
from .hardware import get_hardware_id, get_local_ip_address
from .models import (
    TCP_PORT,
    ChatMessage,
    ContactRequest,
    FileInfo,
    GroupInfo,
    NetworkPacket,
    Peer,
)
from . import network as network_module
from .network import DirectChatListener, DirectChatManager, P2PListener, P2PManager, Protocol
from .securewire import DeviceIdentity
from .storage import ChatStore, SavedGroup, to_saved_message

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
    # Emitted from network threads with (group_id, sender_name, body); the
    # aggregator slot runs on the main thread (queued connection).
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

    # Burst window in ms: incoming notifications within this span are merged
    # into a single tray bubble instead of one popup per message.
    TRAY_AGGREGATE_MS = 2000

    def __init__(self, store: ChatStore, data_dir: str = "."):
        super().__init__()
        self.store = store
        self._lock = threading.RLock()

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

        # Direct member chats: members are first-class — the shared listener
        # must be reachable even with no host group, and the identity must
        # match the group identity so contacts unify.
        self.direct = network_module.DirectChatManager()
        self.direct.attach(self)
        self.host_server.direct_manager = self.direct
        self.host_server.ensure_running()
        self._direct_persisted_ids: Dict[str, set] = {}
        self._direct_pending: Dict[str, Dict[str, bool]] = {}
        self._direct_last: Dict[str, Optional[ChatMessage]] = {}

        # Group mesh: member-to-member links so chat survives the host going
        # offline, plus history backfill on connect.
        self.mesh = network_module.GroupMeshManager()
        self.mesh.attach(self)
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
            if not candidate.is_host and (
                candidate.join_id == group_id or candidate.group_name == group_id
            ):
                return self.store.get_setting(
                    f"group_password_{candidate.current_group_id}", ""
                )
        return None

    def _on_raw_tray(self, gid: str, sender_name: str, body: str) -> None:
        """Aggregate raw tray notifications arriving within the burst window.
        Runs on the main thread via the queued signal connection, so the
        QTimer is only ever touched from the main thread."""
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

    def direct_last_message(self, peer_id: str) -> Optional[ChatMessage]:
        """Last message of a direct chat, for the home-page preview."""
        return self._direct_last.get(peer_id)

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
            )
            for m in saved
        ]
        self.direct.seed_messages(peer_id, msgs)
        self._direct_persisted_ids[peer_id] = {m.id for m in saved}
        self._direct_last[peer_id] = msgs[-1] if msgs else None

    @staticmethod
    def _restored_file_info(m) -> Optional[FileInfo]:
        """Rebuild a FileInfo from a persisted row. Every restored offer is
        shown as expired: the download address was captured in a previous
        session, and the sender's download server dies with its process — once
        EITHER side restarted, a stale address only produces a failed
        download. The bubble renders it as "已过期" instead of offering a
        download that can no longer succeed (Android parity)."""
        if m.file_size <= 0 and not m.download_host:
            return None
        # blank the address for every restored offer, own or received
        return FileInfo(m.id, m.content, m.file_size, "", 0)

    def send_direct_message(self, peer_id: str, content: str) -> bool:
        # peers are first-class: a message may queue as pending while the peer
        # is offline and deliver automatically once it comes online (Android
        # parity). False only when there is no contact and no session.
        return self.direct.send_message(peer_id, content)

    def delete_direct_message(self, peer_id: str, message_id: str, sender_id: str) -> None:
        self.direct.delete_message(peer_id, message_id, sender_id)

    def add_direct_contact(self, ip_port: str, name: str) -> bool:
        ip, parsed_port = self._parse_host_port(ip_port)
        # validate: a syntactically broken endpoint (mangled IP, bad port)
        # used to be accepted silently — the member row then appeared in the
        # list but could never connect, which looks like "adding by IP has
        # no effect". Reject it here; the dialog surfaces 地址无效.
        if not ip or not self._is_valid_host(ip) or not 1 <= parsed_port <= 65535:
            return False
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

    def start_direct_call(self, peer_id: str) -> None:
        """Start a video call with a direct-chat member: signaling rides the
        direct session socket, media over the usual TCP connection."""
        contact = next(
            (c for c in self.direct.contacts_list() if c.id == peer_id), None
        )
        if contact is None:
            self.status_message.emit("无法发起通话：成员不在线")
            return
        if not self.direct.is_chat_alive(peer_id):
            self.status_message.emit("未连接到该成员，无法发起通话")
            return
        self.call_manager.start_direct_call(
            channel_send=self._direct_channel(),
            identity=self.direct,
            peer=contact,
            my_id=self.direct.my_id_value,
            my_name=self.direct.my_name_value,
        )

    def send_direct_file(self, peer_id: str, path: str) -> bool:
        """Offer a local file over a direct session (download server + shared
        file_message protocol). Returns False when it cannot be served."""
        return self.direct.send_file(peer_id, path) is not None

    def download_direct_file(self, peer_id: str, file_id: str, target_path: str) -> None:
        """Download a direct-chat file offer by file_id on a worker thread;
        file_download_finished(file_id, ok, message) fires on completion."""
        msg = next(
            (m for m in self.direct.messages_for(peer_id) if m.id == file_id), None
        )
        if msg is None or msg.file_info is None:
            self.file_download_finished.emit(file_id, False, "文件消息不存在")
            return
        file_info = msg.file_info
        if not file_info.download_host or file_info.download_port <= 0:
            self.file_download_finished.emit(file_id, False, "文件已过期，请对方重新发送")
            return

        def run() -> None:
            ok, message = self.direct.download_file(file_info, target_path)
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

    # GroupMeshListener (called from mesh worker threads; hop to the main
    # thread via queued signals).
    def group_mesh_message(self, group_id: str, msgs) -> None:
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.merge_incoming(msgs)

    def group_mesh_links_changed(self, group_id: str) -> None:
        if group_id == self.active_group_id:
            self.active_connection_lost_changed.emit()

    def group_mesh_delete(self, group_id: str, message_id: str, sender_id: str) -> None:
        """A delete arrived over the group mesh (host-offline path): remove the
        message from the owning group's list. The _messages collector mirrors
        the removal to the database exactly like relay-delivered deletes."""
        p2p = self.group_p2p_map.get(group_id)
        if p2p is not None:
            p2p.remove_local_message(message_id, sender_id)

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
        password = p2p.group_password or self.store.get_setting(
            f"group_password_{group_id}", ""
        )
        self.mesh.enter_group(
            group_id, my_peer, list(seen.values()), list(p2p.messages), password
        )

    def _teardown_group_mesh(self, group_id: str) -> None:
        self.mesh.leave_group(group_id)

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
            if not candidate.is_host and (
                candidate.join_id == id_or_name or candidate.group_name == id_or_name
            ):
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
                        type="join_ack", group_id=gid, members=members, host=host
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
        current = {m.id for m in msgs}
        removed = stored - current
        if removed:
            stored.difference_update(removed)
            for mid in removed:
                pending_map.pop(mid, None)
                self.store.delete_message("direct:" + peer_id, mid)
        new = [m for m in msgs if m.id not in stored]
        if new:
            # Direct chats live under a synthetic "direct:<peerId>" key in the
            # messages table, which has a foreign key to saved_groups — ensure
            # the placeholder group row exists so the insert never violates it
            peer = next((c for c in self.direct.contacts_list() if c.id == peer_id), None)
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
        saved = self.store.get_messages_for_group(group_id)
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
                    )
                    for m in saved
                ]
            )
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
        p2p.initialize_as_host(nick, name, password)
        group_id = p2p.current_group_id
        # The same group name on this device derives the SAME group id: stop
        # the previous instance instead of leaking its sockets/heartbeats, and
        # drop its row (a duplicate id would also break the list's keys).
        old = self.group_p2p_map.pop(group_id, None)
        if old is not None:
            self.call_manager.end_if_on(old, "通话已结束")
            old.stop()
        self.store.set_setting(f"group_password_{group_id}", password)
        self.group_p2p_map[group_id] = p2p
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
        p2p.initialize_as_client(nick, "", password)
        p2p.set_join_id(join_id)
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
            password = self.store.get_setting(f"group_password_{group_id}", "") or None
            new_p2p = P2PManager(
                self,
                port=self.port,
                host_server=self.host_server,
                device_id=self._device_id(),
                hardware_id=self._hardware_fingerprint(),
            )
            new_p2p.initialize_as_host(nick, meta.group_name, password)
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
        p2p = P2PManager(
            self, port=self.port, device_id=self._device_id(), hardware_id=self._hardware_fingerprint()
        )
        self.call_manager.attach(p2p)
        p2p.initialize_as_client(
            sg.my_name or "用户",
            sg.group_name,
            self.store.get_setting(f"group_password_{group_id}", "") or None,
        )
        p2p.set_join_id(self.store.get_setting(f"group_join_id_{group_id}", ""))
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
            p2p.stop()
        self._teardown_group_mesh(group_id)
        self.removed_group_ids.add(group_id)
        self.persisted_message_ids.pop(group_id, None)
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

    def send_message(self, content: str) -> bool:
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
        msg = p2p.send_message(content)
        if msg is not None:
            self.mesh.broadcast(gid, msg)
        return True

    def send_message_to_group(self, group_id: str, content: str) -> bool:
        if not content.strip():
            return False
        p2p = self.group_p2p_map.get(group_id)
        if p2p is None or (p2p.connection_lost and not self.mesh.has_links(group_id)):
            return False
        msg = p2p.send_message(content)
        if msg is not None:
            self.mesh.broadcast(group_id, msg)
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
        return True

    def download_file(self, file_id: str, target_path: str) -> None:
        """Download a file offer by file_id to target_path on a worker thread;
        file_download_finished(file_id, ok, message) fires on completion."""
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
        file_info = msg.file_info

        def run() -> None:
            ok, message = p2p.download_file(file_info, target_path)
            self.file_download_finished.emit(file_id, ok, message)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- video call

    def start_call(self, peer_id: str) -> None:
        """Start a video call with a member of the active group."""
        gid = self.active_group_id
        if gid is None:
            self.status_message.emit("请先进入一个群组")
            return
        p2p = self.group_p2p_map.get(gid)
        if p2p is None or p2p.connection_lost:
            self.status_message.emit("未连接到群组，无法发起通话")
            return
        self.call_manager.start_call(p2p, peer_id)

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
            self.store.get_setting(f"group_password_{group_id}", "") if p2p.is_host else ""
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
            if persisted:
                current_ids = {m.id for m in msgs}
                removed_ids = persisted - current_ids
                if removed_ids:
                    persisted.difference_update(removed_ids)
                    for mid in removed_ids:
                        self.store.delete_message(gid, mid)
            new_messages = [m for m in msgs if m.id not in persisted]
            if new_messages:
                # Keep the mesh history state complete even when the message
                # arrived over the host relay: later mesh backfills must include
                # it for members that were offline at send time.
                for m in new_messages:
                    self.mesh.note_message(gid, m)
                incoming = [m for m in new_messages if not m.is_from_me]
                if gid != self.active_group_id and incoming:
                    if meta is not None:
                        meta.unread_count += len(incoming)
                    if not self.window_active:
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
                    )
                    self.groups.insert(0, meta)
                else:
                    meta.is_host = False
                    meta.connected = True
                    meta.host_ip = host_ip
                    meta.host_port = host_port
                    meta.my_name = p2p.my_name
                self.store.upsert_group(
                    SavedGroup(
                        group_id=gid,
                        group_name=p2p.current_group_name,
                        is_host=False,
                        host_ip=host_ip,
                        host_port=host_port,
                        my_name=p2p.my_name,
                        member_count=len(p2p.peers) + 1,
                    )
                )
                if p2p.group_password:
                    self.store.set_setting(f"group_password_{gid}", p2p.group_password)
                if p2p.join_id:
                    self.store.set_setting(f"group_join_id_{gid}", p2p.join_id)
                self._load_and_replay_messages(gid, p2p)
                # Enter the mesh AFTER replaying persisted history so the mesh
                # state seeds with the full local history and can backfill it
                # to members that come online later.
                self._setup_group_mesh(gid, p2p)
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
        self.call_manager.hangup()
        if self.pending_p2p is not None:
            self.pending_p2p.stop()
        for p2p in self.group_p2p_map.values():
            p2p.stop()
        self.group_p2p_map.clear()
        self.direct.shutdown()
        self.mesh.shutdown()
        self.host_server.shutdown()
        try:
            self.store.close()
        except Exception:
            pass
