const state = {
  authTab: "sandbox", // "sandbox" | "apikey"
  resolvedAppId: "dev-sandbox",
  models: [],
  chatHistory: [],
  activeSnippet: "curl",
  ctxSize: 2048,
  abortController: null,
  lastIssuedKey: "",
};

const $ = (id) => document.getElementById(id);

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3000);
}

function randomSessionId() {
  return "sess_" + Math.random().toString(36).slice(2, 10);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* Safe, lightweight Markdown renderer for assistant bubbles */
function renderMarkdown(raw) {
  const text = String(raw ?? "");
  if (!text.trim()) return "";

  const codeBlocks = [];
  const withPlaceholders = text.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    const cleanLang = escapeHtml(lang || "code");
    const escapedCode = escapeHtml(code.replace(/\n$/, ""));
    codeBlocks.push(
      `<div class="md-code-wrap"><div class="md-code-bar"><span>${cleanLang}</span><button type="button" class="btn btn-ghost btn-xs btn-copy-code">Copy</button></div><pre><code>${escapedCode}</code></pre></div>`
    );
    return `\n@@CODEBLOCK_${idx}@@\n`;
  });

  const formatInline = (line) =>
    escapeHtml(line)
      .replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");

  const lines = withPlaceholders.split("\n");
  const out = [];
  let inUl = false;
  let inOl = false;
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.join("<br>")}</p>`);
      para = [];
    }
  };
  const closeLists = () => {
    if (inUl) { out.push("</ul>"); inUl = false; }
    if (inOl) { out.push("</ol>"); inOl = false; }
  };

  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    const codeMatch = trimmed.match(/^@@CODEBLOCK_(\d+)@@$/);
    if (codeMatch) {
      flushPara();
      closeLists();
      out.push(codeBlocks[Number(codeMatch[1])]);
      continue;
    }
    if (!trimmed) {
      flushPara();
      closeLists();
      continue;
    }
    const heading = trimmed.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushPara();
      closeLists();
      const lvl = Math.min(4, heading[1].length + 1);
      out.push(`<h${lvl}>${formatInline(heading[2])}</h${lvl}>`);
      continue;
    }
    const ulMatch = trimmed.match(/^[-*•]\s+(.*)$/);
    if (ulMatch) {
      flushPara();
      if (inOl) { out.push("</ol>"); inOl = false; }
      if (!inUl) { out.push("<ul>"); inUl = true; }
      out.push(`<li>${formatInline(ulMatch[1])}</li>`);
      continue;
    }
    const olMatch = trimmed.match(/^\d+\.\s+(.*)$/);
    if (olMatch) {
      flushPara();
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (!inOl) { out.push("<ol>"); inOl = true; }
      out.push(`<li>${formatInline(olMatch[1])}</li>`);
      continue;
    }
    closeLists();
    para.push(formatInline(rawLine));
  }

  flushPara();
  closeLists();
  return out.join("");
}

/* Mobile off-canvas drawer helpers */
function closeDrawers() {
  $("paneLeft").classList.remove("drawer-open");
  $("paneRight").classList.remove("drawer-open");
  $("drawerBackdrop").classList.add("hidden");
}

function toggleDrawer(which) {
  const target = which === "left" ? $("paneLeft") : $("paneRight");
  const other = which === "left" ? $("paneRight") : $("paneLeft");
  other.classList.remove("drawer-open");
  const willOpen = !target.classList.contains("drawer-open");
  target.classList.toggle("drawer-open", willOpen);
  $("drawerBackdrop").classList.toggle("hidden", !willOpen);
}

function buildHeaders({ includeJson = true, includeSession = true, isAdmin = false } = {}) {
  const h = {};
  if (includeJson) h["Content-Type"] = "application/json";

  if (isAdmin) {
    const adminKey = $("inputAdminKey").value.trim();
    if (adminKey) h["X-Admin-Key"] = adminKey;
    return h;
  }

  if (state.authTab === "apikey") {
    const key = $("inputApiKey").value.trim();
    if (key) h["Authorization"] = `Bearer ${key}`;
  } else {
    const appId = $("inputAppId").value.trim() || "dev-sandbox";
    h["X-App-Id"] = appId;
  }

  const userId = $("inputUserId").value.trim();
  if (userId) h["X-User-Id"] = userId;

  const memMode = $("selectMemoryMode").value;
  if (memMode) h["X-Memory-Mode"] = memMode;

  if (includeSession && $("chkUseSession").checked) {
    const sid = $("inputSessionId").value.trim();
    if (sid) h["X-Session-Id"] = sid;
  }

  const ttl = $("inputTtlHours").value.trim();
  if (ttl && Number(ttl) > 0) {
    h["X-Session-TTL-Hours"] = String(Number(ttl));
  }
  return h;
}

function currentScopeLabel() {
  const appLabel = state.authTab === "apikey" ? (state.resolvedAppId || "key-app") : ($("inputAppId").value.trim() || "dev-sandbox");
  const user = $("inputUserId").value.trim() || "(none)";
  const sess = $("chkUseSession").checked ? ($("inputSessionId").value.trim() || "(none)") : "stateless";
  return `${appLabel} / ${user} / ${sess}`;
}

function updateScopeBanner() {
  const label = currentScopeLabel();
  $("metricScope").innerHTML = `Scope: <strong>${escapeHtml(label)}</strong>`;
  $("mobileScopePill").textContent = label;
  renderCodeSnippet();
}

async function refreshHealth() {
  try {
    const res = await fetch("/health");
    const data = await res.json();
    state.ctxSize = data.ctx_size || 2048;

    const pills = [];
    const authClass = data.auth_mode === "strict" ? "pill-warn" : "pill-ok";
    pills.push(`<span class="pill ${authClass}">AUTH: ${(data.auth_mode || "dev").toUpperCase()}</span>`);

    const dbStat = data.memory_db?.status || "unknown";
    const dbClass = dbStat === "ok" ? "pill-ok" : (dbStat === "disabled" ? "pill-warn" : "pill-err");
    pills.push(`<span class="pill ${dbClass}" title="${escapeHtml(data.memory_db?.detail || "")}">PG: ${dbStat}</span>`);

    for (const [mId, st] of Object.entries(data.backends || {})) {
      const cls = st === "ok" ? "pill-ok" : "pill-err";
      pills.push(`<span class="pill ${cls}">${escapeHtml(mId)}: ${escapeHtml(st)}</span>`);
    }
    pills.push(`<span class="pill">Queue: ${data.worker_queue_depth ?? 0}</span>`);
    $("healthBar").innerHTML = pills.join("");
  } catch (err) {
    $("healthBar").innerHTML = `<span class="pill pill-err">Router unreachable: ${escapeHtml(err.message)}</span>`;
  }
}

async function loadModels() {
  try {
    const res = await fetch("/v1/models", { headers: buildHeaders({ includeJson: false, includeSession: false }) });
    if (!res.ok) return;
    const body = await res.json();
    state.models = body.data || [];

    const baseModels = state.models.filter((m) => m.type === "base");
    const agents = state.models.filter((m) => m.type === "agent");

    const sel = $("selectModel");
    const prev = sel.value;
    sel.innerHTML = `
      <optgroup label="Base Models">
        ${baseModels.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.id)}</option>`).join("")}
      </optgroup>
      <optgroup label="Virtual Agents">
        ${agents.map((a) => `<option value="${escapeHtml(a.id)}">${escapeHtml(a.id)} (${escapeHtml(a.backend)})</option>`).join("")}
      </optgroup>
    `;
    if (prev && state.models.some((m) => m.id === prev)) {
      sel.value = prev;
    }
    applySelectedModelInfo(true);
  } catch (_) {}
}

