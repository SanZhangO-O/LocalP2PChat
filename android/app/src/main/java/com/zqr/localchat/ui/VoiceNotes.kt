package com.zqr.localchat.ui

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaPlayer
import android.media.MediaRecorder
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.io.RandomAccessFile

/**
 * Voice-message capture and playback (local-only, no protocol code). Both
 * ends exchange plain 16 kHz mono 16-bit PCM WAV files through the ordinary
 * file channel, so no codec is involved: recording uses AudioRecord, playback
 * MediaPlayer (plays WAV on every supported device). Windows parity:
 * windows/localchat/audio_note.py.
 */
object VoiceNotes {
    const val SAMPLE_RATE = 16000
    private const val HEADER_BYTES = 44

    /** Whole-second duration of a WAV file (0 when unreadable). Reads only
     *  the header, so it is safe on the UI thread. */
    fun wavDurationSeconds(path: String): Int = runCatching {
        RandomAccessFile(path, "r").use { raf ->
            val header = ByteArray(HEADER_BYTES)
            if (raf.read(header) < HEADER_BYTES) return 0
            val byteRate =
                (header[28].toInt() and 0xFF) or ((header[29].toInt() and 0xFF) shl 8) or
                    ((header[30].toInt() and 0xFF) shl 16) or ((header[31].toInt() and 0xFF) shl 24)
            if (byteRate <= 0) return 0
            val dataBytes = raf.length() - HEADER_BYTES
            if (dataBytes <= 0) return 0
            Math.round((dataBytes / byteRate.toDouble()).toFloat()).coerceAtLeast(0)
        }
    }.getOrDefault(0)

    /** 0:07 style label. */
    fun formatDuration(seconds: Int): String = "%d:%02d".format(seconds / 60, seconds % 60)

    /** Canonical 44-byte PCM WAV header for [dataBytes] of 16 kHz mono 16-bit
     *  data (RIFF + fmt chunk + data chunk size). Pure, so unit tests can
     *  verify the bytes the recorder writes. */
    fun wavHeader(dataBytes: Int): ByteArray {
        val header = ByteArray(HEADER_BYTES)
        header[0] = 'R'.code.toByte(); header[1] = 'I'.code.toByte()
        header[2] = 'F'.code.toByte(); header[3] = 'F'.code.toByte()
        header[8] = 'W'.code.toByte(); header[9] = 'A'.code.toByte()
        header[10] = 'V'.code.toByte(); header[11] = 'E'.code.toByte()
        header[12] = 'f'.code.toByte(); header[13] = 'm'.code.toByte()
        header[14] = 't'.code.toByte(); header[15] = ' '.code.toByte()
        // fmt chunk size 16
        header[16] = 16; header[20] = 1 // PCM
        header[22] = 1 // mono
        val rate = SAMPLE_RATE
        header[24] = (rate and 0xFF).toByte(); header[25] = ((rate shr 8) and 0xFF).toByte()
        header[26] = ((rate shr 16) and 0xFF).toByte(); header[27] = ((rate shr 24) and 0xFF).toByte()
        val byteRate = rate * 2 // mono, 16-bit
        header[28] = (byteRate and 0xFF).toByte(); header[29] = ((byteRate shr 8) and 0xFF).toByte()
        header[30] = ((byteRate shr 16) and 0xFF).toByte(); header[31] = ((byteRate shr 24) and 0xFF).toByte()
        header[32] = 2 // block align
        header[34] = 16 // bits per sample
        header[36] = 'd'.code.toByte(); header[37] = 'a'.code.toByte()
        header[38] = 't'.code.toByte(); header[39] = 'a'.code.toByte()
        writeIntLe(header, 4, 36 + dataBytes)
        writeIntLe(header, 40, dataBytes)
        return header
    }

    /** Write the header (sizes correct for [dataBytes]). */
    fun writeWavHeader(out: OutputStream, dataBytes: Int) {
        out.write(wavHeader(dataBytes))
    }

    /** Patch the RIFF/data sizes of an already-written WAV to match its final
     *  length. The handle MUST be opened read+write: `FileInputStream`'s
     *  channel is READ-ONLY, so writing through it throws
     *  NonWritableChannelException — the previous implementation swallowed
     *  that and left every clip with zero sizes, which made Windows (Python
     *  `wave`) read 0 frames, show 0:00 and play silence. */
    fun patchWavSizes(file: File) {
        RandomAccessFile(file, "rw").use { raf ->
            val dataBytes = (raf.length() - HEADER_BYTES).coerceAtLeast(0).toInt()
            raf.seek(4)
            raf.write(leInt(36 + dataBytes))
            raf.seek(40)
            raf.write(leInt(dataBytes))
        }
    }

    /** Delete recording sources older than [maxAgeSeconds] (Windows
     *  `prune_recordings` parity): a previous session's file offers are dead
     *  (their download addresses were blanked on restore), so their WAVs can
     *  never be served again. */
    fun pruneRecordings(dir: File, maxAgeSeconds: Long = 3600) {
        val cutoff = System.currentTimeMillis() - maxAgeSeconds * 1000
        val files = dir.listFiles() ?: return
        for (f in files) {
            if (!f.name.endsWith(".wav")) continue
            runCatching { if (f.isFile && f.lastModified() < cutoff) f.delete() }
        }
    }

