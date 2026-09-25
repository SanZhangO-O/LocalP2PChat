"use strict";

const MAX_CONTENT_LENGTH = 5000;

const state = {
  ws: null,
  snapshot: null,
  openKey: null,
  pendingOpenKey: null,
  typingTimers: {},
  typingShown: {},
  reqCounter: 1,
  pendingReplies: {},
  emojiQuick: ["\uD83D\uDC4D", "\u2764\uFE0F", "\uD83D\uDE02", "\uD83D\uDE2E", "\uD83D\uDE22", "\uD83C\uDF89"],
  // multiple accounts per machine/browser: token of the active one, plus the
  // locally remembered list for one-click switching
  auth: { token: null, username: null, accounts: [] },
};

/* ---------------- local account store ---------------- */

const ACCOUNTS_KEY = "lc_accounts";
const ACTIVE_KEY = "lc_active";

function loadStoredAccounts() {
  try {
    const list = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "[]");
    return Array.isArray(list)
      ? list.filter((a) => a && typeof a.username === "string" && typeof a.token === "string")
      : [];
  } catch (e) {
    return [];
  }
}

function saveStoredAccounts(list) {
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(list));
}

function upsertAccount(username, token) {
  state.auth.accounts = state.auth.accounts.filter((a) => a.username !== username);
  state.auth.accounts.push({ username, token });
  saveStoredAccounts(state.auth.accounts);
}

function setActiveUsername(username) {
  if (username) localStorage.setItem(ACTIVE_KEY, username);
  else localStorage.removeItem(ACTIVE_KEY);
}

function getActiveUsername() {
  return localStorage.getItem(ACTIVE_KEY) || null;
}

// every authenticated request goes through here so a switched (non-cookie)
// account keeps working
function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.auth.token) headers["X-LC-Token"] = state.auth.token;
  return fetch(path, Object.assign({}, opts, { headers }));
}

function $(id) {
  return document.getElementById(id);
}

function esc(text) {
  // peer-controlled strings (names, ids, file names) end up inside HTML
  // attributes such as value="...", so quotes must be escaped too
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const suffix = state.auth.token ? `?token=${encodeURIComponent(state.auth.token)}` : "";
  const ws = new WebSocket(`${proto}//${location.host}/${suffix}`);
  state.ws = ws;
  ws.onopen = () => {
    send({ action: "getSnapshot" });
  };
  ws.onclose = async () => {
    // a dropped session (TTL / logout elsewhere) must not leave a silently
    // frozen UI: only reload when the server answers and says we are logged out
    try {
      const doc = await (await apiFetch("/api/whoami")).json();
      if (!doc.authenticated) {
        location.reload();
        return;
      }
    } catch (e) {
      /* server unreachable: keep retrying below */
    }
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    handleServerMessage(msg);
  };
}

function send(obj) {
  if (state.ws && state.ws.readyState === 1) {
    state.ws.send(JSON.stringify(obj));
  }
}

function request(payload) {
  return new Promise((resolve) => {
    const reqId = `r${state.reqCounter++}`;
    state.pendingReplies[reqId] = resolve;
    send({ ...payload, reqId });
    setTimeout(() => {
      if (state.pendingReplies[reqId]) {
        delete state.pendingReplies[reqId];
        resolve(null);
      }
    }, 20000);
  });
}

function handleServerMessage(msg) {
  if (msg.snapshot) {
    state.snapshot = msg.snapshot;
    renderAll();
  }
  if (msg.event) toast(msg.event);
  if (msg.startedDirect || msg.createdGroup) {
    const payload = msg.startedDirect || msg.createdGroup;
    const reqId = payload.reqId;
    if (reqId && state.pendingReplies[reqId]) {
      const resolve = state.pendingReplies[reqId];
      delete state.pendingReplies[reqId];
      resolve(payload);
    }
  }
  if (msg.typing) {
    const { chatKey, senderId, senderName, active } = msg.typing;
    if (chatKey === state.openKey && senderId !== state.snapshot?.profile?.userId) {
      if (active) {
        state.typingShown[chatKey] = senderName;
        clearTimeout(state.typingTimers[chatKey]);
        state.typingTimers[chatKey] = setTimeout(() => {
          state.typingShown[chatKey] = null;
          renderTyping();
        }, 5000);
      } else {
        state.typingShown[chatKey] = null;
        clearTimeout(state.typingTimers[chatKey]);
      }
      renderTyping();
    }
  }
  if (msg.error) toast(String(msg.error), true);
}

