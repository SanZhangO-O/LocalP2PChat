package com.zqr.localchat.ui.screen

import android.graphics.Bitmap
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material.icons.filled.Cameraswitch
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material.icons.filled.Videocam
import androidx.compose.material.icons.filled.VideocamOff
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.call.CallManager

/**
 * Full-screen overlay shown while a call is outgoing/active/incoming: remote
 * video, local preview, mute toggles, camera flip and the hangup button.
 */
@Composable
fun CallOverlay(
    state: CallManager.CallState,
    remoteVideo: Bitmap?,
    localVideo: Bitmap?,
    audioMuted: Boolean,
    videoMuted: Boolean,
    usingFrontCamera: Boolean,
    onToggleAudio: () -> Unit,
    onToggleVideo: () -> Unit,
    onSwitchCamera: () -> Unit,
    onHangup: () -> Unit
) {
    // Incoming calls are handled by the accept/reject dialog: rendering the
    // full-screen overlay too would stack two UIs on top of each other.
    if (state !is CallManager.CallState.Outgoing && state !is CallManager.CallState.Active) return

    // Voice-only call: no video surfaces at all (the media kind is carried
    // by the offer and lives on the call state).
    val audioOnly = when (state) {
        is CallManager.CallState.Outgoing -> state.media == CallManager.MEDIA_AUDIO
        is CallManager.CallState.Active -> state.media == CallManager.MEDIA_AUDIO
        else -> false
    }
    val peerName = when (state) {
        is CallManager.CallState.Outgoing -> state.peerName
        is CallManager.CallState.Active -> state.peerName
        else -> ""
    }

    val title = when (state) {
        is CallManager.CallState.Outgoing -> "正在呼叫 $peerName..."
        is CallManager.CallState.Active -> "与 $peerName 通话中"
        else -> ""
    }

    Surface(modifier = Modifier.fillMaxSize(), color = Color.Black) {
        Box(modifier = Modifier.fillMaxSize()) {
            if (audioOnly) {
                // voice-only: avatar placeholder instead of video
                Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Surface(
                            modifier = Modifier.size(120.dp),
                            shape = CircleShape,
                            color = Color.White.copy(alpha = 0.15f)
                        ) {
                            Box(contentAlignment = Alignment.Center) {
                                Text(
                                    text = avatarChar(peerName.ifBlank { "?" }),
                                    fontSize = 48.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = Color.White
                                )
                            }
                        }
                        Spacer(modifier = Modifier.height(16.dp))
                        Text(
                            text = if (state is CallManager.CallState.Outgoing) "等待对方接听..."
                            else "语音通话中",
                            color = Color.White.copy(alpha = 0.7f),
                            fontSize = 15.sp
                        )
                    }
                }
            } else if (remoteVideo != null) {
                Image(
                    bitmap = remoteVideo.asImageBitmap(),
                    contentDescription = "对方视频",
                    modifier = Modifier.fillMaxSize(),
                    contentScale = ContentScale.Fit
                )
            } else {
                Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = when (state) {
                            is CallManager.CallState.Outgoing -> "等待对方接听..."
                            is CallManager.CallState.Active -> "等待对方视频..."
                            else -> ""
                        },
                        color = Color.White.copy(alpha = 0.7f),
                        fontSize = 16.sp
                    )
                }
            }

            // mirrored local preview, top-end corner (video calls only)
            if (!audioOnly) {
                localVideo?.let { bmp ->
                    Image(
                        bitmap = bmp.asImageBitmap(),
                        contentDescription = "本机预览",
                        modifier = Modifier
                            .align(Alignment.TopEnd)
                            .padding(top = 16.dp, end = 16.dp)
                            .size(width = 150.dp, height = 112.dp)
                            .clip(RoundedCornerShape(12.dp)),
                        contentScale = ContentScale.Crop
                    )
                }
            }

            Text(
                text = title,
                color = Color.White,
                fontSize = 17.sp,
                fontWeight = FontWeight.Medium,
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(top = 16.dp)
            )

            Row(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(bottom = 56.dp),
                horizontalArrangement = Arrangement.spacedBy(28.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                FloatingActionButton(
                    onClick = onToggleAudio,
                    containerColor = if (audioMuted)
                        MaterialTheme.colorScheme.errorContainer
                    else
                        Color.White.copy(alpha = 0.25f),
                    shape = CircleShape
                ) {
                    Icon(
                        if (audioMuted) Icons.Filled.MicOff else Icons.Filled.Mic,
                        contentDescription = if (audioMuted) "取消静音" else "静音",
                        tint = if (audioMuted) MaterialTheme.colorScheme.onErrorContainer else Color.White
                    )
                }
                if (!audioOnly) {
                    FloatingActionButton(
                        onClick = onToggleVideo,
                        containerColor = if (videoMuted)
                            MaterialTheme.colorScheme.errorContainer
                        else
                            Color.White.copy(alpha = 0.25f),
                        shape = CircleShape
                    ) {
                        Icon(
                            if (videoMuted) Icons.Filled.VideocamOff else Icons.Filled.Videocam,
                            contentDescription = if (videoMuted) "开启摄像头" else "关闭摄像头",
                            tint = if (videoMuted) MaterialTheme.colorScheme.onErrorContainer else Color.White
                        )
                    }
                }
                if (!audioOnly && state is CallManager.CallState.Active) {
                    FloatingActionButton(
                        onClick = onSwitchCamera,
                        containerColor = Color.White.copy(alpha = 0.25f),
                        shape = CircleShape
                    ) {
                        Icon(
                            Icons.Filled.Cameraswitch,
                            contentDescription = if (usingFrontCamera) "切换到后置摄像头" else "切换到前置摄像头",
                            tint = Color.White
                        )
                    }
                }
                FloatingActionButton(
                    onClick = onHangup,
                    containerColor = Color(0xFFD32F2F),
                    shape = CircleShape
                ) {
                    Icon(
                        Icons.Filled.CallEnd,
                        contentDescription = "挂断",
                        tint = Color.White
                    )
                }
            }
        }
    }
}
