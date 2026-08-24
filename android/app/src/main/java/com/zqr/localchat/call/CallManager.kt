package com.zqr.localchat.call

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.util.Size
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.lifecycle.LifecycleOwner
import com.zqr.localchat.ChatApp
import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.CallInfo
import com.zqr.localchat.data.Peer
import com.zqr.localchat.network.Handshake
import com.zqr.localchat.network.DeviceIdentity
import com.zqr.localchat.network.NetworkPacket
import com.zqr.localchat.network.P2PManager
import com.zqr.localchat.network.RawLineIn
import com.zqr.localchat.network.Wire
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Video/audio call engine.
 *
 * - Signaling rides the group channel via targeted packets (P2PManager
 *   routes them to this singleton through [handleSignal]); direct member
 *   chats carry the same packets over the session socket ([handleDirectSignal]
 *   with a [CallChannel] backed by DirectChatManager).
 * - Media travels over a single direct TCP connection opened by the caller;
 *   the callee connects back. Frames:
 *       [1 byte channel][4 bytes big-endian length][payload]
 *   channel 0 = video (JPEG), channel 1 = audio (PCM16 mono 16 kHz).
 *
 * One call at a time. All state is exposed as thread-safe StateFlows so the
 * Compose UI can collect from any thread.
 */
object CallManager {

    /** Signaling channel: delivers one call packet to [peerId]. Group calls
     *  use P2PManager.sendTargeted (host relay); direct calls write straight
     *  to the session socket via DirectChatManager. */
    fun interface CallChannel {
        fun send(peerId: String, packet: NetworkPacket)
    }

    private const val TAG = "CallManager"
    private const val CH_VIDEO = 0
    private const val CH_AUDIO = 1

    const val AUDIO_SAMPLE_RATE = 16000
    private const val AUDIO_CHUNK = 640 // 20ms of PCM16 mono
    private const val MEDIA_READ_TIMEOUT_MS = 15_000
    private const val RING_TIMEOUT_MS = 45_000L
    private const val CONNECT_TIMEOUT_MS = 8_000
    private const val MAX_FRAME_LEN = 512 * 1024

    /** Largest ciphertext frame on the wire: a MAX_FRAME_LEN payload plus the
     *  GCM nonce and tag [Crypto.aesGcmEncrypt] prepends/appends per frame. */
    private const val MAX_FRAME_WIRE_LEN =
        MAX_FRAME_LEN + Crypto.GCM_NONCE_LEN + Crypto.GCM_TAG_BITS / 8
    private const val VIDEO_MAX_EDGE = 640
    private const val VIDEO_JPEG_QUALITY = 70

    /** 收到视频帧任一边的绝对上限（解压炸弹防护）：超过直接丢帧。 */
    private const val MAX_VIDEO_FRAME_DIMEN = 4096

    /** 解码目标边长上限：超过则按需 inSampleSize 降采样（正常对端发送
     *  640px，不受影响）。 */
    private const val VIDEO_DECODE_TARGET_DIMEN = 1280

    /** Cap outgoing video at ~10 fps: the Windows client sends ~12 fps and a
     * higher rate only burns CPU/link and crowds audio out of the shared TCP
     * socket (which is what made speech stutter). */
    private const val VIDEO_SEND_INTERVAL_MS = 100L

    // Audio pipeline sizing (see AudioEngine): capture enqueues into a bounded
    // queue drained by an audio-priority sender thread; playback runs through
    // a jitter buffer. All caps use drop-oldest so latency can never grow
    // without bound.
    private const val AUDIO_TX_QUEUE_CAP = 64 // ~1.28s of 20ms chunks
    private const val PLAY_PREROLL_CHUNKS = 4 // ~80ms jitter buffer before playback
    private const val PLAY_QUEUE_CAP = 32 // ~640ms playback queue, drop-oldest
    private const val UNDERRUN_SILENCE_CHUNKS = 8 // ~160ms silence before re-priming

    sealed class CallState {
        data object Idle : CallState()
        data class Outgoing(val peerId: String, val peerName: String, val callId: String) : CallState()
        data class Incoming(
            val callId: String,
            val callerId: String,
            val callerName: String,
            val mediaPort: Int
        ) : CallState()

        data class Active(val peerId: String, val peerName: String, val callId: String) : CallState()
    }

    private val _state = MutableStateFlow<CallState>(CallState.Idle)
    val state: StateFlow<CallState> = _state.asStateFlow()

    /** Remote video frame (rotated/scaled Bitmap) for the in-call UI. */
    private val _remoteVideo = MutableStateFlow<Bitmap?>(null)
    val remoteVideo: StateFlow<Bitmap?> = _remoteVideo.asStateFlow()

    /** Mirrored local preview frame. */
    private val _localVideo = MutableStateFlow<Bitmap?>(null)
    val localVideo: StateFlow<Bitmap?> = _localVideo.asStateFlow()

    private val _audioMuted = MutableStateFlow(false)
    val audioMuted: StateFlow<Boolean> = _audioMuted.asStateFlow()

    private val _videoMuted = MutableStateFlow(false)
    val videoMuted: StateFlow<Boolean> = _videoMuted.asStateFlow()

    /** Whether the outgoing camera is the front (selfie) one; false = back. */
    private val _usingFrontCamera = MutableStateFlow(false)
    val usingFrontCamera: StateFlow<Boolean> = _usingFrontCamera.asStateFlow()

    /** Transient events (rejected/failed/ended...) surfaced as toasts. */
    private val _events = MutableSharedFlow<String>(extraBufferCapacity = 16)
    val events: SharedFlow<String> = _events.asSharedFlow()

    private val lock = Any()
    /** Where call signaling goes out (group relay or direct session). */
    private var channel: CallChannel? = null
    /** Identity of the signaling owner (P2PManager or DirectChatManager);
     *  [endIfOn] compares against it to only end calls on the right channel. */
    private var identity: Any? = null
    /** Our own device id/name for this call (set on both roles). */
    private var myCallerId = ""
    private var myCallerName = ""
    private var peerId = ""
    private var peerName = ""
    private var peerIp = ""
    private var callId = ""
    private var role = "" // "caller" | "callee"
    private var mediaPort = 0
    private var mediaServer: ServerSocket? = null
    private var mediaSocket: Socket? = null
    /** Session key negotiated by the media handshake; every frame is
     *  AES-GCM encrypted under it (nonce travels in the frame). */
    @Volatile
    private var mediaKey: ByteArray? = null

