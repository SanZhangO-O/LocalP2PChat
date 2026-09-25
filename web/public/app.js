"use strict";

const MAX_CONTENT_LENGTH = 5000;

const state = {
  ws: null,
  snapshot: null,
  openKey: null,
  typingTimers: {},
  typingShown: {},
  reqCounter: 1,
  pendingReplies: {},
  emojiQuick: ["\uD83D\uDC4D", "\u2764\uFE0F", "\uD83D\uDE02", "\uD83D\uDE2E", "\uD83D\uDE22", "\uD83C\uDF89"],
};

function $(id) {
  return document.getElementById(id);
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/`);
  state.ws = ws;
  ws.onopen = () => {
    send({ action: "getSnapshot" });
  };
  ws.onclose = () => {
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
  if (msg.queryResult || msg.joinResult || msg.createdGroup) {
    const payload = msg.queryResult || msg.joinResult || msg.createdGroup;
    const reqId = payload.reqId;
    if (reqId && state.pendingReplies[reqId]) {
      const resolve = state.pendingReplies[reqId];
      delete state.pendingReplies[reqId];
      resolve(payload);
    }
  }
  if (msg.typing) {
    const { chatKey, senderId, active } = msg.typing;
    if (chatKey === state.openKey && senderId !== state.snapshot?.profile?.deviceId) {
      if (active) {
        state.typingShown[chatKey] = true;
        clearTimeout(state.typingTimers[chatKey]);
        state.typingTimers[chatKey] = setTimeout(() => {
          state.typingShown[chatKey] = false;
          renderTyping();
        }, 5000);
      } else {
        state.typingShown[chatKey] = false;
        clearTimeout(state.typingTimers[chatKey]);
      }
      renderTyping();
    }
  }
  if (msg.fileProgress && msg.fileProgress.chatKey === state.openKey) {
    updateProgress(msg.fileProgress);
  }
  if (msg.fileDone) {
    send({ action: "getSnapshot" });
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
  renderRequests();
  renderContacts();
  renderGroups();
  if (state.openKey) renderChat();
}

function renderProfile() {
  const p = state.snapshot.profile;
  $("profileName").textContent = p.name;
  $("profileFp").textContent = `\u5b89\u5168\u7801 ${p.fingerprint.slice(0, 4)} ${p.fingerprint.slice(4)}`;
  $("profileAvatar").textContent = (p.name || "W").slice(0, 1).toUpperCase();
}

function findChatSummary(key) {
  const snap = state.snapshot;
  if (key.startsWith("direct:")) {
    const peerId = key.slice(7);
    const contact = snap.contacts.find((c) => c.id === peerId);
    if (!contact) return null;
    return {
      key,
      title: contact.name,
      alive: contact.alive,
      sub: contact.alive ? "\u5728\u7ebf" : "\u79bb\u7ebf",
      kind: "direct",
      contact,
    };
  }
  const group = snap.groups.find((g) => g.groupId === key);
  if (!group) return null;
  return {
    key,
    title: group.name,
    alive: group.alive,
    sub: group.isHost
      ? `\u7fa4\u4e3b \u00b7 ${group.memberCount} \u4eba \u00b7 \u53f7\u7801 ${group.joinId.slice(0, 4)} ${group.joinId.slice(4)}`
      : group.alive
        ? `${group.memberCount} \u4eba\u5728\u7ebf`
        : "\u672a\u8fde\u63a5",
    kind: "group",
    group,
  };
}

function renderRequests() {
  const list = $("requestList");
  list.innerHTML = "";
  const reqs = state.snapshot.requests || [];
  const badge = $("requestBadge");
  if (reqs.length) {
    badge.textContent = String(reqs.length);
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
  for (const r of reqs) {
    const card = document.createElement("div");
    card.className = "requestCard";
    card.innerHTML = `
      <div><b>${esc(r.name)}</b> <span class="dim">${esc(r.ip)}:${r.port}</span></div>
      ${r.peerFingerprint ? `<div class="fp">\u5b89\u5168\u7801 ${r.peerFingerprint}</div>` : ""}
      ${r.fromRemoved ? `<div class="dim">\u5df2\u79fb\u9664\u7684\u6210\u5458</div>` : ""}
      <div class="actions">
        <button class="primary" data-act="accept">\u63a5\u53d7</button>
        <button data-act="ignore">\u5ffd\u7565</button>
      </div>`;
    card.querySelector('[data-act="accept"]').onclick = () =>
      send({ action: "acceptRequest", id: r.id });
    card.querySelector('[data-act="ignore"]').onclick = () =>
      send({ action: "ignoreRequest", id: r.id });
    list.appendChild(card);
  }
}

function renderContacts() {
  const list = $("contactList");
  list.innerHTML = "";
  for (const c of state.snapshot.contacts) {
    const key = "direct:" + c.id;
    const msgs = (state.snapshot.chats[key] || { messages: [] }).messages;
    const last = msgs[msgs.length - 1];
    const row = document.createElement("div");
    row.className = "rowItem" + (key === state.openKey ? " active" : "");
    row.innerHTML = `
      <div class="dot ${c.alive ? "on" : ""}"></div>
      <div class="avatar">${esc((c.name || "?").slice(0, 1).toUpperCase())}</div>
      <div class="meta">
        <div class="title"><span class="name">${esc(c.name)}</span></div>
        <div class="last">${last ? esc(previewOf(last)) : ""}</div>
      </div>
      <div class="actions"><button class="ghost danger" data-act="rm" title="\u5220\u9664\u6210\u5458">\u2715</button></div>`;
    row.onclick = (ev) => {
      if (ev.target.closest("[data-act=rm]")) return;
      openChat(key);
    };
    row.querySelector('[data-act="rm"]').onclick = () => {
      if (confirm(`\u5220\u9664\u6210\u5458 ${c.name}\uff1f`)) {
        send({ action: "removeContact", peerId: c.id });
        if (state.openKey === key) closeChat();
      }
    };
    list.appendChild(row);
  }
}

function previewOf(m) {
  if (m.fileInfo) {
    const kindNames = { image: "\u56fe\u7247", video: "\u89c6\u9891", audio: "\u8bed\u97f3", file: "\u6587\u4ef6" };
    return `[${kindNames[m.fileInfo.kind] || "\u6587\u4ef6"}] ${m.fileInfo.fileName}`;
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
      <div class="dot ${g.alive ? "on" : ""}"></div>
      <div class="avatar" style="background:#7a59d5">${esc((g.name || "G").slice(0, 1))}</div>
      <div class="meta">
        <div class="title"><span class="name">${esc(g.name)}</span></div>
        <div class="last">${last ? esc(previewOf(last)) : g.alive ? `${g.memberCount} \u4eba` : "\u672a\u8fde\u63a5"}</div>
      </div>
      <div class="actions">${!g.isHost && !g.alive ? `<button class="ghost" data-act="rejoin" title="\u91cd\u65b0\u8fde\u63a5">\u21bb</button>` : ""}</div>`;
    row.onclick = (ev) => {
      if (ev.target.closest("[data-act=rejoin]")) return;
      openChat(key);
    };
    const rejoinBtn = row.querySelector('[data-act="rejoin"]');
    if (rejoinBtn) {
      rejoinBtn.onclick = async () => {
        toast("\u6b63\u5728\u91cd\u65b0\u8fde\u63a5…");
        await request({ action: "rejoinGroup", groupId: g.groupId });
      };
    }
    list.appendChild(row);
  }
}

