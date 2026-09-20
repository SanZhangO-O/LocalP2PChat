package com.zqr.localchat.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.ContextCompat
import com.zqr.localchat.network.ContactInvite
import kotlinx.coroutines.launch
import java.util.concurrent.Executors

/**
 * QR dialogs: show this device's QR, scan with the camera, and confirm a
 * scanned contact card. Windows parity: ui/qr_dialogs.py. Generation uses
 * [QrCodec.qrBitmap] (ZXing core); scanning feeds CameraX Y planes into
 * [QrCodec.decodeYPlane].
 */

/** Show a QR for [payload] (or a copyable text fallback when rendering
 *  fails), plus optional label/value rows (security code / group id). */
@Composable
fun QrShowDialog(
    title: String,
    payload: String,
    extraRows: List<Pair<String, String>> = emptyList(),
    onDismiss: () -> Unit
) {
    val clipboard = LocalClipboard.current
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val bitmap = remember(payload) { QrCodec.qrBitmap(payload, 560) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column {
                if (bitmap != null) {
                    Image(
                        bitmap = bitmap.asImageBitmap(),
                        contentDescription = "二维码",
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color.White, RoundedCornerShape(12.dp))
                            .padding(8.dp)
                    )
                } else {
                    Text(
                        "二维码生成失败，可直接复制下方内容发给对方扫一扫",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.error
                    )
                }
                for ((label, value) in extraRows) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(value, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                }
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    "二维码内容",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Text(
                    payload,
                    fontSize = 11.sp,
                    maxLines = 3,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        },
        confirmButton = {
            TextButton(onClick = {
                val clip = androidx.compose.ui.platform.ClipEntry(
                    android.content.ClipData.newPlainText("LocalChat", payload)
                )
                scope.launch {
                    clipboard.setClipEntry(clip)
                    android.widget.Toast.makeText(
                        context, "已复制", android.widget.Toast.LENGTH_SHORT
                    ).show()
                }
            }) { Text("复制内容") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("关闭") }
        }
    )
}

/**
 * Full-screen camera scanner (back camera, ZXing decode per frame). The
 * CAMERA permission is requested inside; scanning stops at the first decoded
 * payload and reports it once via [onFound].
 */
@Composable
fun QrScanOverlay(
    onFound: (String) -> Unit,
    onDismiss: () -> Unit
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted = it }
    androidx.compose.runtime.LaunchedEffect(Unit) {
        if (!granted) permissionLauncher.launch(Manifest.permission.CAMERA)
    }

    // report exactly one payload per scan session
    var reported by remember { mutableStateOf(false) }

    // Cleanup owns BOTH resources in one place: unbind the camera use cases
    // BEFORE the analyzer executor shuts down — leaving them bound would keep
    // delivering frames to a dead executor (RejectedExecutionException) and
    // hold the camera open after the dialog closes.
    val executor = remember { Executors.newSingleThreadExecutor() }
    val cameraProvider = remember {
        java.util.concurrent.atomic.AtomicReference<ProcessCameraProvider?>(null)
    }
    DisposableEffect(Unit) {
        onDispose {
            cameraProvider.get()?.let { provider ->
                runCatching { provider.unbindAll() }
            }
            executor.shutdown()
        }
    }

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(modifier = Modifier.fillMaxSize(), color = Color.Black) {
            Box(modifier = Modifier.fillMaxSize()) {
                if (granted) {
                    androidx.compose.ui.viewinterop.AndroidView(
                        factory = { ctx ->
                            val previewView = androidx.camera.view.PreviewView(ctx)
                            val providerFuture = ProcessCameraProvider.getInstance(ctx)
                            providerFuture.addListener({
                                val provider = providerFuture.get()
                                cameraProvider.set(provider)
                                val preview = Preview.Builder().build().also { p ->
                                    p.surfaceProvider = previewView.surfaceProvider
                                }
                                val analysis = ImageAnalysis.Builder()
                                    .setBackpressureStrategy(
                                        ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST
                                    )
                                    .build()
                                analysis.setAnalyzer(executor) { image ->
                                    val text = useImageProxy(image) { qrTextFromFrame(it) }
                                    if (text != null && !reported) {
                                        reported = true
                                        onFound(text)
                                    }
                                }
                                try {
                                    provider.unbindAll()
                                    provider.bindToLifecycle(
                                        lifecycleOwner,
                                        CameraSelector.DEFAULT_BACK_CAMERA,
                                        preview,
                                        analysis
                                    )
                                } catch (_: Exception) {
                                    // camera busy/unavailable: the hint text explains
                                }
                            }, ContextCompat.getMainExecutor(ctx))
                            previewView
                        },
                        modifier = Modifier.fillMaxSize()
                    )
                } else {
                    Box(
                        modifier = Modifier.fillMaxSize(),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "需要相机权限才能扫二维码",
                            color = Color.White.copy(alpha = 0.8f),
                            fontSize = 15.sp
                        )
                    }
                }

                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 24.dp, start = 12.dp, end = 12.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        "对准对方的二维码",
                        color = Color.White.copy(alpha = 0.85f),
                        fontSize = 14.sp
                    )
                    IconButton(onClick = onDismiss) {
                        Icon(
                            Icons.Filled.Close,
                            contentDescription = "关闭",
                            tint = Color.White
                        )
                    }
                }
            }
        }
    }
}

/** Close [image] whether or not [block] throws (CameraX requires it). */
private inline fun <R> useImageProxy(image: ImageProxy, block: (ImageProxy) -> R): R {
    try {
        return block(image)
    } finally {
        image.close()
    }
}

/** Decode the QR text from a CameraX YUV_420_888 frame (Y plane only). */
private fun qrTextFromFrame(image: ImageProxy): String? {
    return try {
        val plane = image.planes[0]
        val buffer = plane.buffer
        val rowStride = plane.rowStride
        val w = image.width
        val h = image.height
        if (rowStride == w) {
            val y = ByteArray(w * h)
            buffer.get(y)
            QrCodec.decodeYPlane(y, w, h)
        } else {
            val y = ByteArray(rowStride * h)
            for (row in 0 until h) {
                buffer.position(row * rowStride)
                buffer.get(y, row * rowStride, w)
            }
            QrCodec.decodeYPlane(y, rowStride, h, w, h)
        }
    } catch (_: Exception) {
        null
    }
}

/** Scanned contact card confirmation: the security code comparison is
 *  mandatory UX (Windows parity: confirm_contact_invite); confirming pins
 *  the fingerprint so the first handshake must present exactly that key. */
@Composable
fun QrContactConfirmDialog(
    invite: ContactInvite,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("添加联系人") },
        text = {
            Column {
                Text("扫码识别到联系人：")
                Spacer(modifier = Modifier.height(8.dp))
                Text("名字：${invite.name.ifBlank { invite.ip }}", fontSize = 14.sp)
                Text("地址：${invite.ip}:${invite.port}", fontSize = 14.sp)
                Text(
                    if (invite.fingerprint.isNotBlank()) "对方安全码：${invite.fingerprint}"
                    else "对方安全码：（二维码未含安全码）",
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold
                )
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    "请与对方「设置 → 本机安全码」当面核对一致后再添加；\n" +
                        "不一致说明可能存在中间人。添加后首次连接将校验该安全码。",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        },
        confirmButton = {
            Button(onClick = onConfirm) { Text("添加") }
        },
        dismissButton = {
            OutlinedButton(onClick = onDismiss) { Text("取消") }
        }
    )
}
