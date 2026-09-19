package com.zqr.localchat

import com.zqr.localchat.ui.VoiceNotes
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * WAV contracts shared with the Windows client (windows/localchat/audio_note.py):
 * a recorded clip must carry a canonical, size-patched header or Python's
 * `wave` reads 0 frames (duration 0:00, silent playback).
 */
class VoiceNotesTest {

    private fun leInt(bytes: ByteArray, at: Int): Int =
        ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).getInt(at)

    @Test
    fun `header carries the patched sizes and duration rounds half up`() {
        val file = File.createTempFile("voice", ".wav")
        try {
            // 16000 data bytes = 8000 samples = 0.5 s of 16 kHz mono 16-bit
            val samples = ByteArray(16000)
            FileOutputStream(file).use { out ->
                VoiceNotes.writeWavHeader(out, 0)
                out.write(samples)
            }
            VoiceNotes.patchWavSizes(file)
            val bytes = file.readBytes()
            assertEquals("RIFF size", 36 + samples.size, leInt(bytes, 4))
            assertEquals("data size", samples.size, leInt(bytes, 40))
            assertEquals("sample rate", 16000, leInt(bytes, 24))
            assertEquals("channels", 1, bytes[22].toInt())
            assertEquals("bits per sample", 16, bytes[34].toInt())
            // Android Math.round == Python int(x + 0.5): 0.5 s -> 1 s
            assertEquals(1, VoiceNotes.wavDurationSeconds(file.absolutePath))
        } finally {
            file.delete()
        }
    }

    @Test
    fun `duration and label helpers mirror the Windows client`() {
        assertEquals("0:07", VoiceNotes.formatDuration(7))
        assertEquals("1:15", VoiceNotes.formatDuration(75))
        assertEquals("0:00", VoiceNotes.formatDuration(0))
        assertEquals(0, VoiceNotes.wavDurationSeconds("/no/such/file.wav"))
    }

    @Test
    fun `prune removes only stale recordings`() {
        val dir = File(
            System.getProperty("java.io.tmpdir"),
            "lc-voice-test-${System.nanoTime()}"
        )
        assertTrue(dir.mkdirs())
        try {
            val stale = File(dir, "voice_old.wav").apply {
                writeBytes(ByteArray(44))
                setLastModified(System.currentTimeMillis() - 7_200_000)
            }
            val fresh = File(dir, "voice_new.wav").apply { writeBytes(ByteArray(44)) }
            val other = File(dir, "notes.txt").apply { writeText("keep me") }
            VoiceNotes.pruneRecordings(dir, maxAgeSeconds = 3600)
            assertFalse("stale recording must be pruned", stale.exists())
            assertTrue("fresh recording must be kept", fresh.exists())
            assertTrue("non-wav files are untouched", other.exists())
        } finally {
            dir.deleteRecursively()
        }
    }
}
