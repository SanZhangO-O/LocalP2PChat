import json
from dataclasses import dataclass
from typing import Optional, List

MAX_CONTENT_LENGTH = 5000
MAX_LINE_LENGTH = 64 * 1024
TCP_PORT = 9999


@dataclass
class Peer:
    id: str
    name: str
    ip_address: str
    port: int

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "ipAddress": self.ip_address, "port": self.port}

    @staticmethod
    def from_dict(d: dict) -> "Peer":
        return Peer(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            ip_address=str(d.get("ipAddress", "")),
            port=int(d.get("port", 0)),
        )


@dataclass
class FileInfo:
    """Metadata for a file offered in chat. The bytes travel over a separate
    short-lived download server on the sender (see file_download/file_meta
    handshake in network.py), not over the message stream.

    file_key is a random per-file AES key (Base64) that travels INSIDE the
    (already encrypted) message channel and protects the raw download
    stream: chunk framing and GCM authentication are handled by the file
    transfer layer (Android parity)."""

    file_id: str
    file_name: str
    file_size: int
    download_host: str
    download_port: int
    file_key: str = ""

    def to_dict(self) -> dict:
        d = {
            "fileId": self.file_id,
            "fileName": self.file_name,
            "fileSize": self.file_size,
            "downloadHost": self.download_host,
            "downloadPort": self.download_port,
        }
        if self.file_key:
            d["fileKey"] = self.file_key
        return d

    @staticmethod
    def from_dict(d: dict) -> "FileInfo":
        return FileInfo(
            file_id=str(d.get("fileId", "")),
            file_name=str(d.get("fileName", "")),
            file_size=int(d.get("fileSize", 0)),
            download_host=str(d.get("downloadHost", "")),
            download_port=int(d.get("downloadPort", 0)),
            file_key=str(d.get("fileKey", "")),
        )


@dataclass
class ChatMessage:
    id: str
    content: str
    timestamp: int
    sender_id: str
    sender_name: str
    is_from_me: bool = False
    file_info: Optional[FileInfo] = None
    # Local-only delivery state (like is_from_me, never sent over the wire):
    # true while an offline-sent message still waits in the direct chat
    # outbox for the peer to come online (Android parity).
    pending: bool = False

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "content": self.content,
            "timestamp": self.timestamp,
            "senderId": self.sender_id,
            "senderName": self.sender_name,
        }
        if self.file_info is not None:
            d["fileInfo"] = self.file_info.to_dict()
        return d

    @staticmethod
    def from_dict(d: dict) -> "ChatMessage":
        msg_id = str(d.get("id", ""))
        sender_id = str(d.get("senderId", ""))
        if not msg_id or not sender_id:
            raise ValueError("chat message missing required field: id or senderId")
        file_info = None
        if d.get("fileInfo") is not None:
            file_info = FileInfo.from_dict(d["fileInfo"])
        return ChatMessage(
            id=msg_id,
            content=str(d.get("content", "")),
            timestamp=int(d.get("timestamp", 0)),
            sender_id=sender_id,
            sender_name=str(d.get("senderName", "")),
            file_info=file_info,
        )

    def marked_from_me(self, my_id: str) -> "ChatMessage":
        self.is_from_me = self.sender_id == my_id
        return self


@dataclass
class GroupInfo:
    group_name: str
    creator_name: str
    creator_id: str
    member_count: int

    def to_dict(self) -> dict:
        return {
            "groupName": self.group_name,
            "creatorName": self.creator_name,
            "creatorId": self.creator_id,
            "memberCount": self.member_count,
        }

    @staticmethod
    def from_dict(d: dict) -> "GroupInfo":
        return GroupInfo(
            group_name=str(d.get("groupName", "")),
            creator_name=str(d.get("creatorName", "")),
            creator_id=str(d.get("creatorId", "")),
            member_count=int(d.get("memberCount", 1)),
        )


@dataclass
class CallInfo:
    """Metadata for a video/audio call (see docs/video_call_protocol.md).

    Serialization mirrors kotlinx.serialization on the Android side:
    - camelCase keys
    - default-valued fields (accepted=True, audioEnabled=True, mediaPort=0) are
      omitted so the wire bytes match the Kotlin output byte-for-byte.
    """

    call_id: str
    caller_id: str
    caller_name: str
    callee_id: str
    media_port: int = 0
    accepted: bool = True
    audio_enabled: bool = True

    def to_dict(self) -> dict:
        d = {
            "callId": self.call_id,
            "callerId": self.caller_id,
            "callerName": self.caller_name,
            "calleeId": self.callee_id,
        }
        if self.media_port:
            d["mediaPort"] = self.media_port
        if not self.accepted:
            d["accepted"] = False
        if not self.audio_enabled:
            d["audioEnabled"] = False
        return d

    @staticmethod
    def from_dict(d: dict) -> "CallInfo":
        return CallInfo(
            call_id=str(d.get("callId", "")),
            caller_id=str(d.get("callerId", "")),
            caller_name=str(d.get("callerName", "")),
            callee_id=str(d.get("calleeId", "")),
            media_port=int(d.get("mediaPort", 0)),
            accepted=bool(d.get("accepted", True)),
            audio_enabled=bool(d.get("audioEnabled", True)),
        )


