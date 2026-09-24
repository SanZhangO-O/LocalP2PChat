package com.zqr.localchat.call

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.zqr.localchat.ChatApp
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.CallInfo
import com.zqr.localchat.data.Peer
import com.zqr.localchat.network.DeviceIdentity
import com.zqr.localchat.network.Handshake
import com.zqr.localchat.network.NetworkPacket
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.network.Protocol
import com.zqr.localchat.network.RawLineIn
import com.zqr.localchat.network.Wire
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.IOException
import java.io.InputStream
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Group multi-party voice conference (audio only, star topology). Windows
 * parity: localchat/group_call.py — both platforms implement the same wire
 * behavior, so a Windows host and Android members (or vice versa) can share
 * one meeting.
 *
 * Topology:
 * - The initiator becomes the MEETING HOST. It binds an ephemeral media port
 *   (like the 1:1 caller) and sends a targeted `group_call_invite` to every
 *   online group member via the host relay.
 * - An invited member rings ("ring - join"): accepting sends
 *   `group_call_join` to the meeting host, then dials the host's media port.
 *   The media link is a plain TCP connection with the SAME security chain as
 *   a 1:1 call media channel: identity-signed handshake, then a
 *   `call_media_hello` version/participant check (CallInfo.callId carries the
 *   meetingId and CallInfo.meetingId mirrors it), then AES-256-GCM frames.
 * - Frames reuse the 1:1 media format exactly:
 *       [1 byte channel][4 bytes big-endian length][12B nonce || ct||tag]
 *   channel 1 = audio (PCM16 mono 16 kHz). The meetingId binds every link
 *   and packet to its meeting (a second meeting can never hijack a link).
 * - The host MIXES: it sums its own microphone with every member's inbound
 *   audio and sends each member the mix MINUS that member's own audio
 *   (mix-minus-self, so nobody hears themselves). Members simply play the
 *   host's mix and send their microphone.
 *
 * Signaling packets (all targeted, routed by the group host relay; the packet
 * actor must be the authenticated sender — see P2PManager.routeGroupCallPacket):
 * - group_call_invite {groupId, call{callId=meetingId, callerId=hostId,
 *   callerName, calleeId=memberId, mediaPort, meetingId}}
 * - group_call_join   {groupId, call{callId=meetingId, callerId=memberId,
 *   calleeId=hostId, meetingId}}
 * - group_call_leave  {groupId, call{callId=meetingId, callerId=actorId,
 *   calleeId=otherId, meetingId}}  (member leaves / host ends the meeting)
 * - group_call_sync   {groupId, call{...}, members=[participant peers]}
 *   (host -> members after every membership change)
 *
 * Old peers ignore these unknown packet types, so chat / files / 1:1 calls
 * are unaffected. One meeting at a time, per device; mutually exclusive with
 * the 1:1 CallManager because they share the microphone (busyCheck set by the
 * ViewModel). All state is exposed as thread-safe StateFlows so the Compose
 * UI can collect from any thread.
 */
object GroupCallManager {

    private const val TAG = "GroupCallManager"
    private const val CH_AUDIO = 1

    const val AUDIO_SAMPLE_RATE = 16000
    private const val AUDIO_FRAME_BYTES = 640 // 20ms of PCM16 mono
    private const val MEDIA_READ_TIMEOUT_MS = 15_000
    private const val CONNECT_TIMEOUT_MS = 8_000
    private const val MAX_FRAME_LEN = 512 * 1024

    /** Largest ciphertext frame on the wire (payload + GCM nonce + tag). */
    private const val MAX_FRAME_WIRE_LEN =
        MAX_FRAME_LEN + Crypto.GCM_NONCE_LEN + Crypto.GCM_TAG_BITS / 8

    // Smallest ciphertext frame on the wire: GCM nonce + tag.
    private const val MIN_FRAME_WIRE_LEN = Crypto.GCM_NONCE_LEN + 16

    /** Per-link inbound PCM buffer cap (bytes): ~400 ms; older audio is
     *  dropped so a burst can never inflate conference latency forever. */
    private const val INBUF_MAX = AUDIO_FRAME_BYTES * 20

    /** A member must connect its media link within this window after its
     *  group_call_join (the join alone must never activate a link). */
    private const val JOINER_TTL_MS = 30_000L

    /** An unanswered invite stops ringing after this long (member side). */
    private const val RING_TIMEOUT_MS = 45_000L

    /** Member uplink pacing: bounded queue drained by a dedicated sender
     *  thread; a silence keepalive keeps the host mixer clock fed. */
    private const val AUDIO_TX_QUEUE_CAP = 64
    private const val AUDIO_KEEPALIVE_MS = 3_000L

    // Playback jitter buffer (1:1 AudioEngine sizing).
    private const val PLAY_PREROLL_CHUNKS = 4
    private const val PLAY_QUEUE_CAP = 32
    private const val UNDERRUN_SILENCE_CHUNKS = 8

    /** Conference states surfaced to the UI (same vocabulary as 1:1 calls). */
    sealed class GroupCallState {
        data object Idle : GroupCallState()
        /** This device hosts the meeting and waits for members to join. */
        data class Outgoing(val groupName: String) : GroupCallState()
        /** Another member invited us; ringing. */
        data class Incoming(
            val meetingId: String,
            val hostId: String,
            val hostName: String,
            val hostIp: String,
            val mediaPort: Int
        ) : GroupCallState()
        /** The meeting is live (host: at least one link; member: connected). */
        data class Active(val meetingId: String, val title: String) : GroupCallState()
    }