    /** Outgoing audio chunks awaiting the socket; drained by the media sender
     * thread with strict priority over video. Drop-oldest on overflow so a
     * slow link cannot grow call latency forever. */
    private val audioTxQueue = LinkedBlockingQueue<ByteArray>(AUDIO_TX_QUEUE_CAP)

    /** Latest video frame waiting to be sent; replaced (never queued up) so a
     * stale frame is dropped the moment a newer one arrives. */
    @Volatile
    private var pendingVideo: ByteArray? = null

    /** Single writer for the media socket: audio first, video in the gaps. */
    private var senderThread: Thread? = null
    private var watchdogThread: Thread? = null
    @Volatile
    private var running = false

    private var videoEngine: VideoEngine? = null
    private var audioEngine: AudioEngine? = null
    @Volatile
    private var lifecycleOwner: LifecycleOwner? = null

    /** Bytes of remote audio received on the media socket (for verification). */
    @Volatile
    private var receivedAudioBytes = 0L
    private var lastLoggedAudioBytes = 0L

    /** The UI registers its lifecycle so CameraX can bind the camera. */
    fun attachLifecycle(owner: LifecycleOwner?) {
        lifecycleOwner = owner
    }

    // ------------------------------------------------------------- signaling

    /** Network-thread entry: P2PManager delivers group call packets here. */
    fun handleSignal(p2p: P2PManager, packet: NetworkPacket) {
        val call = packet.call ?: return
        when (packet.type) {
            "call_offer" -> onCallOffer(
                channel = CallChannel { pid, pkt -> p2p.sendTargeted(pid, pkt) },
                identity = p2p,
                call = call,
                callerIp = p2p.peers.value[call.callerId]?.ipAddress,
                localId = p2p.myIdValue,
                localName = p2p.myNameValue
            )
            "call_answer" -> onCallAnswer(call)
            "call_reject" -> onCallReject(call)
            "call_failed" -> onCallFailed(call)
            "call_hangup" -> onCallHangup(call)
        }
    }

    /** Session-thread entry: a direct member chat forwards call packets here.
     *  [channel] delivers the reply over the direct session; [callerIp] is the
     *  caller's address (resolved by the ViewModel from the contacts). */
    fun handleDirectSignal(
        channel: CallChannel,
        identity: Any,
        packet: NetworkPacket,
        callerIp: String?,
        localId: String,
        localName: String
    ) {
        val call = packet.call ?: return
        when (packet.type) {
            "call_offer" -> onCallOffer(channel, identity, call, callerIp, localId, localName)
            "call_answer" -> onCallAnswer(call)
            "call_reject" -> onCallReject(call)
            "call_failed" -> onCallFailed(call)
            "call_hangup" -> onCallHangup(call)
        }
    }

    private fun onCallOffer(
        channel: CallChannel,
        identity: Any,
        call: CallInfo,
        callerIp: String?,
        localId: String,
        localName: String
    ) {
        if (call.mediaPort <= 0 || call.callerId.isEmpty()) return
        if (call.calleeId != localId) return
        synchronized(lock) {
            if (_state.value !is CallState.Idle) {
                // busy: decline directly to the offerer
                val reply = CallInfo(
                    callId = call.callId,
                    callerId = call.callerId,
                    callerName = call.callerName,
                    calleeId = call.calleeId
                )
                channel.send(call.callerId, NetworkPacket(type = "call_reject", call = reply))
                return
            }
            this.channel = channel
            this.identity = identity
            myCallerId = localId
            myCallerName = localName
            callId = call.callId
            role = "callee"
            peerId = call.callerId
            peerName = call.callerName
            peerIp = callerIp ?: ""
            mediaPort = call.mediaPort
            _state.value = CallState.Incoming(call.callId, call.callerId, call.callerName, call.mediaPort)
        }
    }

    private fun onCallAnswer(call: CallInfo) {
        // The media socket may already have activated the call (see
        // acceptLoop); the answer is then redundant confirmation.
        synchronized(lock) {
            // An answer is addressed to the original caller — this node.
            if (call.callerId != myCallerId) return
            val cur = _state.value
            val curCallId = (cur as? CallState.Outgoing)?.callId
                ?: (cur as? CallState.Incoming)?.callId
                ?: (cur as? CallState.Active)?.callId
                ?: return
            if (curCallId != call.callId) return
            if (cur is CallState.Outgoing) {
                _state.value = CallState.Active(cur.peerId, cur.peerName, cur.callId)
            } else if (cur !is CallState.Active) {
                return
            }
        }
        startEngines()
    }

    private fun onCallReject(call: CallInfo) {
        synchronized(lock) {
            // Only the call's original caller (this node) may be rejected.
            if (call.callerId != myCallerId) return
            val cur = _state.value
            if (cur !is CallState.Outgoing || cur.callId != call.callId) return
            reset()
        }
        _events.tryEmit("对方拒绝了通话")
    }

    private fun onCallFailed(call: CallInfo) {
        synchronized(lock) {
            // Only the call's original caller (this node) receives failures.
            if (call.callerId != myCallerId) return
            val cur = _state.value
            if (cur !is CallState.Outgoing || cur.callId != call.callId) return
            reset()
        }
        _events.tryEmit("通话建立失败，无法连接到对方")
    }

    private fun onCallHangup(call: CallInfo) {
        val ringing: Boolean
        val srv: ServerSocket?
        val sock: Socket?
        synchronized(lock) {
            // A hangup must come from a participant of THIS node's call.
            if (call.callerId != myCallerId && call.calleeId != myCallerId) return
            val cur = _state.value
            val curCallId = (cur as? CallState.Outgoing)?.callId
                ?: (cur as? CallState.Incoming)?.callId
                ?: (cur as? CallState.Active)?.callId
                ?: return
            if (curCallId != call.callId) return
            ringing = cur is CallState.Incoming || cur is CallState.Outgoing
            srv = mediaServer
            sock = mediaSocket
            reset()
        }
        stopEngines()
        closeSockets(srv, sock)
        _events.tryEmit(if (ringing) "对方取消了通话" else "对方已挂断")
    }

