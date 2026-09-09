/* 铜产品减量化模型智能体 — 前端逻辑 */
"use strict";

const state = {
  meta: null,            // {products, llm_mode, pipelines}
  mode: "single",        // single | batch
  selected: [],          // 选中的product_id列表
  currentRunId: null,
  resultTab: "dims",
  pollTimer: null,
  historyTimer: null,
};

const STATUS_TEXT = {
  running: "运行中", waiting_review: "待人工审核", completed: "已完成",
  failed: "失败", cancelled: "已停止", cancelling: "停止中",
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
  $("placeholder").hidden = true;
  renderWorkflowSkeleton();
  if (state.pollTimer) clearInterval(state.pollTimer);
  await pollRun();
  state.pollTimer = setInterval(pollRun, 1200);
}

/* ================= 轮询与渲染 ================= */

async function pollRun() {
  if (!state.currentRunId) return;
  const res = await fetch(`/api/runs/${state.currentRunId}`);
  if (!res.ok) return;
  const run = await res.json();
  renderRun(run);
  const active = ["running", "waiting_review", "queued", "cancelling"].includes(run.status);
  setRunningUI(active && state.currentRunId === run.run_id);
  if (!active && state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
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
  $("workflow-title").textContent = `✳ 工作流 · ${run.product_name}`;
  const pipeline = state.meta.pipelines[run.graph_type];
  const counts = run.node_counts || {};

  // 结构说明：主管是中枢循环，不是线性步骤（避免"主管还在Running而其它已打勾"的困惑）
  const stepsEl = $("workflow-steps");
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
    stepsEl.appendChild(row);
  });

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

function renderTree(result) {
  const subs = result.functional_subsystems || [];
  const compsByParent = {};
  (result.copper_components || []).forEach(c => {
    (compsByParent[c.parent_subsystem_id] ||= []).push(c);
  });
  const lvl0 = subs.filter(s => !s.parent_subsystem_id);
  const children = (pid) => subs.filter(s => s.parent_subsystem_id === pid);
  const renderNode = (s, depth) => {
    const cus = compsByParent[s.subsystem_id] || [];
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
