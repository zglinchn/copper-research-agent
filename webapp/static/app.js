/* 铜产品减量化模型智能体 — 前端逻辑 */
"use strict";

const state = {
  meta: null,            // {products, llm_mode, pipelines}
  mode: "single",        // single | batch
  selected: [],          // 选中的product_id列表
  currentRunId: null,
  resultTab: "dims",
  streamExpand: null,    // 当前内联展开流式输出的节点；null=全部收起
  streamManual: false,   // 用户是否手动展开/收起过（未操作时自动跟随运行中节点）
  currentRunDetail: null,
  es: null,              // SSE EventSource（token增量推送）
  liveText: {},          // SSE累积的各节点流文本（与后端stream_buf同源）
  typePending: "",       // 打字机待播放字符队列（当前展开节点）
  typingTimer: null,
  pollTimer: null,
  historyTimer: null,
};

const STATUS_TEXT = {
  running: "运行中", waiting_review: "待人工审核", completed: "已完成",
  failed: "失败", cancelled: "已停止", cancelling: "停止中",
  interrupted: "已中断",
};

const STEP_ICONS = {
  start: "👤", supervisor: "🧭", dimension_agent: "📐", structure_agent: "🧱",
  copper_agent: "🟠", critic_agent: "🛡", human_review_gate: "🕓",
  done: "🏁", loader: "📥", reduction_agent: "🔧", quant_agent: "📊",
  integration: "🗂",
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ================= 初始化 ================= */

async function init() {
  state.meta = await fetch("/api/meta").then(r => r.json());
  renderModeBadge();
  renderProducts();
  bindEvents();
  refreshHistory();
  state.historyTimer = setInterval(refreshHistory, 3000);
}

function renderModeBadge() {
  const b = $("mode-badge");
  if (state.meta.llm_mode.startsWith("online")) {
    b.textContent = state.meta.llm_mode === "online+retrieval"
      ? "● 在线LLM + Tavily联网检索" : "● 在线LLM调研";
    b.className = "mode-badge online";
  } else {
    b.textContent = "● 演示模式（占位数据）";
    b.className = "mode-badge demo";
    b.title = "未配置 OPENAI_API_KEY，运行将使用演示占位数据跑通全流程";
  }
}

function renderProducts() {
  const grid = $("product-grid");
  grid.innerHTML = "";
  state.meta.products.forEach(p => {
    const btn = document.createElement("button");
    btn.className = "product-item";
    btn.dataset.pid = p.product_id;
    btn.innerHTML = `<span class="pi-icon">${p.icon}</span><span>${esc(p.product_name)}</span>`;
    btn.onclick = () => {
      if (state.mode === "single") {
        state.selected = [p.product_id];
      } else {
        const i = state.selected.indexOf(p.product_id);
        if (i >= 0) state.selected.splice(i, 1); else state.selected.push(p.product_id);
      }
      syncProductSelection();
      updateHeader();
    };
    grid.appendChild(btn);
  });
  state.selected = [state.meta.products[0].product_id];
  syncProductSelection();
  updateHeader();
}

function syncProductSelection() {
  document.querySelectorAll(".product-item").forEach(el => {
    el.classList.toggle("selected", state.selected.includes(el.dataset.pid));
  });
}

function updateHeader() {
  const p = state.meta.products.find(x => x.product_id === state.selected[0])
    || state.meta.products[0];
  $("product-icon").textContent = p.icon;
  $("run-title").textContent = state.mode === "batch"
    ? "批量运行 · 7类产品调研" : `${p.product_name}多维型号与含铜部位调研`;
}

function bindEvents() {
  document.querySelectorAll(".tab[data-tab]").forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll(".tab[data-tab]").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      state.mode = tab.dataset.tab;
      $("select-hint").textContent = state.mode === "batch" ? "（多选，不选=全部7类）" : "（单选）";
      if (state.mode === "batch") state.selected = [];
      syncProductSelection();
      updateHeader();
    };
  });

  $("btn-run").onclick = startRun;
  $("btn-stop").onclick = stopRun;
  $("btn-clear").onclick = () => {
    $("constraints").value = "";
    state.selected = state.mode === "batch" ? [] : [state.meta.products[0].product_id];
    syncProductSelection(); updateHeader();
  };
  $("btn-approve").onclick = () => submitReview(true);
  $("btn-reject").onclick = () => submitReview(false);
  $("btn-reduction").onclick = startReduction;
}

/* ================= 运行控制 ================= */

