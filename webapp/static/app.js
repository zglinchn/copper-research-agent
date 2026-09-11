/* 铜产品减量化模型智能体 — 前端逻辑 */
"use strict";

const state = {
  meta: null,            // {products, llm_mode, pipelines}
  mode: "single",        // single | batch
  selected: [],          // 选中的product_id列表
  customProductName: "", // 用户自定义产品名称
  currentRunId: null,
  activeRunId: null,     // 后台仍处于活动状态的运行（用于启用停止按钮）
  activeRunStatus: null, // 上述运行的状态（用于区分「运行中」与「停止中」）
  resultTab: "dims",
  streamExpand: null,    // 当前内联展开流式输出的节点；null=全部收起
  streamManual: false,   // 用户是否手动展开/收起过（未操作时自动跟随运行中节点）
  currentRunDetail: null,
  es: null,              // SSE EventSource（token增量推送）
  liveText: {},          // SSE累积的各节点流文本（与后端stream_buf同源）
  streamOrder: [],       // 模型实际开始输出的节点顺序
  pollTimer: null,
  historyTimer: null,
  historyFilter: "all",  // all | success | failure
  stopRequested: false,
};

const STATUS_TEXT = {
  running: "运行中", waiting_review: "待人工审核", completed: "已完成",
  failed: "失败", cancelled: "已停止", cancelling: "停止中",
  interrupted: "已中断",
};

/* 仍在推进、可被停止的状态 */
const ACTIVE_STATUSES = ["running", "waiting_review", "queued", "cancelling"];
const CUSTOM_PRODUCT_ID = "__custom__";