function applySelectedModelInfo(applyDefaults = false) {
  const id = $("selectModel").value;
  const item = state.models.find((m) => m.id === id);
  const card = $("modelInfoCard");
  if (!item) {
    card.textContent = "Select a model or virtual agent.";
    return;
  }
  if (item.type === "agent") {
    card.innerHTML = `<div><strong>Backend:</strong> <code>${escapeHtml(item.backend)}</code> — ${escapeHtml(item.description || "")}</div><div style="margin-top:4px"><strong>Prompt:</strong> ${escapeHtml(item.system_prompt || "")}</div>`;
    if (applyDefaults && item.defaults?.temperature !== undefined) {
      $("rangeTemp").value = item.defaults.temperature;
      $("valTemp").textContent = Number(item.defaults.temperature).toFixed(2);
    }
  } else {
    card.innerHTML = `<div><strong>Base GGUF Model:</strong> <code>${escapeHtml(item.id)}</code> (direct inference)</div>`;
    if (applyDefaults) {
      $("rangeTemp").value = 0.7;
      $("valTemp").textContent = "0.70";
    }
  }
  renderCodeSnippet();
}

async function verifyAppIdentity() {
  try {
    const res = await fetch("/v1/apps/me", { headers: buildHeaders({ includeJson: false, includeSession: false }) });
    const data = await res.json();
    if (!res.ok) {
      $("authVerifiedBadge").textContent = "Auth Error";
      $("authVerifiedBadge").className = "badge";
      toast(data.detail || "API key verification failed");
      return;
    }
    state.resolvedAppId = data.app_id;
    if (data.authenticated) {
      $("authVerifiedBadge").textContent = data.app_id;
      $("authVerifiedBadge").className = "badge badge-ok";
      $("resolvedAppLabel").innerHTML = `Bound to <code>${escapeHtml(data.app_id)}</code> (${data.app?.session_count ?? 0} sessions, ${data.app?.memory_count ?? 0} facts)`;
    } else {
      $("authVerifiedBadge").textContent = data.app_id;
      $("authVerifiedBadge").className = "badge badge-muted";
    }
    updateScopeBanner();
    await loadModels();
    await loadSessions();
    await loadMemories();
  } catch (err) {
    toast(`Verify failed: ${err.message}`);
  }
}