async function startRun() {
  let ids = [...state.selected];
  if (state.mode === "batch" && ids.length === 0) {
    ids = state.meta.products.map(p => p.product_id);
  }
  const body = {
    product_ids: ids,
    region: $("region").value.trim() || "中国",
    baseline_year: parseInt($("baseline-year").value, 10) || 2025,
    constraints: $("constraints").value.trim(),
  };
  const res = await fetch("/api/runs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) { alert((await res.json()).detail || "创建运行失败"); return; }
  const data = await res.json();
  if (state.mode === "batch") switchToRun(data.run_ids[0]);
  else switchToRun(data.run_ids[0]);
  setRunningUI(true);
}

function setRunningUI(running) {
  $("btn-run").disabled = running;
  $("btn-stop").hidden = !running;
}

async function stopRun() {
  if (!state.currentRunId) return;
  await fetch(`/api/runs/${state.currentRunId}/cancel`, { method: "POST" });
}

async function submitReview(approved) {
  const body = approved
    ? { approved: true, approved_by: "网页审核" }
    : {
        approved: false,
        rejection_target_agent: $("reject-target").value,
        rejection_note: $("reject-note").value.trim(),
      };
  const res = await fetch(`/api/runs/${state.currentRunId}/review`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) alert((await res.json()).detail || "提交审核失败");
}

async function startReduction() {
  const res = await fetch(`/api/runs/${state.currentRunId}/start_reduction`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ analysis_run_id: "" }),
  });
  if (!res.ok) { alert((await res.json()).detail || "启动失败"); return; }
  const data = await res.json();
  switchToRun(data.run_id);
}

async function switchToRun(runId) {
  state.currentRunId = runId;
  state.streamExpand = null;   // 切换运行后收起流面板并恢复自动跟随
  state.streamManual = false;
  state.currentRunDetail = null;
  closeStream();
  state.liveText = {};
  $("placeholder").hidden = true;
  renderWorkflowSkeleton();
  if (state.pollTimer) clearInterval(state.pollTimer);
  startStream(runId);
  await pollRun();
  state.pollTimer = setInterval(pollRun, 1200);
}

/* ================= SSE 逐字打字机 =================
   服务端 /api/runs/{id}/events 推送token增量；前端把增量放入
   打字机队列按固定节奏逐字渲染，接近ChatGPT观感。
   断线时EventSource自动重连（重连后的init会重置liveText）。 */
function startStream(runId) {
  closeStream();
  state.liveText = {};
  state.typePending = "";
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.es = es;
  es.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "init") {
      state.liveText = msg.buf || {};
      if (state.currentRunDetail) renderRun(state.currentRunDetail);
    } else if (msg.type === "token") {
      // SSE 已经是实时增量；直接更新当前节点，不能再进入第二个打字机队列，
      // 否则同一段 token 会被重复追加。
      state.liveText[msg.node] = (state.liveText[msg.node] || "") + msg.text;
      if (msg.node === state.streamExpand) renderStreamText(msg.node);
    } else if (msg.type === "end") {
      closeStream();
    }
  };
}

function closeStream() {
  if (state.es) { state.es.close(); state.es = null; }
  if (state.typingTimer) { clearInterval(state.typingTimer); state.typingTimer = null; }
  state.typePending = "";
}

function ensureTypingTimer() {
  if (state.typingTimer) return;
  state.typingTimer = setInterval(() => {
    if (!state.es || !state.typePending) return;
    const node = state.streamExpand;
    if (!node) return;  // 未展开时不播放（liveText仍在累积，不丢数据）
    // 自适应吐字速度：积压越多吐越快，避免追赶不上LLM生成速度
    const take = Math.max(2, Math.ceil(state.typePending.length / 60));
    const chunk = state.typePending.slice(0, take);
    state.typePending = state.typePending.slice(take);
    state.liveText[node] = (state.liveText[node] || "") + chunk;
    renderStreamText(node);
  }, 24);
}

/* ================= 轮询与渲染 ================= */

async function pollRun() {
  if (!state.currentRunId) return;
  const res = await fetch(`/api/runs/${state.currentRunId}`);
  if (!res.ok) return;
  const run = await res.json();
  state.currentRunDetail = run;
  renderRun(run);
  const active = ["running", "waiting_review", "queued", "cancelling"].includes(run.status);
  setRunningUI(active && state.currentRunId === run.run_id);
  if (!active) {
    closeStream();  // 终态：停SSE（服务端也会发end，双保险）
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }
}

function renderWorkflowSkeleton() {
  const steps = $("workflow-steps");
  steps.innerHTML = "";
  $("events-card").hidden = false;
  $("events-list").innerHTML = "";
  $("review-card").hidden = true;
  $("result-card").hidden = true;
  $("run-status-line").hidden = false;
  $("run-status-line").textContent = "正在加载运行状态…";
}