/* 执行节点图标：统一线性风格（stroke=currentColor），与概览卡/产品图标同一套语言 */
const svgIcon = (paths) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const GLOBE = '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.2 2.5 3.3 5.4 3.3 8.5S14.2 17.5 12 20.5c-2.2-3-3.3-5.9-3.3-8.5S9.8 5.9 12 3.5z"/>';
/* 停止按钮图标（与 index.html 中的初始标记保持一致） */
const ICON_STOP = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="3"/><rect x="6.2" y="6.2" width="3.6" height="3.6" rx="0.8" fill="currentColor" stroke="none"/></svg>';
const ICON_SPINNER = '<span class="spinner"></span>';
const STEP_ICONS = {
  start: svgIcon('<circle cx="12" cy="12" r="8.5"/><path d="M10.4 9.2l4.6 2.8-4.6 2.8z"/>'),
  supervisor: svgIcon('<circle cx="12" cy="12" r="8.5"/><path d="M16 8l-2.1 5.9L8 16l2.1-5.9z"/>'),
  country_agent: svgIcon(GLOBE),
  country_policy_agent: svgIcon(GLOBE),
  dimension_agent: svgIcon('<path d="M4.5 19.5V4.5M4.5 19.5h15M9 16l3-4.5 2.5 2L19.5 8"/>'),
  structure_agent: svgIcon('<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M4 12h16M12 4v16"/>'),
  copper_agent: svgIcon('<circle cx="12" cy="12" r="6.8"/><circle cx="12" cy="12" r="2.2"/>'),
  critic_agent: svgIcon('<path d="M12 3.5l7 2.8v4.9c0 4.2-2.9 7.6-7 8.8-4.1-1.2-7-4.6-7-8.8V6.3z"/><path d="M9.3 12l1.9 1.9 3.5-3.6"/>'),
  human_review_gate: svgIcon('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.6V12l2.8 1.7"/>'),
  done: svgIcon('<path d="M6.5 3.5v17M6.5 5.2h11.4l-2 3.4 2 3.4H6.5"/>'),
  loader: svgIcon('<path d="M12 3.8v9.4M8.4 10l3.6 3.6L15.6 10M5 18.6h14"/>'),
  reduction_agent: svgIcon('<path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.8-3.8a6 6 0 01-7.9 7.9l-6.9 6.9a2.1 2.1 0 01-3-3l6.9-6.9a6 6 0 017.9-7.9z"/>'),
  quant_agent: svgIcon('<path d="M5.5 20v-4.8M12 20V9.6M18.5 20V4.5"/>'),
  integration: svgIcon('<path d="M3.5 7.6a2 2 0 012-2h3.1l2 2.5h7.9a2 2 0 012 2v7a2 2 0 01-2 2H5.5a2 2 0 01-2-2z"/>'),
  fallback: svgIcon('<circle cx="12" cy="12" r="3.2"/>'),
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/* ISO时间 → "YYYY-MM-DD HH:MM" */
const fmtTime = (iso) => {
  if (!iso) return "";
  const s = String(iso);
  return s.length >= 16 ? s.slice(0, 16).replace("T", " ") : s;
};

/* 空态/占位使用的线性图标（与概览卡、产品图标同一套线条风格） */
const ICON_DOC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7.5A1.5 1.5 0 0 0 6 4.5v15A1.5 1.5 0 0 0 7.5 21h9a1.5 1.5 0 0 0 1.5-1.5V7z"/><path d="M14 3v4h4"/><path d="M9.5 12.5h5M9.5 16h3.5"/></svg>`;
const ICON_SEARCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-3.6-3.6"/><path d="M8.5 11h5"/></svg>`;

/* ================= 初始化 ================= */

async function init() {
  state.meta = await fetch("/api/meta").then(r => r.json());
  await probeProductIcons(state.meta.products);
  renderProducts();
  loadRegionOptions();
  bindEvents();
  refreshHistory();
  state.historyTimer = setInterval(refreshHistory, 3000);
}

/* 产品图标预处理：/static/icons/{id}.png 是「白底线条+alpha」素材，
   存在时用 CSS mask 着色（未选中墨蓝、选中主题蓝）；缺失时回退 emoji，
   避免出现纯色方块。 */
async function probeProductIcons(products) {
  await Promise.all((products || []).map(p => new Promise(resolve => {
    const img = new Image();
    img.onload = () => { p.has_line_icon = true; resolve(); };
    img.onerror = () => { p.has_line_icon = false; resolve(); };
    img.src = `/static/icons/${p.product_id}.png`;
  })));
}

/* 国家下拉：规范国家由后端注册表提供。 */
async function loadRegionOptions() {
  try {
    const data = await fetch("/api/regions").then(r => r.json());
    const dl = $("region-list");
    if (!dl) return;
    dl.innerHTML = (data.countries || [])
      .map(c => `<option value="${esc(c.country_name)}">${esc(c.country_id)} / ${esc(c.gcam_region_id)}</option>`)
      .join("");
  } catch (e) { /* 区域列表不可用时不阻断页面 */ }
}

/* 区域证据与确定性基线卡片（调研 run 的 assessment / 图B run 的基线） */
function renderRegionBaseline(result) {
  const a = result.evidence_assessment;
  const badge = (tier) => ({ sufficient: "badge-ok", partial: "badge-warn",
                             insufficient: "badge-fallback" }[tier] || "badge-warn");
  const tierCn = { sufficient: "证据充足", partial: "部分充足", insufficient: "证据不足" };
  const part = [];
  const g = result.geography;
  if (g) part.push(`<div class="region-card"><h4>国家研究范围</h4>
    <div class="axis"><b>国家：</b>${esc(g.country_name)} (${esc(g.country_id)}) ·
    <b>GCAM区域：</b>${esc(g.gcam_region_id)}</div></div>`);
  const profile = result.country_profile;
  if (profile) part.push(`<div class="region-card"><h4>国家市场与标准画像</h4>
    <div class="axis">${esc(profile.market_structure_summary)}<br>${esc(profile.material_practice_summary)}
    ${(profile.evidence_items || []).map(i => `<div><b>${esc(i.target_id)}：</b>${esc(i.national_finding)} ` +
      (i.citations || []).map(c => c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noreferrer">来源</a>` : "").join(" ") +
      `</div>`).join("")}</div></div>`);
  const measureReview = result.country_measure_review;
  if (measureReview) part.push(`<div class="region-card"><h4>国家措施适用性</h4><div class="axis">` +
    (measureReview.assessments || []).map(a => `<div><b>${esc(a.measure_id)}：</b>` +
      `${a.applicable_in_country ? "适用" : "不适用"} · ${esc(a.adoption_status)} · ${esc(a.national_constraints)} ` +
      (a.citations || []).map(c => c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noreferrer">来源</a>` : "").join(" ") +
      `</div>`).join("") + `</div></div>`);
  if (a) {
    part.push(`<div class="region-card">
      <h4>区域证据评估 <span class="cat-badge ${badge(a.tier)}">${tierCn[a.tier] || a.tier} · ${a.score}分</span></h4>
      <div class="axis"><b>证据口径：</b>${esc(a.evidence_policy)} ·
        区域命中 ${a.n_region_hits}/${a.n_citations} 条引用<br>
        <b>分项：</b>${Object.entries(a.component_scores || {})
          .map(([k, v]) => `${esc(k.split("_")[0])}=${v}`).join(" / ")}<br>
        ${esc(a.summary_note || "")}</div></div>`);
  }
  if (result.model_baselines) {
    const bi = result.baseline_unit_intensity || {};
    part.push(`<div class="region-card">
      <h4>减量前单位铜强度（确定性计算）</h4>
      <div class="axis"><b>功能单位：</b>${esc(result.chosen_functional_unit || "－")} ·
        <b>主基线：</b>${bi.value ?? "－"} ${esc(bi.unit || "")}<br>
        ${esc(result.baseline_calc_method || "")}<br>
        ${"<table class=\"mini-table\"><tr><th>型号</th><th>子类别</th><th>强度</th><th>高铜参考</th></tr>" +
          (result.model_baselines || []).map(b =>
            `<tr><td>${esc(b.value_name)}</td><td>${esc(b.applicable_scope || "(无子类别)")}</td>` +
            `<td>${b.intensity.value} ${esc(b.intensity.unit)}</td>` +
            `<td>${b.is_reference ? "是" : "否"}</td></tr>`).join("") + "</table>"}</div></div>`);
  }
  return part.join("") || "<div class=\"empty\">无区域/基线信息</div>";
}