async function loadSessions() {
  const userId = $("inputUserId").value.trim();
  const listEl = $("sessionsList");
  if (!userId) {
    listEl.innerHTML = `<div class="empty-note">Enter a User ID to view sessions.</div>`;
    return;
  }
  try {
    const res = await fetch("/v1/sessions?limit=50", {
      headers: buildHeaders({ includeJson: false, includeSession: false }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      listEl.innerHTML = `<div class="empty-note">${escapeHtml(err.detail || "Authenticate to view sessions.")}</div>`;
      return;
    }
    const body = await res.json();
    const sessions = body.data || [];
    const activeSid = $("inputSessionId").value.trim();
    if (!sessions.length) {
      listEl.innerHTML = `<div class="empty-note">No saved sessions for this (app, user).</div>`;
      return;
    }
    listEl.innerHTML = sessions
      .map((s) => {
        const isAct = s.session_id === activeSid ? "active" : "";
        const title = s.title || s.session_id;
        return `<div class="session-item ${isAct}" data-sid="${escapeHtml(s.session_id)}"><div style="min-width:0;flex:1"><div class="session-title">${escapeHtml(title)}</div><div class="session-sub">${escapeHtml(s.session_id)} · ${s.message_count} msgs</div></div><button type="button" class="btn btn-ghost btn-xs btn-del-session" data-sid="${escapeHtml(s.session_id)}" title="Delete session">×</button></div>`;
      })
      .join("");
  } catch (_) {
    listEl.innerHTML = `<div class="empty-note">Failed loading sessions.</div>`;
  }
}

async function selectSession(sessionId) {
  $("chkUseSession").checked = true;
  $("inputSessionId").value = sessionId;
  closeDrawers();
  updateScopeBanner();
  try {
    const res = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
      headers: buildHeaders({ includeJson: false, includeSession: false }),
    });
    if (!res.ok) return;
    const data = await res.json();
    const sess = data.session;
    state.chatHistory = (data.messages || []).map((m) => ({
      role: m.role,
      content: m.content,
      model_id: m.model_id,
      truncated: false,
    }));
    renderChatMessages();
    updateRollingSummaryCard(sess);
    await loadSessions();
  } catch (err) {
    toast(`Error loading session: ${err.message}`);
  }
}

function updateRollingSummaryCard(sess) {
  if (!sess) {
    $("summaryMeta").textContent = "Summarized up to seq 0 / 0 messages";
    $("rollingSummaryText").textContent = "No rolling summary yet.";
    return;
  }
  $("summaryMeta").textContent = `Summarized up to seq ${sess.summary_up_to_seq} / ${sess.message_count} messages (~${sess.summary_token_est} tok)`;
  $("rollingSummaryText").textContent = sess.rolling_summary || "No rolling summary yet (triggers at >=8 turns or click Compact Now).";
}

function renderSingleMessageHtml(m, idx) {
  const isUser = m.role === "user";
  const rowCls = isUser ? "msg-row-user" : "msg-row-assistant";
  const bubCls = isUser ? "msg-user" : "msg-assistant";
  const roleLabel = isUser ? "You" : `Assistant${m.model_id ? ` · ${m.model_id}` : ""}`;

  let bodyHtml;
  if (isUser) {
    bodyHtml = escapeHtml(m.content);
  } else if (m.streaming && !m.content) {
    bodyHtml = `<div class="typing-dots" aria-label="Generating"><span></span><span></span><span></span></div>`;
  } else {
    bodyHtml = renderMarkdown(m.content);
  }

  const truncBar = (!isUser && m.truncated)
    ? `<div class="truncation-bar"><span>Stopped at max_tokens limit</span><button type="button" class="btn btn-secondary btn-xs btn-continue-gen" data-idx="${idx}">Continue generating →</button></div>`
    : "";

  return `<div class="msg-row ${rowCls}"><div class="msg-bubble ${bubCls}" data-msg-idx="${idx}"><div class="msg-header"><span class="msg-role">${escapeHtml(roleLabel)}</span><button type="button" class="msg-copy-btn" data-copy-idx="${idx}" title="Copy message">Copy</button></div><div class="msg-body">${bodyHtml}</div>${truncBar}</div></div>`;
}

function renderChatMessages() {
  const box = $("chatMessages");
  if (!state.chatHistory.length) {
    box.innerHTML = `<div class="empty-hero"><h3>Ready to test models, virtual agents &amp; memory</h3><p>Isolated strictly to <code>${escapeHtml(currentScopeLabel())}</code>. Pick a starter prompt or type below:</p><div class="hero-prompts"><button type="button" class="chip starter-chip" data-prompt="I am building a Python FastAPI service backed by PostgreSQL. Remember my stack.">Save my stack to memory</button><button type="button" class="chip starter-chip" data-prompt="What backend stack do I use, and suggest 3 concise health-check best practices?">Test memory recall</button><button type="button" class="chip starter-chip" data-prompt="Give me 5 concise SaaS micro-product ideas with one-line descriptions.">5 concise product ideas</button></div></div>`;
    return;
  }
  box.innerHTML = state.chatHistory.map((m, idx) => renderSingleMessageHtml(m, idx)).join("");
  box.scrollTop = box.scrollHeight;
}

function buildChatPayload(userMessageForPreview = "Hello!") {
  const model = $("selectModel").value || "llama-3.2-1b";
  const sysPrompt = $("inputSystemPrompt").value.trim();
  const historyMode = $("selectHistoryMode").value;
  const msgs = [];
  if (sysPrompt) {
    msgs.push({ role: "system", content: sysPrompt });
  }
  if (historyMode === "client_array" && state.chatHistory.length > 0) {
    for (const m of state.chatHistory) {
      if (m.content) msgs.push({ role: m.role, content: m.content });
    }
  }
  msgs.push({ role: "user", content: userMessageForPreview });

  const payload = {
    model,
    messages: msgs,
    stream: $("chkStream").checked,
    temperature: Number($("rangeTemp").value),
    top_p: Number($("rangeTopP").value),
    max_tokens: Number($("rangeMaxTokens").value),
  };

  const pres = Number($("rangePresPen").value);
  if (pres !== 0) payload.presence_penalty = pres;
  const freq = Number($("rangeFreqPen").value);
  if (freq !== 0) payload.frequency_penalty = freq;

  const seed = $("inputSeed").value.trim();
  if (seed !== "") payload.seed = Number(seed);

  const stops = $("inputStopSeq").value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (stops.length) payload.stop = stops;

  const userId = $("inputUserId").value.trim();
  if (userId) payload.user = userId;

  return payload;
}

