package com.zqr.localchat

import com.zqr.localchat.crypto.Crypto
import com.zqr.localchat.data.FileInfo
import com.zqr.localchat.network.FileTransfer
import com.zqr.localchat.network.NetworkPacket
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * File download over a real localhost socket with a fake SENDER that speaks
 * the same wire protocol as FileTransfer.runServer: file_download request
 * (with the lc-file-dl-v1 token) -> encrypted meta line -> framed encrypted
 * chunks -> 4B zero EOF. Covers the downloader side (token attach, progress,
 * cancel) in plain JVM.
 */
class FileTransferDownloadTest {

    private val json = Json { ignoreUnknownKeys = true }

    /** Reads one '\n'-terminated line without read-ahead (same as the app). */
    private fun readLineRaw(input: InputStream): String? {
        val buf = ArrayList<Byte>()
        while (true) {
            val b = input.read()
            if (b == -1) return if (buf.isEmpty()) null else String(buf.toByteArray(), Charsets.UTF_8)
            if (b == '\n'.code) return String(buf.toByteArray(), Charsets.UTF_8).removeSuffix("\r")
            buf.add(b.toByte())
        }
    }

    private fun writeChunk(out: OutputStream, key: ByteArray, plain: ByteArray) {
        val blob = Crypto.aesGcmEncrypt(key, plain)
        val header = byteArrayOf(
            (blob.size ushr 24).toByte(),
            (blob.size ushr 16).toByte(),
            (blob.size ushr 8).toByte(),
            blob.size.toByte()
        )
        out.write(header)
        out.write(blob)
        out.flush()
    }

    private fun serveOnce(
        server: ServerSocket,
        fileKey: ByteArray,
        payload: ByteArray,
        requestCapture: AtomicReference<String?>,
        started: CountDownLatch
    ) {
        val thread = Thread {
            try {
                val client = server.accept()
                client.soTimeout = 10_000
                val request = readLineRaw(client.getInputStream())
                requestCapture.set(request)
                // fake sender validates the token exactly like serveFile:
                // present-and-wrong -> hang up without meta; absent -> serve
                val req = request?.let { runCatching { json.decodeFromString<NetworkPacket>(it) }.getOrNull() }
                val expected = fileKey
                if (req?.token != null) {
                    val provided = Crypto.fromB64(req.token!!)
                    val wanted = Crypto.hmacSha256(
                        expected, "lc-file-dl-v1:${req.fileId}".toByteArray(Charsets.US_ASCII)
                    )
                    if (provided == null || !Crypto.constantTimeEquals(provided, wanted)) {
                        client.close()
                        return@Thread
                    }
                }
                val meta = NetworkPacket(
                    type = "file_meta",
                    fileInfo = FileInfo(req?.fileId ?: "x", "v.txt", payload.size.toLong(), "", 0)
                )
                val metaLine = Crypto.toB64(
                    Crypto.aesGcmEncrypt(fileKey, json.encodeToString(meta).toByteArray(Charsets.UTF_8))
                )
                val writer = java.io.PrintWriter(client.getOutputStream(), true)
                writer.println(metaLine)
                writeChunk(client.getOutputStream(), fileKey, payload)
                client.getOutputStream().write(byteArrayOf(0, 0, 0, 0))
                client.getOutputStream().flush()
                // give the client a moment to drain, then close
                client.getInputStream().read(byteArrayOf()) // blocks until EOF/closed
                client.close()
            } catch (_: Exception) {
            } finally {
                started.countDown()
            }
        }
        thread.isDaemon = true
        thread.start()
    }

    private fun offer(fileId: String, key: ByteArray, port: Int, size: Long) =
        FileInfo(fileId, "v.txt", size, "127.0.0.1", port, Crypto.toB64(key))