function toast(text, isError) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

/* ---------------- rendering ---------------- */

function renderAll() {
  renderProfile();
  renderUsers();
  renderGroups();
  if (state.pendingOpenKey) {
    const key = state.pendingOpenKey;
    state.pendingOpenKey = null;
    if (state.snapshot.chats[key]) openChat(key);
  }
  if (state.openKey) renderChat();
}

function renderProfile() {
  const p = state.snapshot.profile;
  $("profileName").textContent = p.name;
  $("profileSub").textContent = `@${p.username}`;
  $("profileAvatar").textContent = (p.name || "W").slice(0, 1).toUpperCase();
}

function findDirectByUser(userId) {
  const d = state.snapshot.directs.find((x) => x.userId === userId);
  return d ? d.key : null;
}

function findChatSummary(key) {
  const snap = state.snapshot;
  if (key.startsWith("direct:")) {
    const d = snap.directs.find((x) => x.key === key);
    if (!d) return null;
    return {
      key,
      title: d.name,
      sub: d.online ? "在线" : "离线",
      kind: "direct",
      direct: d,
    };
  }
  const group = snap.groups.find((g) => g.groupId === key);
  if (!group) return null;
  return {
    key,
    title: group.name,
    sub: `${group.members.length} 人 · 群主 ${group.creatorName}`,
    kind: "group",
    group,
  };
}

function renderUsers() {
  const list = $("userList");
  list.innerHTML = "";
  const me = state.snapshot.profile.userId;
  // existing direct conversations first (with preview + delete)
  for (const d of state.snapshot.directs) {
    const msgs = (state.snapshot.chats[d.key] || { messages: [] }).messages;
    const last = msgs[msgs.length - 1];
    const row = document.createElement("div");
    row.className = "rowItem" + (d.key === state.openKey ? " active" : "");
    row.innerHTML = `
      <div class="dot ${d.online ? "on" : ""}"></div>
      <div class="avatar">${esc((d.name || "?").slice(0, 1).toUpperCase())}</div>
      <div class="meta">
        <div class="title"><span class="name">${esc(d.name)}</span></div>
        <div class="last">${last ? esc(previewOf(last)) : "在线"}</div>
      </div>
      <div class="actions"><button class="ghost danger" data-act="rm" title="删除会话">✕</button></div>`;
    row.onclick = (ev) => {
      if (ev.target.closest("[data-act=rm]")) return;
      openChat(d.key);
    };
    row.querySelector('[data-act="rm"]').onclick = () => {
      if (confirm(`删除与 ${d.name} 的会话？双方的历史记录都会移除。`)) {
        send({ action: "removeDirect", chatKey: d.key });
        if (state.openKey === d.key) closeChat();
      }
    };
    list.appendChild(row);
  }
  // then users without a conversation yet
  for (const u of state.snapshot.users) {
    if (u.id === me) continue;
    if (findDirectByUser(u.id)) continue;
    const row = document.createElement("div");
    row.className = "rowItem";
    row.innerHTML = `
      <div class="dot ${u.online ? "on" : ""}"></div>
      <div class="avatar">${esc((u.name || "?").slice(0, 1).toUpperCase())}</div>
      <div class="meta">
        <div class="title"><span class="name">${esc(u.name)}</span></div>
        <div class="last">@${esc(u.username)}</div>
      </div>`;
    row.onclick = () => openDirectWith(u);
    list.appendChild(row);
  }
}

async function openDirectWith(user) {
  const existing = findDirectByUser(user.id);
  if (existing) {
    openChat(existing);
    return;
  }
  const result = await request({ action: "startDirect", userId: user.id });
  if (result && result.chatKey) {
    state.pendingOpenKey = result.chatKey;
    send({ action: "getSnapshot" });
  }
}

function previewOf(m) {
  if (m.fileInfo) {
    const kindNames = { image: "图片", video: "视频", audio: "语音", file: "文件" };
    return `[${kindNames[m.fileInfo.kind] || "文件"}] ${m.fileInfo.fileName}`;
  }
  return m.content;
}