    // ------------------------------------------------------------ public API

    /** Start a call with a member of the active GROUP (signaling via the
     *  group's P2PManager relay). */
    fun startCall(p2p: P2PManager, peer: Peer) =
        startCall(
            peer,
            CallChannel { pid, pkt -> p2p.sendTargeted(pid, pkt) },
            p2p,
            p2p.myIdValue,
            p2p.myNameValue
        )

    /** Start a call over an arbitrary signaling channel (group relay or
     *  direct session). [callerId]/[callerName] are OUR identity. */
    fun startCall(
        peer: Peer,
        channel: CallChannel,
        identity: Any,
        callerId: String,
        callerName: String
    ) {
        synchronized(lock) {
            if (_state.value !is CallState.Idle) {
                _events.tryEmit("已有进行中的通话")
                return
            }
            if (peer.ipAddress.isEmpty()) {
                _events.tryEmit("无法发起通话：成员不在线")
                return
            }
            val server = try {
                ServerSocket(0).apply { reuseAddress = true }
            } catch (e: IOException) {
                _events.tryEmit("无法开启通话端口")
                return
            }
            this.channel = channel
            this.identity = identity
            myCallerId = callerId
            myCallerName = callerName
            peerId = peer.id
            peerName = peer.name
            peerIp = peer.ipAddress
            callId = UUID.randomUUID().toString()
            role = "caller"
            mediaPort = server.localPort
            mediaServer = server
            running = true
            _state.value = CallState.Outgoing(peer.id, peer.name, callId)
            val offer = NetworkPacket(
                type = "call_offer",
                call = CallInfo(
                    callId = callId,
                    callerId = callerId,
                    callerName = callerName,
                    calleeId = peer.id,
                    mediaPort = mediaPort
                )
            )
            channel.send(peer.id, offer)
            Thread { acceptLoop(server, callId) }.start()
        }
    }