function renderCodeSnippet() {
  const drawer = $("codeDrawer");
  if (drawer.classList.contains("hidden")) return;

  const sampleText = $("chatInput").value.trim() || "What stack do I prefer?";
  const payload = buildChatPayload(sampleText);
  const headers = buildHeaders({ includeJson: true, includeSession: true });
  const origin = window.location.origin;

  if (state.activeSnippet === "json") {
    $("codeSnippetContent").textContent = JSON.stringify({ headers, body: payload }, null, 2);
    return;
  }

  if (state.activeSnippet === "python") {
    const apiKey = state.authTab === "apikey" ? ($("inputApiKey").value.trim() || "ak_your_key") : "not-needed";
    const extraHeaders = { ...headers };
    delete extraHeaders["Content-Type"];
    delete extraHeaders["Authorization"];
    $("codeSnippetContent").textContent = `from openai import OpenAI

client = OpenAI(
    base_url="${origin}/v1",
    api_key="${apiKey}",
    default_headers=${JSON.stringify(extraHeaders, null, 4)},
)

resp = client.chat.completions.create(
    model="${payload.model}",
    messages=${JSON.stringify(payload.messages)},
    temperature=${payload.temperature},
    max_tokens=${payload.max_tokens},
    stream=${payload.stream ? "True" : "False"},
)`;
    return;
  }

  const headerFlags = Object.entries(headers)
    .map(([k, v]) => `  -H "${k}: ${v}" \\`)
    .join("\n");
  $("codeSnippetContent").textContent = `curl ${payload.stream ? "-N " : "-s "}${origin}/v1/chat/completions \\\n${headerFlags}\n  -d '${JSON.stringify(payload, null, 2)}'`;
}

function updateBudgetMeter(tokensUsed) {
  const used = Number(tokensUsed) || 0;
  const pct = Math.min(100, Math.round((used / state.ctxSize) * 100));
  $("metricTokens").innerHTML = `Context: <strong>${used} / ${state.ctxSize}</strong> tok`;
  $("budgetLabel").textContent = `${used} / ${state.ctxSize}`;
  $("budgetFill").style.width = `${pct}%`;
}

function resizeComposer() {
  const ta = $("chatInput");
  ta.style.height = "auto";
  ta.style.height = `${Math.min(150, ta.scrollHeight)}px`;
}

async function sendPromptText(text) {
  if (!text) return;

  if ($("chkUseSession").checked && !$("inputSessionId").value.trim()) {
    $("inputSessionId").value = randomSessionId();
  }

  const payload = buildChatPayload(text);
  const headers = buildHeaders({ includeJson: true, includeSession: true });

  state.chatHistory.push({ role: "user", content: text });
  $("chatInput").value = "";
  resizeComposer();

  const assistantEntry = {
    role: "assistant",
    content: "",
    model_id: payload.model,
    streaming: true,
    truncated: false,
  };
  const assistantIdx = state.chatHistory.length;
  state.chatHistory.push(assistantEntry);
  renderChatMessages();
  updateScopeBanner();

  const chatBox = $("chatMessages");
  const getAssistantBodyEl = () =>
    chatBox.querySelector(`[data-msg-idx="${assistantIdx}"] .msg-body`);

  const t0 = performance.now();
  let ttft = null;
  let finishReason = null;

  state.abortController = new AbortController();
  $("btnStopStream").classList.remove("hidden");
  $("btnSendChat").disabled = true;

  try {
    const res = await fetch("/v1/chat/completions", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: state.abortController.signal,
    });

    const ctxTok = res.headers.get("x-context-tokens");
    const memRecalled = res.headers.get("x-memories-recalled");
    if (ctxTok !== null) updateBudgetMeter(ctxTok);
    if (memRecalled !== null) {
      $("metricMemories").innerHTML = `Recalled: <strong>${memRecalled}</strong>`;
    }

    if (!res.ok) {
      const errText = await res.text();
      assistantEntry.streaming = false;
      assistantEntry.content = `[HTTP ${res.status}] ${errText}`;
      renderChatMessages();
      return;
    }

    if (!payload.stream) {
      const data = await res.json();
      const choice = data.choices?.[0] || {};
      assistantEntry.content = choice.message?.content || "";
      assistantEntry.streaming = false;
      assistantEntry.truncated = choice.finish_reason === "length";
      renderChatMessages();
      const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
      $("metricSpeed").innerHTML = `Speed: <strong>${elapsed}s</strong>`;
    } else {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let approxTokens = 0;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, idx).trim();
          buffer = buffer.slice(idx + 1);
          if (!line.startsWith("data:")) continue;
          const raw = line.slice(5).trim();
          if (!raw || raw === "[DONE]") continue;
          try {
            const parsed = JSON.parse(raw);
            const ch = parsed.choices?.[0];
            if (ch?.finish_reason) finishReason = ch.finish_reason;
            const delta = ch?.delta?.content;
            if (delta) {
              if (ttft === null) ttft = ((performance.now() - t0) / 1000).toFixed(2);
              assistantEntry.content += delta;
              approxTokens += 1;
              const bodyEl = getAssistantBodyEl();
              if (bodyEl) {
                const nearBottom = chatBox.scrollHeight - chatBox.scrollTop - chatBox.clientHeight < 120;
                bodyEl.innerHTML = renderMarkdown(assistantEntry.content);
                if (nearBottom) chatBox.scrollTop = chatBox.scrollHeight;
              }
            }
          } catch (_) {}
        }
      }
      assistantEntry.streaming = false;
      assistantEntry.truncated = finishReason === "length";
      renderChatMessages();

      const totalSec = Math.max(0.05, (performance.now() - t0) / 1000);
      const genSec = Math.max(0.05, totalSec - (Number(ttft) || 0));
      const tps = (approxTokens / genSec).toFixed(1);
      $("metricSpeed").innerHTML = `TTFT: <strong>${ttft ?? totalSec.toFixed(2)}s</strong> · <strong>${tps} tok/s</strong>`;
    }

    await loadSessions();
    const sid = $("inputSessionId").value.trim();
    if ($("chkUseSession").checked && sid) {
      const sRes = await fetch(`/v1/sessions/${encodeURIComponent(sid)}`, {
        headers: buildHeaders({ includeJson: false, includeSession: false }),
      });
      if (sRes.ok) {
        const sData = await sRes.json();
        updateRollingSummaryCard(sData.session);
      }
    }
    setTimeout(() => {
      loadMemories();
      refreshHealth();
    }, 2200);
  } catch (err) {
    assistantEntry.streaming = false;
    if (err.name === "AbortError") {
      assistantEntry.content += "\n\n*(Generation stopped)*";
    } else {
      assistantEntry.content = `[Network error] ${err.message}`;
    }
    renderChatMessages();
  } finally {
    $("btnStopStream").classList.add("hidden");
    $("btnSendChat").disabled = false;
    state.abortController = null;
  }
}