function renderRun(run) {
  const scrollState = captureScrollState();
  $("workflow-title").textContent = `✳ 工作流 · ${run.product_name}`;
  const pipeline = state.meta.pipelines[run.graph_type];
  const counts = run.node_counts || {};

  // 结构说明：主管是中枢循环，不是线性步骤（避免"主管还在Running而其它已打勾"的困惑）
  const stepsEl = $("workflow-steps");
  // 签名缓存：步骤区状态无变化时不重建DOM，
  // 避免每轮轮询摧毁打字机正在播放的流文本节点
  const stepSig = JSON.stringify([run.graph_type, counts, run.status,
    run.current_node, run.total_round, state.streamExpand]);
  if (stepsEl.dataset.sig === stepSig) {
    renderStream(run);
  } else {
    stepsEl.dataset.sig = stepSig;
    stepsEl.innerHTML = "";
    const note = document.createElement("div");
    note.className = "workflow-note";
    note.textContent = "结构说明：主管调度是中枢循环——每个专业节点执行完毕后都回到主管进行下一轮分派，"
      + "因此主管会循环多次（↻ 已循环N轮），专业节点完成后打勾，二者并不矛盾。";
    stepsEl.appendChild(note);
    pipeline.forEach(({ node, label }) => {
    const count = counts[node] || 0;
    const running = run.current_node === node && run.status === "running";
    const isHub = node === "supervisor";  // 中枢节点：循环执行，不用线性✓语义
    let cls = "step", statusHtml;
    if (node === "start") {
      cls += " done"; statusHtml = `<span>✓</span>`;
    } else if (node === "done") {
      if (run.status === "completed") { cls += " done"; statusHtml = `<span>✓</span>`; }
      else if (run.status === "failed") { cls += " error"; statusHtml = `<span>失败</span>`; }
      else if (run.status === "cancelled") { cls += " error"; statusHtml = `<span>已停止</span>`; }
      else { cls += " pending"; statusHtml = `<span>—</span>`; }
    } else if (running) {
      cls += " running";
      statusHtml = isHub
        ? `<span>第${count + 1}轮分派中</span><span class="spinner"></span>`
        : `<span>Running</span><span class="spinner"></span>`;
    } else if (count > 0) {
      cls += " done";
      if (isHub) cls += " hub-done";
      statusHtml = isHub
        ? `<span>↻ 已循环${count}轮</span>`
        : `<span>✓${count > 1 ? ` ×${count}` : ""}</span>`;
    } else {
      cls += " pending"; statusHtml = `<span>—</span>`;
    }
    if (node === "human_review_gate" && run.status === "waiting_review") {
      cls += " running";
      statusHtml = `<span>等待审核</span><span class="spinner"></span>`;
    }
    const row = document.createElement("div");
    row.className = cls;
    row.innerHTML = `
      <div class="step-icon">${STEP_ICONS[node] || "•"}</div>
      <div><span class="step-name">${esc(label)}</span>${count > 1 ? `<span class="step-count">已执行${count}次</span>` : ""}</div>
      <div class="step-status">${statusHtml}</div>`;
    // 点击节点行：展开/收起该节点下方的LLM实时流式输出
    row.onclick = () => toggleStream(node);
    if (state.streamExpand === node) row.classList.add("selected");
    stepsEl.appendChild(row);
    // 内联流容器（紧跟在节点行下方，展开时显示）
    const holder = document.createElement("div");
    holder.className = "step-stream";
    holder.dataset.streamNode = node;
    if (state.streamExpand !== node) holder.hidden = true;
    stepsEl.appendChild(holder);
    });
  }

  renderStream(run);

  const statusLine = $("run-status-line");
  statusLine.hidden = false;
  statusLine.textContent =
    `状态：${STATUS_TEXT[run.status] || run.status} ｜ 轮次：${run.total_round ?? 0} ｜ `
    + (run.error ? `错误：${run.error}` : (run.current_node ? `当前节点：${run.current_node}` : "排队中"));

  // 事件日志
  $("events-card").hidden = false;
  const evEl = $("events-list");
  evEl.innerHTML = (run.events || []).slice().reverse().map(e => `
    <div class="event-row">
      <span class="event-time">${esc(e.time)}</span>
      <span class="event-node">${esc(e.label)}</span>
      <span class="event-detail">${esc(e.detail)}</span>
    </div>`).join("") || `<div class="empty">暂无事件</div>`;

  // 人工审核
  if (run.status === "waiting_review" && run.review) {
    $("review-card").hidden = false;
    const r = run.review;
    $("review-summary").innerHTML = `
      <span class="stat-chip">分类维度 <b>${r.n_dimensions}</b> 组</span>
      <span class="stat-chip">功能子系统 <b>${r.n_subsystems}</b> 个</span>
      <span class="stat-chip">含铜部位 <b>${r.n_components}</b> 个</span>
      <span class="stat-chip">历史质询 <b>${r.n_objections}</b> 条</span>
      <span class="stat-chip">校验标记 <b>${r.n_flags}</b> 条</span>`;
    renderReviewBody(run.review_preview || {});
  } else {
    $("review-card").hidden = true;
  }

  // 结果面板
  if (run.result) {
    renderResult(run);
  }
  restoreScrollState(scrollState);
}

