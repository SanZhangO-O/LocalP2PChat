package com.zqr.localchat.ui.screen

import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Group
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.network.DirectChatManager
import java.text.SimpleDateFormat
import java.util.*

/**
 * Member list — the home screen and the first-class management unit. Every
 * known member (seen in a group, met through a direct chat, or added by
 * address) appears here; tapping one immediately pulls up a 1:1 chat, no
 * confirmation needed. Incoming first-contact requests (and re-add attempts
 * from removed members) are parked in the request box rendered above the
 * list — accept or ignore, nothing is silently dropped. Groups are a
 * secondary entry in the header.
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun MemberListScreen(
    contacts: List<DirectChatManager.Contact>,
    requests: List<DirectChatManager.ContactRequest>,
    lastMessages: Map<String, ChatMessage>,
    onOpenGroups: () -> Unit,
    onOpenSettings: () -> Unit,
    onOpenChat: (DirectChatManager.Contact) -> Unit,
    onAddContact: (ipPort: String, name: String) -> Boolean,
    onRemoveContact: (id: String) -> Unit,
    onAcceptRequest: (id: String) -> Unit,
    onIgnoreRequest: (id: String) -> Unit
) {
    val context = LocalContext.current
    var showAdd by remember { mutableStateOf(false) }
    var pendingDelete by remember { mutableStateOf<String?>(null) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("成员") },
                actions = {
                    IconButton(onClick = onOpenSettings) {
                        Icon(Icons.Filled.Settings, contentDescription = "设置")
                    }
                    IconButton(onClick = onOpenGroups) {
                        Icon(Icons.Filled.Group, contentDescription = "群组")
                    }
                    IconButton(onClick = { showAdd = true }) {
                        Icon(Icons.Filled.Add, contentDescription = "添加成员")
                    }
                }
            )
        }
    ) { padding ->
        if (requests.isEmpty() && contacts.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Icon(
                        Icons.Filled.Person,
                        contentDescription = null,
                        modifier = Modifier.size(64.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                    Text("暂无成员", fontSize = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        "群组成员会自动出现在这里；也可点击右上角 + 按“IP:端口”添加",
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f),
                        modifier = Modifier.padding(horizontal = 32.dp)
                    )
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                if (requests.isNotEmpty()) {
                    item(key = "req-header") {
                        Row(
                            modifier = Modifier.padding(top = 4.dp, bottom = 2.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                "联系人请求（${requests.size}）",
                                fontSize = 13.sp,
                                fontWeight = FontWeight.Bold,
                                color = MaterialTheme.colorScheme.primary
                            )
                            Spacer(modifier = Modifier.width(6.dp))
                            Text(
                                "等待你确认的添加请求",
                                fontSize = 11.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
                            )
                        }
                    }
                    items(requests, key = { "req-${it.id}" }) { request ->
                        RequestItem(
                            request = request,
                            onAccept = { onAcceptRequest(request.id) },
                            onIgnore = { onIgnoreRequest(request.id) }
                        )
                    }
                }
                if (contacts.isNotEmpty()) {
                    item(key = "contacts-header") {
                        Text(
                            "成员",
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = if (requests.isEmpty()) 4.dp else 12.dp, bottom = 2.dp)
                        )
                    }
                    items(contacts, key = { it.id }) { contact ->
                        MemberItem(
                            contact = contact,
                            lastMessage = lastMessages[contact.id],
                            onClick = { onOpenChat(contact) },
                            onRemove = { pendingDelete = contact.id }
                        )
                    }
                }
            }
        }
    }

    pendingDelete?.let { id ->
        val contact = contacts.find { it.id == id }
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("移除成员") },
            text = { Text("确定要移除成员 ${contact?.name ?: ""} 吗？\n聊天记录不会删除，成员仍可通过地址重新添加。") },
            confirmButton = {
                TextButton(onClick = {
                    onRemoveContact(id)
                    pendingDelete = null
                }) {
                    Text("移除", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { pendingDelete = null }) { Text("取消") }
            }
        )
    }

    if (showAdd) {
        var ipText by remember { mutableStateOf("") }
        var nameText by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { showAdd = false },
            title = { Text("添加成员") },
            text = {
                Column {
                    Text(
                        "输入对方的“IP:端口”（默认端口可省略），例如 192.168.1.100 或 192.168.1.100:9999",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = ipText,
                        onValueChange = { ipText = it },
                        singleLine = true,
                        label = { Text("IP:端口") },
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = nameText,
                        onValueChange = { nameText = it },
                        singleLine = true,
                        label = { Text("备注名（可选）") },
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    if (onAddContact(ipText.trim(), nameText.trim())) {
                        showAdd = false
                    } else {
                        Toast.makeText(context, "地址无效", Toast.LENGTH_SHORT).show()
                    }
                }) {
                    Text("添加")
                }
            },
            dismissButton = {
                TextButton(onClick = { showAdd = false }) { Text("取消") }
            }
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun MemberItem(
    contact: DirectChatManager.Contact,
    lastMessage: ChatMessage?,
    onClick: () -> Unit,
    onRemove: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .combinedClickable(onClick = onClick, onLongClick = onRemove),
        shape = RoundedCornerShape(14.dp)
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                modifier = Modifier
                    .size(46.dp)
                    .clip(CircleShape)
                    .background(MaterialTheme.colorScheme.primaryContainer),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = avatarChar(contact.name),
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.onPrimaryContainer
                )
            }
            Spacer(modifier = Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = contact.name,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Spacer(modifier = Modifier.height(2.dp))
                if (lastMessage != null) {
                    Text(
                        text = lastMessage.content,
                        fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                } else {
                    Text(
                        text = "${contact.ip}:${contact.port}",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
                    )
                }
            }
            if (lastMessage != null) {
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = briefTime(lastMessage.timestamp),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
                )
            }
            // explicit remove entry: long-press alone is hard to discover,
            // especially for accessibility users
            IconButton(onClick = onRemove) {
                Icon(
                    Icons.Filled.Delete,
                    contentDescription = "移除成员",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

/** A parked contact request: who wants in, with accept / ignore buttons. */
@Composable
private fun RequestItem(
    request: DirectChatManager.ContactRequest,
    onAccept: () -> Unit,
    onIgnore: () -> Unit
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.35f)
        )
    ) {
        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(42.dp)
                        .clip(CircleShape)
                        .background(MaterialTheme.colorScheme.primary),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = avatarChar(request.name),
                        fontSize = 16.sp,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onPrimary
                    )
                }
                Spacer(modifier = Modifier.width(12.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = request.name,
                        fontSize = 15.sp,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = "${request.ip}:${request.port}",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(2.dp))
                    Text(
                        text = if (request.fromRemoved) "已移除的成员请求重新添加" else "请求添加你为成员",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.error.copy(alpha = 0.9f)
                    )
                }
            }
            Spacer(modifier = Modifier.height(10.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = onIgnore) {
                    Text("忽略", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(modifier = Modifier.width(4.dp))
                Button(onClick = onAccept) {
                    Text("接受")
                }
            }
        }
    }
}

/** Today -> "HH:mm", otherwise -> "M月d日". */
private fun briefTime(timestamp: Long): String {
    val cal = Calendar.getInstance().apply { timeInMillis = timestamp }
    val now = Calendar.getInstance()
    val pattern = if (cal.get(Calendar.YEAR) == now.get(Calendar.YEAR) &&
        cal.get(Calendar.DAY_OF_YEAR) == now.get(Calendar.DAY_OF_YEAR)
    ) "HH:mm" else "M月d日"
    return SimpleDateFormat(pattern, Locale.getDefault()).format(Date(timestamp))
}