function openChat(key) {
  state.openKey = key;
  send({ action: "openChat", chatKey: key });
  $("emptyHint").classList.add("hidden");
  $("chatView").classList.remove("hidden");
  renderChat();
  renderContacts();
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
  const group = summary.group;
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
  banner.innerHTML = `<span>\uD83D\uDCCC ${esc(latest.senderName)}\uff1a${esc(previewOf(latest))}</span>`;
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
  const myId = state.snapshot.profile.deviceId;
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
      col.appendChild(buildFileBubble(m, summary));
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
        tag.textContent = "\u5df2\u7f16\u8f91";
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
    if (m.pending) bits.push("\u23f3 \u5f85\u9001\u8fbe");
    if (mine && summary.kind === "direct" && m.read) bits.push("\u2713\u2713 \u5df2\u8bfb");
    if (mine && summary.kind === "group" && (m.readers || []).length) {
      const others = summary.group.memberCount - 1;
      bits.push(`\u5df2\u8bfb ${m.readers.length}/${others}`);
    }
    stateLine.textContent = bits.join(" \u00b7 ");
    col.appendChild(stateLine);
    row.appendChild(avatar);
    row.appendChild(col);
    attachBubbleMenu(row.querySelector(".bubble"), m, summary);
    container.appendChild(row);
  }
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