/* 轮询会刷新多个区域，但不能打断用户当前阅读位置。 */
function captureScrollState() {
  const right = document.querySelector(".panel-right");
  const reviewBody = $("review-body");
  const events = $("events-list");
  return {
    rightTop: right?.scrollTop || 0,
    reviewTop: reviewBody?.scrollTop || 0,
    eventTop: events?.scrollTop || 0,
  };
}

function restoreScrollState(scrollState) {
  if (!scrollState) return;
  const right = document.querySelector(".panel-right");
  const reviewBody = $("review-body");
  const events = $("events-list");
  if (right) right.scrollTop = Math.min(scrollState.rightTop, right.scrollHeight - right.clientHeight);
  if (reviewBody) reviewBody.scrollTop = Math.min(scrollState.reviewTop, reviewBody.scrollHeight - reviewBody.clientHeight);
  if (events) events.scrollTop = Math.min(scrollState.eventTop, events.scrollHeight - events.clientHeight);
}

/* 节点行内联展开的LLM流式输出：点击节点行展开/收起；
   未手动操作过时自动跟随当前正在产token的节点。 */
function toggleStream(node) {
  state.streamManual = true;
  state.streamExpand = state.streamExpand === node ? null : node;
  if (state.currentRunDetail) renderRun(state.currentRunDetail);
}

function renderStream(run) {
  // 未手动操作过时，自动跟随正在生成token的节点（运行中）
  if (!state.streamManual && run.status === "running" && run.stream_current) {
    state.streamExpand = run.stream_current;
  }
  const pollBuf = run.stream_buf || {};
  document.querySelectorAll(".step-stream").forEach(el => {
    const node = el.dataset.streamNode;
    if (node !== state.streamExpand) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    // SSE活跃时用SSE累积文本，否则回退轮询数据（两者同源）
    const text = state.liveText[node] ?? pollBuf[node] ?? "";
    const live = run.status === "running" && run.stream_current === node;
    renderStreamShell(el, text, live);
  });
}

/* 打字机tick时只更新已展开节点的文本（不重建DOM） */
function renderStreamText(node) {
  const el = document.querySelector(`.step-stream[data-stream-node="${node}"]`);
  if (!el || el.hidden) return;
  const run = state.currentRunDetail;
  const live = !!(run && run.status === "running" && run.stream_current === node);
  renderStreamShell(el, state.liveText[node] || "", live);
}

/* 构建/更新流面板：pre内用三个span的textContent分段渲染
  （JSON骨架暗色、reasoning高亮、尾部暗色），打字机逐字追加时
   只更新textContent不重建DOM，滚动位置与光标动画得以保留。 */
function renderStreamShell(el, raw, live) {
  if (!raw) {
    el.innerHTML = `<div class="stream-empty">该节点暂无LLM流式输出（尚未调用LLM，或为演示模式瞬时生成）</div>`;
    return;
  }
  const previousScroll = el.querySelector(".stream-messages")?.scrollTop || 0;
  // 轮询/SSE 更新会重建 details；先保存用户的展开状态，避免每次更新自动收起。
  const previousDetails = {};
  el.querySelectorAll("details").forEach(details => {
    if (details.classList.contains("evidence-card")) previousDetails.evidence = details.open;
    if (details.classList.contains("stream-raw")) previousDetails.raw = details.open;
  });
  const parsed = parsePartialJson(raw);
  const narrative = streamNarrative(parsed, raw);
  el.innerHTML = `<div class="stream-head">
      <span class="stream-title-text">Agent 工作过程</span>
      <span class="stream-live" ${live ? "" : "hidden"}>● 实时</span>
    </div>
    <div class="stream-messages">
      <div class="agent-message">
        <div class="agent-avatar">✦</div>
        <div class="agent-message-body">
          <div class="agent-message-meta">${esc(streamAgentLabel(el.dataset.streamNode))}</div>
          <div class="agent-message-text">${esc(narrative)}${live ? '<span class="typing-caret">▌</span>' : ""}</div>
        </div>
      </div>
      ${streamEvidenceCard(parsed, raw)}
      <details class="stream-raw">
        <summary>结构化输出${parsed.complete ? "（已解析）" : "（生成中）"}</summary>
        <pre>${esc(raw)}</pre>
      </details>
    </div>`;
  const evidence = el.querySelector("details.evidence-card");
  const rawDetails = el.querySelector("details.stream-raw");
  if (evidence && previousDetails.evidence !== undefined) evidence.open = previousDetails.evidence;
  if (rawDetails && previousDetails.raw !== undefined) rawDetails.open = previousDetails.raw;
  const messages = el.querySelector(".stream-messages");
  if (messages) {
    messages.scrollTop = previousScroll;
    if (live) messages.scrollTop = messages.scrollHeight;
  }
}