function renderGroups() {
  const list = $("groupList");
  list.innerHTML = "";
  for (const g of state.snapshot.groups) {
    const key = g.groupId;
    const msgs = (state.snapshot.chats[key] || { messages: [] }).messages;
    const last = msgs[msgs.length - 1];
    const row = document.createElement("div");
    row.className = "rowItem" + (key === state.openKey ? " active" : "");
    row.innerHTML = `
      <div class="avatar" style="background:#7a59d5">${esc((g.name || "G").slice(0, 1))}</div>
      <div class="meta">
        <div class="title"><span class="name">${esc(g.name)}</span></div>
        <div class="last">${last ? esc(previewOf(last)) : `${g.members.length} 人`}</div>
      </div>`;
    row.onclick = () => openChat(key);
    list.appendChild(row);
  }
}

function openChat(key) {
  state.openKey = key;
  send({ action: "openChat", chatKey: key });
  $("emptyHint").classList.add("hidden");
  $("chatView").classList.remove("hidden");
  renderChat();
  renderUsers();
  renderGroups();
  $("input").focus();
}

function closeChat() {
  state.openKey = null;
  $("chatView").classList.add("hidden");
  $("emptyHint").classList.remove("hidden");
}

function renderChat() {
  const summary = findChatSummary(state.openKey);
  if (!summary) {
    closeChat();
    return;
  }
  $("chatTitle").textContent = summary.title;
  $("chatSub").textContent = summary.sub;
  const settingsBtn = $("btnGroupSettings");
  if (summary.kind === "group") {
    settingsBtn.classList.remove("hidden");
  } else {
    settingsBtn.classList.add("hidden");
  }
  renderPinBanner(summary);
  renderMessages(summary);
  renderTyping();
}

function renderPinBanner(summary) {
  const banner = $("pinBanner");
  const chat = state.snapshot.chats[summary.key];
  const pinned = chat ? chat.messages.filter((m) => m.pinned) : [];
  const latest = pinned[pinned.length - 1];
  if (!latest) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  banner.innerHTML = `<span>📌 ${esc(latest.senderName)}：${esc(previewOf(latest))}</span>`;
}

function isBigEmoji(content) {
  const text = String(content || "").trim();
  if (!text) return false;
  const cps = Array.from(text);
  if (cps.length > 16) return false;
  const isEmojiChar = (cp) =>
    cp >= 0x1f000 ||
    (cp >= 0x2600 && cp <= 0x27bf) ||
    (cp >= 0x2b00 && cp <= 0x2bff) ||
    [0xfe0f, 0x200d, 0x20e3, 0x2764, 0xa9, 0xae, 0x2122].includes(cp);
  if (!cps.every((ch) => isEmojiChar(ch.codePointAt(0)))) return false;
  return cps.some((ch) => {
    const cp = ch.codePointAt(0);
    return (
      cp >= 0x1f000 ||
      (cp >= 0x2600 && cp <= 0x27bf) ||
      (cp >= 0x2b00 && cp <= 0x2bff) ||
      [0x2764, 0xa9, 0xae, 0x2122].includes(cp)
    );
  });
}

function fmtSize(bytes) {
  if (bytes >= 1024 * 1024 * 1024) return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
  return bytes + " B";
}