async function sendChatMessage(ev) {
  ev.preventDefault();
  const text = $("chatInput").value.trim();
  if (!text) return;
  await sendPromptText(text);
}

async function loadMemories() {
  const userId = $("inputUserId").value.trim();
  const listEl = $("memoriesList");
  if (!userId) {
    listEl.innerHTML = `<div class="empty-note">Set a User ID to inspect memories.</div>`;
    return;
  }
  try {
    const res = await fetch("/v1/memories", {
      headers: buildHeaders({ includeJson: false, includeSession: false }),
    });
    if (!res.ok) {
      listEl.innerHTML = `<div class="empty-note">Authenticate to view user memories.</div>`;
      return;
    }
    const body = await res.json();
    renderMemoriesItems(body.data || []);
  } catch (_) {
    listEl.innerHTML = `<div class="empty-note">Could not load memories.</div>`;
  }
}

function renderMemoriesItems(items) {
  const listEl = $("memoriesList");
  if (!items.length) {
    listEl.innerHTML = `<div class="empty-note">No matching memories for this (app, user).</div>`;
    return;
  }
  listEl.innerHTML = items
    .map((m) => {
      const simTag = m.similarity !== undefined ? ` · sim ${(m.similarity * 100).toFixed(1)}%` : "";
      return `<div class="memory-item"><div class="memory-top"><span><span class="cat-tag">${escapeHtml(m.category)}</span> ★${m.importance}${simTag}</span><button type="button" class="btn btn-ghost btn-xs btn-del-mem" data-mid="${escapeHtml(m.memory_id)}">Delete</button></div><div>${escapeHtml(m.content)}</div></div>`;
    })
    .join("");
}

async function searchMemories() {
  const q = $("inputSearchMemories").value.trim();
  if (!q) return loadMemories();
  try {
    const res = await fetch("/v1/memories/search", {
      method: "POST",
      headers: buildHeaders({ includeJson: true, includeSession: false }),
      body: JSON.stringify({ query: q, top_k: 10, min_similarity: 0.15 }),
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.detail || "Search failed");
      return;
    }
    renderMemoriesItems(data.data || []);
  } catch (err) {
    toast(`Search error: ${err.message}`);
  }
}

async function addManualMemory() {
  const content = $("inputNewMemory").value.trim();
  if (!content) return;
  try {
    const res = await fetch("/v1/memories", {
      method: "POST",
      headers: buildHeaders({ includeJson: true, includeSession: false }),
      body: JSON.stringify({
        content,
        category: $("selectMemCategory").value,
        importance: Number($("selectMemImportance").value),
        source_session_id: $("inputSessionId").value.trim() || null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.detail || "Failed saving memory");
      return;
    }
    $("inputNewMemory").value = "";
    toast(`Memory ${data.action}: ${data.memory.category}`);
    await loadMemories();
  } catch (err) {
    toast(`Error: ${err.message}`);
  }
}

async function compactActiveSession() {
  const sid = $("inputSessionId").value.trim();
  if (!sid) return toast("No active session_id selected");
  $("btnCompactSession").disabled = true;
  try {
    const res = await fetch(`/v1/sessions/${encodeURIComponent(sid)}/compact`, {
      method: "POST",
      headers: buildHeaders({ includeJson: false, includeSession: false }),
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.detail || "Compaction failed");
      return;
    }
    if (data.compacted) {
      toast(`Compacted up to seq ${data.summary_up_to_seq}`);
      await selectSession(sid);
    } else {
      toast(`Skipped compaction: ${data.reason}`);
    }
  } finally {
    $("btnCompactSession").disabled = false;
  }
}

function cosineSim(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb) || 1);
}