function parsePartialJson(raw) {
  try { return { value: JSON.parse(raw), complete: true }; } catch (e) {}
  const reason = raw.match(/"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (reason) {
    try { return { value: { reasoning: JSON.parse(`"${reason[1]}"`) }, complete: false }; }
    catch (e) { return { value: { reasoning: reason[1] }, complete: false }; }
  }
  return { value: null, complete: false };
}

function streamAgentLabel(node) {
  const labels = Object.fromEntries((state.meta?.pipelines?.research || [])
    .concat(state.meta?.pipelines?.reduction || []).map(x => [x.node, x.label]));
  return labels[node] || node || "Agent";
}

function streamNarrative(parsed, raw) {
  if (parsed.value?.reasoning) return parsed.value.reasoning;
  if (!parsed.value) return "正在生成结构化研究结果，并准备进行 Schema 校验……";
  const v = parsed.value;
  const counts = Object.entries(v).filter(([, x]) => Array.isArray(x))
    .map(([k, x]) => `${k} ${x.length} 项`).join("，");
  if (counts) return `已生成${counts}，正在继续补充字段并校验引用与跨字段关系……`;
  return parsed.complete ? "结构化结果已生成，正在完成校验……" : "正在组织结构化结果……";
}

function streamEvidenceCard(parsed, raw) {
  const v = parsed.value;
  const citations = [];
  const walk = x => {
    if (!x || typeof x !== "object") return;
    if (Array.isArray(x)) return x.forEach(walk);
    if (x.url) citations.push({ title: x.title || x.url, url: x.url });
    Object.values(x).forEach(walk);
  };
  walk(v);
  if (!citations.length) return "";
  return `<details class="evidence-card"><summary>已识别 ${citations.length} 条来源</summary>
    <div>${citations.slice(0, 8).map(c => `<a href="${esc(c.url)}" target="_blank" rel="noreferrer">${esc(c.title)}</a>`).join("")}</div></details>`;
}

/* ================= 结果渲染 ================= */

function renderResult(run) {
  $("result-card").hidden = false;
  const result = run.result;
  const isReduction = !!result.reduction_measures;

  const tabs = [];
  if (result.classification_dimensions) {
    tabs.push(["dims", "分类维度体系"], ["tree", "结构分解"], ["copper", "含铜部位"]);
  }
  if (isReduction) {
    tabs.push(["measures", "减量措施"], ["scenarios", "情景定义"], ["traj", "强度路径"]);
  }
  tabs.push(["flags", "质询与校验"], ["log", "交锋日志"], ["json", "JSON"]);

  if (!tabs.some(([k]) => k === state.resultTab)) state.resultTab = tabs[0][0];
  $("result-tabs").innerHTML = tabs.map(([k, label]) =>
    `<button class="tab ${k === state.resultTab ? "active" : ""}" data-rtab="${k}">${label}</button>`).join("");
  document.querySelectorAll("[data-rtab]").forEach(b => {
    b.onclick = () => { state.resultTab = b.dataset.rtab; renderResult(run); };
  });

  const body = $("result-body");
  const t = state.resultTab;
  if (t === "dims") body.innerHTML = renderDims(result.classification_dimensions || []);
  else if (t === "tree") body.innerHTML = renderTree(result);
  else if (t === "copper") body.innerHTML = renderCopper(result.copper_components || []);
  else if (t === "measures") body.innerHTML = renderMeasures(result.reduction_measures || []);
  else if (t === "scenarios") body.innerHTML = renderScenarios(result.scenarios || []);
  else if (t === "traj") body.innerHTML = renderTrajectory(result.trajectory || []);
  else if (t === "flags") body.innerHTML = renderFlags(result);
  else if (t === "log") body.innerHTML = renderLog(result.debate_log || []);
  else if (t === "json") body.innerHTML = `<pre class="json-view">${esc(JSON.stringify(result, null, 2))}</pre>`;

  // 导出链接 + 图B入口（仅调研成果已完成时）
  $("btn-export-xlsx").onclick = () => downloadFile(`/api/runs/${run.run_id}/export.xlsx`);
  $("btn-export-json").onclick = () => downloadFile(`/api/runs/${run.run_id}/result.json`);
  $("btn-export-xlsx").hidden = false;
  $("btn-export-json").hidden = false;
  $("btn-reduction").hidden = !result.classification_dimensions;
}