function renderProducts() {
  const grid = $("product-grid");
  grid.innerHTML = "";
  state.meta.products.forEach(p => {
    const btn = document.createElement("button");
    btn.className = "product-item";
    btn.dataset.pid = p.product_id;
    btn.title = p.product_name;
    // 名称拆成「主名 + 括号副名」两行，避免长名称把卡片撑高或折成三行
    const m = String(p.product_name).match(/^(.+?)[（(]\s*([^）)]*?)\s*[）)]$/);
    const main = m ? m[1] : p.product_name;
    const sub = m ? m[2] : "";
    const icon = p.has_line_icon
      ? `<span class="pi-icon" style="--pi:url('/static/icons/${esc(p.product_id)}.png')"></span>`
      : `<span class="pi-emoji">${p.icon}</span>`;
    btn.innerHTML = icon
      + `<span class="pi-copy"><span class="pi-name">${esc(main)}</span>`
      + (sub ? `<span class="pi-sub">${esc(sub)}</span>` : "")
      + `</span><span class="pi-radio"></span>`;
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
  // 固定放在第 8 个位置：与现有产品卡片保持同样的矩形样式，支持直接输入额外产品。
  const custom = document.createElement("div");
  custom.className = "product-item custom-product-item";
  custom.dataset.pid = CUSTOM_PRODUCT_ID;
  custom.title = "输入额外产品名称";
  custom.innerHTML = `<span class="custom-add-icon">＋</span>
    <span class="custom-input-wrap"><input id="custom-product-name" maxlength="80" placeholder="输入额外产品" aria-label="额外产品名称"></span>
    <span class="pi-radio"></span>`;
  const selectCustom = () => {
    if (state.mode === "single") {
      state.selected = [CUSTOM_PRODUCT_ID];
    } else if (!state.selected.includes(CUSTOM_PRODUCT_ID)) {
      state.selected.push(CUSTOM_PRODUCT_ID);
    }
    syncProductSelection();
    updateHeader();
  };
  custom.onclick = selectCustom;
  const customInput = custom.querySelector("input");
  customInput.value = state.customProductName;
  customInput.onclick = (event) => event.stopPropagation();
  customInput.onfocus = selectCustom;
  customInput.oninput = () => {
    state.customProductName = customInput.value.trim();
    selectCustom();
  };
  grid.appendChild(custom);
  state.selected = [state.meta.products[0].product_id];
  syncProductSelection();
  updateHeader();
}

function syncProductSelection() {
  document.querySelectorAll(".product-item").forEach(el => {
    el.classList.toggle("selected", state.selected.includes(el.dataset.pid));
  });
}

/* 工作区标题跟随当前选择的产品（面包屑与大标题同步） */
function setWorkspaceTitle(text) {
  const h2 = $("workspace-title");
  const crumb = $("workspace-breadcrumb-title");
  if (h2 && h2.textContent !== text) h2.textContent = text;
  if (crumb && crumb.textContent !== text) crumb.textContent = text;
}

function updateHeader() {
  if (!state.meta) return;
  const selectedId = state.selected[0];
  const customName = state.customProductName.trim();
  const p = state.meta.products.find(x => x.product_id === selectedId)
    || state.meta.products[0];
  const productName = selectedId === CUSTOM_PRODUCT_ID
    ? (customName || "额外产品") : p.product_name;
  const batchTitle = state.selected.includes(CUSTOM_PRODUCT_ID)
    ? "批量运行 · 标准产品与额外产品调研" : "批量运行 · 7类产品调研";
  setWorkspaceTitle(state.mode === "batch"
    ? batchTitle : `${productName}多维型号与含铜部位调研`);
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
    state.customProductName = "";
    const customInput = $("custom-product-name");
    if (customInput) customInput.value = "";
    state.selected = state.mode === "batch" ? [] : [state.meta.products[0].product_id];
    syncProductSelection(); updateHeader();
  };
  $("btn-approve").onclick = () => submitReview(true);
  $("btn-reject").onclick = () => submitReview(false);
  $("btn-reduction").onclick = startReduction;
  $("history-filter").onchange = (e) => {
    state.historyFilter = e.target.value;
    refreshHistory();
  };
  $("btn-clear-history").onclick = clearHistory;
  $("btn-delete-selected").onclick = deleteSelectedRun;

  // 补充约束字数统计
  const ta = $("constraints");
  ta.addEventListener("input", () => {
    $("char-counter").textContent = `${ta.value.length}/500`;
  });
}

/* ================= 运行控制 ================= */

