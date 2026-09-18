package com.zqr.localchat.ui.screen

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zqr.localchat.data.ChatMessage
import com.zqr.localchat.data.FileKind
import com.zqr.localchat.data.MessageSearch
import com.zqr.localchat.viewmodel.ChatViewModel
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * Global message search: keyword + optional conversation scope over the
 * persisted group and direct-chat history (see
 * [ChatViewModel.searchHistory]). Tapping a hit opens that conversation and
 * asks it to scroll to and highlight the message.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SearchScreen(
    viewModel: ChatViewModel,
    onOpenResult: (conversationId: String, messageId: String) -> Unit,
    onBack: () -> Unit
) {
    var keyword by remember { mutableStateOf("") }
    var scopeId by remember { mutableStateOf<String?>(null) }
    var hits by remember { mutableStateOf<List<ChatViewModel.SearchHit>>(emptyList()) }
    var searching by remember { mutableStateOf(false) }
    val groups by viewModel.groups.collectAsState()
    val contacts by viewModel.directContacts.collectAsState()
    val scopes = remember(groups, contacts) { viewModel.searchScopes() }

    LaunchedEffect(keyword, scopeId) {
        val kw = keyword.trim()
        if (kw.isEmpty()) {
            hits = emptyList()
            searching = false
            return@LaunchedEffect
        }
        // small debounce: typing should not run a query per keystroke
        delay(200)
        searching = true
        hits = viewModel.searchHistory(kw, scopeId)
        searching = false
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("搜索消息") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = "返回",
                            tint = MaterialTheme.colorScheme.onSurface
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    titleContentColor = MaterialTheme.colorScheme.onSurface
                )
            )
        }
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            OutlinedTextField(
                value = keyword,
                onValueChange = { keyword = it },
                placeholder = { Text("输入关键词，搜索群聊与私聊记录") },
                singleLine = true,
                leadingIcon = { Icon(Icons.Default.Search, contentDescription = null) },
                trailingIcon = {
                    if (keyword.isNotEmpty()) {
                        IconButton(onClick = { keyword = "" }) {
                            Icon(Icons.Default.Close, contentDescription = "清空")
                        }
                    }
                },
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 8.dp)
            )
            ScopeChips(
                scopes = scopes,
                selectedId = scopeId,
                onSelect = { scopeId = it }
            )
            when {
                keyword.isBlank() -> HintText("输入关键词后即可搜索历史消息")
                searching -> HintText("搜索中…")
                hits.isEmpty() -> HintText("没有找到匹配的消息")
                else -> HintText("找到 ${hits.size} 条消息")
            }
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                items(hits, key = { it.conversationId + "/" + it.message.id }) { hit ->
                    SearchHitCard(hit) {
                        onOpenResult(hit.conversationId, hit.message.id)
                    }
                }
            }
        }
    }
}

@Composable
private fun HintText(text: String) {
    Text(
        text = text,
        fontSize = 12.sp,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp)
    )
}