/* 导出下载：fetch+blob 方式。服务端出错时给出可读提示，
   避免把错误响应存成打不开的 export.xlsx */
async function downloadFile(url) {
  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { msg = (await resp.json()).detail || msg; } catch (e) {}
      alert(`导出失败：${msg}`);
      return;
    }
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = decodeURIComponent((resp.headers.get("Content-Disposition") || "")
      .match(/filename="?([^"]+)"?/)?.[1] || url.split("/").pop());
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) {
    alert(`导出失败：${err.message}`);
  }
}

function renderReviewBody(preview) {
  const body = $("review-body");
  const previousSections = [...body.querySelectorAll("details.review-section")]
    .map(section => section.open);
  const previousSectionScrolls = [...body.querySelectorAll(".review-section-body")]
    .map(sectionBody => sectionBody.scrollTop);
  const previousScrollTop = body.scrollTop;
  if (!preview || Object.keys(preview).length === 0) {
    body.innerHTML = `<div class="empty">预览加载中…</div>`;
    return;
  }
  const section = (open, title, inner) => `
    <details class="review-section" ${open ? "open" : ""}>
      <summary>${title}</summary>
      <div class="review-section-body">${inner}</div>
    </details>`;
  body.innerHTML = [
    section(true, `① 分类维度体系（${(preview.classification_dimensions || []).length} 组，重点检查同轴一致性）`,
      renderDims(preview.classification_dimensions || [])),
    section(false, `② 功能子系统结构树（${(preview.functional_subsystems || []).length} 个）`,
      renderTree(preview)),
    section(false, `③ 含铜部位清单（${(preview.copper_components || []).length} 个）`,
      renderCopper(preview.copper_components || [])),
    section((preview.objections || []).length > 0,
      `④ 质询与校验标记（${(preview.objections || []).length} 条质询 / ${(preview.validation_flags || []).length} 条标记）`,
      renderFlags(preview)),
  ].join("");
  body.querySelectorAll("details.review-section").forEach((section, index) => {
    if (previousSections[index] !== undefined) section.open = previousSections[index];
    const sectionBody = section.querySelector(".review-section-body");
    if (sectionBody && previousSectionScrolls[index] !== undefined) {
      sectionBody.scrollTop = previousSectionScrolls[index];
    }
  });
  body.scrollTop = previousScrollTop;
}

function renderDims(dims) {
  return dims.map(d => `
    <div class="dim-card">
      <h4>${esc(d.dimension_name)}
        <span class="cat-badge">${esc(d.dimension_category)}</span></h4>
      <div class="axis"><b>分类轴：</b>${esc(d.axis_of_variation)}<br>
        <b>正交性：</b>${esc(d.orthogonality_check)}</div>
      <div class="dim-values">
        ${d.values.map(v => `<span class="dim-value" title="${esc(v.axis_conformity_justification)}">
            ${esc(v.value_name)}${v.prevalence_desc ? `<span class="cat">${esc(v.prevalence_desc)}</span>` : ""}
          </span>`).join("")}
      </div>
    </div>`).join("");
}

// CopperAgent对"确认不含铜"的叶子节点，会显式返回一条copper_form==="other"
// 且function_of_copper以"不适用"开头的CopperComponent记录（这是有意为之的
// 设计：不遗漏不提，参见COPPER_AGENT_PERSONA）。这不代表该部件真的含铜，
// 树状视图之前对copper_components数组不加区分地全部打Cu·标签，会让"玻璃/
// EVA/背板"这类被正确判定为不含铜的部件在视觉上显得像是被误判为含铜——
// 这是一次真实实测暴露的展示bug，不是CopperAgent的判断问题。
function isNoCopperPlaceholder(c) {
  return c.copper_form === "other" && String(c.function_of_copper || "").startsWith("不适用");
}

function renderTree(result) {
  const subs = result.functional_subsystems || [];
  const compsByParent = {};
  (result.copper_components || []).forEach(c => {
    (compsByParent[c.parent_subsystem_id] ||= []).push(c);
  });
  const lvl0 = subs.filter(s => !s.parent_subsystem_id);
  const children = (pid) => subs.filter(s => s.parent_subsystem_id === pid);
  const renderNode = (s, depth) => {
    const allCus = compsByParent[s.subsystem_id] || [];
    const cus = allCus.filter(c => !isNoCopperPlaceholder(c));
    const kids = children(s.subsystem_id).map(c => renderNode(c, depth + 1)).join("");
    return `
      <div class="tree-node" style="padding-left:${depth * 26}px">
        <span class="lv">L${s.decomposition_level}</span>
        <span>${esc(s.subsystem_name)}</span>
        <span class="cat-badge">${esc(s.subsystem_id)}</span>
        ${cus.map(c => `<span class="cu-tag" title="${esc(c.function_of_copper)}">Cu·${esc(c.component_name)}</span>`).join("")}
      </div>${kids}`;
  };
  return `<div class="tree">${lvl0.map(s => renderNode(s, 0)).join("")}</div>`;
}