    fun acceptCall() {
        val targetIp: String
        val targetPort: Int
        val currentCallId: String
        synchronized(lock) {
            val cur = _state.value
            if (cur !is CallState.Incoming) return
            targetIp = peerIp
            targetPort = mediaPort
            currentCallId = cur.callId
        }
        if (targetIp.isEmpty() || targetPort <= 0) {
            rejectCall()
            return
        }
        Thread {
            var sock: Socket? = null
            var handedOff = false
            try {
                val s = Socket()
                sock = s
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(targetIp, targetPort), CONNECT_TIMEOUT_MS)
                s.soTimeout = 10_000
                // Identity handshake on the media socket: proves the caller's
                // long-term key and derives the frame-encryption session key.
                val wire = Wire(
                    RawLineIn(s.getInputStream()),
                    PrintWriter(s.getOutputStream(), true)
                )
                val secured = Handshake.initiateDirect(
                    wire,
                    // an "ip:..." placeholder is not a stable device id: binding
                    // TOFU state to it would false-positive once the real id is
                    // known (the handshake still authenticates the caller's
                    // long-term key via its signature)
                    expectedPeerId = peerId.takeIf { it.isNotEmpty() && !it.startsWith("ip:") },
                    onIdentityMismatch = {
                        _events.tryEmit("安全警告：对方媒体身份验证失败，通话已结束")
                    }
                )
                  // Bind this TCP connection to the call before activating it.
                  // The media port is reachable by anyone on the LAN; without
                  // this, a random device could complete the identity handshake
                  // and make the caller's phone ring/activate.
                  wire.sendPacket(
                      NetworkPacket(
                          type = "call_media_hello",
                          call = CallInfo(
                              callId = currentCallId,
                              callerId = peerId,
                              callerName = peerName,
                              calleeId = myCallerId
                          )
                      )
                  )
                val handoff: Boolean
                synchronized(lock) {
                    val cur = _state.value
                    handoff = cur is CallState.Incoming && cur.callId == currentCallId
                    if (handoff) {
                        mediaSocket = s
                        mediaKey = wire.sessionKey
                        running = true
                        _state.value = CallState.Active(cur.callerId, cur.callerName, cur.callId)
                    }
                }
                if (!handoff) {
                    s.close()
                    return@Thread
                }
                handedOff = true
                val ch = channel
                if (ch != null) {
                    val call = CallInfo(
                        callId = currentCallId,
                        callerId = peerId,
                        callerName = peerName,
                        calleeId = myCallerId
                    )
                    ch.send(peerId, NetworkPacket(type = "call_answer", call = call))
                }
                if (!startEnginesSafe()) {
                    // the media socket already connected; end the call with a
                    // clear message instead of blaming the firewall/network
                    endCall("通话组件启动失败，请重试", isError = true)
                    return@Thread
                }
                // startReadLoop spawns its own thread and returns immediately:
                // the media socket must stay open for the whole call, so it is
                // only closed here when the call never took ownership of it
                // (connect failed / no handoff) — endCall/hangup close it
                // otherwise.
                startReadLoop(s)
            } catch (e: Exception) {
                Log.w(TAG, "media connect failed", e)
                val ch = channel
                if (ch != null) {
                    val call = CallInfo(
                        callId = currentCallId,
                        callerId = peerId,
                        callerName = peerName,
                        calleeId = myCallerId
                    )
                    ch.send(
                        peerId,
                        NetworkPacket(
                            type = "call_failed",
                            call = call,
                            errorMessage = "无法连接媒体通道"
                        )
                    )
                }
                // Include the exact target so the user can tell a firewall drop
                // (timeout) apart from a wrong/unreachable address.
                endCall(
                    "无法连接媒体通道（$targetIp:$targetPort）。请检查创建者电脑的防火墙，或确认两台设备在同一网络",
                    isError = true
                )
            } finally {
                if (!handedOff) {
                    runCatching { sock?.close() }
                }
            }
        }.start()
    }

    fun rejectCall() {
        synchronized(lock) {
            if (_state.value !is CallState.Incoming) return
            sendCallPacket("call_reject", callId)
            reset()
        }
    }

    fun hangup() {
        val srv: ServerSocket?
        val sock: Socket?
        synchronized(lock) {
            if (_state.value is CallState.Idle) return
            sendCallPacket("call_hangup", callId)
            srv = mediaServer
            sock = mediaSocket
            reset()
        }
        stopEngines()
        closeSockets(srv, sock)
        _events.tryEmit("已挂断")
    }

    /**
     * End a call when its signaling transport is no longer usable. Direct-chat
     * sessions share one [identity], so [peerId] additionally scopes a direct
     * session-close event to the call participant; an unrelated contact
     * reconnecting must not tear down this call.
     */
    fun endIfOn(identity: Any, reason: String, peerId: String? = null) {
        synchronized(lock) {
            if (_state.value is CallState.Idle || this.identity !== identity) return
            if (peerId != null && this.peerId != peerId) return
        }
        endCall(reason)
    }

    fun setAudioMuted(muted: Boolean) {
        _audioMuted.value = muted
    }

    fun setVideoMuted(muted: Boolean) {
        _videoMuted.value = muted
    }

    /** Flip between the front and back camera during an active call. */
    fun switchCamera() {
        if (_state.value !is CallState.Active) return
        val engine = videoEngine ?: return
        _usingFrontCamera.value = !_usingFrontCamera.value
        engine.switchCamera()
    }

    // ------------------------------------------------------------ internals

    private fun sendCallPacket(type: String, callId: String) {
        val ch = channel ?: return
        val callerId: String
        val callerName: String
        val calleeId: String
        synchronized(lock) {
            if (role == "caller") {
                callerId = myCallerId; callerName = myCallerName; calleeId = peerId
            } else {
                callerId = peerId; callerName = peerName; calleeId = myCallerId
            }
        }
        ch.send(
            peerId,
            NetworkPacket(
                type = type,
                call = CallInfo(callId, callerId, callerName, calleeId)
            )
        )
    }

    private fun acceptLoop(server: ServerSocket, currentCallId: String) {
        var accepted: Socket? = null
        var handedOff = false
        try {
            server.soTimeout = RING_TIMEOUT_MS.toInt()
            val sock = server.accept()
            accepted = sock
            sock.tcpNoDelay = true
            // The media port is reachable by anyone on the LAN: NEVER activate
            // on the bare TCP accept. Activation happens only after the
            // identity handshake AND call_media_hello below prove this
            // connection belongs to the current call (parity with the Windows
            // _accept_loop, which also validates before emitting the socket).
            sock.soTimeout = 10_000
            val pre = _state.value
            if (pre !is CallState.Outgoing || pre.callId != currentCallId) {
                return
            }
            // Identity handshake on the media socket: the callee (initiator)
            // proves its long-term key; frames after this are encrypted. Raw
            // line IO — a BufferedReader would swallow the first frames.
            // "ip:..." placeholders are not stable device ids (same rule as
            // the outgoing side): never bind TOFU state to one
            val expectedPeer = synchronized(lock) { peerId }
                .takeIf { it.isNotEmpty() && !it.startsWith("ip:") }
            val wire = Wire(
                RawLineIn(sock.getInputStream()),
                PrintWriter(sock.getOutputStream(), true)
            )
            val start = wire.recvRaw()
            val secured = if (start != null) {
                Handshake.acceptDirect(wire, start, expectedPeer) {
                    _events.tryEmit("安全警告：对方媒体身份验证失败，通话已结束")
                }
            } else null
            if (secured == null) {
                endCall("安全握手失败，通话已结束", isError = true)
                return
            }
            mediaKey = wire.sessionKey
              val hello = wire.recvPacket()
              val helloCall = hello?.call
              if (hello?.type != "call_media_hello" || helloCall == null ||
                  helloCall.callId != currentCallId ||
                  helloCall.callerId != myCallerId ||
                  helloCall.calleeId != peerId
              ) {
                  endCall("媒体通道校验失败，通话已结束", isError = true)
                  return
              }
              // Deferred TOFU binding: acceptDirect ran with remember=false, so
              // only a connection that proved knowledge of the callId is bound
              // to the expected peer identity.
              if (expectedPeer != null) {
                  DeviceIdentity.checkPeer(expectedPeer, secured.peerIdent!!)
              }
            // Everything verified: hand the socket over and go active NOW
            // (the callee only connects after accepting, so this connection
            // IS the answer; the call_answer packet is redundant confirmation).
            val activate: Boolean
            synchronized(lock) {
                val cur = _state.value
                if (cur is CallState.Outgoing && cur.callId == currentCallId) {
                    mediaSocket = sock
                    _state.value = CallState.Active(cur.peerId, cur.peerName, cur.callId)
                    activate = true
                } else {
                    activate = false
                }
            }
            if (!activate) {
                return
            }
            handedOff = true
            if (!startEnginesSafe()) {
                endCall("通话组件启动失败，请重试", isError = true)
                return
            }
            startReadLoop(sock)
        } catch (e: java.net.SocketTimeoutException) {
            synchronized(lock) {
                if (_state.value is CallState.Outgoing) {
                    sendCallPacket("call_hangup", currentCallId)
                    reset()
                }
            }
            _events.tryEmit("对方未接听")
        } catch (e: Exception) {
            // server closed (call cancelled)
        } finally {
            // Before activation the socket is not tracked by any field, so
            // every bail-out path (verification failure, stale call state,
            // exception) must close it here or it leaks.
            if (!handedOff) {
                runCatching { accepted?.close() }
            }
        }
    }

    private fun endCall(reason: String, isError: Boolean = false) {
        val srv: ServerSocket?
        val sock: Socket?
        synchronized(lock) {
            if (_state.value is CallState.Idle) return
            srv = mediaServer
            sock = mediaSocket
            reset()
        }
        stopEngines()
        closeSockets(srv, sock)
        _events.tryEmit(if (isError) reason else "通话结束")
    }

    private fun reset() {
        channel = null
        identity = null
        myCallerId = ""
        myCallerName = ""
        peerId = ""
        peerName = ""
        peerIp = ""
        callId = ""
        role = ""
        mediaPort = 0
        mediaServer = null
        mediaSocket = null
        mediaKey = null
        running = false
        audioTxQueue.clear()
        pendingVideo = null
        _state.value = CallState.Idle
        _remoteVideo.value = null
        _localVideo.value = null
        _audioMuted.value = false
        _videoMuted.value = false
        _usingFrontCamera.value = false
    }

    private fun closeSockets(srv: ServerSocket?, sock: Socket?) {
        runCatching { srv?.close() }
        runCatching { sock?.close() }
    }

    private fun startEngines() {
        // Atomic with reset/stopEngines: the engines hold the mic and camera.
        // An engine created for a call that already ended — or a second engine
        // created concurrently by the accept loop and the call_answer signal
        // racing past the old unlocked null-check — would never be stopped,
        // so its AudioRecord keeps the mic in use and the status-bar mic
        // indicator never clears.
        synchronized(lock) {
            if (!running || _state.value !is CallState.Active) return
            val ctx = ChatApp.instance
            if (videoEngine == null) {
                videoEngine = VideoEngine(
                    ctx,
                    lifecycleOwner,
                    ::onLocalJpeg,
                    ::onLocalPreview,
                    onUnavailable = {
                        _events.tryEmit("摄像头不可用，对方将看到黑屏（通话仍可进行）")
                    },
                    usingFront = { _usingFrontCamera.value },
                    onSwitchFailed = {
                        // the requested camera could not be bound; fall back to back
                        _usingFrontCamera.value = false
                        _events.tryEmit("无法切换摄像头")
                    }
                )
                videoEngine?.start()
            }
            if (audioEngine == null) {
                audioEngine = AudioEngine(ctx, ::onLocalAudio)
                audioEngine?.start()
            }
            startSender()
            startVideoWatchdog()
        }
    }

    /**
     * Start capture/playback engines; returns false when any engine fails so
     * the caller can end the call with a clear message instead of reporting a
     * media-connect failure (or staying silently ACTIVE without media).
     */
    private fun startEnginesSafe(): Boolean {
        return try {
            startEngines()
            true
        } catch (e: Exception) {
            Log.w(TAG, "media engines failed to start", e)
            false
        }
    }

    /** Timestamp of the last media frame sent on the media socket. */
    @Volatile
    private var lastMediaSentAt = 0L

    private var mutedFrameCount = 0

    private fun onLocalJpeg(jpeg: ByteArray) {
        if (_videoMuted.value) {
            // keep the media socket alive even when video is off (audio
            // silence may be unavailable if the mic failed to open)
            if (++mutedFrameCount % 12 == 0) pendingVideo = blackJpeg
            return
        }
        pendingVideo = jpeg
    }

    /**
     * While the call is active, keep the media socket alive no matter what:
     * whenever no frame has been sent for 3 seconds (camera failed to bind,
     * mic unavailable, video muted with a dead camera, ...) send a black
     * frame, so the remote side never hits its read timeout and the call
     * never drops with "no signal".
     */
    private fun startVideoWatchdog() {
        if (watchdogThread?.isAlive == true) return
        watchdogThread = Thread {
            try {
                while (running) {
                    Thread.sleep(2000)
                    if (!running) break
                    if (System.currentTimeMillis() - lastMediaSentAt > 3000) {
                        pendingVideo = blackJpeg
                    }
                }
            } catch (e: InterruptedException) {
                // shutting down
            }
        }.apply {
            isDaemon = true
            start()
        }
    }

    /** Tiny black JPEG sent periodically while video is muted. */
    private val blackJpeg: ByteArray by lazy {
        val bmp = Bitmap.createBitmap(160, 120, Bitmap.Config.ARGB_8888)
        bmp.eraseColor(android.graphics.Color.BLACK)
        val bos = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, 50, bos)
        bos.toByteArray()
    }

    private fun stopEngines() {
        val video: VideoEngine?
        val audio: AudioEngine?
        synchronized(lock) {
            video = videoEngine
            audio = audioEngine
            videoEngine = null
            audioEngine = null
            stopSender()
        }
        // Stop outside the lock: AudioEngine.stop() joins its capture and
        // playback threads and must not block call signaling.
        video?.stop()
        audio?.stop()
    }

    // ------------------------------------------------------------- media I/O

    private fun onLocalPreview(bitmap: Bitmap) {
        _localVideo.value = bitmap
    }

    private fun onLocalAudio(pcm: ByteArray) {
        val data = if (_audioMuted.value) ByteArray(pcm.size) else pcm
        // Drop-oldest when the sender cannot keep up: better to lose the
        // oldest 20ms than to block the microphone read (which would overrun
        // the AudioRecord buffer and garble speech) or to grow latency.
        while (!audioTxQueue.offer(data)) audioTxQueue.poll()
    }

    /** Audio-priority media sender: audio chunks first, video frames only in
     *  the gaps, so a big/slow video frame can never queue audio behind it or
     *  block the capture thread.
     *
     *  NB: video must be sent right AFTER each audio chunk, not only when the
     *  audio queue times out — audio is produced every 20ms for the whole
     *  call (silence frames included), so a blocking poll would starve video
     *  entirely and the peer would never receive frames. The video source is
     *  throttled to ~10fps, so at most one frame is pending per ~100ms.
     *
     *  Must be called with [lock] held so the isAlive guard is atomic. */
    private fun startSender() {
        if (senderThread?.isAlive == true) return
        senderThread = Thread {
            try {
                while (running) {
                    val audio = audioTxQueue.poll(100, TimeUnit.MILLISECONDS)
                    if (audio != null) {
                        writeMedia(CH_AUDIO, audio)
                        sendPendingVideo()
                        continue
                    }
                    sendPendingVideo()
                }
            } catch (e: InterruptedException) {
                // shutting down
            } catch (e: Exception) {
                endCall("连接已断开")
            }
        }.apply {
            isDaemon = true
            name = "call-media-sender"
            start()
        }
    }

    /** Send the newest queued video frame, if any. Runs on the sender thread. */
    private fun sendPendingVideo() {
        val video = pendingVideo
        if (video != null) {
            pendingVideo = null
            writeMedia(CH_VIDEO, video)
        }
    }

    private fun stopSender() {
        senderThread?.interrupt()
        senderThread = null
        audioTxQueue.clear()
        pendingVideo = null
    }

    /** Write one framed, AES-GCM encrypted media packet. Runs only on the
     *  sender thread, so no interleaving with the capture loop is possible.
     *  Frame: [1B channel][4B length][12B nonce][ciphertext||tag]. */
    private fun writeMedia(channel: Int, payload: ByteArray) {
        val sock = mediaSocket
        val key = mediaKey
        if (running && sock != null && key != null) {
            val blob = Crypto.aesGcmEncrypt(key, payload)
            val header = ByteArray(5)
            header[0] = channel.toByte()
            header[1] = (blob.size ushr 24).toByte()
            header[2] = (blob.size ushr 16).toByte()
            header[3] = (blob.size ushr 8).toByte()
            header[4] = blob.size.toByte()
            val out = sock.getOutputStream()
            out.write(header)
            out.write(blob)
            out.flush()
            lastMediaSentAt = System.currentTimeMillis()
        }
    }

    private fun startReadLoop(sock: Socket) {
        Thread {
            var reason = "对方已挂断"
            try {
                sock.soTimeout = MEDIA_READ_TIMEOUT_MS
                val input = sock.getInputStream()
                while (running && !sock.isClosed) {
                    val header = readExact(input, 5) ?: break
                    val channel = header[0].toInt() and 0xFF
                    val length = ((header[1].toInt() and 0xFF) shl 24) or
                        ((header[2].toInt() and 0xFF) shl 16) or
                        ((header[3].toInt() and 0xFF) shl 8) or
                        (header[4].toInt() and 0xFF)
                    if (length < Crypto.GCM_NONCE_LEN + 16 || length > MAX_FRAME_WIRE_LEN) break
                    val blob = readExact(input, length) ?: break
                    val key = mediaKey ?: break
                    val payload = try {
                        Crypto.aesGcmDecrypt(key, blob)
                    } catch (e: Exception) {
                        Log.w(TAG, "media frame failed authentication", e)
                        break
                    }
                    if (channel == CH_VIDEO) {
                        val bmp = decodeVideoFrame(payload)
                        if (bmp != null) _remoteVideo.value = bmp
                    } else if (channel == CH_AUDIO) {
                        audioEngine?.enqueuePlayback(payload)
                        receivedAudioBytes += payload.size
                        if (receivedAudioBytes - lastLoggedAudioBytes > 16000 * 2 * 2) {
                            lastLoggedAudioBytes = receivedAudioBytes
                            Log.i(TAG, "rx audio bytes=$receivedAudioBytes")
                        }
                    }
                }
            } catch (e: java.net.SocketTimeoutException) {
                reason = "连接已断开"
            } catch (e: Exception) {
                Log.w(TAG, "media read loop ended", e)
            } finally {
                endCall(reason)
            }
        }.start()
    }

    private fun readExact(input: java.io.InputStream, n: Int): ByteArray? {
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

    /** bounds-first 两段解码收到的 JPEG 帧：先用 inJustDecodeBounds 取
     *  尺寸，任一边超过 MAX_VIDEO_FRAME_DIMEN（解压炸弹）直接丢帧；
     *  正常帧按需 inSampleSize 降采样到 ~VIDEO_DECODE_TARGET_DIMEN 再
     *  解码，避免一张超大图分配出巨大的位图。 */
    private fun decodeVideoFrame(payload: ByteArray): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(payload, 0, payload.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            Log.w(TAG, "drop undecodable video frame (${payload.size} bytes)")
            return null
        }
        if (bounds.outWidth > MAX_VIDEO_FRAME_DIMEN || bounds.outHeight > MAX_VIDEO_FRAME_DIMEN) {
            Log.w(TAG, "drop oversized video frame ${bounds.outWidth}x${bounds.outHeight}")
            return null
        }
        val opts = BitmapFactory.Options().apply {
            inSampleSize = videoInSampleSize(bounds.outWidth, bounds.outHeight)
        }
        return BitmapFactory.decodeByteArray(payload, 0, payload.size, opts)
    }

    /** 2 的幂次 inSampleSize：解码后最长边不超过 VIDEO_DECODE_TARGET_DIMEN。 */
    private fun videoInSampleSize(width: Int, height: Int): Int {
        var sample = 1
        while (maxOf(width, height) / (sample * 2) >= VIDEO_DECODE_TARGET_DIMEN) {
            sample *= 2
        }
        return sample
    }

    // -------------------------------------------------------------- engines

    /** CameraX-based video capture with rotation/scale; falls back to no video
     * when the camera cannot be opened. YUV_420_888 frames are converted to
     * NV21 and JPEG-encoded (the JPEG output format is @RestrictTo internal).
     * The front/back camera can be flipped mid-call via [switchCamera]. */
    private class VideoEngine(
        private val context: Context,
        private val lifecycleOwner: LifecycleOwner?,
        private val onJpeg: (ByteArray) -> Unit,
        private val onPreview: (Bitmap) -> Unit,
        private val onUnavailable: (() -> Unit)? = null,
        private val usingFront: () -> Boolean,
        private val onSwitchFailed: (() -> Unit)? = null
    ) {
        @Volatile
        private var stopped = false
        private val analyzerExecutor = Executors.newSingleThreadExecutor()
        private var camera: Camera? = null
        private var analysis: ImageAnalysis? = null

        private fun cameraSelector(): CameraSelector =
            if (usingFront()) CameraSelector.DEFAULT_FRONT_CAMERA
            else CameraSelector.DEFAULT_BACK_CAMERA

        fun start() {
            val owner = lifecycleOwner
            if (owner == null) {
                Log.w(TAG, "no lifecycle owner; video disabled")
                onUnavailable?.invoke()
                return
            }
            val mainHandler = Handler(Looper.getMainLooper())
            Thread {
                try {
                    val provider = ProcessCameraProvider.getInstance(context).get(4, TimeUnit.SECONDS)
                    mainHandler.post {
                        if (stopped) return@post
                        if (analysis == null) {
                            analysis = ImageAnalysis.Builder()
                                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                                .setResolutionSelector(
                                    ResolutionSelector.Builder()
                                        .setResolutionStrategy(
                                            ResolutionStrategy(
                                                Size(640, 480),
                                                ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER
                                            )
                                        )
                                        .build()
                                )
                                .build()
                                .also { it.setAnalyzer(analyzerExecutor) { image -> analyze(image) } }
                        }
                        bind(provider, owner, firstBind = true)
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "camera provider unavailable", e)
                    onUnavailable?.invoke()
                }
            }.start()
        }

        /** Flip front/back; the selector is read from [usingFront] at bind time. */
        fun switchCamera() {
            val owner = lifecycleOwner
            if (owner == null) return
            val mainHandler = Handler(Looper.getMainLooper())
            Thread {
                try {
                    val provider = ProcessCameraProvider.getInstance(context).get(4, TimeUnit.SECONDS)
                    mainHandler.post {
                        if (stopped) return@post
                        bind(provider, owner, firstBind = false)
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "camera provider unavailable on switch", e)
                    onSwitchFailed?.invoke()
                }
            }.start()
        }

        private fun bind(provider: ProcessCameraProvider, owner: LifecycleOwner, firstBind: Boolean) {
            val useCase = analysis ?: return
            val selector = cameraSelector()
            if (!provider.hasCamera(selector)) {
                Log.w(TAG, "camera unavailable: $selector")
                if (firstBind) onUnavailable?.invoke() else onSwitchFailed?.invoke()
                return
            }
            try {
                provider.unbindAll()
                camera = provider.bindToLifecycle(owner, selector, useCase)
            } catch (e: Exception) {
                Log.w(TAG, "camera bind failed", e)
                if (firstBind) onUnavailable?.invoke() else onSwitchFailed?.invoke()
            }
        }

        @Volatile
        private var lastFrameSentAt = 0L

        private fun analyze(image: ImageProxy) {
            try {
                // Throttle the encode+send pipeline to ~10 fps: the camera can
                // deliver 30 fps, and encoding/sending every frame only wastes
                // CPU and crowds audio out of the shared TCP socket.
                val now = System.currentTimeMillis()
                if (now - lastFrameSentAt < VIDEO_SEND_INTERVAL_MS) return
                lastFrameSentAt = now
                val nv21 = yuv420ToNv21(image) ?: return
                val yuv = android.graphics.YuvImage(
                    nv21, android.graphics.ImageFormat.NV21,
                    image.width, image.height, null
                )
                val jpegOut = ByteArrayOutputStream()
                yuv.compressToJpeg(
                    android.graphics.Rect(0, 0, image.width, image.height),
                    VIDEO_JPEG_QUALITY,
                    jpegOut
                )
                val src = BitmapFactory.decodeByteArray(jpegOut.toByteArray(), 0, jpegOut.size())
                    ?: return
                val rotation = image.imageInfo.rotationDegrees
                val upright: Bitmap = if (rotation != 0) {
                    val matrix = Matrix().apply { postRotate(rotation.toFloat()) }
                    Bitmap.createBitmap(src, 0, 0, src.width, src.height, matrix, true)
                } else {
                    src
                }
                val maxEdge = maxOf(upright.width, upright.height)
                val out = if (maxEdge > VIDEO_MAX_EDGE) {
                    val scale = VIDEO_MAX_EDGE.toFloat() / maxEdge
                    Bitmap.createScaledBitmap(
                        upright,
                        (upright.width * scale).toInt(),
                        (upright.height * scale).toInt(),
                        true
                    )
                } else {
                    upright
                }
                val outJpeg = ByteArrayOutputStream()
                out.compress(Bitmap.CompressFormat.JPEG, VIDEO_JPEG_QUALITY, outJpeg)
                onJpeg(outJpeg.toByteArray())
                // selfie-style mirrored local preview only for the front camera
                // (the sent frame is never mirrored, matching the Windows client)
                val mirror = if (usingFront()) Matrix().apply { postScale(-1f, 1f) } else Matrix()
                val preview = Bitmap.createBitmap(out, 0, 0, out.width, out.height, mirror, true)
                onPreview(preview)
            } catch (e: Exception) {
                Log.w(TAG, "analyze failed", e)
            } finally {
                image.close()
            }
        }

        /** Pack a YUV_420_888 ImageProxy into a contiguous NV21 byte array.
         *  The output is tightly packed (Y: w*h, then VU interleaved: w*h/2),
         *  so row strides/pixel strides with padding are handled by copying
         *  per row; sizing by w*h*3/2 never overflows. */
        private fun yuv420ToNv21(image: ImageProxy): ByteArray? {
            try {
                val yPlane = image.planes[0]
                val uPlane = image.planes[1]
                val vPlane = image.planes[2]
                val w = image.width
                val h = image.height
                val yBuffer = yPlane.buffer
                val uBuffer = uPlane.buffer
                val vBuffer = vPlane.buffer
                val out = ByteArray(w * h * 3 / 2)
                var pos = 0
                // Y plane: w bytes per row (rowStride may include padding)
                val yStride = yPlane.rowStride
                val yPixelStride = yPlane.pixelStride
                if (yStride == w && yPixelStride == 1) {
                    yBuffer.position(0)
                    yBuffer.get(out, 0, w * h)
                    pos = w * h
                } else {
                    for (row in 0 until h) {
                        yBuffer.position(row * yStride)
                        yBuffer.get(out, pos, w)
                        pos += w
                    }
                }
                // UV planes: VU interleaved, w/2 entries per row, h/2 rows
                val uvStride = uPlane.rowStride
                val uvPixelStride = uPlane.pixelStride
                for (row in 0 until h / 2) {
                    var srcRow = row * uvStride
                    for (col in 0 until w / 2) {
                        out[pos++] = vBuffer.get(srcRow)
                        out[pos++] = uBuffer.get(srcRow)
                        srcRow += uvPixelStride
                    }
                }
                return out
            } catch (e: Exception) {
                Log.w(TAG, "yuv conversion failed", e)
                return null
            }
        }

        fun stop() {
            stopped = true
            analyzerExecutor.shutdownNow()
            // resolve the provider off the main thread, unbind on the main thread
            Thread {
                val provider = runCatching {
                    ProcessCameraProvider.getInstance(context).get(2, TimeUnit.SECONDS)
                }.getOrNull() ?: return@Thread
                Handler(Looper.getMainLooper()).post {
                    runCatching { provider.unbindAll() }
                }
            }.start()
            camera = null
        }
    }

    /**
     * AudioRecord capture + AudioTrack playback of PCM16 mono 16 kHz.
     *
     * Quality-focused design:
     * - Capture uses [MediaRecorder.AudioSource.VOICE_COMMUNICATION] so the
     *   platform's AEC/NS/AGC preprocessor runs while in
     *   MODE_IN_COMMUNICATION (a raw [MediaRecorder.AudioSource.MIC] capture
     *   with the speaker on = echo and howling). Explicit AcousticEchoCanceler
     *   / NoiseSuppressor / AutomaticGainControl are attached to the record
     *   session too, for devices that need them.
     * - Capture never touches the socket: it only enqueues 20ms chunks, so a
     *   slow network or a big video frame can never block the microphone read
     *   (which would overrun the AudioRecord buffer and garble speech).
     * - Playback uses a jitter buffer: ~80ms pre-roll before the first chunk,
     *   a bounded queue (stale chunks dropped to keep latency low) and short
     *   silence insertion on underruns with re-priming after a sustained gap,
     *   so network jitter becomes brief pauses instead of stutter, and latency
     *   can never drift upward forever.
     */
    private class AudioEngine(
        private val context: Context,
        private val onAudio: (ByteArray) -> Unit
    ) {
        private var recorder: AudioRecord? = null
        private var track: AudioTrack? = null
        private var captureThread: Thread? = null
        private val playQueue = LinkedBlockingQueue<ByteArray>(PLAY_QUEUE_CAP)
        @Volatile
        private var stopped = false
        private var playThread: Thread? = null
        private val silenceChunk = ByteArray(AUDIO_CHUNK)

        // The RECORD_AUDIO permission is checked before a call starts
        // (requireCallPermission) and the AudioRecord constructor is wrapped in
        // try/catch that treats SecurityException as "recorder unavailable" —
        // lint cannot see that, so annotate this method.
        @SuppressLint("MissingPermission")
        fun start() {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            // Enter communication mode BEFORE opening the recorder so the
            // audio HAL routes the mic through the call preprocessor
            // (AEC/NS/AGC); MIC-source capture would feed the speaker's own
            // output straight back into the mic.
            runCatching {
                am.mode = AudioManager.MODE_IN_COMMUNICATION
                am.isSpeakerphoneOn = true
                am.requestAudioFocus(
                    null, AudioManager.STREAM_VOICE_CALL, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT
                )
            }

            val minIn = AudioRecord.getMinBufferSize(
                AUDIO_SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            val rec = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    AUDIO_SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minIn * 2, AUDIO_CHUNK * 8)
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
                // Explicit preprocessors: most devices already apply them for
                // VOICE_COMMUNICATION in communication mode; attaching them
                // explicitly covers the rest.
                if (rec != null) {
                    // Diagnostics: confirm the platform preprocessor actually
                    // engaged, so a device that still echoes can be identified
                    // from logcat instead of guessing.
                    Log.i(
                        TAG,
                        "audiofx available: AEC=${AcousticEchoCanceler.isAvailable()} " +
                            "NS=${NoiseSuppressor.isAvailable()} AGC=${AutomaticGainControl.isAvailable()}"
                    )
                    if (AcousticEchoCanceler.isAvailable()) {
                        runCatching {
                            AcousticEchoCanceler.create(rec.audioSessionId)?.apply {
                                enabled = true
                                Log.i(TAG, "AEC enabled: ${enabled}")
                            }
                        }.onFailure { Log.w(TAG, "AEC enable failed", it) }
                    }
                    if (NoiseSuppressor.isAvailable()) {
                        runCatching {
                            NoiseSuppressor.create(rec.audioSessionId)?.apply {
                                enabled = true
                                Log.i(TAG, "NS enabled: ${enabled}")
                            }
                        }.onFailure { Log.w(TAG, "NS enable failed", it) }
                    }
                    if (AutomaticGainControl.isAvailable()) {
                        runCatching {
                            AutomaticGainControl.create(rec.audioSessionId)?.apply {
                                enabled = true
                                Log.i(TAG, "AGC enabled: ${enabled}")
                            }
                        }.onFailure { Log.w(TAG, "AGC enable failed", it) }
                    }
                }
            }

            val minOut = AudioTrack.getMinBufferSize(
                AUDIO_SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
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
                    .setBufferSizeInBytes(maxOf(minOut, AUDIO_CHUNK * 8))
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
                    val buf = ByteArray(AUDIO_CHUNK)
                    while (!stopped) {
                        val n = rec.read(buf, 0, AUDIO_CHUNK)
                        if (n > 0) {
                            onAudio(if (n == AUDIO_CHUNK) buf.copyOf() else buf.copyOfRange(0, n))
                        }
                    }
                }.apply {
                    name = "call-audio-capture"
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
                        // Timed poll (not a blocking take) so stop() can join
                        // this thread without relying on interruption.
                        var chunk = try {
                            playQueue.poll(50, TimeUnit.MILLISECONDS)
                        } catch (e: InterruptedException) {
                            break
                        }
                        if (chunk == null) {
                            // underrun: insert a short silence so the track does
                            // not stall, then re-prime so we do not drift far
                            // from the remote's timeline
                            if (++silenceCount > UNDERRUN_SILENCE_CHUNKS) {
                                primed = false
                                silenceCount = 0
                                continue
                            }
                            chunk = silenceChunk
                        } else {
                            silenceCount = 0
                        }
                        runCatching { trk.write(chunk, 0, chunk.size, AudioTrack.WRITE_BLOCKING) }
                    }
                }.apply {
                    name = "call-audio-playback"
                    start()
                }
            }
        }

        /** Block until at least [PLAY_PREROLL_CHUNKS] chunks are buffered
         * (the jitter buffer is full enough to absorb network jitter). */
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
            // Drop-oldest so a network burst cannot inflate call latency forever.
            while (!playQueue.offer(data)) playQueue.poll()
        }

        fun stop() {
            stopped = true
            playQueue.clear()
            // recorder.stop() unblocks a capture thread parked inside read().
            // The recorder must be released ONLY after that thread has left
            // read(): releasing an AudioRecord during a pending read is
            // undefined and can leave the microphone noted as in use — the
            // system mic indicator then never disappears after the call.
            runCatching { recorder?.stop() }
            runCatching { track?.stop() }
            joinQuietly(captureThread)
            joinQuietly(playThread)
            runCatching { recorder?.release() }
            recorder = null
            runCatching { track?.release() }
            track = null
            runCatching {
                val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                am.abandonAudioFocus(null)
                am.mode = AudioManager.MODE_NORMAL
            }
        }

        private fun joinQuietly(thread: Thread?) {
            if (thread == null) return
            runCatching { thread.join(1500) }
        }
    }
}
