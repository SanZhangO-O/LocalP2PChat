package com.zqr.localchat.ui.screen

import android.content.ClipData
import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.ChatApp
import com.zqr.localchat.network.Constants
import com.zqr.localchat.network.GroupInfo
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.launch

internal const val DEFAULT_GROUP_PORT = Constants.TCP_PORT

internal data class ParsedHostPort(val host: String, val port: Int)

/**
 * Normalizes the full-width punctuation and digits a Chinese IME produces
 * (：．。０-９) to their ASCII equivalents. Without this, an address typed with
 * the IME in Chinese/full-width punctuation mode is saved verbatim and can
 * never connect (Windows parity: ChatViewModel._parse_host_port).
 */
internal fun normalizeAddressInput(input: String): String {
    val sb = StringBuilder(input.length)
    for (ch in input) {
        sb.append(
            when (ch) {
                '：' -> ':'
                '．', '。' -> '.'
                '，' -> ','
                '　' -> ' '
                in '０'..'９' -> '0' + (ch - '０')
                else -> ch
            }
        )
    }
    return sb.toString().trim()
}

/**
 * True for a plausible IPv4 dotted quad or DNS hostname. All-digit but
 * non-IPv4 inputs ("127001", "999") are rejected — they are what a mangled
 * IP entry looks like and would only ever fail to connect.
 */
internal fun isValidHost(host: String): Boolean {
    if (host.isEmpty() || host.length > 253) return false
    val parts = host.split(".")
    if (parts.all { p -> p.isNotEmpty() && p.all { it in '0'..'9' } }) {
        return parts.size == 4 && parts.all { it.toInt() in 0..255 }
    }
    val label = Regex("^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$")
    return parts.all { it.isNotEmpty() && label.matches(it) }
}

/**
 * Parses a host input that accepts either "192.168.1.100" or "192.168.1.100:9999".
 * Full-width IME input is normalized first (see [normalizeAddressInput]);
 * splitting then happens at the last ':' — a trailing all-digit segment is
 * treated as the port, anything else is kept as part of the host. A missing
 * port falls back to [DEFAULT_GROUP_PORT] (9999).
 */
internal fun parseHostPort(input: String): ParsedHostPort {
    val text = normalizeAddressInput(input)
    val idx = text.lastIndexOf(':')
    if (idx >= 0 && idx < text.lastIndex) {
        val tail = text.substring(idx + 1).trim()
        if (tail.isNotEmpty() && tail.all { it.isDigit() }) {
            val port = tail.toIntOrNull()
            if (port != null && port > 0) {
                return ParsedHostPort(text.substring(0, idx).trim(), port)
            }
        }
    }
    return ParsedHostPort(text, DEFAULT_GROUP_PORT)
}

/** True when the input ends with ":digits" but those digits are not a usable
 *  port: 0, above 65535, or too large to parse (silently falling back to the
 *  default port would just fail to connect, so surface it to the user). */
internal fun hasInvalidPort(input: String): Boolean {
    val text = input.trim()
    val idx = text.lastIndexOf(':')
    if (idx < 0 || idx >= text.lastIndex) return false
    val tail = text.substring(idx + 1)
    if (tail.isEmpty() || !tail.all { it.isDigit() }) return false
    val port = tail.toIntOrNull()
    return port == null || port !in 1..65535
}