function renderCopper(comps) {
  if (!comps.length) return `<div class="empty">无含铜部位记录</div>`;
  return `<table class="data-table"><thead><tr>
      <th>部件</th><th>所属子系统</th><th>铜形态</th><th>用铜原因</th>
      <th>单位质量</th><th>数据依据</th><th>引用数</th></tr></thead><tbody>
    ${comps.map(c => `<tr>
      <td><b>${esc(c.component_name)}</b><br><span class="cat-badge">${esc(c.component_id)}</span></td>
      <td>${esc(c.parent_subsystem_id)}</td>
      <td>${esc(c.copper_form)}</td>
      <td>${esc(c.function_of_copper)}</td>
      <td>${quantText(c.unit_mass)}</td>
      <td>${esc(c.mass_data_basis)}</td>
      <td>${(c.citations || []).length}</td></tr>`).join("")}
  </tbody></table>`;
}

function renderMeasures(measures) {
  if (!measures.length) return `<div class="empty">无措施记录</div>`;
  return `<table class="data-table"><thead><tr>
      <th>措施</th><th>类别</th><th>机理</th><th>预期降幅</th><th>成熟度</th>
      <th>最早可行年</th><th>工程案例</th><th>权衡</th></tr></thead><tbody>
    ${measures.map(m => `<tr>
      <td><b>${esc(m.measure_name)}</b><br><span class="cat-badge">${esc(m.measure_id)}</span></td>
      <td>${esc(m.mechanism_category)}</td>
      <td>${esc(m.mechanism)}</td>
      <td>${quantText(m.expected_reduction)}</td>
      <td>${esc(m.maturity)}</td>
      <td>${esc(m.earliest_feasible_year)}</td>
      <td>${esc(m.engineering_case ? `${m.engineering_case.project_or_product_name} (${m.engineering_case.year})` : "无")}</td>
      <td>${esc(m.trade_offs || "－")}</td></tr>`).join("")}
  </tbody></table>`;
}

function renderScenarios(scenarios) {
  if (!scenarios.length) return `<div class="empty">无情景记录</div>`;
  return `<table class="data-table"><thead><tr>
      <th>情景</th><th>子类别</th><th>定义</th><th>Δmax</th><th>启动年</th>
      <th>目标年</th><th>实现率(%)</th><th>扩散方式</th></tr></thead><tbody>
    ${scenarios.map(s => `<tr>
      <td><b>${esc(s.scenario_id)}</b> ${esc(s.scenario_name)}</td>
      <td>${esc(s.applicable_scope || "(无)")}</td>
      <td>${esc(s.scenario_definition)}</td>
      <td>${quantText(s.full_implementation_reduction_pct)}</td>
      <td>${esc(s.measure_start_year)}</td>
      <td>${esc(s.target_achievement_year)}</td>
      <td>${esc(s.target_achievement_rate_pct)}</td>
      <td>${esc(s.diffusion_method)}</td></tr>`).join("")}
  </tbody></table>`;
}