function buildFileBubble(m, summary) {
  const info = m.fileInfo;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (info.kind === "image" && info.state === "done") {
    bubble.classList.add("imageBubble");
    const img = document.createElement("img");
    img.src = `/files/${info.fileId}`;
    img.alt = info.fileName;
    img.onclick = () => window.open(`/files/${info.fileId}`, "_blank");
    bubble.appendChild(img);
  } else {
    const card = document.createElement("div");
    card.className = "fileCard";
    const icon = { image: "\uD83D\uDDBC", video: "\uD83C\uDFAC", audio: "\uD83C\uDFB5" }[info.kind] || "\uD83D\uDCCE";
    let actionHtml;
    if (m.senderId === state.snapshot.profile.deviceId) {
      actionHtml = `<span class="dim">\u5df2\u53d1\u9001</span>`;
    } else if (info.state === "done") {
      actionHtml = `<a href="/files/${info.fileId}" download="${esc(info.fileName)}">\u4fdd\u5b58</a>`;
    } else if (info.state && info.state.downloading) {
      const pct = info.state.total ? Math.min(100, Math.round((info.state.received / info.state.total) * 100)) : 0;
      actionHtml = `
        <div class="fileMeta">
          <div class="dim">\u4e0b\u8f7d\u4e2d ${pct}%</div>
          <div class="progressOuter"><div class="progressBar" style="width:${pct}%"></div></div>
        </div>`;
    } else {
      actionHtml = `<button class="primary" data-act="dl">\u4e0b\u8f7d</button>`;
    }
    card.innerHTML = `
      <div class="fileIcon">${icon}</div>
      <div class="fileMeta">
        <div class="fileName">${esc(info.fileName)}</div>
        <div class="fileSize">${fmtSize(info.fileSize)}</div>
      </div>
      ${actionHtml}`;
    const dlBtn = card.querySelector('[data-act="dl"]');
    if (dlBtn) {
      dlBtn.onclick = () =>
        send({ action: "downloadFile", chatKey: summary.key, messageId: m.id });
    }
    bubble.appendChild(card);
  }
  return bubble;
}

let lastProgressSnapshot = 0;
function updateProgress(prog) {
  const now = Date.now();
  if (now - lastProgressSnapshot > 600) {
    lastProgressSnapshot = now;
    send({ action: "getSnapshot" });
  }
}

