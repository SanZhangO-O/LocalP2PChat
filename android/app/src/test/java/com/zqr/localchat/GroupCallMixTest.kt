package com.zqr.localchat

import com.zqr.localchat.call.ConferenceMix
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The conference mixing rule both platforms share (Windows parity:
 * group_call.mix_pcm16, pinned there by test_group_call.py-style vectors):
 * PCM16 little-endian mono, zero-padding to the frame size, saturation
 * instead of wrap-around.
 */
class GroupCallMixTest {

    /** One-sample PCM16 LE frame; grows past [frameBytes] when more samples
     *  are given (the oversized-chunk test relies on that). */
    private fun frame(vararg samples: Short, frameBytes: Int = 4): ByteArray {
        val out = ByteArray(maxOf(frameBytes, samples.size * 2))
        for ((i, v) in samples.withIndex()) {
            out[i * 2] = (v.toInt() and 0xFF).toByte()
            out[i * 2 + 1] = ((v.toInt() ushr 8) and 0xFF).toByte()
        }
        return out
    }

    private fun sampleAt(pcm: ByteArray, index: Int): Short {
        val lo = pcm[index * 2].toInt() and 0xFF
        val hi = pcm[index * 2 + 1].toInt() and 0xFF
        return (((hi shl 8) or lo).toShort())
    }

    @Test
    fun `empty mix is silence`() {
        val mixed = ConferenceMix.mixPcm(emptyList())
        assertEquals(ConferenceMix.AUDIO_FRAME_BYTES, mixed.size)
        for (i in 0 until ConferenceMix.AUDIO_FRAME_BYTES / 2) {
            assertEquals(0, sampleAt(mixed, i).toInt())
        }
    }

    @Test
    fun `two voices sum`() {
        val mixed = ConferenceMix.mixPcm(
            listOf(frame(1000), frame(2000)),
            frameBytes = 4
        )
        assertEquals(3000, sampleAt(mixed, 0).toInt())
    }

    @Test
    fun `clipping saturates instead of wrapping`() {
        val hi = ConferenceMix.mixPcm(
            listOf(frame(20000), frame(20000)),
            frameBytes = 4
        )
        assertEquals(32767, sampleAt(hi, 0).toInt())

        val lo = ConferenceMix.mixPcm(
            listOf(frame(-20000), frame(-20000)),
            frameBytes = 4
        )
        assertEquals(-32768, sampleAt(lo, 0).toInt())
    }

    @Test
    fun `a short chunk is zero padded not dropped`() {
        // a member that sent less than one frame still contributes its samples
        val mixed = ConferenceMix.mixPcm(
            listOf(frame(500), byteArrayOf()),
            frameBytes = 4
        )
        assertEquals(500, sampleAt(mixed, 0).toInt())
    }

    @Test
    fun `an oversized chunk is truncated to one frame`() {
        val long = frame(100, 100, 100, 100) // 2 frames worth at frameBytes=4
        val mixed = ConferenceMix.mixPcm(listOf(long), frameBytes = 4)
        assertEquals(100, sampleAt(mixed, 0).toInt())
        assertEquals(100, sampleAt(mixed, 1).toInt())
    }

    @Test
    fun `full frame size matches the 20ms contract`() {
        // 16000 Hz * 2 bytes * 20 ms
        assertEquals(640, ConferenceMix.AUDIO_FRAME_BYTES)
        val a = ByteArray(640)
        val b = ByteArray(640)
        val mixed = ConferenceMix.mixPcm(listOf(a, b))
        assertTrue(mixed.size == 640)
    }
}
