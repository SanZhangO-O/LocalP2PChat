package com.zqr.localchat.ui.screen

import android.Manifest
import android.content.pm.PackageManager
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.EmojiEmotions
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Movie
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Bottom composer shared by the group and the 1:1 chat.
 *
 * Only three things stay on the row: the attach toggle, the text field and
 * send. File / image / video / folder / emoji / voice live in the panel the
 * toggle opens — six inline icon buttons left the text field a sliver on
 * phone-width screens. While a voice clip is recording the toggle turns into
 * a stop chip so capture can always be ended even with the panel closed.
 */
@Composable
internal fun ChatInputBar(
    value: TextFieldValue,
    onValueChange: (TextFieldValue) -> Unit,
    /** Send button state; the IME action stays ungated so the callee can explain itself. */
    canSend: Boolean,
    onSend: () -> Unit,
    /** File / image / video / folder / voice availability (connection). */
    actionsEnabled: Boolean,
    onPickFile: () -> Unit,
    onPickImage: () -> Unit,
    onPickVideo: () -> Unit,
    onPickFolder: () -> Unit,
    onEmoji: () -> Unit,
    /** Start capture; the caller owns the recorder and its 60s timer. */
    onVoiceStart: () -> Unit,
    /** Stop the running capture (the caller stops and offers the clip). */
    onVoiceStop: () -> Unit,
    voiceRecording: Boolean = false,
    voiceSeconds: Int = 0,
    maxLines: Int = 5,
    /** IME action key; the direct chat keeps `Default` so Enter inserts a newline. */
    imeAction: ImeAction = ImeAction.Send,
    isError: Boolean = false,
    supportingText: @Composable (() -> Unit)? = null,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    var actionsOpen by rememberSaveable { mutableStateOf(false) }

    // RECORD_AUDIO is a runtime permission: gate both chat screens here (a
    // fresh install that never placed a call has no grant, and AudioRecord
    // then stays silent — an empty clip).
    val recordPermissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            onVoiceStart()
        } else {
            Toast.makeText(
                context,
                "未授予麦克风权限，无法录制语音（可在系统设置中开启）",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    /** Toggle capture; the recording state itself lives in the caller. */
    fun toggleVoice() {
        if (voiceRecording) {
            onVoiceStop()
        } else if (actionsEnabled) {
            val granted = ContextCompat.checkSelfPermission(
                context, Manifest.permission.RECORD_AUDIO
            ) == PackageManager.PERMISSION_GRANTED
            if (granted) {
                onVoiceStart()
            } else {
                recordPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
            }
        }
    }

    // Recording is only reachable through the panel; collapse it so the stop
    // chip (and the draft) stay visible for the whole capture.
    LaunchedEffect(voiceRecording) {
        if (voiceRecording) actionsOpen = false
    }

    /** Run a panel action and collapse the panel. */
    fun pick(action: () -> Unit) {
        actionsOpen = false
        action()
    }

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp)
    ) {
        if (actionsOpen) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(bottom = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(2.dp)
            ) {
                ComposerAction(
                    modifier = Modifier.weight(1f),
                    label = "文件",
                    enabled = actionsEnabled,
                    onClick = { pick(onPickFile) }
                ) { tint ->
                    Icon(Icons.Filled.AttachFile, contentDescription = "发送文件", tint = tint)
                }
                ComposerAction(
                    modifier = Modifier.weight(1f),
                    label = "图片",
                    enabled = actionsEnabled,
                    onClick = { pick(onPickImage) }
                ) { tint ->
                    Icon(Icons.Filled.Image, contentDescription = "发送图片", tint = tint)
                }
                ComposerAction(
                    modifier = Modifier.weight(1f),
                    label = "视频",
                    enabled = actionsEnabled,
                    onClick = { pick(onPickVideo) }
                ) { tint ->
                    Icon(Icons.Filled.Movie, contentDescription = "发送视频", tint = tint)
                }
                ComposerAction(
                    modifier = Modifier.weight(1f),
                    label = "文件夹",
                    enabled = actionsEnabled,
                    onClick = { pick(onPickFolder) }
                ) { tint ->
                    Icon(Icons.Filled.Folder, contentDescription = "发送文件夹", tint = tint)
                }
                ComposerAction(
                    modifier = Modifier.weight(1f),
                    label = "表情",
                    enabled = true,
                    onClick = { pick(onEmoji) }
                ) { tint ->
                    Icon(Icons.Filled.EmojiEmotions, contentDescription = "表情", tint = tint)
                }
                ComposerAction(
                    modifier = Modifier.weight(1f),
                    label = "语音",
                    enabled = actionsEnabled,
                    onClick = { pick { toggleVoice() } }
                ) { tint ->
                    Icon(Icons.Filled.Mic, contentDescription = "语音", tint = tint)
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.Bottom
        ) {
            if (voiceRecording) {
                TextButton(onClick = onVoiceStop) {
                    Icon(
                        Icons.Filled.Stop,
                        contentDescription = "停止录音并发送",
                        tint = MaterialTheme.colorScheme.error,
                        modifier = Modifier.size(18.dp)
                    )
                    Spacer(modifier = Modifier.width(4.dp))
                    Text(
                        text = "${voiceSeconds}s",
                        color = MaterialTheme.colorScheme.error,
                        fontSize = 13.sp,
                        maxLines = 1
                    )
                }
            } else {
                IconButton(onClick = { actionsOpen = !actionsOpen }) {
                    Icon(
                        imageVector = if (actionsOpen) Icons.Filled.Close else Icons.Filled.Add,
                        contentDescription = if (actionsOpen) "收起发送选项" else "更多发送选项",
                        tint = MaterialTheme.colorScheme.primary
                    )
                }
            }
            OutlinedTextField(
                value = value,
                onValueChange = onValueChange,
                placeholder = { Text("输入消息...") },
                modifier = Modifier.weight(1f),
                shape = RoundedCornerShape(20.dp),
                minLines = 1,
                maxLines = maxLines,
                isError = isError,
                supportingText = supportingText,
                keyboardOptions = KeyboardOptions(imeAction = imeAction),
                // The callee must stay reachable while the send button is
                // disabled (e.g. disconnected): it reports why nothing was sent.
                keyboardActions = KeyboardActions(onSend = { onSend() })
            )
            Spacer(modifier = Modifier.width(8.dp))
            FilledIconButton(
                onClick = onSend,
                enabled = canSend,
                modifier = Modifier.size(48.dp)
            ) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送")
            }
        }
    }
}

/** One labelled action inside the collapsed panel. */
@Composable
private fun ComposerAction(
    modifier: Modifier,
    label: String,
    enabled: Boolean,
    onClick: () -> Unit,
    icon: @Composable (Color) -> Unit
) {
    val tint = if (enabled) MaterialTheme.colorScheme.primary
    else MaterialTheme.colorScheme.onSurfaceVariant
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(14.dp))
            .clickable(enabled = enabled, onClick = onClick)
            .padding(vertical = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        icon(tint)
        Spacer(modifier = Modifier.height(4.dp))
        Text(
            text = label,
            fontSize = 11.sp,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            textAlign = TextAlign.Center,
            color = if (enabled) MaterialTheme.colorScheme.onSurface
            else MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}