function renderTyping() {
  const bar = $("typingBar");
  if (state.openKey && state.typingShown[state.openKey]) {
    bar.textContent = "\u5bf9\u65b9\u6b63\u5728\u8f93\u5165…";
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
  const myId = state.snapshot.profile.deviceId;
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
      addItem(`${emoji} ${senders.length ? "\u53d6\u6d88\u56de\u5e94" : "\u56de\u5e94"}`, () =>
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
  addItem(m.pinned ? "\u53d6\u6d88\u7f6e\u9876" : "\u7f6e\u9876", () =>
    send({ action: "pin", chatKey: summary.key, messageId: m.id, active: !m.pinned })
  );
  if (m.senderId === myId) {
    if (!m.fileInfo) {
      addItem("\u7f16\u8f91", () => editMessageDialog(summary, m));
    }
    addItem("\u5220\u9664", () =>
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
    if (content.length > MAX_CONTENT_LENGTH) {
      toast("\u6d88\u606f\u8fc7\u957f\uff08\u4e0a\u9650 5000 \u5b57\uff09", true);
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
    $("fileInput").value = "";
    if (!file || !state.openKey) return;
    if (file.size > 512 * 1024 * 1024) {
      toast("\u6587\u4ef6\u8fc7\u5927\uff08\u8d85\u8fc7 512 MB\uff09", true);
      return;
    }
    toast(`\u6b63\u5728\u4e0a\u4f20 ${file.name}…`);
    try {
      const resp = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST",
        body: file,
      });
      const doc = await resp.json();
      send({ action: "sendFile", chatKey: state.openKey, uploadId: doc.uploadId });
    } catch (e) {
      toast("\u4e0a\u4f20\u5931\u8d25", true);
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
      <button data-act="cancel">\u5173\u95ed</button>
    </div>
    <div class="formError" id="formError"></div>`);
  $("modalCard").querySelector('[data-act="cancel"]').onclick = closeModal;
}

function addContactDialog() {
  dialogShell(
    "\u6dfb\u52a0\u6210\u5458",
    `<label>\u5bf9\u65b9 IP \u5730\u5740\uff08\u53ef\u9009 \u7aef\u53e3\uff0c\u9ed8\u8ba4 9999\uff09</label>
     <input type="text" id="fIp" placeholder="\u4f8b\u5982 192.168.1.20 \u6216 192.168.1.20:9999">`,
    `<button class="primary" data-act="ok">\u8fde\u63a5</button>`
  );
  $("modalCard").querySelector('[data-act="ok"]').onclick = () => {
    const raw = $("fIp").value.trim();
    if (!raw) return;
    send({ action: "addContact", ip: raw, port: 0 });
    closeModal();
    toast("\u6b63\u5728\u8fde\u63a5\uff0c\u5bf9\u65b9\u786e\u8ba4\u540e\u4f1a\u51fa\u73b0\u5728\u6210\u5458\u5217\u8868");
  };
}

function createGroupDialog() {
  dialogShell(
    "\u521b\u5efa\u7fa4\u7ec4",
    `<label>\u7fa4\u540d</label>
     <input type="text" id="fGName" placeholder="\u4f8b\u5982 \u5bb6\u5ead\u7fa4">
     <label>\u7fa4\u5bc6\u7801\uff08\u53ef\u7559\u7a7a\uff1b\u5efa\u8bae\u8bbe\u7f6e\uff0c\u9632\u4e3b\u52a8\u4e2d\u95f4\u4eba\uff09</label>
     <input type="password" id="fGPass">`,
    `<button class="primary" data-act="ok">\u521b\u5efa</button>`
  );
  $("modalCard").querySelector('[data-act="ok"]').onclick = async () => {
    const name = $("fGName").value.trim();
    if (!name) return;
    const result = await request({ action: "createGroup", name, password: $("fGPass").value });
    if (result && result.joinId) {
      closeModal();
      toast(
        `\u7fa4\u7ec4\u5df2\u521b\u5efa\uff0c\u5165\u7fa4\u53f7\u7801 ${result.joinId.slice(0, 4)} ${result.joinId.slice(4)}\uff08\u52a0\u5165\u65f6\u9700\u586b\u5199\uff09`
      );
      if (state.snapshot) openChat(result.groupId);
    }
  };
}

function joinGroupDialog() {
  dialogShell(
    "\u52a0\u5165\u7fa4\u7ec4",
    `<label>\u4efb\u4e00\u6210\u5458\u7684 IP\uff08\u53ef\u9009 \u7aef\u53e3\uff09</label>
     <input type="text" id="fHost" placeholder="\u4f8b\u5982 192.168.1.10">
     <label>\u5165\u7fa4\u53f7\u7801\uff088 \u4f4d\u6570\u5b57\uff09</label>
     <input type="text" id="fJoinId" placeholder="\u4f8b\u5982 1234 5678">
     <label>\u7fa4\u5bc6\u7801</label>
     <input type="password" id="fGPass">`,
    `<button data-act="query">\u67e5\u8be2\u7fa4\u4fe1\u606f</button>
     <button class="primary" data-act="ok">\u52a0\u5165</button>`
  );
  const hostOf = () => {
    const raw = $("fHost").value.trim();
    if (raw.includes(":")) return raw;
    return raw;
  };
  $("modalCard").querySelector('[data-act="query"]').onclick = async () => {
    const joinId = $("fJoinId").value.replace(/\s+/g, "");
    const result = await request({
      action: "queryGroup",
      host: hostOf(),
      joinId,
      password: $("fGPass").value,
    });
    const err = $("formError");
    if (result && result.ok) {
      err.textContent = "";
      toast(`\u7fa4\u7ec4\uff1a${result.info.groupName}\uff08\u7fa4\u4e3b ${result.info.creatorName}\uff0c${result.info.memberCount} \u4eba\uff09`);
    } else {
      err.textContent = (result && result.message) || "\u67e5\u8be2\u5931\u8d25";
    }
  };
  $("modalCard").querySelector('[data-act="ok"]').onclick = async () => {
    const joinId = $("fJoinId").value.replace(/\s+/g, "");
    if (!joinId) return;
    const result = await request({
      action: "joinGroup",
      host: hostOf(),
      joinId,
      password: $("fGPass").value,
    });
    const err = $("formError");
    if (result && result.ok) {
      closeModal();
      toast("\u5df2\u52a0\u5165\u7fa4\u7ec4");
    } else {
      err.textContent = (result && result.message) || "\u52a0\u5165\u5931\u8d25";
    }
  };
}

function editMessageDialog(summary, m) {
  dialogShell(
    "\u7f16\u8f91\u6d88\u606f",
    `<textarea id="fEdit" style="width:100%;height:110px;border:1px solid var(--border);border-radius:8px;padding:8px;font:inherit">${esc(m.content)}</textarea>`,
    `<button class="primary" data-act="ok">\u4fdd\u5b58</button>`
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
  const amOwner = g.creatorId === state.snapshot.profile.deviceId;
  let membersHtml = "";
  for (const mem of g.members) {
    membersHtml += `
      <div class="memberRow">
        <div class="avatar">${esc((mem.name || "?").slice(0, 1).toUpperCase())}</div>
        <div class="flex">
          <div>${esc(mem.name)} ${mem.isSelf ? '<span class="dim">\uff08\u6211\uff09</span>' : ""}</div>
          <div class="fp">${mem.verified ? `\u5b89\u5168\u7801 ${mem.fingerprint}` : "\u672a\u9a8c\u8bc1"}</div>
        </div>
        ${mem.verified ? '<span class="vtag">\u5df2\u9a8c\u8bc1</span>' : ""}
        ${amOwner && !mem.isSelf ? `<button class="danger" data-kick="${esc(mem.id)}">\u79fb\u51fa</button>` : ""}
      </div>`;
  }
  dialogShell(
    `\u7fa4\u8bbe\u7f6e \u00b7 ${g.name}`,
    `
    ${amOwner ? `
      <label>\u7fa4\u540d</label>
      <input type="text" id="fGroupName" value="${esc(g.name)}">
      <label>\u7fa4\u516c\u544a</label>
      <input type="text" id="fAnnouncement" value="${esc(g.announcement || "")}">
      <button class="primary" data-act="save" style="margin-top:10px">\u4fdd\u5b58\u7fa4\u4fe1\u606f</button>
      <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">` : ""}
    <label>\u6210\u5458\uff08${g.members.length}\uff09</label>
    <div>${membersHtml}</div>
    <p class="dim" style="font-size:12px">\u5165\u7fa4\u53f7\u7801 ${g.joinId.slice(0, 4)} ${g.joinId.slice(4)} \u00b7 ${g.isHost ? "\u672c\u673a\u662f\u7fa4\u4e3b" : ""}</p>`,
    `<button class="danger" data-act="leave">\u9000\u51fa\u7fa4\u7ec4</button>`
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
      toast("\u5df2\u4fdd\u5b58");
    };
  }
  card.querySelectorAll("[data-kick]").forEach((btn) => {
    btn.onclick = () => {
      if (confirm("\u786e\u8ba4\u5c06\u8be5\u6210\u5458\u79fb\u51fa\u7fa4\u7ec4\uff1f")) {
        send({ action: "kickMember", groupId: g.groupId, targetId: btn.getAttribute("data-kick") });
        closeModal();
      }
    };
  });
  card.querySelector('[data-act="leave"]').onclick = () => {
    if (confirm("\u9000\u51fa\u540e\u9700\u91cd\u65b0\u8f93\u5165\u53f7\u7801\u52a0\u5165\uff0c\u786e\u8ba4\uff1f")) {
      send({ action: "leaveGroup", groupId: g.groupId });
      closeModal();
      closeChat();
    }
  };
}

/* ---------------- auth ---------------- */

async function checkAuth() {
  try {
    const resp = await fetch("/api/whoami");
    return await resp.json();
  } catch (e) {
    return { authenticated: false, firstRun: false };
  }
}

function showAuthView(mode) {
  $("app").classList.add("hidden");
  $("authView").classList.remove("hidden");
  const firstRun = mode === "firstRun";
  $("authTitle").textContent = firstRun ? "\u521d\u59cb\u5316\u670d\u52a1\u5668" : "\u767b\u5f55 LocalChat Web";
  $("authHint").textContent = firstRun
    ? "\u8fd8\u6ca1\u6709\u4efb\u4f55\u8d26\u53f7\uff0c\u5148\u521b\u5efa\u7b2c\u4e00\u4e2a\u8d26\u53f7\uff08\u6bcf\u4e2a\u8d26\u53f7\u662f\u4e00\u4e2a\u72ec\u7acb\u7684 LocalChat \u8bbe\u5907\uff09"
    : "\u6bcf\u4e2a\u8d26\u53f7\u662f\u4e00\u4e2a\u72ec\u7acb\u7684 LocalChat \u8bbe\u5907\uff0c\u62e5\u6709\u81ea\u5df1\u7684\u6210\u5458\u4e0e\u7fa4\u7ec4";
  $("userLabel").classList.remove("hidden");
  $("authUser").classList.remove("hidden");
  $("passLabel").classList.remove("hidden");
  $("authPass").classList.remove("hidden");
  $("pass2Label").classList.toggle("hidden", !firstRun);
  $("authPass2").classList.toggle("hidden", !firstRun);
  $("authSubmit").classList.remove("hidden");
  $("authSubmit").textContent = firstRun ? "\u521b\u5efa\u8d26\u53f7\u5e76\u8fdb\u5165" : "\u767b\u5f55";
  $("authError").textContent = "";
  $("authSubmit").onclick = async () => {
    const username = $("authUser").value.trim();
    const password = $("authPass").value;
    const body = { username, password };
    if (firstRun) {
      if (password !== $("authPass2").value) {
        $("authError").textContent = "\u4e24\u6b21\u5bc6\u7801\u4e0d\u4e00\u81f4";
        return;
      }
    }
    try {
      const resp = await fetch(firstRun ? "/api/register" : "/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const doc = await resp.json();
      if (!resp.ok || !doc.ok) {
        $("authError").textContent = doc.message || "\u64cd\u4f5c\u5931\u8d25";
        return;
      }
      enterApp();
    } catch (e) {
      $("authError").textContent = "\u7f51\u7edc\u9519\u8bef";
    }
  };
}

function enterApp() {
  $("authView").classList.add("hidden");
  $("app").classList.remove("hidden");
  connect();
}

async function boot() {
  const auth = await checkAuth();
  if (auth.authenticated) {
    enterApp();
  } else {
    showAuthView(auth.firstRun ? "firstRun" : "login");
  }
}

async function logout() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch (e) {
    /* ignore */
  }
  location.reload();
}

function profileDialog() {
  const p = state.snapshot.profile;
  dialogShell(
    "\u8d26\u53f7\u4e0e\u8bbe\u7f6e",
    `<label>\u6635\u79f0\uff08\u5c40\u57df\u7f51\u5185\u5c55\u793a\u7684\u540d\u5b57\uff09</label>
     <input type="text" id="fNick" value="${esc(p.name)}">
     <label>\u8d26\u53f7</label>
     <input type="text" value="${esc(p.username || "")}" disabled>
     <label>\u8bbe\u5907 ID</label>
     <input type="text" value="${esc(p.deviceId)}" disabled>
     <label>\u5b89\u5168\u7801\uff08\u6307\u7eb9\uff0c\u53ef\u4e0e\u5bf9\u65b9\u5f53\u9762\u6bd4\u5bf9\uff09</label>
     <input type="text" value="${esc(p.fingerprint)}" disabled>
     <label>\u672c\u8d26\u53f7\u7684\u534f\u8bae\u5730\u5740\uff08\u5bf9\u65b9\u6dfb\u52a0\u6210\u5458\u65f6\u586b\u5199\uff09</label>
     <input type="text" value="${esc(p.ip)}:${p.port}" disabled>
     ${p.bindError ? `<div class="formError">${esc(p.bindError)}</div>` : ""}
     <div class="accountActions">
       <button data-act="addAccount">\uff0b\u65b0\u5efa\u8d26\u53f7</button>
       <button class="danger" data-act="logout">\u9000\u51fa\u767b\u5f55</button>
     </div>`,
    `<button class="primary" data-act="ok">\u4fdd\u5b58\u6635\u79f0</button>`
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
}

function addAccountDialog() {
  dialogShell(
    "\u65b0\u5efa\u8d26\u53f7",
    `<p class="dim" style="font-size:12px">\u65b0\u8d26\u53f7\u662f\u4e00\u4e2a\u72ec\u7acb\u7684 LocalChat \u8bbe\u5907\uff0c\u62e5\u6709\u81ea\u5df1\u7684\u8eab\u4efd\u3001\u6210\u5458\u4e0e\u7fa4\u7ec4\uff0c\u5e76\u4f7f\u7528\u4e0b\u4e00\u4e2a\u53ef\u7528\u7684\u534f\u8bae\u7aef\u53e3\u3002</p>
     <label>\u7528\u6237\u540d</label>
     <input type="text" id="fNewUser">
     <label>\u5bc6\u7801\uff08\u81f3\u5c11 6 \u4f4d\uff09</label>
     <input type="password" id="fNewPass">`,
    `<button class="primary" data-act="ok">\u521b\u5efa</button>`
  );
  $("modalCard").querySelector('[data-act="ok"]').onclick = async () => {
    try {
      const resp = await fetch("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: $("fNewUser").value.trim(),
          password: $("fNewPass").value,
        }),
      });
      const doc = await resp.json();
      if (!resp.ok || !doc.ok) {
        $("formError").textContent = doc.message || "\u521b\u5efa\u5931\u8d25";
        return;
      }
      closeModal();
      toast(`\u8d26\u53f7 ${doc.username} \u5df2\u521b\u5efa\uff0c\u534f\u8bae\u7aef\u53e3 TCP ${doc.port}`);
    } catch (e) {
      $("formError").textContent = "\u7f51\u7edc\u9519\u8bef";
    }
  };
}

/* ---------------- boot ---------------- */

function setupChrome() {
  $("btnAddContact").onclick = addContactDialog;
  $("btnCreateGroup").onclick = createGroupDialog;
  $("btnJoinGroup").onclick = joinGroupDialog;
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