async function startRun() {
  const customName = state.customProductName.trim();
  const customSelected = state.selected.includes(CUSTOM_PRODUCT_ID);
  if (customSelected && !customName) {
    alert("请填写额外产品名称");
    $("custom-product-name")?.focus();
    return;
  }
  let ids = state.selected.filter(id => id !== CUSTOM_PRODUCT_ID);
  if (state.mode === "batch" && ids.length === 0) {
    ids = state.meta.products.map(p => p.product_id);
  }
  if (!ids.length && !customName) {
    alert("请至少选择一个产品或填写额外产品");
    return;
  }
  const body = {
    product_ids: ids,
    custom_product_name: customName,
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
  setStopButton(true, false);
}

function setRunningUI(running) {
  $("btn-run").disabled = running;
  if (!running) state.stopRequested = false;
}

/* 停止按钮常驻显示：无进行中的任务时置灰，避免「按钮时有时无、找不到」
   cancelling=true 时显示红色 spinner 并禁止重复点击 */
function setStopButton(enabled, cancelling) {
  const stop = $("btn-stop");
  stop.disabled = cancelling;
  stop.classList.toggle("cancelling", !!cancelling);
  stop.title = cancelling ? "正在停止后台服务" : "停止本地后台服务";
  // 用 innerHTML，textContent 会把按钮里的内联图标冲掉
  stop.innerHTML = cancelling ? `${ICON_SPINNER}停止服务中…` : `${ICON_STOP}停止后台服务`;
}

/* 停止/开始按钮的可用性统一在此同步，由 renderRun 调用，
   覆盖轮询 / 审核 / 停止 / 点历史等任何渲染路径。
   - 开始运行：只跟「当前展示的运行是否活动」绑定；
   - 停止运行：当前展示的运行在跑，或后台仍有活动中的运行（刷新页面后也能停止）；
   - 后端状态已进入 cancelling 时，以状态为准显示「停止中…」。 */
function syncRunControls(run) {
  const shownActive = ACTIVE_STATUSES.includes(run.status) && state.currentRunId === run.run_id;
  if (!shownActive || run.status === "cancelling") state.stopRequested = false;
  const targetStatus = shownActive ? run.status : state.activeRunStatus;
  const stopping = targetStatus === "cancelling" || (shownActive && state.stopRequested);
  setRunningUI(shownActive);
  setStopButton(shownActive || !!state.activeRunId, stopping);
}

async function stopRun() {
  if (!confirm("停止后将关闭本地后台服务，当前运行也会中断。确定停止吗？")) return;
  state.stopRequested = true;
  setStopButton(true, true);
  try {
    const res = await fetch("/api/shutdown", { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "停止后台服务失败");
    }
    showServiceStopped();
  } catch (err) {
    // 服务可能在响应返回前已退出，此时也视为停止成功。
    showServiceStopped();
  }
}

function showServiceStopped() {
  document.body.innerHTML = `<main class="service-stopped">
    <div class="service-stopped-icon">■</div>
    <h1>后台服务已停止</h1>
    <p>本地服务已关闭。重新启动服务后刷新此页面即可继续使用。</p>
  </main>`;
}

async function clearHistory() {
  if (!confirm("将删除所有已结束运行的历史记录及其导出快照；进行中的任务会保留。确定清空吗？")) return;
  const button = $("btn-clear-history");
  button.disabled = true;
  button.textContent = "清空中…";
  try {
    const res = await fetch("/api/runs/history", { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "清空历史失败");
    if (data.removed_ids?.includes(state.currentRunId)) resetRunView();
    await refreshHistory();
  } catch (err) {
    alert(`清空失败：${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "清空历史";
  }
}

async function deleteSelectedRun() {
  if (!state.currentRunId) return;
  if (!confirm("将删除当前选中案例及其关联调研/减量化记录和导出快照。确定删除吗？")) return;
  const button = $("btn-delete-selected");
  button.disabled = true;
  button.textContent = "删除中…";
  try {
    const res = await fetch(`/api/run-groups/${state.currentRunId}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "删除案例失败");
    if (data.removed_ids?.includes(state.currentRunId)) resetRunView();
    await refreshHistory();
  } catch (err) {
    alert(`删除失败：${err.message}`);
  } finally {
    button.textContent = "删除选中";
    await refreshHistory();
  }
}

function resetRunView() {
  state.currentRunId = null;
  state.currentRunDetail = null;
  closeStream();
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  $("placeholder").hidden = false;
  ["review-card", "run-overview-card", "workflow-card", "workflow-detail-card", "events-card", "result-card"]
    .forEach(id => { $(id).hidden = true; });
  setRunningUI(false);
  setStopButton(!!state.activeRunId, state.activeRunStatus === "cancelling");
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
  state.streamOrder = [];
  $("placeholder").hidden = true;
  renderWorkflowSkeleton();
  if (state.pollTimer) clearInterval(state.pollTimer);
  startStream(runId);
  await pollRun();
  state.pollTimer = setInterval(pollRun, 1200);
}

/* ================= SSE 实时token流 =================
   服务端 /api/runs/{id}/events 直接推送模型返回的增量文本。
   断线时EventSource自动重连（重连后的init会恢复已有缓冲）。 */
function startStream(runId) {
  closeStream();
  state.liveText = {};
  state.streamOrder = [];
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.es = es;
  es.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "init") {
      state.liveText = msg.buf || {};
      state.streamOrder = msg.order || [];
      if (state.currentRunDetail) renderRun(state.currentRunDetail);
    } else if (msg.type === "token") {
      // SSE 已经是实时增量，按服务端顺序直接追加，避免二次加工或重复追加。
      state.liveText[msg.node] = (state.liveText[msg.node] || "") + msg.text;
      if (!state.streamOrder.includes(msg.node)) state.streamOrder.push(msg.node);
      if (state.currentRunDetail) {
        state.currentRunDetail.stream_current = msg.node;
        renderExecutionLog(state.currentRunDetail);
      }
      if (msg.node === state.streamExpand) renderStreamText(msg.node);
    } else if (msg.type === "end") {
      closeStream();
    }
  };
}

function closeStream() {
  if (state.es) { state.es.close(); state.es = null; }
}

/* ================= 轮询与渲染 ================= */

async function pollRun() {
  if (!state.currentRunId) return;
  const res = await fetch(`/api/runs/${state.currentRunId}`);
  if (!res.ok) return;
  const run = await res.json();
  state.currentRunDetail = run;
  renderRun(run);
  const active = ACTIVE_STATUSES.includes(run.status);
  syncRunControls(run);
  if (!active) {
    closeStream();  // 终态：停SSE（服务端也会发end，双保险）
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }
}

function renderWorkflowSkeleton() {
  const steps = $("workflow-steps");
  steps.innerHTML = "";
  $("workflow-detail-steps").innerHTML = "";
  $("workflow-card").hidden = false;
  $("workflow-detail-card").hidden = false;
  $("run-overview-card").hidden = true;
  $("events-card").hidden = false;
  $("events-list").innerHTML = "";
  $("review-card").hidden = true;
  // 结果卡随任务一起出现，先展示空态占位
  $("result-card").hidden = false;
  $("result-tabs").hidden = true;
  $("result-tabs").innerHTML = "";
  $("result-body").innerHTML = `<div class="result-empty">
    <div class="re-icon">${ICON_DOC}</div>
    <b>调研结果数据将在运行完成后显示</b>
    <p>工作流正在初始化，请稍候…</p>
  </div>`;
  $("btn-export-xlsx").hidden = true;
  $("btn-export-json").hidden = true;
  $("run-status-line").hidden = true;
}