    /** One conference participant for the UI (self marked). */
    data class Participant(val id: String, val name: String, val self: Boolean)

    private val _state = MutableStateFlow<GroupCallState>(GroupCallState.Idle)
    val state: StateFlow<GroupCallState> = _state.asStateFlow()

    private val _participants = MutableStateFlow<List<Participant>>(emptyList())
    val participants: StateFlow<List<Participant>> = _participants.asStateFlow()

    private val _events = MutableSharedFlow<String>(extraBufferCapacity = 16)
    val events: SharedFlow<String> = _events.asSharedFlow()

    private val _audioMuted = MutableStateFlow(false)
    val audioMuted: StateFlow<Boolean> = _audioMuted.asStateFlow()

    /** Set by the ViewModel: true while a 1:1 call is active (a conference
     *  never rings/starts while the microphone is busy with a 1:1 call). */
    @Volatile
    var busyCheck: (() -> Boolean)? = null

    private val lock = Any()

    // Meeting scope (guarded by [lock]; cleared in leaveLocal()).
    private var p2p: P2PManager? = null
    private var groupId = ""
    private var groupName = ""
    private var myId = ""
    private var myName = ""
    private var meetingId = ""
    private var role = "" // "host" | "member"
    private var hostId = ""
    private var hostName = ""
    private var mediaPort = 0

    /** Host: expected joiners (member id -> deadline nanoTime) declared by
     *  group_call_join; only a hello from an expected member activates a
     *  link (the media port is TCP-public, like the 1:1 caller's). */
    private val expectedJoiners = ConcurrentHashMap<String, Long>()

    /** Host: live member links. */
    private val links = ConcurrentHashMap<String, Link>()

    // Member: the single uplink to the meeting host.
    @Volatile
    private var memberSocket: Socket? = null
    @Volatile
    private var memberKey: ByteArray? = null

    /** True while the dial thread from [acceptInvite] is in flight: the
     *  dialog stays visible (state is still Incoming) until the media
     *  handoff, so a second tap must not spawn a second dial. */
    @Volatile
    private var dialing = false

    private var mediaServer: ServerSocket? = null

    @Volatile
    private var running = false

    /** Main-thread handler for the member ring timeout. */
    private val mainHandler = Handler(Looper.getMainLooper())
    private var ringRunnable: Runnable? = null

    // Audio engine; created in startEngines (mirrors the 1:1 CallManager).
    private var audioEngine: ConferenceAudio? = null

    // Host mixing input: own microphone PCM buffer (drop-oldest).
    private val micBuf = PcmBuffer(INBUF_MAX)

    // Member uplink queue drained by the sender thread.
    private val audioTxQueue = LinkedBlockingQueue<ByteArray>(AUDIO_TX_QUEUE_CAP)
    private var mixerThread: Thread? = null
    private var senderThread: Thread? = null

    @Volatile
    private var lastMediaSentAt = 0L

    // ------------------------------------------------------- mix primitive

    /** Saturating sum of PCM16 mono buffers (voice-grade conference mix).
     *  Delegates to [ConferenceMix] (kept Android-free so JVM unit tests can
     *  pin the cross-platform mixing rule; Windows parity:
     *  group_call.mix_pcm16). */
    fun mixPcm(chunks: List<ByteArray>, frameBytes: Int = AUDIO_FRAME_BYTES): ByteArray =
        ConferenceMix.mixPcm(chunks, frameBytes)

    // ------------------------------------------------------- wire protocol

    /** Network-thread entry: P2PManager delivers group call packets here. */
    fun handleSignal(p2p: P2PManager, packet: NetworkPacket) {
        val call = packet.call ?: return
        when (packet.type) {
            "group_call_invite" -> onInvite(p2p, packet, call)
            "group_call_join" -> onJoin(packet, call)
            "group_call_leave" -> onLeavePacket(packet, call)
            "group_call_sync" -> onSync(packet, call)
        }
    }

    // ------------------------------------------------------------ public API

    /** Start a conference in the active group: this device becomes the
     *  meeting host (mixer) and invites every online member. */
    fun startMeeting(p2p: P2PManager) {
        val server: ServerSocket
        synchronized(lock) {
            if (_state.value !is GroupCallState.Idle) {
                _events.tryEmit("已有进行中的语音会议")
                return
            }
            if (busyCheck?.invoke() == true) {
                _events.tryEmit("通话进行中，无法发起语音会议")
                return
            }
            server = try {
                ServerSocket(0).apply { reuseAddress = true }
            } catch (e: IOException) {
                _events.tryEmit("无法开启语音会议端口")
                return
            }
            this.p2p = p2p
            groupId = p2p.currentGroupId
            groupName = p2p.currentGroupName
            role = "host"
            myId = p2p.myIdValue
            myName = p2p.myNameValue
            hostId = myId
            hostName = myName
            meetingId = UUID.randomUUID().toString()
            mediaPort = server.localPort
            mediaServer = server
            expectedJoiners.clear()
            links.clear()
            running = true
            _state.value = GroupCallState.Outgoing(groupName)
        }
        publishParticipants()
        val meeting = meetingId
        for (peer in p2p.peers.value.values) {
            if (peer.ipAddress.isEmpty()) continue
            p2p.sendTargeted(peer.id, groupPacket("group_call_invite", myId, peer.id))
        }
        Thread { acceptLoop(server, meeting) }.apply {
            name = "group-call-accept"
            isDaemon = true
            start()
        }
    }