/** Range selector: 全部 + every known conversation, horizontally scrollable. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ScopeChips(
    scopes: List<ChatViewModel.SearchScope>,
    selectedId: String?,
    onSelect: (String?) -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState())
            .padding(horizontal = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        FilterChip(
            selected = selectedId == null,
            onClick = { onSelect(null) },
            label = { Text("全部", fontSize = 12.sp) }
        )
        scopes.forEach { scope ->
            FilterChip(
                selected = selectedId == scope.id,
                onClick = { onSelect(scope.id) },
                label = { Text(scope.name, fontSize = 12.sp) }
            )
        }
    }
}

@Composable
private fun SearchHitCard(hit: ChatViewModel.SearchHit, onClick: () -> Unit) {
    val message = hit.message
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
        shape = RoundedCornerShape(14.dp)
    ) {
        Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = hit.conversationName,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.weight(1f),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = searchTime(message.timestamp),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.7f)
                )
            }
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                text = "${if (message.isFromMe) "我" else message.senderName}：${messagePreview(message)}",
                fontSize = 13.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
        }
    }
}

/**
 * In-conversation search bar + result dropdown, rendered over the message list
 * by the group/direct chat screens.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun ChatSearchOverlay(
    query: String,
    onQueryChange: (String) -> Unit,
    results: List<ChatMessage>,
    onPick: (ChatMessage) -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier
) {
    Surface(
        modifier = modifier,
        tonalElevation = 6.dp,
        shadowElevation = 6.dp,
        color = MaterialTheme.colorScheme.surface
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp)
        ) {
            OutlinedTextField(
                value = query,
                onValueChange = onQueryChange,
                placeholder = { Text("在本会话中搜索") },
                singleLine = true,
                leadingIcon = { Icon(Icons.Default.Search, contentDescription = null) },
                trailingIcon = {
                    IconButton(onClick = onClose) {
                        Icon(Icons.Default.Close, contentDescription = "关闭搜索")
                    }
                },
                modifier = Modifier.fillMaxWidth()
            )
            if (query.isNotBlank()) {
                if (results.isEmpty()) {
                    Text(
                        text = "没有匹配的消息",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(horizontal = 4.dp, vertical = 6.dp)
                    )
                } else {
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 220.dp),
                        contentPadding = PaddingValues(vertical = 4.dp)
                    ) {
                        items(results, key = { it.id }) { message ->
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { onPick(message) }
                                    .padding(horizontal = 6.dp, vertical = 6.dp)
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text(
                                        text = if (message.isFromMe) "我" else message.senderName,
                                        fontSize = 12.sp,
                                        fontWeight = FontWeight.Medium,
                                        modifier = Modifier.weight(1f)
                                    )
                                    Text(
                                        text = searchTime(message.timestamp),
                                        fontSize = 10.sp,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                                Text(
                                    text = messagePreview(message),
                                    fontSize = 13.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

/** Wraps one conversation row so a search-jump target flashes visibly. */
@Composable
internal fun HighlightWrapper(highlighted: Boolean, content: @Composable () -> Unit) {
    if (!highlighted) {
        content()
        return
    }
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .background(
                MaterialTheme.colorScheme.primary.copy(alpha = 0.18f),
                RoundedCornerShape(14.dp)
            )
            .padding(2.dp)
    ) {
        content()
    }
}

/** True when [item] is the message a search jump targets. */
internal fun MessageItem.matchesMessageId(messageId: String): Boolean = when (this) {
    is MessageItem.Folder -> group.entries.any { it.id == messageId }
    is MessageItem.Msg -> message.id == messageId
}

/** One-line preview of a search hit (mirrors MessageSearch.preview for the
 *  in-memory [ChatMessage] shape). */
internal fun messagePreview(message: ChatMessage): String {
    val text = message.content.replace('\n', ' ').trim()
    val fi = message.fileInfo ?: return truncateSnippet(text)
    val body = fi.relativePath.ifEmpty { text }
    val prefix = when (fi.kind) {
        FileKind.IMAGE -> "[图片] "
        FileKind.VIDEO -> "[视频] "
        else -> "[文件] "
    }
    return truncateSnippet(prefix + body)
}

private fun truncateSnippet(text: String): String =
    if (text.length <= MessageSearch.SNIPPET_MAX) text
    else text.take(MessageSearch.SNIPPET_MAX) + "..."

/** Today -> "HH:mm", otherwise -> "M月d日". */
internal fun searchTime(timestamp: Long): String {
    val cal = Calendar.getInstance().apply { timeInMillis = timestamp }
    val now = Calendar.getInstance()
    val pattern = if (cal.get(Calendar.YEAR) == now.get(Calendar.YEAR) &&
        cal.get(Calendar.DAY_OF_YEAR) == now.get(Calendar.DAY_OF_YEAR)
    ) "HH:mm" else "M月d日"
    return SimpleDateFormat(pattern, Locale.getDefault()).format(Date(timestamp))
}
