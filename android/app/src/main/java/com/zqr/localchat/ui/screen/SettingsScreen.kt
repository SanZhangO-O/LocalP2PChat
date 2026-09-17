package com.zqr.localchat.ui.screen

import android.content.ClipData
import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ClipEntry
import androidx.compose.ui.platform.LocalClipboard
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.network.LocalAddress
import kotlinx.coroutines.launch

/**
 * 独立设置界面：显示/修改自己的昵称、本机 IP 与本机端口（端口修改后整个
 * 程序的监听地址随之改变，需要把新的"IP:端口"分享给其他人）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    nickname: String,
    localIp: String,
    localPort: Int,
    backgroundRunning: Boolean,
    allIps: List<LocalAddress> = emptyList(),
    securityCode: String = "",
    onSaveNickname: (String) -> Unit,
    onSavePort: (Int) -> Unit,
    onToggleBackgroundRunning: (Boolean) -> Unit,
    onBack: () -> Unit
) {
    val context = LocalContext.current
    val clipboard = LocalClipboard.current
    val scope = rememberCoroutineScope()
    var nickText by remember { mutableStateOf(nickname) }
    var portText by remember { mutableStateOf(localPort.toString()) }
    var portError by remember { mutableStateOf<String?>(null) }
    var showPortSaved by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("设置") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "返回")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .consumeWindowInsets(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // ---- 昵称 ----
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("我的昵称", fontWeight = FontWeight.Medium, fontSize = 15.sp)
                    Text(
                        "群组、直聊和通话中显示的名字；新加入的群组会使用它",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    OutlinedTextField(
                        value = nickText,
                        onValueChange = { nickText = it.take(20) },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth()
                    )
                    Button(
                        onClick = {
                            val trimmed = nickText.trim()
                            if (trimmed.isEmpty()) {
                                Toast.makeText(context, "昵称不能为空", Toast.LENGTH_SHORT).show()
                            } else {
                                onSaveNickname(trimmed)
                                Toast.makeText(context, "昵称已保存", Toast.LENGTH_SHORT).show()
                            }
                        },
                        enabled = nickText.isNotBlank(),
                        modifier = Modifier.align(Alignment.End)
                    ) {
                        Text("保存昵称")
                    }
                }
            }

            // ---- 本机 IP ----
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("本机 IP", fontWeight = FontWeight.Medium, fontSize = 15.sp)
                    Text(
                        "其他设备通过它连接你；创建群组后把\"IP:端口\"分享给成员",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = localIp.ifBlank { "未连接到网络" },
                            fontSize = 16.sp,
                            fontWeight = FontWeight.Medium,
                            modifier = Modifier.weight(1f)
                        )
                        TextButton(
                            onClick = {
                                scope.launch {
                                    clipboard.setClipEntry(
                                        ClipEntry(ClipData.newPlainText("LocalChat", localIp))
                                    )
                                    Toast.makeText(context, "已复制IP", Toast.LENGTH_SHORT).show()
                                }
                            },
                            enabled = localIp.isNotBlank()
                        ) {
                            Text("复制", fontSize = 13.sp)
                        }
                    }
                    // 设备可能同时连接多个网络；只在真有多个地址（或挑不出
                    // 主地址但存在其他网卡）时才列出全部，单一网络的常见
                    // 场景保持简洁。
                    val distinctIps = allIps.distinctBy { it.address }
                    if (distinctIps.size > 1 || (localIp.isBlank() && distinctIps.isNotEmpty())) {
                        Text(
                            "所有网络地址",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        distinctIps.forEach { ip ->
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    text = ip.interfaceName,
                                    fontSize = 13.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.width(64.dp)
                                )
                                Text(
                                    text = ip.address,
                                    fontSize = 14.sp,
                                    modifier = Modifier.weight(1f)
                                )
                                if (ip.address == localIp) {
                                    Text(
                                        "当前使用",
                                        fontSize = 11.sp,
                                        color = MaterialTheme.colorScheme.primary,
                                        modifier = Modifier.padding(end = 4.dp)
                                    )
                                }
                                TextButton(
                                    onClick = {
                                        scope.launch {
                                            clipboard.setClipEntry(
                                                ClipEntry(ClipData.newPlainText("LocalChat", ip.address))
                                            )
                                            Toast.makeText(context, "已复制IP", Toast.LENGTH_SHORT).show()
                                        }
                                    }
                                ) {
                                    Text("复制", fontSize = 13.sp)
                                }
                            }
                        }
                        Text(
                            "设备可能同时连接多个网络（Wi-Fi、热点、VPN、USB共享等），对方只有在对应网络内才能连上该地址",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }

            // ---- 端口 ----
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("本机端口", fontWeight = FontWeight.Medium, fontSize = 15.sp)
                    Text(
                        "整个程序使用同一个端口（默认 $DEFAULT_GROUP_PORT），所有群组和直聊都通过它。修改后立即生效，需要把新的\"IP:端口\"地址分享给其他人。",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    OutlinedTextField(
                        value = portText,
                        onValueChange = {
                            portText = it.filter { ch -> ch.isDigit() }.take(5)
                            portError = null
                        },
                        singleLine = true,
                        isError = portError != null,
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                            keyboardType = KeyboardType.Number
                        ),
                        modifier = Modifier.fillMaxWidth()
                    )
                    if (portError != null) {
                        Text(
                            text = portError ?: "",
                            color = MaterialTheme.colorScheme.error,
                            fontSize = 12.sp
                        )
                    }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        if (showPortSaved) {
                            Text(
                                "端口已修改",
                                fontSize = 12.sp,
                                color = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.weight(1f)
                            )
                        } else {
                            Spacer(modifier = Modifier.weight(1f))
                        }
                        Button(
                            onClick = {
                                val p = portText.toIntOrNull()
                                if (p == null || p !in 1..65535) {
                                    portError = "端口必须在 1-65535 之间"
                                } else {
                                    onSavePort(p)
                                    showPortSaved = true
                                }
                            }
                        ) {
                            Text("保存端口")
                        }
                    }
                }
            }

            // ---- 安全码 ----
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("本机安全码", fontWeight = FontWeight.Medium, fontSize = 15.sp)
                    Text(
                        "设备身份密钥的指纹。直聊和音视频通话全程加密；首次联系后若对方身份变化会自动拒绝连接。可与对方当面核对安全码，完全排除中间人。",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = securityCode.ifBlank { "未生成" },
                            fontSize = 16.sp,
                            fontWeight = FontWeight.Medium,
                            letterSpacing = 2.sp,
                            modifier = Modifier.weight(1f)
                        )
                        TextButton(
                            onClick = {
                                scope.launch {
                                    clipboard.setClipEntry(
                                        ClipEntry(ClipData.newPlainText("LocalChat", securityCode))
                                    )
                                    Toast.makeText(context, "已复制安全码", Toast.LENGTH_SHORT).show()
                                }
                            },
                            enabled = securityCode.isNotBlank()
                        ) {
                            Text("复制", fontSize = 13.sp)
                        }
                    }
                }
            }

            // ---- 后台运行 ----
            Card(modifier = Modifier.fillMaxWidth()) {
                Row(
                    modifier = Modifier.padding(16.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text("后台运行", fontWeight = FontWeight.Medium, fontSize = 15.sp)
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            "开启后，应用退到后台时仍保持群组连接，并显示常驻通知。关闭后，仅在应用处于前台时保持连接。",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Spacer(modifier = Modifier.width(16.dp))
                    Switch(
                        checked = backgroundRunning,
                        onCheckedChange = onToggleBackgroundRunning
                    )
                }
            }

            // ---- 联系作者 ----
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("联系作者", fontWeight = FontWeight.Medium, fontSize = 15.sp)
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = AUTHOR_EMAIL,
                            fontSize = 14.sp,
                            modifier = Modifier.weight(1f)
                        )
                        TextButton(
                            onClick = {
                                scope.launch {
                                    clipboard.setClipEntry(
                                        ClipEntry(ClipData.newPlainText("LocalChat", AUTHOR_EMAIL))
                                    )
                                    Toast.makeText(context, "已复制邮箱", Toast.LENGTH_SHORT).show()
                                }
                            }
                        ) {
                            Text("复制", fontSize = 13.sp)
                        }
                    }
                }
            }
        }
    }
}

private const val AUTHOR_EMAIL = "jjwt@163.com"
