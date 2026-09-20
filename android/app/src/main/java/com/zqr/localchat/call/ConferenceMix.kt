package com.zqr.localchat.call

/**
 * Voice-grade conference mixing primitive, deliberately free of any Android
 * class so JVM unit tests can pin the exact behavior both platforms share
 * (Windows parity: group_call.mix_pcm16).
 */
object ConferenceMix {

    const val AUDIO_FRAME_BYTES = 640 // 20ms of PCM16 mono 16 kHz

    /** Saturating sum of PCM16 little-endian mono buffers. Every chunk is
     *  padded (zeros) or truncated to [frameBytes]; an empty list yields
     *  silence. Values saturate at ±32767 instead of wrapping. */
    fun mixPcm(chunks: List<ByteArray>, frameBytes: Int = AUDIO_FRAME_BYTES): ByteArray {
        val samples = frameBytes / 2
        val acc = IntArray(samples)
        for (chunk in chunks) {
            if (chunk.isEmpty()) continue
            val usable = minOf(chunk.size, frameBytes)
            var i = 0
            var s = 0
            while (i + 1 < usable) {
                val lo = chunk[i].toInt() and 0xFF
                val hi = chunk[i + 1].toInt() and 0xFF
                acc[s] += (((hi shl 8) or lo).toShort()).toInt()
                i += 2
                s++
            }
        }
        val out = ByteArray(frameBytes)
        for (s in 0 until samples) {
            val v = acc[s].coerceIn(-32768, 32767)
            out[s * 2] = (v and 0xFF).toByte()
            out[s * 2 + 1] = ((v ushr 8) and 0xFF).toByte()
        }
        return out
    }
}