function fmtTime(ts) {
  const d = new Date(ts);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

function renderMessages(summary) {
  const container = $("messages");
  const nearBottom =
    container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  container.innerHTML = "";
  const chat = state.snapshot.chats[summary.key] || { messages: [] };
  const myId = state.snapshot.profile.userId;
  for (const m of chat.messages) {
    const mine = m.senderId === myId;
    const row = document.createElement("div");
    row.className = "msgRow" + (mine ? " mine" : "");
    const avatar = document.createElement("div");
    avatar.className = "msgAvatar";
    avatar.textContent = (m.senderName || "?").slice(0, 1).toUpperCase();

    const col = document.createElement("div");
    col.className = "msgCol";
    if (!mine) {
      const sender = document.createElement("div");
      sender.className = "msgSender";
      sender.textContent = m.senderName;
      col.appendChild(sender);
    }

    if (m.fileInfo) {
      col.appendChild(buildFileBubble(m));
    } else if (isBigEmoji(m.content)) {
      const bubble = document.createElement("div");
      bubble.className = "bubble bigEmoji";
      bubble.textContent = m.content;
      col.appendChild(bubble);
    } else {
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = m.content;
      if (m.edited) {
        const tag = document.createElement("span");
        tag.className = "editedTag";
        tag.textContent = "已编辑";
        bubble.appendChild(tag);
      }
      col.appendChild(bubble);
    }

    const reactions = Object.entries(m.reactions || {});
    if (reactions.length) {
      const rrow = document.createElement("div");
      rrow.className = "reactionsRow";
      for (const [emoji, senders] of reactions) {
        const pill = document.createElement("span");
        pill.className = "reactionPill" + (senders.includes(myId) ? " mine" : "");
        pill.textContent = `${emoji} ${senders.length}`;
        pill.onclick = () =>
          send({
            action: "react",
            chatKey: summary.key,
            messageId: m.id,
            emoji,
            active: !senders.includes(myId),
          });
        rrow.appendChild(pill);
      }
      col.appendChild(rrow);
    }

    const stateLine = document.createElement("div");
    stateLine.className = "msgState";
    const bits = [fmtTime(m.timestamp)];
    if (mine && summary.kind === "direct" && m.read) bits.push("✓✓ 已读");
    if (mine && summary.kind === "group" && (m.readers || []).length) {
      const others = summary.group.members.length - 1;
      bits.push(`已读 ${m.readers.length}/${others}`);
    }
    stateLine.textContent = bits.join(" · ");
    col.appendChild(stateLine);
    row.appendChild(avatar);
    row.appendChild(col);
    attachBubbleMenu(row.querySelector(".bubble"), m, summary);
    container.appendChild(row);
  }
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

function fileUrl(fileId) {
  const base = `/files/${encodeURIComponent(fileId)}`;
  // a switched account authenticates by token, not by cookie
  return state.auth.token ? `${base}?token=${encodeURIComponent(state.auth.token)}` : base;
}

function buildFileBubble(m) {
  const info = m.fileInfo;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const fileUrl_ = fileUrl(info.fileId);
  const mine = m.senderId === state.snapshot.profile.userId;
  if (info.kind === "image") {
    bubble.classList.add("imageBubble");
    const img = document.createElement("img");
    img.src = fileUrl_;
    img.alt = info.fileName;
    img.onclick = () => window.open(fileUrl_, "_blank");
    bubble.appendChild(img);
  } else {
    const card = document.createElement("div");
    card.className = "fileCard";
    const icon = { image: "🖼", video: "🎬", audio: "🎵" }[info.kind] || "📄";
    card.innerHTML = `
      <div class="fileIcon">${icon}</div>
      <div class="fileMeta">
        <div class="fileName">${esc(info.fileName)}</div>
        <div class="fileSize">${fmtSize(info.fileSize)}</div>
      </div>
      ${mine ? '<span class="dim">已发送</span>' : `<a href="${esc(fileUrl_)}" download="${esc(info.fileName)}">保存</a>`}`;
    bubble.appendChild(card);
  }
  return bubble;
}

function renderTyping() {
  const bar = $("typingBar");
  const name = state.openKey ? state.typingShown[state.openKey] : null;
  if (name) {
    bar.textContent = `${name} 正在输入…`;
    bar.classList.remove("hidden");
  } else {
    bar.classList.add("hidden");
  }
}

function attachBubbleMenu(bubble, m, summary) {
  if (!bubble) return;
  bubble.oncontextmenu = (ev) => {
    ev.preventDefault();
    showBubbleMenu(ev.clientX, ev.clientY, m, summary);
  };
  bubble.ondblclick = (ev) => showBubbleMenu(ev.clientX, ev.clientY, m, summary);
}

function showBubbleMenu(x, y, m, summary) {
  const existing = document.querySelector(".msgMenu");
  if (existing) existing.remove();
  const myId = state.snapshot.profile.userId;
  const menu = document.createElement("div");
  menu.className = "msgMenu";
  const addItem = (label, fn) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.onclick = () => {
      menu.remove();
      fn();
    };
    menu.appendChild(btn);
  };
  if (!m.fileInfo) {
    for (const emoji of state.emojiQuick) {
      const senders = (m.reactions || {})[emoji] || [];
      addItem(`${emoji} ${senders.length ? "取消回应" : "回应"}`, () =>
        send({
          action: "react",
          chatKey: summary.key,
          messageId: m.id,
          emoji,
          active: !senders.length,
        })
      );
    }
  }
  addItem(m.pinned ? "取消置顶" : "置顶", () =>
    send({ action: "pin", chatKey: summary.key, messageId: m.id, active: !m.pinned })
  );
  if (m.senderId === myId) {
    if (!m.fileInfo) {
      addItem("编辑", () => editMessageDialog(summary, m));
    }
    addItem("删除", () =>
      send({ action: "deleteMessage", chatKey: summary.key, messageId: m.id })
    );
  }
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - rect.width - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - rect.height - 8) + "px";
  setTimeout(() => {
    document.addEventListener("click", function onClose(ev) {
      if (!menu.contains(ev.target)) {
        menu.remove();
        document.removeEventListener("click", onClose);
      }
    });
  }, 0);
}