    @Test
    fun `download sends the token, decrypts the stream and reports progress`() {
        val key = Crypto.randomBytes(32)
        val payload = ByteArray(200) { it.toByte() }
        val server = ServerSocket(0)
        val request = AtomicReference<String?>()
        val done = CountDownLatch(1)
        serveOnce(server, key, payload, request, done)
        try {
            val out = ByteArrayOutputStream()
            val progress = ArrayList<String>()
            val result = FileTransfer.download(
                offer("file-1", key, server.localPort, payload.size.toLong()),
                out,
                onProgress = { received, total -> progress.add("$received/$total") },
                cancelled = { false }
            )
            assertTrue("download must succeed: ${result.message}", result.ok)
            assertEquals(payload.toList(), out.toByteArray().toList())
            assertTrue("progress must fire", progress.isNotEmpty())
            assertEquals("200/200", progress.last())
            // the request line MUST carry the lc-file-dl-v1 token
            val sent = request.get()
            assertNotNull(sent)
            val req = json.decodeFromString<NetworkPacket>(sent!!)
            assertEquals("file_download", req.type)
            assertEquals("file-1", req.fileId)
            assertEquals(
                FileTransfer.downloadToken("file-1", key),
                req.token
            )
        } finally {
            assertTrue(done.await(15, TimeUnit.SECONDS))
            server.close()
        }
    }

    @Test
    fun `cancelled download aborts`() {
        val key = Crypto.randomBytes(32)
        val payload = ByteArray(64) { 7 }
        val server = ServerSocket(0)
        val request = AtomicReference<String?>()
        val done = CountDownLatch(1)
        serveOnce(server, key, payload, request, done)
        try {
            val out = ByteArrayOutputStream()
            val result = FileTransfer.download(
                offer("file-2", key, server.localPort, payload.size.toLong()),
                out,
                onProgress = { _, _ -> },
                cancelled = { true }
            )
            assertFalse("a cancelled download must not succeed", result.ok)
            assertTrue("cancellation must be visible in the message", result.message.contains("取消"))
        } finally {
            assertTrue(done.await(15, TimeUnit.SECONDS))
            server.close()
        }
    }

    @Test
    fun `cancel unblocks a stalled read through the socket holder`() {
        // A cancel flag alone only lands at the next chunk boundary: while the
        // downloader is blocked in a partial frame read (a stalled peer, the
        // 120s read timeout away) the canceller must be able to shut the
        // socket down. The holder is how ViewModel.cancelDownload does it.
        val key = Crypto.randomBytes(32)
        val payload = ByteArray(64) { 7 }
        val server = ServerSocket(0)
        val firstChunk = CountDownLatch(1)
        val senderDone = CountDownLatch(1)
        val sender = Thread {
            try {
                val client = server.accept()
                client.soTimeout = 10_000
                readLineRaw(client.getInputStream()) // the file_download request
                val meta = NetworkPacket(
                    type = "file_meta",
                    fileInfo = FileInfo("file-3", "v.txt", payload.size.toLong(), "", 0)
                )
                val writer = java.io.PrintWriter(client.getOutputStream(), true)
                writer.println(
                    Crypto.toB64(
                        Crypto.aesGcmEncrypt(key, json.encodeToString(meta).toByteArray(Charsets.UTF_8))
                    )
                )
                writeChunk(client.getOutputStream(), key, payload)
                firstChunk.countDown()
                // half a 4-byte frame header, then stall WITHOUT closing: the
                // reader blocks until the canceller shuts the socket down
                client.getOutputStream().write(byteArrayOf(0, 0))
                client.getOutputStream().flush()
                senderDone.await(30, TimeUnit.SECONDS)
            } catch (_: Exception) {
            }
        }
        sender.isDaemon = true
        sender.start()

        val holder = java.util.Collections.synchronizedList(mutableListOf<java.net.Socket>())
        val cancelFlag = java.util.concurrent.atomic.AtomicBoolean(false)
        val canceller = Thread {
            try {
                if (firstChunk.await(15, TimeUnit.SECONDS)) {
                    cancelFlag.set(true)
                    for (s in holder.toList()) {
                        runCatching { s.shutdownInput() }
                        runCatching { s.shutdownOutput() }
                    }
                }
            } catch (_: InterruptedException) {
            }
        }
        canceller.isDaemon = true
        canceller.start()

        try {
            val out = ByteArrayOutputStream()
            val began = System.currentTimeMillis()
            val result = FileTransfer.download(
                offer("file-3", key, server.localPort, payload.size.toLong()),
                out,
                onProgress = { _, _ -> },
                cancelled = { cancelFlag.get() },
                sockHolder = holder
            )
            val elapsed = System.currentTimeMillis() - began
            assertFalse("a cancelled download must not succeed", result.ok)
            assertTrue("the socket must be handed to the canceller", holder.isNotEmpty())
            assertTrue(
                "cancel must not wait for the 120s read timeout (took ${elapsed}ms)",
                elapsed < 15_000
            )
        } finally {
            senderDone.countDown()
            server.close()
        }
    }
}