function renderRun(run) {
  const scrollState = captureScrollState();
  $("workflow-title").textContent = `工作流 · ${run.product_name}`;
  // 标题以「当前展示的运行」为准，避免从历史里点开别的产品时标题对不上
  if (run.product_name) setWorkspaceTitle(`${run.product_name}多维型号与含铜部位调研`);
  syncRunControls(run);
  const stageState = unifiedStageState(run);
  const stepsEl = $("workflow-steps");
  const stepSig = JSON.stringify([run.graph_type, run.status, run.current_node, run.total_round]);
  if (stepsEl.dataset.sig !== stepSig) {
    stepsEl.dataset.sig = stepSig;
    stepsEl.innerHTML = stageState.map((stage, index) => `
      <div class="step ${stage.state}" title="${esc(stage.detail)}">
        <div class="step-icon">${stage.state === "done" ? "✓" : index + 1}</div>
        <span class="step-name">${stage.label}</span>
      </div>
      ${index < stageState.length - 1 ? `<div class="step-connector ${stage.state === "done" ? "done" : ""}"></div>` : ""}`).join("");
  }
  renderUnifiedOverview(run, stageState);
  renderDetailedWorkflow(run);

  // 仅在出错时展示状态行
  const statusLine = $("run-status-line");
  if (run.error) {
    statusLine.hidden = false;
    statusLine.textContent = `错误：${run.error}`;
  } else {
    statusLine.hidden = true;
  }

  // 执行日志改为真实模型输出：不再用节点摘要替代token流。
  $("events-card").hidden = false;
  renderExecutionLog(run);

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

  // 结果面板：有产出渲染结果，否则展示正在生成中的结构化结果预览
  if (run.result) {
    renderResult(run);
  } else {
    renderResultEmpty(run);
  }
  restoreScrollState(scrollState);
}

function renderExecutionLog(run) {
  const el = $("events-list");
  if (!el) return;
  const buffers = { ...(run.stream_buf || {}) };
  Object.entries(state.liveText || {}).forEach(([node, text]) => {
    buffers[node] = text;
  });
  const pipeline = state.meta?.pipelines?.[run.graph_type] || [];
  const preferredOrder = [
    ...(state.streamOrder || []), ...(run.stream_order || []),
    ...pipeline.map(x => x.node),
  ];
  const order = [...new Set(preferredOrder)].filter(node => buffers[node]);
  if (!order.length) {
    const demo = run.llm_mode === "demo"
      ? "当前为演示模式，未配置真实大模型，因此没有真实 token 流；结构化结果仍会在下方实时汇总。"
      : "正在等待大模型返回第一个 token…";
    el.innerHTML = `<div class="stream-log-empty">${esc(demo)}</div>`;
    return;
  }
  const active = ACTIVE_STATUSES.includes(run.status);
  const counts = run.node_counts || {};
  const messages = order.map(node => {
    const live = active && run.stream_current === node;
    const text = buffers[node] || "";
    const rawId = `raw-stream-${node}`;
    return `<section class="conversation-message ${live ? "live" : "done"}">
      <div class="conversation-avatar">AI</div>
      <div class="conversation-body">
        <div class="conversation-head">
          <b>${esc(streamAgentLabel(node))}</b>
          <span>${live ? "● 正在进行" : "✓ 已完成"}</span>
        </div>
        <div class="conversation-text">${esc(live ? liveActivity(node) : completedActivity(node, run, counts[node] || 0))}${live ? '<span class="typing-caret">▌</span>' : ""}</div>
        <div class="conversation-meta">已接收 ${text.length.toLocaleString()} 个可见输出字符</div>
        <details class="stream-raw-inline" id="${rawId}">
          <summary>查看原始模型输出</summary>
          <pre>${esc(text)}</pre>
        </details>
      </div>
    </section>`;
  }).join("");
  el.innerHTML = messages;
  if (active) el.scrollTop = el.scrollHeight;
}

function liveActivity(node) {
  const activities = {
    start: "我正在初始化这次调研任务，确认研究国家、基准年和补充约束。",
    supervisor: "我正在根据当前结果调度下一位分析 Agent，并判断是否需要补充证据。",
    country_agent: "我正在检索目标国家的市场、标准、政策和材料惯例，并整理可核验来源。",
    dimension_agent: "我正在归纳主流型号的分类维度，检查每个取值是否落在对应分类轴上。",
    structure_agent: "我正在拆解产品功能子系统，建立从系统到叶子部件的结构树。",
    copper_agent: "我正在逐个结构节点识别含铜部位，补充铜形态、用铜原因和证据。",
    critic_agent: "我正在审查维度、结构、含铜部位和证据之间的一致性，标记需要修订的问题。",
    region_evidence_gate: "我正在评估区域证据覆盖度，确认国家事实和引用口径是否满足发布条件。",
    human_review_gate: "调研初稿已经形成，我正在等待人工审核后继续下一阶段。",
    loader: "我正在加载已经审核通过的调研成果，准备减量化分析。",
    baseline_agent: "我正在统一功能单位和基线参数，为后续确定性计算准备输入。",
    baseline_calc_node: "我正在用固定程序计算减量前单位铜强度基线。",
    reduction_agent: "我正在检索可落地的减量化措施、工程案例和适用边界。",
    country_policy_agent: "我正在核查减量化措施在目标国家的采用状态、政策限制和工程条件。",
    quant_agent: "我正在定义不同情景的启动年、目标年、实现率和扩散方式。",
    calculation_node: "我正在用确定性程序计算各情景下的铜强度路径。",
    integration: "我正在整合调研证据、减量措施和计算结果，准备生成最终成果。",
  };
  return activities[node] || "我正在处理这一阶段的结构化研究结果。";
}