/* ---------------- composer ---------------- */

function setupComposer() {
  const input = $("input");
  const sendNow = () => {
    const content = input.value.replace(/\s+$/, "");
    if (!content || !state.openKey) return;
    if (Array.from(content).length > MAX_CONTENT_LENGTH) {
      toast("消息过长（上限 5000 字）", true);
      return;
    }
    send({ action: "sendChat", chatKey: state.openKey, content });
    input.value = "";
    input.style.height = "auto";
  };
  $("btnSend").onclick = sendNow;
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      sendNow();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(140, input.scrollHeight) + "px";
    if (state.openKey) {
      send({ action: "sendTyping", chatKey: state.openKey, active: true });
      clearTimeout(state.typingTimers["__me"]);
      state.typingTimers["__me"] = setTimeout(() => {
        if (state.openKey) {
          send({ action: "sendTyping", chatKey: state.openKey, active: false });
        }
      }, 2000);
    }
  });
  $("btnAttach").onclick = () => {
    if (!state.openKey) return;
    $("fileInput").click();
  };
  $("fileInput").onchange = async () => {
    const file = $("fileInput").files[0];
    const chatKey = state.openKey;
    $("fileInput").value = "";
    if (!file || !chatKey) return;
    if (file.size > 512 * 1024 * 1024) {
      toast("文件过大（超过 512 MB）", true);
      return;
    }
    toast(`正在上传 ${file.name}…`);
    try {
      const resp = await apiFetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST",
        body: file,
      });
      const doc = await resp.json();
      if (!resp.ok || !doc.uploadId) {
        toast("上传失败", true);
        return;
      }
      // send to the chat that was open when the file was picked, not the
      // (possibly) switched-to chat when the upload finishes
      send({ action: "sendFile", chatKey, uploadId: doc.uploadId });
    } catch (e) {
      toast("上传失败", true);
    }
  };
}

/* ---------------- dialogs ---------------- */

function showModal(html) {
  $("modalCard").innerHTML = html;
  $("modal").classList.remove("hidden");
}

function closeModal() {
  $("modal").classList.add("hidden");
  $("modalCard").innerHTML = "";
}

function dialogShell(title, bodyHtml, actionsHtml) {
  showModal(`
    <h3>${esc(title)}</h3>
    ${bodyHtml}
    <div class="modalActions">${actionsHtml}
      <button data-act="cancel">关闭</button>
    </div>
    <div class="formError" id="formError"></div>`);
  $("modalCard").querySelector('[data-act="cancel"]').onclick = closeModal;
}

function createGroupDialog() {
  const others = state.snapshot.users.filter((u) => u.id !== state.snapshot.profile.userId);
  let membersHtml = "";
  for (const u of others) {
    membersHtml += `
      <label class="checkRow"><input type="checkbox" value="${esc(u.username)}"> ${esc(u.name)} <span class="dim">@${esc(u.username)}</span></label>`;
  }
  dialogShell(
    "创建群组",
    `<label>群名</label>
     <input type="text" id="fGName" placeholder="例如 家庭群">
     ${others.length ? `<label>邀请成员</label><div>${membersHtml}</div>` : '<p class="dim">服务器上还没有其他用户</p>'}`,
    `<button class="primary" data-act="ok">创建</button>`
  );
  $("modalCard").querySelector('[data-act="ok"]').onclick = async () => {
    const name = $("fGName").value.trim();
    if (!name) return;
    const members = Array.from($("modalCard").querySelectorAll("input[type=checkbox]:checked")).map(
      (el) => el.value
    );
    const result = await request({ action: "createGroup", name, members });
    if (result && result.groupId) {
      closeModal();
      toast(`群组「${result.name}」已创建`);
      state.pendingOpenKey = result.groupId;
      send({ action: "getSnapshot" });
    }
  };
}