@dataclass
class NetworkPacket:
    type: str
    group_id: Optional[str] = None
    peer: Optional[Peer] = None
    members: Optional[List[Peer]] = None
    message: Optional[ChatMessage] = None
    messages: Optional[List[ChatMessage]] = None
    message_id: Optional[str] = None
    sender_id: Optional[str] = None
    error_message: Optional[str] = None
    group_info: Optional[GroupInfo] = None
    file_info: Optional[FileInfo] = None
    file_id: Optional[str] = None
    target_id: Optional[str] = None
    call: Optional[CallInfo] = None
    # The group's host (creator), returned by a member-sponsored join so the
    # newcomer can connect to the host for the relay path.
    host: Optional[Peer] = None
    # Handshake: which kind of secured connection is being set up
    # (query/join/mesh/direct).
    hs_mode: Optional[str] = None
    # Handshake: Base64 ephemeral ECDH public key.
    eph: Optional[str] = None
    # Handshake: Base64 long-term identity public key (direct mode).
    ident: Optional[str] = None
    # Handshake: Base64 HMAC confirmation (password modes).
    mac: Optional[str] = None
    # Handshake: Base64 ECDSA signature over the transcript (direct mode).
    sig: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"type": self.type}
        if self.group_id is not None:
            d["groupId"] = self.group_id
        if self.peer is not None:
            d["peer"] = self.peer.to_dict()
        if self.members is not None:
            d["members"] = [p.to_dict() for p in self.members]
        if self.message is not None:
            d["message"] = self.message.to_dict()
        if self.messages is not None:
            d["messages"] = [m.to_dict() for m in self.messages]
        if self.message_id is not None:
            d["messageId"] = self.message_id
        if self.sender_id is not None:
            d["senderId"] = self.sender_id
        if self.error_message is not None:
            d["errorMessage"] = self.error_message
        if self.group_info is not None:
            d["groupInfo"] = self.group_info.to_dict()
        if self.file_info is not None:
            d["fileInfo"] = self.file_info.to_dict()
        if self.file_id is not None:
            d["fileId"] = self.file_id
        if self.target_id is not None:
            d["targetId"] = self.target_id
        if self.call is not None:
            d["call"] = self.call.to_dict()
        if self.host is not None:
            d["host"] = self.host.to_dict()
        if self.hs_mode is not None:
            d["hsMode"] = self.hs_mode
        if self.eph is not None:
            d["eph"] = self.eph
        if self.ident is not None:
            d["ident"] = self.ident
        if self.mac is not None:
            d["mac"] = self.mac
        if self.sig is not None:
            d["sig"] = self.sig
        return d

    def to_json(self) -> str:
        # Compact separators so the bytes match kotlinx.serialization's output
        # on the Android side ({"type":"chat",...} without spaces).
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_dict(d: dict) -> "NetworkPacket":
        pkt_type = str(d.get("type", ""))
        if not pkt_type:
            raise ValueError("packet missing required field: type")
        pkt = NetworkPacket(type=pkt_type)
        if d.get("groupId") is not None:
            pkt.group_id = str(d["groupId"])
        if d.get("peer") is not None:
            pkt.peer = Peer.from_dict(d["peer"])
        if d.get("members") is not None:
            pkt.members = [Peer.from_dict(m) for m in d["members"]]
        if d.get("errorMessage") is not None:
            pkt.error_message = str(d["errorMessage"])
        if d.get("message") is not None:
            pkt.message = ChatMessage.from_dict(d["message"])
        if d.get("messageId") is not None:
            pkt.message_id = str(d["messageId"])
        if d.get("messages") is not None:
            pkt.messages = [ChatMessage.from_dict(m) for m in d["messages"]]
        if d.get("senderId") is not None:
            pkt.sender_id = str(d["senderId"])
        if d.get("groupInfo") is not None:
            pkt.group_info = GroupInfo.from_dict(d["groupInfo"])
        if d.get("fileInfo") is not None:
            pkt.file_info = FileInfo.from_dict(d["fileInfo"])
        if d.get("fileId") is not None:
            pkt.file_id = str(d["fileId"])
        if d.get("targetId") is not None:
            pkt.target_id = str(d["targetId"])
        if d.get("call") is not None:
            pkt.call = CallInfo.from_dict(d["call"])
        if d.get("host") is not None:
            pkt.host = Peer.from_dict(d["host"])
        if d.get("hsMode") is not None:
            pkt.hs_mode = str(d["hsMode"])
        if d.get("eph") is not None:
            pkt.eph = str(d["eph"])
        if d.get("ident") is not None:
            pkt.ident = str(d["ident"])
        if d.get("mac") is not None:
            pkt.mac = str(d["mac"])
        if d.get("sig") is not None:
            pkt.sig = str(d["sig"])
        if pkt_type == "error" and pkt.error_message is None:
            raise ValueError("error packet missing required field: errorMessage")
        if pkt_type == "chat" and pkt.message is None:
            raise ValueError("chat packet missing required field: message")
        if pkt_type == "file_message" and pkt.message is None:
            raise ValueError("file_message packet missing required field: message")
        if pkt_type == "file_download" and not pkt.file_id:
            raise ValueError("file_download packet missing required field: fileId")
        if pkt_type == "delete_message" and not pkt.message_id:
            raise ValueError("delete_message packet missing required field: messageId")
        return pkt

    @staticmethod
    def from_json(line: str) -> "NetworkPacket":
        return NetworkPacket.from_dict(json.loads(line))


def is_valid_content(content: str) -> bool:
    return bool(content.strip()) and len(content) <= MAX_CONTENT_LENGTH