function completedActivity(node, run, count) {
  const event = [...(run.events || [])].reverse().find(e => e.node === node);
  if (event?.detail) return event.detail;
  const partial = run.partial_result || {};
  const sizes = {
    country_agent: partial.country_profile?.evidence_items?.length,
    dimension_agent: partial.classification_dimensions?.length,
    structure_agent: partial.functional_subsystems?.length,
    copper_agent: partial.copper_components?.length,
    reduction_agent: partial.reduction_measures?.length,
    quant_agent: partial.scenario_params?.length,
  };
  const size = sizes[node];
  return size ? `这一阶段已完成，形成 ${size} 项结构化结果。` : `这一阶段已完成${count > 1 ? `（第 ${count} 次）` : ""}。`;
}

function renderDetailedWorkflow(run) {
  const card = $("workflow-detail-card");
  const stepsEl = $("workflow-detail-steps");
  const live = $("detail-live");
  const pipeline = state.meta.pipelines[run.graph_type] || [];
  const counts = run.node_counts || {};
  card.hidden = false;
  live.hidden = !(run.status === "running" && run.stream_current);

  const sig = JSON.stringify([run.graph_type, counts, run.status, run.current_node, state.streamExpand]);
  if (stepsEl.dataset.sig !== sig) {
    stepsEl.dataset.sig = sig;
    stepsEl.innerHTML = "";
    pipeline.forEach(({ node, label }) => {
      const count = counts[node] || 0;
      const running = run.current_node === node && run.status === "running";
      const isHub = node === "supervisor";
      let stateClass = "pending";
      let status = "待执行";
      if (node === "start") {
        stateClass = "done"; status = "已开始";
      } else if (node === "done") {
        if (run.status === "completed") { stateClass = "done"; status = "已完成"; }
        else if (["failed", "cancelled", "interrupted"].includes(run.status)) { stateClass = "error"; status = STATUS_TEXT[run.status]; }
      } else if (node === "human_review_gate" && run.status === "waiting_review") {
        stateClass = "running"; status = "等待审核";
      } else if (running) {
        stateClass = "running"; status = isHub ? `第${count + 1}轮分派中` : "正在运行";
      } else if (count > 0) {
        stateClass = "done"; status = isHub ? `已调度${count}轮` : (count > 1 ? `已执行${count}次` : "已完成");
      }
      const row = document.createElement("button");
      row.type = "button";
      row.className = `detail-step ${stateClass}${state.streamExpand === node ? " selected" : ""}`;
      row.innerHTML = `
        <span class="detail-icon">${STEP_ICONS[node] || STEP_ICONS.fallback}</span>
        <span class="detail-name">${esc(label)}</span>
        <span class="detail-count">${count > 1 ? `执行 ${count} 次` : ""}</span>
        <span class="detail-status">${stateClass === "done" ? "✓ " : ""}${esc(status)}${stateClass === "running" ? '<span class="spinner"></span>' : ""}</span>`;
      row.onclick = () => toggleStream(node);
      stepsEl.appendChild(row);

      const holder = document.createElement("div");
      holder.className = "step-stream detail-stream";
      holder.dataset.streamNode = node;
      holder.hidden = state.streamExpand !== node;
      stepsEl.appendChild(holder);
    });
  }
  renderStream(run);
}

/* 结果空态：运行中/待审核时结果卡的占位内容 */
function renderResultEmpty(run) {
  $("result-card").hidden = false;
  $("btn-export-xlsx").hidden = true;
  $("btn-export-json").hidden = true;
  renderResultPreview(run);
}

function renderResultPreview(run) {
  const result = run.partial_result || {
    product_id: run.product_id, product_name: run.product_name,
  };
  const isReduction = run.graph_type === "reduction" || !!result.reduction_measures;
  const tabs = [["dims", "分类维度体系"], ["tree", "结构分解"], ["copper", "含铜部件"], ["region", "区域与基线"]];
  if (isReduction) tabs.push(["measures", "减量措施"], ["scenarios", "情景定义"], ["traj", "强度路径"]);
  tabs.push(["flags", "审查记录"], ["json", "JSON"]);
  if (!tabs.some(([key]) => key === state.resultTab)) state.resultTab = tabs[0][0];
  const tabsEl = $("result-tabs");
  tabsEl.hidden = false;
  tabsEl.innerHTML = tabs.map(([key, label]) =>
    `<button class="tab ${key === state.resultTab ? "active" : ""}" data-rtab="${key}">${label}</button>`).join("");
  document.querySelectorAll("[data-rtab]").forEach(button => {
    button.onclick = () => { state.resultTab = button.dataset.rtab; renderResultPreview(run); };
  });
  const progressNote = run.status === "waiting_review"
    ? "产品调研已完成，当前展示人工审核前的结构化结果。"
    : run.status === "running"
      ? "结构化结果正在随节点完成实时汇总，以下内容不是摘要。"
      : "运行已停止，以下为本次已产出的结构化结果。";
  const body = $("result-body");
  const t = state.resultTab;
  let content;
  if (t === "dims") content = result.classification_dimensions?.length ? renderDims(result.classification_dimensions) : previewEmpty("分类维度体系", "等待调研维度规划节点输出");
  else if (t === "tree") content = result.functional_subsystems?.length ? renderTree(result) : previewEmpty("功能子系统结构树", "等待结构分解节点输出");
  else if (t === "copper") content = result.copper_components?.length ? renderCopper(result.copper_components) : previewEmpty("含铜部位清单", "等待含铜部位识别节点输出");
  else if (t === "region") content = result.geography || result.country_profile || result.evidence_assessment ? renderRegionBaseline(result) : previewEmpty("区域与基线", "等待国家研究与区域证据评估节点输出");
  else if (t === "measures") content = result.reduction_measures?.length ? renderMeasures(result.reduction_measures) : previewEmpty("减量措施", "等待减量化调研节点输出");
  else if (t === "scenarios") content = result.scenarios?.length ? renderScenarios(result.scenarios) : previewEmpty("情景定义", "等待情景参数节点输出");
  else if (t === "traj") content = result.trajectory?.length ? renderTrajectory(result.trajectory) : previewEmpty("强度路径", "等待确定性计算节点输出");
  else if (t === "flags") content = renderFlags(result);
  else content = `<pre class="json-view">${esc(JSON.stringify(result, null, 2))}</pre>`;
  body.innerHTML = `<div class="result-live-note">● ${esc(progressNote)}</div>${content}`;
}