@Composable
fun SetupScreen(
    localIpAddress: String,
    localPort: Int,
    isQuerying: Boolean,
    isJoining: Boolean = false,
    queriedGroupInfo: GroupInfo?,
    queryError: String?,
    connectionError: String? = null,
    onCreateGroup: (userName: String, groupName: String) -> Unit,
    onQueryGroup: (userName: String, groupName: String, hostIp: String, password: String?) -> Unit,
    onConfirmJoin: () -> Unit,
    onCancelJoin: () -> Unit,
    onClearError: () -> Unit = {},
    onBack: () -> Unit
) {
    val context = LocalContext.current
    var userName by rememberSaveable { mutableStateOf(ChatApp.savedNickname(context)) }
    var groupName by rememberSaveable { mutableStateOf("") }
    var hostIp by rememberSaveable { mutableStateOf("") }
    var groupPassword by rememberSaveable { mutableStateOf("") }
    var modeName by rememberSaveable { mutableStateOf<String?>(null) }
    val mode = modeName?.let { name -> runCatching { SetupMode.valueOf(name) }.getOrNull() }
    var pendingDuplicateCreate by rememberSaveable { mutableStateOf(false) }
    val savedGroupNames by ChatViewModel.savedGroupNames.collectAsState()

    val addressText = if (localIpAddress.isNotEmpty()) {
        "$localIpAddress:$localPort"
    } else {
        "未连接到网络"
    }

    Surface(
        modifier = Modifier.fillMaxSize(),
        color = MaterialTheme.colorScheme.background
    ) {
        when {
            queriedGroupInfo != null && connectionError == null && mode == SetupMode.JOIN -> {
                ConfirmJoinDialog(
                    groupInfo = queriedGroupInfo,
                    isLoading = isJoining,
                    onConfirm = onConfirmJoin,
                    onCancel = onCancelJoin
                )
                JoinGroupForm(
                    userName = userName,
                    groupName = groupName,
                    hostIp = hostIp,
                    groupPassword = groupPassword,
                    isLoading = isQuerying,
                    onUserNameChange = { userName = it.take(20) },
                    onGroupNameChange = { groupName = it },
                    onHostIpChange = { hostIp = it },
                    onPasswordChange = { groupPassword = it },
                    onJoin = {
                        val parsed = parseHostPort(hostIp)
                        if (isValidHost(parsed.host)) {
                            onQueryGroup(
                                userName,
                                groupName,
                                "${parsed.host}:${parsed.port}",
                                groupPassword.ifBlank { null }
                            )
                        }
                    },
                    onBack = { modeName = null }
                )
            }
            else -> {
                when (mode) {
                    null -> SetupModeSelect(
                        addressText = addressText,
                        localIpAddress = localIpAddress,
                        onSelectMode = { modeName = it.name },
                        onBack = onBack
                    )
                    SetupMode.CREATE -> CreateGroupForm(
                        userName = userName,
                        groupName = groupName,
                        addressText = addressText,
                        localIpAddress = localIpAddress,
                        onUserNameChange = { userName = it.take(20) },
                        onGroupNameChange = { groupName = it.take(20) },
                        onCreate = {
                            if (groupName.trim() in savedGroupNames) {
                                pendingDuplicateCreate = true
                            } else {
                                onCreateGroup(userName, groupName)
                            }
                        },
                        onBack = { modeName = null }
                    )
                    SetupMode.JOIN -> JoinGroupForm(
                        userName = userName,
                        groupName = groupName,
                        hostIp = hostIp,
                        groupPassword = groupPassword,
                        isLoading = isQuerying,
                        errorMessage = queryError ?: connectionError,
                        onUserNameChange = {
                            userName = it.take(20)
                            onClearError()
                        },
                        onGroupNameChange = {
                            groupName = it
                            onClearError()
                        },
                        onHostIpChange = {
                            hostIp = it
                            onClearError()
                        },
                        onPasswordChange = {
                            groupPassword = it
                            onClearError()
                        },
                        onJoin = {
                            val parsed = parseHostPort(hostIp)
                            if (isValidHost(parsed.host)) {
                                onQueryGroup(
                                    userName,
                                    groupName,
                                    "${parsed.host}:${parsed.port}",
                                    groupPassword.ifBlank { null }
                                )
                            }
                        },
                        onBack = {
                            modeName = null
                            onCancelJoin()
                        }
                    )
                }
            }
        }
    }

    if (pendingDuplicateCreate) {
        AlertDialog(
            onDismissRequest = { pendingDuplicateCreate = false },
            title = { Text("已存在同名群组") },
            text = { Text("已存在同名群组，重新创建将替换原群组（聊天记录保留），是否继续？") },
            confirmButton = {
                TextButton(
                    onClick = {
                        pendingDuplicateCreate = false
                        onCreateGroup(userName, groupName)
                    }
                ) {
                    Text("继续创建")
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingDuplicateCreate = false }) { Text("取消") }
            }
        )
    }
}

private enum class SetupMode {
    CREATE, JOIN
}