function editMessageDialog(summary, m) {
  dialogShell(
    "编辑消息",
    `<textarea id="fEdit" style="width:100%;height:110px;border:1px solid var(--border);border-radius:8px;padding:8px;font:inherit">${esc(m.content)}</textarea>`,
    `<button class="primary" data-act="ok">保存</button>`
  );
  $("modalCard").querySelector('[data-act="ok"]').onclick = () => {
    const content = $("fEdit").value.trim();
    if (!content) return;
    send({ action: "editMessage", chatKey: summary.key, messageId: m.id, content });
    closeModal();
  };
}

function groupSettingsDialog() {
  const summary = findChatSummary(state.openKey);
  if (!summary || summary.kind !== "group") return;
  const g = summary.group;
  const amOwner = g.creatorId === state.snapshot.profile.userId;
  let membersHtml = "";
  for (const mem of g.members) {
    membersHtml += `
      <div class="memberRow">
        <div class="avatar">${esc((mem.name || "?").slice(0, 1).toUpperCase())}</div>
        <div class="flex">
          <div>${esc(mem.name)} ${mem.id === state.snapshot.profile.userId ? '<span class="dim">（我）</span>' : ""}</div>
          <div class="fp">@${esc(mem.username || "")}</div>
        </div>
        <div class="dot ${mem.online ? "on" : ""}"></div>
        ${amOwner && mem.id !== state.snapshot.profile.userId ? `<button class="danger" data-kick="${esc(mem.id)}">移出</button>` : ""}
      </div>`;
  }
  const candidates = state.snapshot.users.filter(
    (u) => u.id !== state.snapshot.profile.userId && !g.members.some((m) => m.id === u.id)
  );
  let inviteHtml = "";
  if (candidates.length) {
    inviteHtml = `
      <label>邀请成员</label>
      <select id="fInvite" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:8px;font:inherit">
        ${candidates.map((u) => `<option value="${esc(u.username)}">${esc(u.name)} (@${esc(u.username)})</option>`).join("")}
      </select>
      <button data-act="invite" style="margin-top:8px">邀请</button>`;
  }
  dialogShell(
    `群设置 · ${g.name}`,
    `
    ${amOwner ? `
      <label>群名</label>
      <input type="text" id="fGroupName" value="${esc(g.name)}">
      <label>群公告</label>
      <input type="text" id="fAnnouncement" value="${esc(g.announcement || "")}">
      <button class="primary" data-act="save" style="margin-top:10px">保存群信息</button>
      <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">` : ""}
    <label>成员（${g.members.length}）· 群主 ${esc(g.creatorName)}</label>
    <div>${membersHtml}</div>
    ${inviteHtml}`,
    `<button class="danger" data-act="leave">${amOwner ? "解散群组" : "退出群组"}</button>`
  );
  const card = $("modalCard");
  const saveBtn = card.querySelector('[data-act="save"]');
  if (saveBtn) {
    saveBtn.onclick = () => {
      send({
        action: "groupUpdate",
        groupId: g.groupId,
        name: $("fGroupName").value.trim(),
        announcement: $("fAnnouncement").value,
      });
      toast("已保存");
    };
  }
  const inviteBtn = card.querySelector('[data-act="invite"]');
  if (inviteBtn) {
    inviteBtn.onclick = () => {
      send({ action: "inviteMember", groupId: g.groupId, username: $("fInvite").value });
      toast("已邀请");
      closeModal();
    };
  }
  card.querySelectorAll("[data-kick]").forEach((btn) => {
    btn.onclick = () => {
      if (confirm("确认将该成员移出群组？")) {
        send({ action: "kickMember", groupId: g.groupId, targetId: btn.getAttribute("data-kick") });
        closeModal();
      }
    };
  });
  card.querySelector('[data-act="leave"]').onclick = () => {
    const text = amOwner
      ? "群主退出将解散群组，所有人的聊天记录都会删除，确认？"
      : "退出后需要重新被邀请，确认？";
    if (confirm(text)) {
      send({ action: "leaveGroup", groupId: g.groupId });
      closeModal();
      closeChat();
    }
  };
}

/* ---------------- auth ---------------- */

async function whoami(token) {
  try {
    const opts = token ? { headers: { "X-LC-Token": token } } : {};
    const resp = await fetch("/api/whoami", opts);
    return await resp.json();
  } catch (e) {
    return { authenticated: false, firstRun: false };
  }
}

function enterWith(username, token) {
  state.auth.username = username;
  state.auth.token = token;
  setActiveUsername(username);
  enterApp();
}

// Resolve the active account for this browser: the cookie session wins unless
// the user last switched to a locally remembered account whose token is valid.
async function boot() {
  state.auth.accounts = loadStoredAccounts();
  let who = await whoami();
  let token = null;
  const preferred = getActiveUsername();
  if (preferred && preferred !== who.username) {
    const entry = state.auth.accounts.find((a) => a.username === preferred);
    if (entry) {
      const switched = await whoami(entry.token);
      if (switched.authenticated) {
        who = switched;
        token = entry.token;
      }
    }
  }
  if (!who.authenticated) {
    for (const entry of state.auth.accounts) {
      const remembered = await whoami(entry.token);
      if (remembered.authenticated) {
        who = remembered;
        token = entry.token;
        break;
      }
    }
  }
  if (who.authenticated) {
    enterWith(who.username, token);
  } else {
    showAuthView(who.firstRun ? "firstRun" : "login");
  }
}

function showAuthView(mode) {
  $("app").classList.add("hidden");
  $("authView").classList.remove("hidden");
  const firstRun = mode === "firstRun";
  const registerMode = mode === "register";
  $("authTitle").textContent = firstRun
    ? "初始化服务器"
    : registerMode
      ? "注册新账号"
      : "登录 LocalChat Web";
  $("authHint").textContent = firstRun
    ? "还没有任何账号，先创建第一个账号"
    : registerMode
      ? "注册一个新账号，注册后直接进入"
      : "多用户服务器聊天：登录后与服务器上的其他用户聊天";
  $("userLabel").classList.remove("hidden");
  $("authUser").classList.remove("hidden");
  $("passLabel").classList.remove("hidden");
  $("authPass").classList.remove("hidden");
  const confirmPass = firstRun || registerMode;
  $("pass2Label").classList.toggle("hidden", !confirmPass);
  $("authPass2").classList.toggle("hidden", !confirmPass);
  $("authSubmit").classList.remove("hidden");
  $("authSubmit").textContent = firstRun ? "创建账号并进入" : registerMode ? "注册" : "登录";
  $("authToggle").classList.toggle("hidden", firstRun);
  $("authToggle").textContent = registerMode ? "已有账号？返回登录" : "没有账号？注册新账号";
  $("authToggle").onclick = () => showAuthView(registerMode ? "login" : "register");
  $("authError").textContent = "";
  $("authSubmit").onclick = async () => {
    const username = $("authUser").value.trim();
    const password = $("authPass").value;
    const body = { username, password };
    if (confirmPass) {
      if (password !== $("authPass2").value) {
        $("authError").textContent = "两次密码不一致";
        return;
      }
    }
    try {
      const resp = await fetch(firstRun || registerMode ? "/api/register" : "/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const doc = await resp.json();
      if (!resp.ok || !doc.ok) {
        $("authError").textContent = doc.message || "操作失败";
        return;
      }
      upsertAccount(doc.username, doc.token);
      enterWith(doc.username, doc.token);
    } catch (e) {
      $("authError").textContent = "网络错误";
    }
  };
}

function enterApp() {
  $("authView").classList.add("hidden");
  $("app").classList.remove("hidden");
  connect();
}

async function logout() {
  try {
    await apiFetch("/api/logout", { method: "POST" });
  } catch (e) {
    /* ignore */
  }
  state.auth.accounts = state.auth.accounts.filter((a) => a.username !== state.auth.username);
  saveStoredAccounts(state.auth.accounts);
  setActiveUsername(null);
  state.auth.token = null;
  state.auth.username = null;
  location.reload();
}

async function switchAccount(username) {
  setActiveUsername(username);
  location.reload();
}

async function forgetAccount(username) {
  const entry = state.auth.accounts.find((a) => a.username === username);
  if (entry && username !== state.auth.username) {
    // drop that account's server session without touching our own cookie
    try {
      await fetch("/api/logout", {
        method: "POST",
        headers: { "X-LC-Token": entry.token },
      });
    } catch (e) {
      /* ignore */
    }
  }
  state.auth.accounts = state.auth.accounts.filter((a) => a.username !== username);
  saveStoredAccounts(state.auth.accounts);
  if (username === state.auth.username) {
    await logout();
    return;
  }
  profileDialog();
}

function profileDialog() {
  const p = state.snapshot.profile;
  const accountsHtml = state.auth.accounts
    .map((a) => {
      const current = a.username === state.auth.username;
      return `
      <div class="memberRow">
        <div class="avatar">${esc((a.username || "?").slice(0, 1).toUpperCase())}</div>
        <div class="flex"><div>${esc(a.username)}${current ? ' <span class="dim">（当前）</span>' : ""}</div></div>
        ${current ? "" : `<button data-switch="${esc(a.username)}">切换</button>`}
        <button class="danger" data-forget="${esc(a.username)}">移除</button>
      </div>`;
    })
    .join("");
  dialogShell(
    "账号与设置",
    `<label>昵称</label>
     <input type="text" id="fNick" value="${esc(p.name)}">
     <label>账号</label>
     <input type="text" value="${esc(p.username || "")}" disabled>
     <label>本浏览器保存的账号（${state.auth.accounts.length}）</label>
     <div>${accountsHtml || '<p class="dim">无</p>'}</div>
     <div class="accountActions">
       <button data-act="addAccount">＋新建账号</button>
       <button class="danger" data-act="logout">退出登录</button>
     </div>`,
    `<button class="primary" data-act="ok">保存昵称</button>`
  );
  const card = $("modalCard");
  card.querySelector('[data-act="ok"]').onclick = () => {
    const name = $("fNick").value.trim();
    if (name) send({ action: "setNickname", name });
    closeModal();
  };
  card.querySelector('[data-act="logout"]').onclick = logout;
  card.querySelector('[data-act="addAccount"]').onclick = () => {
    addAccountDialog();
  };
  card.querySelectorAll("[data-switch]").forEach((btn) => {
    btn.onclick = () => switchAccount(btn.getAttribute("data-switch"));
  });
  card.querySelectorAll("[data-forget]").forEach((btn) => {
    btn.onclick = () => forgetAccount(btn.getAttribute("data-forget"));
  });
}

function addAccountDialog() {
  dialogShell(
    "新建账号",
    `<p class="dim" style="font-size:12px">在同一台机器上可以注册多个账号；新建不会退出当前账号，创建后可在「账号与设置」里一键切换。</p>
     <label>用户名</label>
     <input type="text" id="fNewUser">
     <label>密码（至少 6 位）</label>
     <input type="password" id="fNewPass">`,
    `<button class="primary" data-act="ok">创建</button>`
  );
  $("modalCard").querySelector('[data-act="ok"]').onclick = async () => {
    try {
      const resp = await apiFetch("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: $("fNewUser").value.trim(),
          password: $("fNewPass").value,
        }),
      });
      const doc = await resp.json();
      if (!resp.ok || !doc.ok) {
        $("formError").textContent = doc.message || "创建失败";
        return;
      }
      upsertAccount(doc.username, doc.token);
      closeModal();
      toast(`账号 ${doc.username} 已创建，当前登录未变，可随时切换`);
    } catch (e) {
      $("formError").textContent = "网络错误";
    }
  };
}

/* ---------------- boot ---------------- */

function setupChrome() {
  $("btnCreateGroup").onclick = createGroupDialog;
  $("profile").onclick = profileDialog;
  $("btnGroupSettings").onclick = groupSettingsDialog;
  $("modal").onclick = (ev) => {
    if (ev.target === $("modal")) closeModal();
  };
  for (const id of ["authUser", "authPass", "authPass2"]) {
    $(id).addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !$("authView").classList.contains("hidden")) {
        $("authSubmit").click();
      }
    });
  }
  setupComposer();
}

setupChrome();
boot();