function previewEmpty(title, note) {
  return `<div class="result-preview-empty"><div class="re-icon">${ICON_DOC}</div><b>${esc(title)}</b><p>${esc(note)}</p></div>`;
}

/* 阶段状态 → 状态胶囊的文字与配色 */
function setStatusPill(el, state) {
  if (!el) return;
  const map = { done: ["已完成", "success"], running: ["运行中", "running"], error: ["失败", "error"] };
  const [text, tone] = map[state] || ["待开始", "pending"];
  el.textContent = text;
  el.className = `status-pill ${tone}`;
}

function unifiedStageState(run) {
  const stages = [
    { label: "产品调研", detail: "多维型号、结构、含铜部位与证据调研" },
    { label: "人工审核", detail: "审核产品调研成果后进入下一阶段" },
    { label: "减量化", detail: "基线、减量措施与定量建模" },
    { label: "成果导出", detail: "导出已完成的标准 JSON 与正式报告" },
  ].map(x => ({ ...x, state: "pending" }));
  if (run.graph_type === "reduction") {
    stages[0].state = "done";
    stages[1].state = "done";
    stages[2].state = run.status === "completed" ? "done" : run.status === "failed" ? "error" : "running";
    stages[3].state = run.status === "completed" ? "done" : "pending";
  } else {
    stages[0].state = run.status === "waiting_review" || run.status === "completed" ? "done" : run.status === "failed" ? "error" : "running";
    stages[1].state = run.status === "waiting_review" ? "running" : run.status === "completed" ? "done" : "pending";
  }
  return stages;
}

function renderUnifiedOverview(run, stages) {
  const card = $("run-overview-card");
  if (!card) return;
  card.hidden = false;
  const reduction = run.graph_type === "reduction";
  const stopping = run.status === "cancelling";
  const completed = run.status === "completed";
  const failed = ["failed", "cancelled", "interrupted"].includes(run.status);
  const percent = stopping ? null : completed ? 100 : failed ? null : reduction ? 68 : stages[0].state === "done" ? 45 : 18;
  const ring = document.querySelector(".overview-ring");
  ring.className = `overview-ring ${completed ? "done" : (failed || stopping) ? "paused" : ""}`;
  const titles = {
    waiting_review: "等待人工审核",
    cancelling: "正在停止运行",
    failed: "运行失败",
    cancelled: "运行已停止",
    interrupted: "运行已中断",
  };
  $("overview-title").textContent = titles[run.status]
    || (reduction ? "正在进行减量化分析" : "正在进行产品调研");
  $("overview-note").textContent = stopping
    ? "已发出停止信号，将在当前节点执行结束处生效"
    : failed
      ? (run.error ? String(run.error).slice(0, 80) : "可从左侧重新发起运行")
      : reduction ? "基于已批准的产品调研成果" : "调研成果与减量化分析将在同一任务中连续展示";
  $("overview-percent").textContent = percent === null ? "—" : `${percent}%`;
  $("overview-progress-bar").style.width = `${percent === null ? 0 : percent}%`;
  // 阶段状态 → 文字 + 配色（done/running/pending/error 四种，不再一律用运行中色）
  setStatusPill($("research-status"), stages[0].state);
  setStatusPill($("reduction-status"),
    completed ? "done" : failed ? "error" : reduction ? "running" : "pending");
  const reductionButton = $("btn-reduction");
  if (reductionButton) {
    reductionButton.hidden = !(run.graph_type === "research" && run.status === "completed" && !!run.result?.classification_dimensions);
  }
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

/* 实时token到达时只更新已展开节点的文本（不重建DOM） */
function renderStreamText(node) {
  const el = document.querySelector(`.step-stream[data-stream-node="${node}"]`);
  if (!el || el.hidden) return;
  const run = state.currentRunDetail;
  const live = !!(run && run.status === "running" && run.stream_current === node);
  renderStreamShell(el, state.liveText[node] || "", live);
}

/* 构建/更新流面板：保留结构化输出原文，并在token到达时更新可见文本。 */
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
    tabs.push(["dims", "分类维度体系"], ["tree", "结构分解"], ["copper", "含铜部件"]);
  }
  if (result.classification_dimensions || result.evidence_assessment || result.model_baselines) {
    tabs.push(["region", "区域与基线"]);
  }
  if (isReduction) {
    tabs.push(["measures", "减量措施"], ["scenarios", "情景定义"], ["traj", "强度路径"]);
  }
  tabs.push(["flags", "审查记录"], ["log", "交锋日志"], ["json", "JSON"]);

  if (!tabs.some(([k]) => k === state.resultTab)) state.resultTab = tabs[0][0];
  $("result-tabs").hidden = false;
  $("result-tabs").innerHTML = tabs.map(([k, label]) =>
    `<button class="tab ${k === state.resultTab ? "active" : ""}" data-rtab="${k}">${label}</button>`).join("");
  document.querySelectorAll("[data-rtab]").forEach(b => {
    b.onclick = () => { state.resultTab = b.dataset.rtab; renderResult(run); };
  });

  const body = $("result-body");
  const t = state.resultTab;
  if (t === "region") body.innerHTML = renderRegionBaseline(result);
  else if (t === "dims") body.innerHTML = renderDims(result.classification_dimensions || []);
  else if (t === "tree") body.innerHTML = renderTree(result);
  else if (t === "copper") body.innerHTML = renderCopper(result.copper_components || []);
  else if (t === "measures") body.innerHTML = renderMeasures(result.reduction_measures || []);
  else if (t === "scenarios") body.innerHTML = renderScenarios(result.scenarios || []);
  else if (t === "traj") body.innerHTML = renderTrajectory(result.trajectory || []);
  else if (t === "flags") body.innerHTML = renderFlags(result);
  else if (t === "log") body.innerHTML = renderLog(result.debate_log || []);
  else if (t === "json") body.innerHTML = `<pre class="json-view">${esc(JSON.stringify(result, null, 2))}</pre>`;

  // 导出链接 + 减量化入口（仅调研成果已完成时）
  $("btn-export-xlsx").onclick = () => downloadFile(`/api/runs/${run.run_id}/export.xlsx`);
  $("btn-export-json").onclick = () => downloadFile(`/api/runs/${run.run_id}/result.json`);
  $("btn-export-xlsx").hidden = false;
  $("btn-export-json").hidden = false;
  $("btn-reduction").hidden = !(run.graph_type === "research" && run.status === "completed" && result.classification_dimensions);
  $("result-card").hidden = false;
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
    preview.evidence_assessment ? section(true, `⓪ 区域证据与基线`, renderRegionBaseline(preview)) : "",
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