@Composable
private fun ConfirmJoinDialog(
    groupInfo: GroupInfo,
    isLoading: Boolean,
    onConfirm: () -> Unit,
    onCancel: () -> Unit
) {
    AlertDialog(
        onDismissRequest = { if (!isLoading) onCancel() },
        title = { Text("确认加入群组") },
        text = {
            Column {
                Text(
                    text = "找到以下群组，请确认是否加入：",
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.height(12.dp))
                ConfirmInfoRow("群组名称", groupInfo.groupName)
                Spacer(modifier = Modifier.height(4.dp))
                ConfirmInfoRow("创建者", groupInfo.creatorName)
                Spacer(modifier = Modifier.height(4.dp))
                ConfirmInfoRow("当前成员数", "${groupInfo.memberCount}人")
                if (isLoading) {
                    Spacer(modifier = Modifier.height(16.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                        Spacer(modifier = Modifier.width(10.dp))
                        Text(
                            text = "正在加入...",
                            fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onConfirm, enabled = !isLoading) {
                Text("确认加入")
            }
        },
        dismissButton = {
            TextButton(onClick = onCancel, enabled = !isLoading) {
                Text("取消")
            }
        },
        shape = RoundedCornerShape(16.dp)
    )
}

@Composable
private fun ConfirmInfoRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(
            text = label,
            fontSize = 14.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            text = value,
            fontSize = 14.sp,
            fontWeight = FontWeight.Medium
        )
    }
}

@Composable
private fun CopyAddressButton(address: String, networkAvailable: Boolean) {
    val context = LocalContext.current
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    if (!networkAvailable) return
    TextButton(
        onClick = {
            scope.launch {
                clipboard.setClipEntry(
                    ClipEntry(ClipData.newPlainText("LocalChat", address))
                )
                Toast.makeText(context, "已复制地址", Toast.LENGTH_SHORT).show()
            }
        }
    ) {
        Text("复制", fontSize = 12.sp)
    }
}

@Composable
private fun SetupModeSelect(
    addressText: String,
    localIpAddress: String,
    onSelectMode: (SetupMode) -> Unit,
    onBack: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(32.dp)
            .imePadding()
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            text = "LocalChat",
            fontSize = 36.sp,
            fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.primary
        )
        Spacer(modifier = Modifier.height(8.dp))
        Text(
            text = "局域网群组聊天",
            fontSize = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.height(48.dp))
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp),
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant
            )
        ) {
            Column(
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text(
                    text = "本机地址",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.height(4.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = addressText,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.weight(1f)
                    )
                    CopyAddressButton(addressText, localIpAddress.isNotEmpty())
                }
            }
        }
        Spacer(modifier = Modifier.height(32.dp))
        Button(
            onClick = { onSelectMode(SetupMode.CREATE) },
            modifier = Modifier
                .fillMaxWidth()
                .height(50.dp),
            shape = RoundedCornerShape(12.dp)
        ) {
            Text("创建群组", fontSize = 16.sp)
        }
        Spacer(modifier = Modifier.height(16.dp))
        OutlinedButton(
            onClick = { onSelectMode(SetupMode.JOIN) },
            modifier = Modifier
                .fillMaxWidth()
                .height(50.dp),
            shape = RoundedCornerShape(12.dp)
        ) {
            Text("加入群组", fontSize = 16.sp)
        }
        Spacer(modifier = Modifier.height(12.dp))
        TextButton(onClick = onBack) {
            Text("返回")
        }
    }
}

@Composable
private fun CreateGroupForm(
    userName: String,
    groupName: String,
    addressText: String,
    localIpAddress: String,
    onUserNameChange: (String) -> Unit,
    onGroupNameChange: (String) -> Unit,
    onCreate: () -> Unit,
    onBack: () -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(32.dp)
            .imePadding()
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            text = "创建群组",
            fontSize = 28.sp,
            fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.primary
        )
        Spacer(modifier = Modifier.height(32.dp))
        OutlinedTextField(
            value = userName,
            onValueChange = onUserNameChange,
            label = { Text("你的昵称") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp)
        )
        Spacer(modifier = Modifier.height(16.dp))
        OutlinedTextField(
            value = groupName,
            onValueChange = onGroupNameChange,
            label = { Text("群组名称") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp)
        )
        Spacer(modifier = Modifier.height(24.dp))
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp),
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant
            )
        ) {
            Column(
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text(
                    text = "本机地址（分享给其他人加入）",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.height(4.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = addressText,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.weight(1f)
                    )
                    CopyAddressButton(addressText, localIpAddress.isNotEmpty())
                }
            }
        }
        Spacer(modifier = Modifier.height(24.dp))
        Button(
            onClick = onCreate,
            enabled = userName.isNotBlank() && groupName.isNotBlank(),
            modifier = Modifier
                .fillMaxWidth()
                .height(50.dp),
            shape = RoundedCornerShape(12.dp)
        ) {
            Text("创建", fontSize = 16.sp)
        }
        Spacer(modifier = Modifier.height(12.dp))
        TextButton(onClick = onBack) {
            Text("返回")
        }
    }
}