    /** Join the meeting we were invited to: announce, then dial the meeting
     *  host's media port (identity handshake + call_media_hello). */
    fun acceptInvite() {
        val hostId: String
        val hostIp: String
        val port: Int
        val meeting: String
        synchronized(lock) {
            val cur = _state.value as? GroupCallState.Incoming ?: return
            if (dialing) return
            dialing = true
            hostId = cur.hostId
            hostIp = cur.hostIp
            port = cur.mediaPort
            meeting = cur.meetingId
        }
        stopRingTimer()
        val channel = p2p
        if (channel != null && hostId.isNotEmpty()) {
            channel.sendTargeted(hostId, groupPacket("group_call_join", myId, hostId))
        }
        if (hostIp.isEmpty() || port <= 0) {
            leaveLocal("无法加入语音会议：地址无效")
            return
        }
        Thread {
            var sock: Socket? = null
            var handedOff = false
            try {
                val s = Socket()
                sock = s
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(hostIp, port), CONNECT_TIMEOUT_MS)
                s.soTimeout = 10_000
                val wire = Wire(
                    RawLineIn(s.getInputStream()),
                    PrintWriter(s.getOutputStream(), true)
                )
                // An "ip:..." placeholder is not a stable device id: binding
                // TOFU state to it would false-positive once the real id is
                // known (the handshake still authenticates the host's
                // long-term key via its signature).
                Handshake.initiateDirect(
                    wire,
                    expectedPeerId = hostId.takeIf { it.isNotEmpty() && !it.startsWith("ip:") },
                    onIdentityMismatch = {
                        _events.tryEmit("安全警告：会议主持人身份验证失败")
                    }
                )
                // Bind this TCP connection to the meeting (the media port is
                // TCP-public: only a peer that knows the meetingId — from the
                // encrypted invite — may turn this connection into a link).
                wire.sendPacket(
                    NetworkPacket(
                        type = "call_media_hello",
                        call = CallInfo(
                            callId = meeting,
                            callerId = hostId,
                            callerName = synchronized(lock) { hostName },
                            calleeId = myId,
                            meetingId = meeting
                        )
                    )
                )
                var activate = false
                synchronized(lock) {
                    if (_state.value is GroupCallState.Incoming && meetingId == meeting) {
                        memberSocket = s
                        memberKey = wire.sessionKey
                        activate = true
                    }
                }
                if (activate) {
                    handedOff = true
                    goActive(meeting)
                }
            } catch (e: Exception) {
                Log.w(TAG, "member media connect failed", e)
                val stillMine = synchronized(lock) { meetingId == meeting && role == "member" }
                if (stillMine) leaveLocal("加入语音会议失败")
            } finally {
                if (!handedOff) runCatching { sock?.close() }
            }
        }.apply {
            name = "group-call-dial"
            isDaemon = true
            start()
        }
    }

    /** Decline the ringing invite (no packet: the host only activates links
     *  whose member announced AND connected). */
    fun declineInvite() {
        synchronized(lock) {
            if (_state.value !is GroupCallState.Incoming) return
        }
        stopRingTimer()
        leaveLocal("已拒绝语音会议邀请")
    }

    /** Leave the meeting from the UI (any state; host: ends the meeting). */
    fun leaveMeeting() {
        val isHost: Boolean
        val meeting: String
        val channel: P2PManager?
        synchronized(lock) {
            if (_state.value is GroupCallState.Idle) return
            isHost = role == "host"
            meeting = meetingId
            channel = p2p
        }
        stopRingTimer()
        if (channel != null && meeting.isNotEmpty()) {
            if (!isHost && hostId.isNotEmpty()) {
                channel.sendTargeted(hostId, groupPacket("group_call_leave", myId, hostId))
            } else if (isHost) {
                // the meeting is over: tell every participant (joined or
                // still ringing) so they tear down immediately
                val memberIds = (links.keys + expectedJoiners.keys).distinct()
                for (mid in memberIds) {
                    channel.sendTargeted(mid, groupPacket("group_call_leave", myId, mid))
                }
            }
        }
        leaveLocal("已挂断")
    }

    fun setAudioMuted(muted: Boolean) {
        _audioMuted.value = muted
    }

    /** Tear the meeting down when it rides the given group connection
     *  (connection lost / leaving the group): signaling is dead, media
     *  follows. */
    fun endIfOn(channel: P2PManager, reason: String) {
        synchronized(lock) {
            if (_state.value is GroupCallState.Idle || p2p !== channel) return
        }
        stopRingTimer()
        leaveLocal(reason)
    }

    // ------------------------------------------------------- signal handlers

    private fun onInvite(p2p: P2PManager, packet: NetworkPacket, call: CallInfo) {
        // An invited member: ring (busy conferences/1:1 calls never ring).
        if (call.calleeId != p2p.myIdValue) return
        val meeting = call.meetingId ?: return
        if (meeting.isEmpty() || call.callerId.isEmpty() || call.mediaPort <= 0) return
        if (packet.groupId != null && packet.groupId != p2p.currentGroupId) return
        if (busyCheck?.invoke() == true) return
        val hostIp = p2p.peers.value[call.callerId]?.ipAddress ?: ""
        if (hostIp.isEmpty()) return
        synchronized(lock) {
            if (_state.value !is GroupCallState.Idle) return
            this.p2p = p2p
            groupId = p2p.currentGroupId
            groupName = p2p.currentGroupName
            role = "member"
            myId = p2p.myIdValue
            myName = p2p.myNameValue
            meetingId = meeting
            hostId = call.callerId
            hostName = call.callerName
            mediaPort = call.mediaPort
            memberSocket = null
            memberKey = null
            _state.value = GroupCallState.Incoming(meeting, hostId, hostName, hostIp, mediaPort)
        }
        publishParticipants()
        startRingTimer()
    }

    private fun startRingTimer() {
        stopRingTimer()
        val meeting = meetingId
        val runnable = Runnable {
            val expired = synchronized(lock) {
                _state.value is GroupCallState.Incoming && meetingId == meeting
            }
            if (expired) leaveLocal("未响应语音会议邀请")
        }
        ringRunnable = runnable
        mainHandler.postDelayed(runnable, RING_TIMEOUT_MS)
    }

    private fun stopRingTimer() {
        ringRunnable?.let { mainHandler.removeCallbacks(it) }
        ringRunnable = null
    }

    /** The meeting host records the announced member (host role only). */
    private fun onJoin(packet: NetworkPacket, call: CallInfo) {
        val meeting = call.meetingId ?: return
        synchronized(lock) {
            if (role != "host" || _state.value is GroupCallState.Idle) return
            if (call.calleeId != myId || call.callerId == myId) return
            if (meeting.isEmpty() || meeting != meetingId) return
            if (packet.groupId != null && packet.groupId != groupId) return
            val peers = p2p?.peers?.value
            if (peers == null || call.callerId !in peers) return
            expectedJoiners[call.callerId] = System.nanoTime() + JOINER_TTL_MS * 1_000_000
        }
    }

    private fun onLeavePacket(packet: NetworkPacket, call: CallInfo) {
        val meeting = call.meetingId ?: return
        val isHost: Boolean
        synchronized(lock) {
            if (_state.value is GroupCallState.Idle || meeting != meetingId) return
            if (packet.groupId != null && groupId.isNotEmpty() && packet.groupId != groupId) return
            isHost = role == "host"
        }
        if (isHost) {
            // a member left: drop its link (joiners that never connected are
            // dropped from the expected set too)
            expectedJoiners.remove(call.callerId)
            val link = links.remove(call.callerId)
            if (link != null) {
                runCatching { link.socket.close() }
                broadcastSync()
                publishParticipants()
            }
        } else {
            // only the meeting host may end the meeting for us
            if (call.callerId != hostId) return
            stopRingTimer()
            leaveLocal("语音会议已结束")
        }
    }

    /** Member: refresh the participant list from the meeting host. */
    private fun onSync(packet: NetworkPacket, call: CallInfo) {
        val members = packet.members ?: return
        synchronized(lock) {
            if (role != "member" || _state.value !is GroupCallState.Active) return
            if (call.callerId != hostId) return
            _participants.value = members.map { Participant(it.id, it.name, it.id == myId) }
        }
    }

    // ------------------------------------------------------ host media links

    private fun acceptLoop(server: ServerSocket, meeting: String) {
        try {
            server.soTimeout = 250
        } catch (e: Exception) {
            return
        }
        while (true) {
            val alive = synchronized(lock) { meetingId == meeting && role == "host" && running }
            if (!alive) return
            val sock = try {
                server.accept()
            } catch (e: SocketTimeoutException) {
                continue
            } catch (e: IOException) {
                return
            }
            Thread { hostLinkThread(sock, meeting) }.apply {
                name = "group-call-link-setup"
                isDaemon = true
                start()
            }
        }
    }

    /** Handshake + call_media_hello validation for ONE joining member
     *  (worker thread; mirrors the 1:1 accept loop security chain). */
    private fun hostLinkThread(sock: Socket, meeting: String) {
        var memberId = ""
        try {
            sock.soTimeout = 10_000
            val wire = Wire(
                RawLineIn(sock.getInputStream()),
                PrintWriter(sock.getOutputStream(), true)
            )
            val start = wire.recvRaw() ?: throw IOException("no media handshake")
            if (start.type != Protocol.HS_START) throw IOException("no media handshake")
            // The hello (below) declares which member this is, so the TOFU
            // check runs AFTER the handshake against that declared id — same
            // deferred-binding rule as the 1:1 accept loop. An unexpected
            // member is rejected before its identity is bound.
            val secured = Handshake.acceptDirect(wire, start, expectedPeerId = null)
                ?: throw IOException("media handshake rejected")
            val hello = wire.recvPacket()
            val helloCall = hello?.call
            if (hello?.type != "call_media_hello" || helloCall == null ||
                helloCall.callId != meeting ||
                (!helloCall.meetingId.isNullOrEmpty() && helloCall.meetingId != meeting)
            ) {
                throw IOException("call_media_hello mismatch")
            }
            memberId = helloCall.calleeId
            val valid = synchronized(lock) {
                val deadline = expectedJoiners[memberId] ?: 0L
                meetingId == meeting && role == "host" && running &&
                    memberId.isNotEmpty() && memberId != myId &&
                    deadline >= System.nanoTime()
            }
            if (!valid) throw IOException("member not expected")
            if (!memberId.startsWith("ip:") &&
                !DeviceIdentity.checkPeer(memberId, secured.peerIdent ?: "")
            ) {
                _events.tryEmit("安全警告：会议成员身份验证失败")
                throw IOException("member identity changed")
            }
            val key = wire.sessionKey ?: throw IOException("no session key")
            val old = links.put(memberId, Link(memberId, sock, key))
            if (old != null) runCatching { old.socket.close() }
            expectedJoiners.remove(memberId)
            startLinkReadLoop(meeting, memberId, sock, key)
            synchronized(lock) {
                val cur = _state.value
                if (cur is GroupCallState.Outgoing && meetingId == meeting) {
                    _state.value = GroupCallState.Active(meeting, groupName)
                }
            }
            broadcastSync()
            publishParticipants()
            startEngines()
        } catch (e: Exception) {
            Log.w(TAG, "host link failed for $memberId", e)
            runCatching { sock.close() }
        }
    }

    private fun startLinkReadLoop(meeting: String, memberId: String, sock: Socket, key: ByteArray) {
        Thread {
            val nonces = CallManager.NonceLru()
            try {
                sock.soTimeout = MEDIA_READ_TIMEOUT_MS
                val input = sock.getInputStream()
                while (true) {
                    val link = links[memberId] ?: break
                    if (link.socket !== sock) break
                    val header = readExact(input, 5) ?: break
                    val channel = header[0].toInt() and 0xFF
                    val length = ((header[1].toInt() and 0xFF) shl 24) or
                        ((header[2].toInt() and 0xFF) shl 16) or
                        ((header[3].toInt() and 0xFF) shl 8) or
                        (header[4].toInt() and 0xFF)
                    if (length < MIN_FRAME_WIRE_LEN || length > MAX_FRAME_WIRE_LEN) break
                    val blob = readExact(input, length) ?: break
                    if (nonces.isReplay(blob)) break
                    val payload = try {
                        Crypto.aesGcmDecrypt(key, blob)
                    } catch (e: Exception) {
                        break
                    }
                    if (channel != CH_AUDIO) continue // conferences are audio-only
                    link.pushPcm(payload)
                }
            } catch (e: SocketTimeoutException) {
                // the link died
            } catch (e: Exception) {
                Log.w(TAG, "link read loop ended for $memberId", e)
            } finally {
                onLinkDied(meeting, memberId, sock)
            }
        }.apply {
            name = "group-call-link"
            isDaemon = true
            start()
        }
    }

    private fun onLinkDied(meeting: String, memberId: String, sock: Socket) {
        val live = synchronized(lock) { meetingId == meeting && role == "host" && running }
        if (!live) return
        val link = links.remove(memberId) ?: return
        if (link.socket !== sock) {
            // a newer link already replaced this one: put it back
            links[memberId] = link
            return
        }
        runCatching { sock.close() }
        broadcastSync()
        publishParticipants()
    }

    /** Host: push the participant list to every member (targeted). */
    private fun broadcastSync() {
        val channel = p2p ?: return
        if (synchronized(lock) { role != "host" }) return
        for (mid in links.keys.toList()) {
            channel.sendTargeted(mid, groupPacket("group_call_sync", myId, mid))
        }
    }

    // ---------------------------------------------------- member media link

    private fun goActive(meeting: String) {
        synchronized(lock) {
            val cur = _state.value as? GroupCallState.Incoming ?: return
            if (cur.meetingId != meeting) return
            _state.value = GroupCallState.Active(meeting, cur.hostName)
            // The member path owns the running flag too (1:1 parity:
            // CallManager sets it on BOTH caller and callee handoff). The
            // member read loop (:live check), the uplink sender and
            // startEngines all gate on it — without this, a joined member's
            // media path tears itself down on its first loop iteration.
            running = true
        }
        publishParticipants()
        startEngines()
        startMemberSender(meeting)
        startMemberReadLoop(meeting)
    }

    private fun startMemberReadLoop(meeting: String) {
        Thread {
            var reason = "连接已断开"
            val nonces = CallManager.NonceLru()
            val sock = memberSocket
            val key = memberKey
            if (sock == null || key == null) {
                leaveLocal(reason)
                return@Thread
            }
            try {
                sock.soTimeout = MEDIA_READ_TIMEOUT_MS
                val input = sock.getInputStream()
                while (true) {
                    val live = synchronized(lock) {
                        memberSocket === sock && meetingId == meeting && running
                    }
                    if (!live) return@Thread
                    val header = readExact(input, 5) ?: break
                    val channel = header[0].toInt() and 0xFF
                    val length = ((header[1].toInt() and 0xFF) shl 24) or
                        ((header[2].toInt() and 0xFF) shl 16) or
                        ((header[3].toInt() and 0xFF) shl 8) or
                        (header[4].toInt() and 0xFF)
                    if (length < MIN_FRAME_WIRE_LEN || length > MAX_FRAME_WIRE_LEN) break
                    val blob = readExact(input, length) ?: break
                    if (nonces.isReplay(blob)) break
                    val payload = try {
                        Crypto.aesGcmDecrypt(key, blob)
                    } catch (e: Exception) {
                        break
                    }
                    if (channel != CH_AUDIO) continue
                    audioEngine?.enqueuePlayback(payload)
                }
            } catch (e: SocketTimeoutException) {
                reason = "连接已断开"
            } catch (e: Exception) {
                Log.w(TAG, "member read loop ended", e)
            } finally {
                val stillMine = synchronized(lock) { meetingId == meeting && role == "member" }
                if (stillMine) leaveLocal(reason)
            }
        }.apply {
            name = "group-call-member"
            isDaemon = true
            start()
        }
    }

    /** Single writer for the member uplink; sends a silence keepalive so the
     *  host's mixer clock never starves on a dead microphone. */
    private fun startMemberSender(meeting: String) {
        if (senderThread?.isAlive == true) return
        lastMediaSentAt = System.currentTimeMillis()
        senderThread = Thread {
            try {
                while (running) {
                    val payload = try {
                        audioTxQueue.poll(100, TimeUnit.MILLISECONDS)
                    } catch (e: InterruptedException) {
                        break
                    }
                    if (payload != null) {
                        writeMemberFrame(payload, meeting)
                        continue
                    }
                    val now = System.currentTimeMillis()
                    if (now - lastMediaSentAt >= AUDIO_KEEPALIVE_MS) {
                        lastMediaSentAt = now
                        writeMemberFrame(ByteArray(AUDIO_FRAME_BYTES), meeting)
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "member sender ended", e)
            }
        }.apply {
            name = "group-call-sender"
            isDaemon = true
            start()
        }
    }

    private fun writeMemberFrame(payload: ByteArray, meeting: String) {
        val sock = memberSocket ?: return
        val key = memberKey ?: return
        val live = synchronized(lock) { meetingId == meeting && role == "member" && running }
        if (!live) return
        try {
            writeFrame(sock, key, payload)
            lastMediaSentAt = System.currentTimeMillis()
        } catch (e: IOException) {
            // the read loop reports the dead link
        }
    }

    // ------------------------------------------------------------- engines

    /** Start audio + mixer exactly once per meeting. Atomic with [leaveLocal]:
     *  two members joining at the same moment run concurrent hostLinkThreads,
     *  and the old unlocked null-check raced into two ConferenceAudio
     *  instances — the overwritten one never stops and its AudioRecord keeps
     *  the microphone in use (1:1 parity: CallManager.startEngines holds the
     *  same lock against the same incident). */
    private fun startEngines() {
        synchronized(lock) {
            if (!running || audioEngine != null) return
            val isHost = role == "host"
            audioEngine = ConferenceAudio(::onMicPcm)
            audioEngine?.start()
            if (isHost && mixerThread?.isAlive != true) {
                val meeting = meetingId
                mixerThread = Thread { mixerLoop(meeting) }.apply {
                    name = "group-call-mixer"
                    isDaemon = true
                    start()
                }
            }
        }
    }

    /** Capture callback: route one chunk. Muted capture feeds silence so
     *  links stay warm (1:1 parity). */
    private fun onMicPcm(pcm: ByteArray) {
        if (!running) return
        val data = if (_audioMuted.value) ByteArray(pcm.size) else pcm
        val isHost = synchronized(lock) { role == "host" }
        if (isHost) {
            micBuf.push(data)
        } else {
            // Drop-oldest when the sender cannot keep up: better to lose the
            // oldest 20ms than to block the microphone read (which would
            // overrun the AudioRecord buffer and garble speech).
            while (!audioTxQueue.offer(data)) audioTxQueue.poll()
        }
    }

    /** 20ms clock: build each member's mix-minus-self and the host's own
     *  playback sum, then write one frame per link. */
    private fun mixerLoop(meeting: String) {
        while (true) {
            val live = synchronized(lock) { meetingId == meeting && role == "host" && running }
            if (!live) return
            val started = System.nanoTime()
            val frame = AUDIO_FRAME_BYTES
            val mic = micBuf.take(frame)
            val activeLinks = links.values.toList()
            val chunks = activeLinks.associate { it.memberId to it.takePcm(frame) }
            var play = ByteArray(frame)
            for (link in activeLinks) {
                val own = chunks[link.memberId] ?: continue
                val others = chunks.filterKeys { it != link.memberId }.values
                val mixed = mixPcm(listOf(mic) + others, frame)
                try {
                    link.writeFrame(mixed)
                } catch (e: IOException) {
                    onLinkDied(meeting, link.memberId, link.socket)
                    continue
                }
                play = mixPcm(listOf(play, own), frame)
            }
            if (activeLinks.isNotEmpty()) audioEngine?.enqueuePlayback(play)
            val elapsedMs = (System.nanoTime() - started) / 1_000_000
            val sleepMs = 20L - elapsedMs
            try {
                Thread.sleep(if (sleepMs < 2) 2 else sleepMs)
            } catch (e: InterruptedException) {
                return
            }
        }
    }

    // ------------------------------------------------------------ teardown

    private fun publishParticipants() {
        val out = ArrayList<Participant>()
        synchronized(lock) {
            if (role == "host") {
                out.add(Participant(myId, myName, true))
                val peers = p2p?.peers?.value
                for (link in links.values) {
                    val name = peers?.get(link.memberId)?.name ?: link.memberId
                    out.add(Participant(link.memberId, name, false))
                }
            } else if (role == "member") {
                // the host shows first; the rest arrive via group_call_sync
                if (hostId.isNotEmpty()) {
                    out.add(Participant(hostId, hostName, false))
                }
            }
        }
        if (_state.value !is GroupCallState.Idle) _participants.value = out
    }

    private fun groupPacket(pktType: String, actorId: String, otherId: String): NetworkPacket {
        val meeting: String
        val port: Int
        val gid: String
        val myId: String
        val myName: String
        synchronized(lock) {
            meeting = this.meetingId
            port = mediaPort
            gid = groupId
            myId = this.myId
            myName = this.myName
        }
        val callerName = if (actorId == myId) myName else synchronized(lock) { hostName }
        val packet = NetworkPacket(
            type = pktType,
            groupId = gid,
            call = CallInfo(
                callId = meeting,
                callerId = actorId,
                callerName = callerName,
                calleeId = otherId,
                mediaPort = if (pktType == "group_call_invite") port else 0,
                meetingId = meeting
            )
        )
        if (pktType == "group_call_sync") {
            val members = ArrayList<Peer>()
            synchronized(lock) {
                members.add(Peer(this.myId, this.myName, "", 0))
                val peers = p2p?.peers?.value
                for (link in links.values) {
                    val name = peers?.get(link.memberId)?.name ?: link.memberId
                    members.add(Peer(link.memberId, name, "", 0))
                }
            }
            return packet.copy(members = members)
        }
        return packet
    }

    private fun leaveLocal(reason: String) {
        val server: ServerSocket?
        val sock: Socket?
        val memberLinks: List<Link>
        synchronized(lock) {
            if (_state.value is GroupCallState.Idle) return
            server = mediaServer
            sock = memberSocket
            memberLinks = links.values.toList()
            links.clear()
            expectedJoiners.clear()
            _state.value = GroupCallState.Idle
            role = ""
            meetingId = ""
            hostId = ""
            hostName = ""
            mediaPort = 0
            p2p = null
            groupId = ""
            groupName = ""
            memberSocket = null
            memberKey = null
            dialing = false
            running = false
            // Engine teardown INSIDE the lock (startEngines holds the same
            // lock): a leave immediately followed by a new join must not let
            // the new meeting's engines be created between the flag flip and
            // the old stop — the overwritten engine would never stop and
            // keep the microphone.
            audioEngine?.stop()
            audioEngine = null
            senderThread = null
            mixerThread = null
        }
        stopRingTimer()
        runCatching { server?.close() }
        runCatching { sock?.close() }
        for (link in memberLinks) {
            runCatching { link.socket.close() }
        }
        micBuf.clear()
        audioTxQueue.clear()
        _participants.value = emptyList()
        _events.tryEmit("语音会议结束：$reason")
    }

    // ------------------------------------------------------------- plumbing

    /** Write one framed, AES-GCM encrypted media packet:
     *  [1B channel][4B length][12B nonce][ciphertext||tag]. One writer at a
     *  time per socket (the mixer owns host links; the sender thread owns the
     *  member uplink), but the lock also guards a link handover race. */
    private fun writeFrame(sock: Socket, key: ByteArray, payload: ByteArray) {
        val blob = Crypto.aesGcmEncrypt(key, payload)
        val header = ByteArray(5)
        header[0] = CH_AUDIO.toByte()
        header[1] = (blob.size ushr 24).toByte()
        header[2] = (blob.size ushr 16).toByte()
        header[3] = (blob.size ushr 8).toByte()
        header[4] = blob.size.toByte()
        val out = sock.getOutputStream()
        synchronized(sock) {
            out.write(header)
            out.write(blob)
            out.flush()
        }
    }

    private fun readExact(input: InputStream, n: Int): ByteArray? {
        val buf = ByteArray(n)
        var read = 0
        while (read < n) {
            val got = try {
                input.read(buf, read, n - read)
            } catch (e: IOException) {
                return null
            }
            if (got < 0) return null
            read += got
        }
        return buf
    }

    /** Drop-oldest bounded PCM byte buffer (host mic + per-link inbound). */
    private class PcmBuffer(private val cap: Int) {
        private var buf = ByteArray(cap * 2)
        private var len = 0

        @Synchronized
        fun push(data: ByteArray) {
            if (buf.size < len + data.size) {
                buf = buf.copyOf(maxOf(len + data.size, cap * 2))
            }
            System.arraycopy(data, 0, buf, len, data.size)
            len += data.size
            if (len > cap) {
                // drop the OLDEST bytes beyond the cap
                System.arraycopy(buf, len - cap, buf, 0, cap)
                len = cap
            }
        }

        @Synchronized
        fun take(frameBytes: Int): ByteArray {
            val out = ByteArray(frameBytes)
            if (len < frameBytes) {
                // zero-pad: the mix clock never waits for a silent peer
                System.arraycopy(buf, 0, out, 0, len)
                len = 0
            } else {
                System.arraycopy(buf, 0, out, 0, frameBytes)
                System.arraycopy(buf, frameBytes, buf, 0, len - frameBytes)
                len -= frameBytes
            }
            return out
        }

        @Synchronized
        fun clear() {
            len = 0
        }
    }

    /** One member media link on the meeting host (socket + inbound buffer). */
    private class Link(val memberId: String, val socket: Socket, val key: ByteArray) {
        private val inBuf = PcmBuffer(INBUF_MAX)

        fun pushPcm(pcm: ByteArray) {
            inBuf.push(pcm)
        }

        fun takePcm(frameBytes: Int): ByteArray = inBuf.take(frameBytes)

        fun writeFrame(payload: ByteArray) {
            GroupCallManager.writeFrame(socket, key, payload)
        }
    }

    /**
     * AudioRecord capture + AudioTrack playback of PCM16 mono 16 kHz for the
     * conference. Mirrors the 1:1 AudioEngine: VOICE_COMMUNICATION source in
     * MODE_IN_COMMUNICATION so the platform AEC/NS/AGC preprocessor runs;
     * capture never touches the sockets (it only hands 20ms chunks to
     * [onMic]); playback runs through a jitter buffer with short silence on
     * underruns and re-priming after a sustained gap.
     */
    @SuppressLint("MissingPermission")
    private class ConferenceAudio(private val onMic: (ByteArray) -> Unit) {
        private var recorder: AudioRecord? = null
        private var track: AudioTrack? = null
        private var captureThread: Thread? = null
        private var playThread: Thread? = null
        private val playQueue = LinkedBlockingQueue<ByteArray>(PLAY_QUEUE_CAP)
        @Volatile
        private var stopped = false
        private val silenceChunk = ByteArray(AUDIO_FRAME_BYTES)

        fun start() {
            val ctx = ChatApp.instance
            val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            runCatching {
                am.mode = AudioManager.MODE_IN_COMMUNICATION
                am.isSpeakerphoneOn = true
                am.requestAudioFocus(
                    null, AudioManager.STREAM_VOICE_CALL,
                    AudioManager.AUDIOFOCUS_GAIN_TRANSIENT
                )
            }

            val minIn = AudioRecord.getMinBufferSize(
                AUDIO_SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            val rec = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    AUDIO_SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minIn * 2, AUDIO_FRAME_BYTES * 8)
                )
            } catch (e: Exception) {
                Log.w(TAG, "AudioRecord unavailable", e)
                null
            }
            if (rec != null && rec.state != AudioRecord.STATE_INITIALIZED) {
                runCatching { rec.release() }
                recorder = null
            } else {
                recorder = rec
                if (rec != null && AcousticEchoCanceler.isAvailable()) {
                    runCatching {
                        AcousticEchoCanceler.create(rec.audioSessionId)?.enabled = true
                    }
                }
            }

            val minOut = AudioTrack.getMinBufferSize(
                AUDIO_SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            val trk = try {
                AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build()
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(AUDIO_SAMPLE_RATE)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build()
                    )
                    // keep the track buffer small: the jitter buffer above is
                    // the real buffer, a huge track buffer only adds latency
                    .setBufferSizeInBytes(maxOf(minOut, AUDIO_FRAME_BYTES * 8))
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build()
            } catch (e: Exception) {
                Log.w(TAG, "AudioTrack unavailable", e)
                null
            }
            if (trk != null && trk.state != AudioTrack.STATE_INITIALIZED) {
                runCatching { trk.release() }
                track = null
            } else {
                track = trk
            }

            if (rec != null && rec.state == AudioRecord.STATE_INITIALIZED) {
                rec.startRecording()
                captureThread = Thread {
                    val buf = ByteArray(AUDIO_FRAME_BYTES)
                    while (!stopped) {
                        val n = rec.read(buf, 0, AUDIO_FRAME_BYTES)
                        if (n > 0) {
                            onMic(if (n == AUDIO_FRAME_BYTES) buf.copyOf() else buf.copyOfRange(0, n))
                        }
                    }
                }.apply {
                    name = "group-call-capture"
                    isDaemon = true
                    start()
                }
            }
            if (trk != null && trk.state == AudioTrack.STATE_INITIALIZED) {
                trk.play()
                playThread = Thread {
                    var primed = false
                    var silenceCount = 0
                    while (!stopped) {
                        if (!primed) {
                            if (!waitForPreroll()) break
                            primed = true
                        }
                        val chunk = try {
                            playQueue.poll(50, TimeUnit.MILLISECONDS)
                        } catch (e: InterruptedException) {
                            break
                        }
                        if (chunk == null) {
                            // underrun: insert a short silence so the track
                            // does not stall, then re-prime
                            if (++silenceCount > UNDERRUN_SILENCE_CHUNKS) {
                                primed = false
                                silenceCount = 0
                                continue
                            }
                            runCatching {
                                trk.write(silenceChunk, 0, silenceChunk.size, AudioTrack.WRITE_BLOCKING)
                            }
                        } else {
                            silenceCount = 0
                            runCatching {
                                trk.write(chunk, 0, chunk.size, AudioTrack.WRITE_BLOCKING)
                            }
                        }
                    }
                }.apply {
                    name = "group-call-playback"
                    isDaemon = true
                    start()
                }
            }
        }

        private fun waitForPreroll(): Boolean {
            while (!stopped) {
                if (playQueue.size >= PLAY_PREROLL_CHUNKS) return true
                try {
                    Thread.sleep(4)
                } catch (e: InterruptedException) {
                    return false
                }
            }
            return false
        }

        fun enqueuePlayback(data: ByteArray) {
            if (stopped) return
            // Drop-oldest so a network burst cannot inflate latency forever.
            while (!playQueue.offer(data)) playQueue.poll()
        }

        fun stop() {
            stopped = true
            playQueue.clear()
            // recorder.stop() unblocks a capture thread parked inside read();
            // release ONLY after that thread left read().
            runCatching { recorder?.stop() }
            runCatching { track?.stop() }
            runCatching { captureThread?.join(1500) }
            runCatching { playThread?.join(1500) }
            runCatching { recorder?.release() }
            recorder = null
            runCatching { track?.release() }
            track = null
            runCatching {
                val am = ChatApp.instance.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                am.abandonAudioFocus(null)
                am.mode = AudioManager.MODE_NORMAL
            }
        }
    }
}