async function runEmbeddingsLab() {
  const textA = $("embTextA").value.trim();
  const textB = $("embTextB").value.trim();
  if (!textA) return toast("Enter Text A first");

  const out = $("embResults");
  out.classList.remove("hidden");
  out.textContent = "Computing embeddings...";

  const t0 = performance.now();
  try {
    const fetchVec = async (txt) => {
      const r = await fetch("/v1/embeddings", {
        method: "POST",
        headers: buildHeaders({ includeJson: true, includeSession: false }),
        body: JSON.stringify({ model: "all-minilm-l6-v2", input: txt }),
      });
      if (!r.ok) throw new Error(await r.text());
      const j = await r.json();
      let v = j.data?.[0]?.embedding || [];
      if (Array.isArray(v[0])) v = v[0];
      return v;
    };

    const vecA = await fetchVec(textA);
    let simHtml = "";
    if (textB) {
      const vecB = await fetchVec(textB);
      const sim = cosineSim(vecA, vecB);
      simHtml = `<div style="margin-top:8px;font-size:14px"><strong>Cosine Similarity (A vs B):</strong> <code>${sim.toFixed(4)}</code> (${(sim * 100).toFixed(1)}%)</div>`;
    }
    const ms = (performance.now() - t0).toFixed(1);
    out.innerHTML = `<div><strong>Dimensions:</strong> <code>${vecA.length}</code> · <strong>Elapsed:</strong> <code>${ms} ms</code></div>${simHtml}<div style="margin-top:8px" class="hint">First 12 values: <code>[${vecA.slice(0, 12).map((x) => Number(x).toFixed(4)).join(", ")}, ...]</code></div>`;
  } catch (err) {
    out.textContent = `Error: ${err.message}`;
  }
}