function renderTrajectory(points) {
  if (!points.length) return `<div class="empty">无路径数据</div>`;
  const colors = { S0: "#9aa3b2", S1: "#2f6bff", S2: "#e8890c", S3: "#1fa45a" };
  const byScenario = {};
  points.forEach(p => (byScenario[p.scenario_id] ||= []).push(p));
  const xs = [...new Set(points.map(p => p.year))].sort((a, b) => a - b);
  const W = 640, H = 280, PAD = 46;
  const ys = points.map(p => p.unit_copper_intensity?.value ?? 0);
  const yMin = Math.min(...ys, 0), yMax = Math.max(...ys) * 1.08 || 1;
  const X = y => PAD + (y - xs[0]) / Math.max(xs[xs.length - 1] - xs[0], 1) * (W - PAD - 16);
  const Y = v => H - PAD - (v - yMin) / Math.max(yMax - yMin, 1e-9) * (H - PAD - 20);
  const lines = Object.entries(byScenario).map(([sid, pts]) => {
    pts.sort((a, b) => a.year - b.year);
    const d = pts.map(p => `${X(p.year).toFixed(1)},${Y(p.unit_copper_intensity?.value ?? 0).toFixed(1)}`).join(" ");
    return `<polyline points="${d}" fill="none" stroke="${colors[sid] || "#666"}" stroke-width="2.5"/>
      ${pts.map(p => `<circle cx="${X(p.year).toFixed(1)}" cy="${Y(p.unit_copper_intensity?.value ?? 0).toFixed(1)}" r="3.5" fill="${colors[sid] || "#666"}"/>`).join("")}`;
  }).join("");
  const axis = xs.map(y => `<text x="${X(y)}" y="${H - PAD + 18}" font-size="11" fill="#5b6472" text-anchor="middle">${y}</text>`).join("")
    + [yMin, (yMin + yMax) / 2, yMax].map(v =>
      `<text x="${PAD - 8}" y="${Y(v) + 4}" font-size="11" fill="#5b6472" text-anchor="end">${v.toFixed(2)}</text>`).join("");
  const legend = Object.keys(byScenario).map(sid =>
    `<span><span class="sw" style="background:${colors[sid] || "#666"}"></span>${sid}</span>`).join("");
  return `
    <div class="chart-wrap">
      <div class="legend">${legend}</div>
      <svg width="${W}" height="${H}" style="background:#fbfcfe;border:1px solid var(--line);border-radius:8px">
        <line x1="${PAD}" y1="${H - PAD}" x2="${W - 16}" y2="${H - PAD}" stroke="#d8dce4"/>
        <line x1="${PAD}" y1="16" x2="${PAD}" y2="${H - PAD}" stroke="#d8dce4"/>
        ${lines}${axis}
      </svg>
    </div>
    ${renderTrajectoryTable(points)}`;
}

function renderTrajectoryTable(points) {
  return `<table class="data-table"><thead><tr>
      <th>年份</th><th>情景</th><th>子类别</th><th>实现率(%)</th>
      <th>单位铜强度</th><th>较基准ΔCu</th></tr></thead><tbody>
    ${points.slice().sort((a, b) => a.scenario_id.localeCompare(b.scenario_id) || a.year - b.year)
      .map(p => `<tr>
        <td>${esc(p.year)}</td><td><b>${esc(p.scenario_id)}</b></td>
        <td>${esc(p.applicable_scope || "(无)")}</td>
        <td>${esc(p.scenario_realization_rate_pct)}</td>
        <td>${quantText(p.unit_copper_intensity)}</td>
        <td>${quantText(p.delta_vs_baseline)}</td></tr>`).join("")}
  </tbody></table>`;
}

function renderFlags(result) {
  const flags = (result.validation_flags || []).map(f => `
    <div class="objection-item ${f.severity}">
      <div class="meta">校验标记 · ${esc(f.flag_type)} · ${esc(f.severity)}</div>
      ${esc(f.description)}</div>`).join("");
  const objs = (result.objections || []).map(o => `
    <div class="objection-item ${o.severity}">
      <div class="meta">质询 ${esc(o.objection_id)} · ${esc(o.flag_type)} ·
        目标 ${esc(o.target_agent)} · ${o.addressed ? "已回应" : "未回应"}</div>
      ${esc(o.detail)}
      ${o.response_note ? `<div class="meta" style="margin-top:4px">回应：${esc(o.response_note)}</div>` : ""}
    </div>`).join("");
  return (flags + objs) || `<div class="empty">无校验标记与质询记录</div>`;
}

function renderLog(log) {
  if (!log.length) return `<div class="empty">无日志</div>`;
  return log.map(e => `
    <div class="log-item"><span class="who">R${e.round} ${esc(e.speaker)}</span>
      [${esc(e.message_type)}] ${esc(e.summary)}</div>`).join("");
}

function quantText(q) {
  if (!q) return "－";
  return `${q.value ?? "－"} ${q.unit || ""}`;
}

/* ================= 运行历史 ================= */

async function refreshHistory() {
  const res = await fetch("/api/runs");
  if (!res.ok) return;
  const data = await res.json();
  const list = $("history-list");
  if (!data.runs.length) { list.innerHTML = `<div class="empty">暂无运行记录</div>`; return; }
  list.innerHTML = data.runs.map(r => {
    const st = STATUS_TEXT[r.status] || r.status;
    const active = r.run_id === state.currentRunId;
    const icon = { research: "🔍", reduction: "📉" }[r.graph_type] || "•";
    return `<div class="history-item ${active ? "active" : ""}" data-rid="${r.run_id}">
      <span>${icon}</span>
      <span>${esc(r.product_name)} · ${r.graph_type === "research" ? "调研" : "减量化"}</span>
      <span class="st">${esc(st)}</span></div>`;
  }).join("");
  list.querySelectorAll("[data-rid]").forEach(el => {
    el.onclick = () => switchToRun(el.dataset.rid);
  });
}

init();
