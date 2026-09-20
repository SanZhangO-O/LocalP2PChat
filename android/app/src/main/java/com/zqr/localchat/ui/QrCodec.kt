package com.zqr.localchat.ui

import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.EncodeHintType
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import com.google.zxing.qrcode.QRCodeWriter

/**
 * QR imaging on top of ZXing core (pure JVM math + optional Bitmap render):
 * one small dependency covers both generation and camera decoding, with no
 * Play-services requirement (CameraX already feeds the analyzer).
 */
object QrCodec {

    /** Encode [text] into a QR BitMatrix, or null when the input is empty or
     *  too large for a QR symbol. Bitmap-free so JVM unit tests can use it. */
    fun encodeMatrix(text: String): com.google.zxing.common.BitMatrix? {
        if (text.isEmpty()) return null
        return runCatching {
            QRCodeWriter().encode(
                text, BarcodeFormat.QR_CODE, 0, 0,
                mapOf(EncodeHintType.MARGIN to 2)
            )
        }.getOrNull()
    }

    /** Render [text] as a square QR Bitmap (null when `qrcode`-equivalent
     *  encoding fails, e.g. payload too large). */
    fun qrBitmap(text: String, size: Int = 640): Bitmap? {
        val matrix = encodeMatrix(text) ?: return null
        val width = matrix.width
        val height = matrix.height
        val pixels = IntArray(width * height)
        for (y in 0 until height) {
            for (x in 0 until width) {
                pixels[y * width + x] = if (matrix.get(x, y)) Color.BLACK else Color.WHITE
            }
        }
        val raw = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        raw.setPixels(pixels, 0, width, 0, 0, width, height)
        return if (size == width) raw else Bitmap.createScaledBitmap(raw, size, size, false)
    }

    /**
     * Decode a QR code from a camera Y (luminance) plane. [y] must be the
     * rowStride-padded plane buffer; a compact copy is made when rowStride
     * exceeds [width]. Returns the decoded text or null.
     */
    fun decodeYPlane(
        y: ByteArray,
        dataWidth: Int,
        dataHeight: Int,
        width: Int = dataWidth,
        height: Int = dataHeight
    ): String? {
        return runCatching {
            val source = PlanarYUVLuminanceSource(
                y, dataWidth, dataHeight, 0, 0, width, height, false
            )
            decodeBinary(BinaryBitmap(HybridBinarizer(source)))
        }.getOrNull()
    }

    /** Decode an already-built binary bitmap (shared by the camera path and
     *  tests). */
    fun decodeBinary(bitmap: BinaryBitmap): String? {
        return runCatching {
            val reader = QRCodeReader()
            val result = reader.decode(
                bitmap, mapOf(DecodeHintType.TRY_HARDER to true)
            )
            reader.reset()
            result.text
        }.getOrNull()
    }
}