async function runRawCompletion() {
  const prompt = $("rawPromptInput").value;
  if (!prompt.trim()) return;
  const out = $("rawCompletionOutput");
  out.classList.remove("hidden");
  out.textContent = "Generating...";
  const model = $("selectModel").value || "llama-3.2-1b";
  try {
    const res = await fetch("/v1/completions", {
      method: "POST",
      headers: buildHeaders({ includeJson: true, includeSession: false }),
      body: JSON.stringify({
        model,
        prompt,
        temperature: Number($("rangeTemp").value),
        max_tokens: Number($("rangeMaxTokens").value),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      out.textContent = JSON.stringify(data, null, 2);
      return;
    }
    out.textContent = data.choices?.[0]?.text || JSON.stringify(data, null, 2);
  } catch (err) {
    out.textContent = `Error: ${err.message}`;
  }
}

async function loadAppsList() {
  const wrap = $("appsTableWrap");
  try {
    const res = await fetch("/v1/apps", { headers: buildHeaders({ includeJson: false, isAdmin: true }) });
    const data = await res.json();
    if (!res.ok) {
      wrap.innerHTML = `<div class="empty-note">${escapeHtml(data.detail || "Provide X-Admin-Key to list apps.")}</div>`;
      return;
    }
    const apps = data.data || [];
    if (!apps.length) {
      wrap.innerHTML = `<div class="empty-note">No integrated apps registered yet.</div>`;
      return;
    }
    wrap.innerHTML = `<table class="apps-table"><thead><tr><th>App ID</th><th>Name</th><th>Key Prefix</th><th>Status</th><th>Sessions / Facts</th><th>Actions</th></tr></thead><tbody>${apps.map((a) => `<tr><td><code>${escapeHtml(a.app_id)}</code></td><td>${escapeHtml(a.name)}</td><td><code>${escapeHtml(a.api_key_prefix || "(sandbox)")}</code></td><td>${a.is_active ? '<span class="badge badge-ok">Active</span>' : '<span class="badge badge-muted">Revoked</span>'}</td><td>${a.session_count ?? 0} / ${a.memory_count ?? 0}</td><td class="input-row"><button type="button" class="btn btn-secondary btn-xs btn-rot-app" data-aid="${escapeHtml(a.app_id)}">Rotate Key</button><button type="button" class="btn btn-danger btn-xs btn-rev-app" data-aid="${escapeHtml(a.app_id)}">Revoke</button></td></tr>`).join("")}</tbody></table>`;
  } catch (err) {
    wrap.innerHTML = `<div class="empty-note">Error loading apps: ${escapeHtml(err.message)}</div>`;
  }
}

function showIssuedKey(rawKey) {
  state.lastIssuedKey = rawKey;
  $("issuedKeyValue").textContent = rawKey;
  $("issuedKeyBanner").classList.remove("hidden");
}

function wireEvents() {
  $("inputSessionId").value = randomSessionId();
  renderChatMessages();

  /* Mobile drawer toggles */
  $("btnToggleLeftDrawer").addEventListener("click", () => toggleDrawer("left"));
  $("btnToggleRightDrawer").addEventListener("click", () => toggleDrawer("right"));
  $("btnCloseLeftDrawer").addEventListener("click", closeDrawers);
  $("btnCloseRightDrawer").addEventListener("click", closeDrawers);
  $("drawerBackdrop").addEventListener("click", closeDrawers);

  document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-auth-tab]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.authTab = btn.dataset.authTab;
      $("authSandboxBox").classList.toggle("hidden", state.authTab !== "sandbox");
      $("authApiKeyBox").classList.toggle("hidden", state.authTab !== "apikey");
      verifyAppIdentity();
    });
  });

  $("btnToggleKeyVis").addEventListener("click", () => {
    const inp = $("inputApiKey");
    inp.type = inp.type === "password" ? "text" : "password";
    $("btnToggleKeyVis").textContent = inp.type === "password" ? "Show" : "Hide";
  });

  $("btnVerifyKey").addEventListener("click", verifyAppIdentity);
  $("inputAppId").addEventListener("change", verifyAppIdentity);
  $("inputUserId").addEventListener("change", () => {
    state.chatHistory = [];
    $("inputSessionId").value = randomSessionId();
    renderChatMessages();
    updateScopeBanner();
    loadSessions();
    loadMemories();
  });

  document.querySelectorAll(".user-preset").forEach((chip) => {
    chip.addEventListener("click", () => {
      $("inputUserId").value = chip.dataset.user;
      $("inputUserId").dispatchEvent(new Event("change"));
    });
  });

  $("btnNewSession").addEventListener("click", () => {
    $("chkUseSession").checked = true;
    $("inputSessionId").value = randomSessionId();
    state.chatHistory = [];
    renderChatMessages();
    updateRollingSummaryCard(null);
    updateScopeBanner();
    loadSessions();
    closeDrawers();
  });

  $("chkUseSession").addEventListener("change", () => {
    $("activeSessionBox").classList.toggle("hidden", !$("chkUseSession").checked);
    updateScopeBanner();
  });

  $("sessionsList").addEventListener("click", async (ev) => {
    const delBtn = ev.target.closest(".btn-del-session");
    if (delBtn) {
      ev.stopPropagation();
      const sid = delBtn.dataset.sid;
      await fetch(`/v1/sessions/${encodeURIComponent(sid)}`, {
        method: "DELETE",
        headers: buildHeaders({ includeJson: false, includeSession: false }),
      });
      if ($("inputSessionId").value.trim() === sid) {
        state.chatHistory = [];
        $("inputSessionId").value = randomSessionId();
        renderChatMessages();
      }
      await loadSessions();
      return;
    }
    const row = ev.target.closest(".session-item");
    if (row) selectSession(row.dataset.sid);
  });

  /* Delegated clicks inside chat stream (starter chips, copy message, copy code, continue truncated generation) */
  $("chatMessages").addEventListener("click", (ev) => {
    const starter = ev.target.closest(".starter-chip");
    if (starter) {
      sendPromptText(starter.dataset.prompt);
      return;
    }
    const copyMsg = ev.target.closest(".msg-copy-btn");
    if (copyMsg) {
      const m = state.chatHistory[Number(copyMsg.dataset.copyIdx)];
      if (m) {
        navigator.clipboard.writeText(m.content);
        toast("Copied message");
      }
      return;
    }
    const copyCode = ev.target.closest(".btn-copy-code");
    if (copyCode) {
      const codeEl = copyCode.closest(".md-code-wrap")?.querySelector("code");
      if (codeEl) {
        navigator.clipboard.writeText(codeEl.textContent);
        toast("Copied code block");
      }
      return;
    }
    const contBtn = ev.target.closest(".btn-continue-gen");
    if (contBtn) {
      const prev = state.chatHistory[Number(contBtn.dataset.idx)];
      if (prev) prev.truncated = false;
      sendPromptText("Continue seamlessly right from where your previous message cut off, without repeating earlier points.");
    }
  });

  document.querySelectorAll(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".mode-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const mode = tab.dataset.mode;
      $("viewChat").classList.toggle("hidden", mode !== "chat");
      $("viewEmbeddings").classList.toggle("hidden", mode !== "embeddings");
      $("viewCompletions").classList.toggle("hidden", mode !== "completions");
    });
  });

  document.querySelectorAll("[data-right-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-right-tab]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const t = btn.dataset.rightTab;
      $("rightParamsPane").classList.toggle("hidden", t !== "params");
      $("rightMemoryPane").classList.toggle("hidden", t !== "memory");
      if (t === "memory") loadMemories();
    });
  });

  $("selectModel").addEventListener("change", () => applySelectedModelInfo(true));
  $("btnResetDefaults").addEventListener("click", () => {
    $("rangeTopP").value = 0.95; $("valTopP").textContent = "0.95";
    $("rangeMaxTokens").value = 512; $("valMaxTokens").textContent = "512";
    $("rangePresPen").value = 0; $("valPresPen").textContent = "0.0";
    $("rangeFreqPen").value = 0; $("valFreqPen").textContent = "0.0";
    $("inputSeed").value = "";
    $("inputStopSeq").value = "";
    $("inputSystemPrompt").value = "";
    applySelectedModelInfo(true);
  });

  const bindSlider = (id, labelId, decimals = 2) => {
    $(id).addEventListener("input", () => {
      $(labelId).textContent = Number($(id).value).toFixed(decimals);
      renderCodeSnippet();
    });
  };
  bindSlider("rangeTemp", "valTemp", 2);
  bindSlider("rangeTopP", "valTopP", 2);
  bindSlider("rangeMaxTokens", "valMaxTokens", 0);
  bindSlider("rangePresPen", "valPresPen", 1);
  bindSlider("rangeFreqPen", "valFreqPen", 1);

  ["selectMemoryMode", "selectHistoryMode", "chkStream", "inputSystemPrompt", "inputSeed", "inputStopSeq", "inputTtlHours"].forEach((id) => {
    $(id).addEventListener("change", renderCodeSnippet);
  });

  $("chatForm").addEventListener("submit", sendChatMessage);
  $("chatInput").addEventListener("input", resizeComposer);
  $("chatInput").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      $("chatForm").requestSubmit();
    }
  });
  $("btnStopStream").addEventListener("click", () => state.abortController?.abort());
  $("btnClearChat").addEventListener("click", () => {
    state.chatHistory = [];
    renderChatMessages();
  });

  $("btnToggleCodeDrawer").addEventListener("click", () => {
    $("codeDrawer").classList.toggle("hidden");
    renderCodeSnippet();
  });
  document.querySelectorAll("[data-snippet]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-snippet]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.activeSnippet = btn.dataset.snippet;
      renderCodeSnippet();
    });
  });
  $("btnCopySnippet").addEventListener("click", () => {
    navigator.clipboard.writeText($("codeSnippetContent").textContent);
    toast("Copied snippet to clipboard");
  });

  $("btnCompactSession").addEventListener("click", compactActiveSession);
  $("btnRefreshMemories").addEventListener("click", loadMemories);
  $("btnSearchMemories").addEventListener("click", searchMemories);
  $("btnAddMemory").addEventListener("click", addManualMemory);

  $("memoriesList").addEventListener("click", async (ev) => {
    const del = ev.target.closest(".btn-del-mem");
    if (!del) return;
    await fetch(`/v1/memories/${encodeURIComponent(del.dataset.mid)}`, {
      method: "DELETE",
      headers: buildHeaders({ includeJson: false, includeSession: false }),
    });
    await loadMemories();
  });

  $("btnWipeUserData").addEventListener("click", async () => {
    const uid = $("inputUserId").value.trim();
    if (!uid || !confirm(`Delete ALL sessions and memories for user '${uid}' in this app?`)) return;
    const res = await fetch(`/v1/users/${encodeURIComponent(uid)}/data`, {
      method: "DELETE",
      headers: buildHeaders({ includeJson: false, includeSession: false }),
    });
    if (res.ok) {
      state.chatHistory = [];
      renderChatMessages();
      updateRollingSummaryCard(null);
      await loadSessions();
      await loadMemories();
      toast(`Wiped data for ${uid}`);
    }
  });

  $("btnRunEmbeddings").addEventListener("click", runEmbeddingsLab);
  $("btnRunCompletion").addEventListener("click", runRawCompletion);
  $("btnRefreshHealth").addEventListener("click", refreshHealth);

  /* Modal events */
  $("btnOpenAppsModal").addEventListener("click", () => {
    $("appsModal").classList.remove("hidden");
    loadAppsList();
  });
  $("btnCloseAppsModal").addEventListener("click", () => $("appsModal").classList.add("hidden"));
  $("inputAdminKey").addEventListener("change", loadAppsList);

  $("createAppForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const appId = $("newAppId").value.trim();
    const name = $("newAppName").value.trim();
    const res = await fetch("/v1/apps", {
      method: "POST",
      headers: buildHeaders({ includeJson: true, isAdmin: true }),
      body: JSON.stringify({ app_id: appId, name }),
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.detail || "Failed registering app");
      return;
    }
    $("newAppId").value = "";
    $("newAppName").value = "";
    showIssuedKey(data.api_key);
    await loadAppsList();
    toast(`Registered ${data.app.app_id}`);
  });

  $("btnCopyIssuedKey").addEventListener("click", () => {
    navigator.clipboard.writeText(state.lastIssuedKey);
    toast("Copied API key");
  });

  $("btnUseIssuedKey").addEventListener("click", () => {
    $("inputApiKey").value = state.lastIssuedKey;
    document.querySelector('[data-auth-tab="apikey"]').click();
    $("appsModal").classList.add("hidden");
    toast("Activated issued API key in Playground");
  });

  $("appsTableWrap").addEventListener("click", async (ev) => {
    const rot = ev.target.closest(".btn-rot-app");
    if (rot) {
      const aid = rot.dataset.aid;
      const res = await fetch(`/v1/apps/${encodeURIComponent(aid)}/rotate-key`, {
        method: "POST",
        headers: buildHeaders({ includeJson: false, isAdmin: true }),
      });
      const data = await res.json();
      if (res.ok) {
        showIssuedKey(data.api_key);
        await loadAppsList();
      } else {
        toast(data.detail || "Rotation failed");
      }
      return;
    }
    const rev = ev.target.closest(".btn-rev-app");
    if (rev) {
      const aid = rev.dataset.aid;
      if (!confirm(`Deactivate API key for '${aid}'?`)) return;
      const res = await fetch(`/v1/apps/${encodeURIComponent(aid)}`, {
        method: "DELETE",
        headers: buildHeaders({ includeJson: false, isAdmin: true }),
      });
      if (res.ok) {
        await loadAppsList();
        toast(`Deactivated ${aid}`);
      }
    }
  });
}

(async function init() {
  wireEvents();
  await refreshHealth();
  await loadModels();
  await verifyAppIdentity();
})();