@Composable
private fun JoinGroupForm(
    userName: String,
    groupName: String,
    hostIp: String,
    groupPassword: String,
    isLoading: Boolean,
    errorMessage: String? = null,
    onUserNameChange: (String) -> Unit,
    onGroupNameChange: (String) -> Unit,
    onHostIpChange: (String) -> Unit,
    onPasswordChange: (String) -> Unit,
    onJoin: () -> Unit,
    onBack: () -> Unit
) {
    val parsedHost = remember(hostIp) { parseHostPort(hostIp) }
    val hostInputError = if (hostIp.isNotBlank() && !isValidHost(parsedHost.host)) "请输入有效的IP地址" else null
    val hostPortError = if (hasInvalidPort(hostIp)) "端口无效（范围 1-65535）" else null

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(32.dp)
            .imePadding()
            .verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            text = "加入群组",
            fontSize = 28.sp,
            fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.primary
        )
        Spacer(modifier = Modifier.height(32.dp))
        OutlinedTextField(
            value = userName,
            onValueChange = onUserNameChange,
            label = { Text("你的昵称") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp)
        )
        Spacer(modifier = Modifier.height(16.dp))
        OutlinedTextField(
            value = groupName,
            onValueChange = onGroupNameChange,
            label = { Text("群组数字ID") },
            placeholder = { Text("例如: 4829 1357") },
            supportingText = { Text("创建者分享的8位数字ID；ID 由创建者设备指纹生成，与群名无关") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp)
        )
        Spacer(modifier = Modifier.height(16.dp))
        OutlinedTextField(
            value = hostIp,
            onValueChange = onHostIpChange,
            label = { Text("创建者的IP地址（可含端口）") },
            placeholder = { Text("例如: 192.168.1.100 或 192.168.1.100:9999") },
            supportingText = { Text("省略端口时默认 $DEFAULT_GROUP_PORT；地址可在创建者的“本机地址”卡片中点击复制") },
            isError = hostInputError != null || hostPortError != null,
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp)
        )
        if (hostInputError != null) {
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = hostInputError,
                color = MaterialTheme.colorScheme.error,
                fontSize = 12.sp
            )
        }
        if (hostPortError != null) {
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = hostPortError,
                color = MaterialTheme.colorScheme.error,
                fontSize = 12.sp
            )
        }
        if (errorMessage != null) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = errorMessage,
                color = MaterialTheme.colorScheme.error,
                fontSize = 13.sp
            )
        }
        Spacer(modifier = Modifier.height(16.dp))
        OutlinedTextField(
            value = groupPassword,
            onValueChange = onPasswordChange,
            label = { Text("群组密码") },
            placeholder = { Text("创建者分享的8位密码") },
            supportingText = { Text("密码错误将被拒绝加入；传输全程加密，密码不会明文出现") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp)
        )
        Spacer(modifier = Modifier.height(24.dp))
        val joinIdDigits = groupName.filter { it.isDigit() }
        Button(
            onClick = onJoin,
            enabled = !isLoading && userName.isNotBlank() &&
                joinIdDigits.length == 8 && isValidHost(parsedHost.host) && hostPortError == null,
            modifier = Modifier
                .fillMaxWidth()
                .height(50.dp),
            shape = RoundedCornerShape(12.dp)
        ) {
            if (isLoading) {
                CircularProgressIndicator(
                    modifier = Modifier.size(20.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.onPrimary
                )
            } else {
                Text("查找群组", fontSize = 16.sp)
            }
        }
        Spacer(modifier = Modifier.height(12.dp))
        TextButton(onClick = onBack) {
            Text("返回")
        }
    }
}