// 与后端 runner.run_outcome 同口径：completed=成功；failed/cancelled/interrupted=失败
function runOutcome(status) {
  if (status === "completed") return "success";
  if (["failed", "cancelled", "interrupted"].includes(status)) return "failure";
  return "running";
}

async function refreshHistory() {
  const res = await fetch("/api/runs");
  if (!res.ok) return;
  const data = await res.json();
  // 记录后台是否仍有活动中的运行：刷新页面后即使没展示它，停止按钮也应可用
  const live = (data.runs || []).filter(r => ACTIVE_STATUSES.includes(r.status))
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  state.activeRunId = live.length ? live[0].run_id : null;
  state.activeRunStatus = live.length ? live[0].status : null;
  if (!state.currentRunDetail) {
    setRunningUI(false);
    setStopButton(!!state.activeRunId, state.activeRunStatus === "cancelling");
  }
  const list = $("history-list");
  const groups = groupRunHistory(data.runs || []);
  const filtered = state.historyFilter === "all"
    ? groups
    : groups.filter(g => runOutcome(g.status) === state.historyFilter);
  const selected = groups.find(g => g.current.run_id === state.currentRunId);
  const deleteButton = $("btn-delete-selected");
  if (deleteButton) {
    deleteButton.disabled = !selected || !["completed", "failed", "cancelled", "interrupted"].includes(selected.status);
  }
  if (!filtered.length) {
    list.innerHTML = `<div class="empty">${data.runs.length ? "该筛选条件下暂无运行记录" : "暂无运行记录"}</div>`;
    return;
  }
  list.innerHTML = filtered.map(g => {
    const active = g.current.run_id === state.currentRunId;
    const pid = g.research.product_id;
    const knownProduct = state.meta?.products.find(p => p.product_id === pid);
    const thumb = knownProduct
      ? `<img class="hi-thumb" src="/static/products/${esc(pid)}.png" alt="">`
      : `<span class="hi-thumb hi-thumb-custom" aria-label="额外产品">＋</span>`;
    const outcome = runOutcome(g.status);
    const pill = outcome === "success"
      ? `<span class="hi-pill success">已完成</span>`
      : outcome === "failure"
        ? `<span class="hi-pill danger">${esc(STATUS_TEXT[g.status] || "失败")}</span>`
        : `<span class="hi-pill running">${esc(STATUS_TEXT[g.status] || "运行中")}</span>`;
    const time = outcome === "success"
      ? `完成时间：${fmtTime(g.reduction?.finished_at || g.research.finished_at) || "－"}`
      : `开始时间：${fmtTime(g.current.created_at || g.research.created_at) || "－"}`;
    const detail = `<div class="hi-meta">${esc(time)}</div>`;
    return `<div class="history-item ${active ? "active" : ""}" data-rid="${g.current.run_id}">
      ${thumb}
      <div class="hi-body">
        <div class="hi-head">
          <span class="hi-title">${esc(g.product_name)} · 调研与减量化</span>
          ${pill}
        </div>
        ${detail}
      </div>
      <span class="hi-chevron">›</span>
    </div>`;
  }).join("");
  list.querySelectorAll("[data-rid]").forEach(el => {
    el.onclick = () => switchToRun(el.dataset.rid);
  });
}

function groupRunHistory(runs) {
  const byId = Object.fromEntries(runs.map(r => [r.run_id, r]));
  const groups = new Map();
  runs.forEach(r => {
    const rootId = r.graph_type === "reduction" && r.parent_run_id ? r.parent_run_id : r.run_id;
    const root = byId[rootId] || r;
    if (!groups.has(rootId)) groups.set(rootId, { research: root, reduction: null, current: root });
    const g = groups.get(rootId);
    if (r.graph_type === "reduction" && (!g.reduction || r.created_at > g.reduction.created_at)) {
      g.reduction = r;
    }
    g.current = r.created_at >= g.current.created_at ? r : g.current;
  });
  return [...groups.values()].map(g => {
    const status = g.reduction?.status && g.reduction.status !== "completed"
      ? g.reduction.status : g.reduction?.status === "completed" ? "completed" : g.research.status;
    return { ...g, status, product_name: g.research.product_name };
  });
}

init();