    private fun writeIntLe(target: ByteArray, offset: Int, value: Int) {
        target[offset] = (value and 0xFF).toByte()
        target[offset + 1] = ((value shr 8) and 0xFF).toByte()
        target[offset + 2] = ((value shr 16) and 0xFF).toByte()
        target[offset + 3] = ((value shr 24) and 0xFF).toByte()
    }

    private fun leInt(value: Int): ByteArray {
        val b = ByteArray(4)
        writeIntLe(b, 0, value)
        return b
    }
}

/**
 * Capture the device microphone into a 16 kHz mono WAV. [start] returns false
 * when capture cannot begin (missing permission / no mic). Call [stop] from a
 * worker thread: it finalizes the file and returns the path ("" when nothing
 * usable was recorded). [cancel] is safe from the main thread (onDispose): it
 * signals the capture thread, which then discards the file itself.
 */
class VoiceRecorder(private val dir: File) {
    private var record: AudioRecord? = null
    private var thread: Thread? = null
    private var target: File? = null
    private var startedAt = 0L

    @Volatile
    private var recording = false

    @Volatile
    private var cancelled = false

    /** Whole seconds since [start] (0 when idle). */
    fun elapsedSeconds(): Int {
        if (!recording) return 0
        return ((android.os.SystemClock.elapsedRealtime() - startedAt) / 1000).toInt()
    }

    fun start(): Boolean {
        if (recording) return false
        val minBuf = AudioRecord.getMinBufferSize(
            VoiceNotes.SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) return false
        val audio = try {
            AudioRecord(
                MediaRecorder.AudioSource.MIC,
                VoiceNotes.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                minBuf * 2
            )
        } catch (_: Exception) {
            return false
        }
        if (audio.state != AudioRecord.STATE_INITIALIZED) {
            audio.release()
            return false
        }
        val started = try {
            audio.startRecording()
            true
        } catch (_: Exception) {
            false
        }
        // Without the permission (or with a busy mic) startRecording() can
        // also silently fail: verify the state instead of capturing an empty
        // clip the user only notices when the recording is discarded.
        if (!started || audio.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            runCatching { audio.release() }
            return false
        }
        dir.mkdirs()
        val file = File(dir, "voice_${System.currentTimeMillis()}.wav")
        cancelled = false
        recording = true
        record = audio
        target = file
        startedAt = android.os.SystemClock.elapsedRealtime()
        thread = Thread({
            var written = 0L
            try {
                FileOutputStream(file).use { out ->
                    VoiceNotes.writeWavHeader(out, 0)
                    val buf = ByteArray(3200) // 100 ms of 16 kHz mono 16-bit
                    while (recording) {
                        val n = audio.read(buf, 0, buf.size)
                        if (n > 0) {
                            out.write(buf, 0, n)
                            written += n
                        } else if (n < 0) {
                            // transient read error: back off instead of busy
                            // spinning on a failing capture
                            Thread.sleep(5)
                        }
                    }
                    out.flush()
                }
                if (written > 0) VoiceNotes.patchWavSizes(file) else file.delete()
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                file.delete()
            } catch (_: Exception) {
                file.delete()
            } finally {
                runCatching { audio.stop() }
                runCatching { audio.release() }
                if (cancelled) file.delete()
            }
        }, "voice-record").also { it.start() }
        return true
    }

    /** Finish capture and return the recording path ("" when nothing usable
     *  was recorded). Must not run on the main thread (joins the capture
     *  thread). */
    fun stop(): String {
        if (!recording) return ""
        recording = false
        thread?.join(1500)
        thread = null
        record = null
        cancelled = false
        val file = target
        target = null
        if (file == null || !file.isFile || file.length() <= 44) {
            file?.delete()
            return ""
        }
        return file.absolutePath
    }

    /** Abandon the in-progress capture WITHOUT blocking: the capture thread
     *  notices the flag and deletes the file itself. Safe from the main
     *  thread (screen dispose). */
    fun cancel() {
        if (!recording) return
        cancelled = true
        recording = false
    }
}

/**
 * One-at-a-time WAV playback: [play] replaces the current file, [stop] halts.
 * The player is NOT thread-safe beyond "UI thread usage".
 */
class VoicePlayer {
    private var player: MediaPlayer? = null

    var onFinished: (() -> Unit)? = null

    fun play(path: String): Boolean {
        stop()
        val p = MediaPlayer()
        return try {
            p.setDataSource(path)
            p.setOnCompletionListener {
                runCatching { p.release() }
                player = null
                onFinished?.invoke()
            }
            p.prepare()
            p.start()
            player = p
            true
        } catch (_: IOException) {
            runCatching { p.release() }
            false
        } catch (_: IllegalStateException) {
            runCatching { p.release() }
            false
        } catch (_: Exception) {
            runCatching { p.release() }
            false
        }
    }

    fun stop() {
        val p = player
        player = null
        if (p == null) return
        // release() runs in its own runCatching: a throwing stop() (illegal
        // state after an error) must not skip the release and leak the player
        runCatching { p.stop() }
        runCatching { p.release() }
    }
}
