"use strict";

const state = { runs: [], filteredRuns: [], selectedRun: null, activeSection: 6 };
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const val = value => value === null || value === undefined || value === "" ? "－" : esc(value);
const q = value => value && typeof value === "object" && value.value !== undefined
  ? `${esc(value.value)} ${esc(value.unit || "")}`.trim() : val(value);
const firstCitationTitle = item => item?.citations?.[0]?.title || "－";

const SECTIONS = [
  { id: 1, title: "1. 研究对象、边界与口径" },
  { id: 2, title: "2. 主流产品分类与代表型号" },
  { id: 3, title: "3. 含铜部位清单与单位铜强度证据" },
  { id: 4, title: "4. 减量化措施清单" },
  { id: 5, title: "5. 减量化情景定义" },
  { id: 6, title: "6. 单位铜强度变化路径（基准年–2035）" },
];

async function init() {
  try {
    const payload = await fetch("/api/results").then(r => r.json());
    state.runs = payload.runs || [];
    populateFilters(state.runs);
    applyFilters();
  } catch (error) {
    renderError(`成果数据加载失败：${error.message || error}`);
  }
  $("results-filters").addEventListener("submit", event => {
    event.preventDefault();
    applyFilters();
  });
  $("filter-run").addEventListener("change", applyFilters);
}

function populateFilters(runs) {
  const products = [...new Map(runs.map(r => [r.product_id, r.product_name])).entries()];
  const regions = [...new Set(runs.map(r => r.region).filter(Boolean))];
  $("filter-product").innerHTML = `<option value="all">全部产品</option>` + products
    .map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`).join("");
  $("filter-region").innerHTML = `<option value="all">全部区域</option>` + regions
    .map(region => `<option value="${esc(region)}">${esc(region)}</option>`).join("");
}

function applyFilters() {
  const product = $("filter-product").value;
  const region = $("filter-region").value;
  const start = Number($("filter-start").value) || 2000;
  const end = Number($("filter-end").value) || 2100;
  state.filteredRuns = state.runs.filter(run => {
    if (product !== "all" && run.product_id !== product) return false;
    if (region !== "all" && run.region !== region) return false;
    const year = Number(run.baseline_year) || 0;
    return year <= end && year >= start;
  });
  const sortedRuns = sortRuns(state.filteredRuns);
  const requestedRunId = $("filter-run").value;
  const hasRequestedRun = requestedRunId !== "all" && sortedRuns.some(run => run.run_id === requestedRunId);
  const selectedRunId = hasRequestedRun ? requestedRunId : "all";
  renderRecordOptions(sortedRuns, selectedRunId);
  state.selectedRun = hasRequestedRun
    ? sortedRuns.find(run => run.run_id === requestedRunId)
    : sortedRuns[0] || null;
  renderTabs();
  renderSection();
}

function sortRuns(runs) {
  return runs.slice().sort((a, b) => String(b.finished_at || b.created_at || "")
    .localeCompare(String(a.finished_at || a.created_at || "")));
}

function renderRecordOptions(runs, selectedRunId) {
  const select = $("filter-run");
  select.innerHTML = `<option value="all">全部成功记录（默认最新）</option>` + runs.map(run => {
    const time = run.finished_at || run.created_at || "时间未知";
    return `<option value="${esc(run.run_id)}">${esc(run.product_name)} · ${esc(run.region)} · ${esc(time)} · ${esc(run.run_id.slice(0, 8))}</option>`;
  }).join("");
  select.value = selectedRunId === "all" ? "all" : selectedRunId;
}

function renderTabs() {
  $("template-tabs").innerHTML = SECTIONS.map(section => `
    <button class="template-tab ${state.activeSection === section.id ? "active" : ""}"
      type="button" data-section="${section.id}">${section.title}</button>`).join("");
  document.querySelectorAll(".template-tab").forEach(button => {
    button.addEventListener("click", () => {
      state.activeSection = Number(button.dataset.section);
      renderTabs();
      renderSection();
    });
  });
}

function renderSection() {
  const view = $("template-view");
  if (!state.selectedRun) {
    view.innerHTML = `<div class="template-empty"><div><b>暂无可展示的成功成果</b><p>请先成功完成一次减量化全流程，成果页只收录成功记录。</p></div></div>`;
    return;
  }
  const run = state.selectedRun;
  const title = SECTIONS.find(section => section.id === state.activeSection)?.title || "";
  const count = sectionRowCount(run, state.activeSection);
  view.innerHTML = `<div class="subtable-head"><div><h2>${title}</h2><p>${sectionDescription(state.activeSection)}</p></div><div class="subtable-meta">${esc(run.product_name)} · ${esc(run.region)} · 完成于 ${esc(run.finished_at || run.created_at || "时间未知")}</div></div>
    ${renderActiveSection(run, state.activeSection)}
    <div class="page-footer-note">当前子表共 ${count} 条记录。切换顶部子表后，仅替换当前展示内容。</div>`;
}

function sectionDescription(id) {
  return {
    1: "研究对象、边界、功能单位与建模口径。",
    2: "按分类维度展开主流产品型号与代表取值。",
    3: "展示型号、功能子系统、含铜部件与单位铜强度证据。",
    4: "展示减量化措施、作用部件、适用范围与绝对降铜量。",
    5: "展示 S0–S3 情景定义、纳入措施与目标年份。",
    6: "按“年份 × 适用范围/子类别 × 情景代码”展示单位铜强度路径。",
  }[id];
}

function sourceParts(run) {
  const result = run.result || {};
  const research = run.research_output || (result.classification_dimensions ? result : {});
  const reduction = result.reduction_measures || result.trajectory ? result : {};
  return { result, research, reduction };
}

function renderActiveSection(run, id) {
  const { result, research, reduction } = sourceParts(run);
  if (id === 1) return renderSection1(run, research, reduction);
  if (id === 2) return renderSection2(research);
  if (id === 3) return renderSection3(result, research, reduction);
  if (id === 4) return renderSection4(reduction);
  if (id === 5) return renderSection5(reduction);
  return renderSection6(run, reduction);
}

function renderSection1(run, research, reduction) {
  const trajectory = reduction.trajectory || [];
  const years = trajectory.map(row => Number(row.year)).filter(Number.isFinite);
  const end = years.length ? Math.max(...years) : 2035;
  const unit = reduction.chosen_functional_unit || q(reduction.baseline_unit_intensity) || "－";
  const pairs = [
    ["产品名称", run.product_name, "研究区域", run.region || research.region_name],
    ["情景时间范围", `${run.baseline_year || 2025}–${end}`, "单位铜强度口径", unit],
    ["产品系统边界/统计口径", research.system_boundary || "－", "情景作用对象", reduction.scenario_subject || "－"],
    ["减量化的定义", reduction.baseline_calc_method || "－", "关键建模口径", reduction.functional_unit_rationale || "－"],
  ];
  return `<div class="data-scroll"><table class="kv-table"><tbody>${pairs.map(row => `<tr><th>${row[0]}</th><td>${val(row[1])}</td><th>${row[2]}</th><td>${val(row[3])}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderSection2(research) {
  const rows = [];
  (research.classification_dimensions || []).forEach(dimension => {
    (dimension.values || []).forEach(item => {
      rows.push([dimension.dimension_name, dimension.dimension_category, item.value_name,
        item.typical_spec_range, item.manufacturer || item.manufacturers || "－",
        item.market_share?.value, item.market_share_caliber, item.market_share_year]);
    });
  });
  return renderTable(["分类维度名称", "分类维度类别", "型号/取值名称", "关键额定参数/容量", "主要制造商/代表厂商", "市场占有率(%)", "占有率口径", "占有率年份"], rows, ["150px", "125px", "180px", "190px", "170px", "105px", "170px", "95px"]);
}

function renderSection3(result, research, reduction) {
  const rows = [];
  const subsystems = Object.fromEntries((research.functional_subsystems || []).map(item => [item.subsystem_id, item.subsystem_name]));
  (reduction.model_baselines || []).forEach(base => {
    const components = (base.component_weights || []).map(item => `${item.component_name} ${item.mass} ${item.mass_unit}`).join("；");
    rows.push([`${base.value_name}（研究基准）`, base.applicable_scope || "(无子类别)", "全部含铜部件", components || "－", base.component_weights?.[0]?.mass_unit || "－", base.intensity?.value, base.intensity?.unit, firstCitationTitle(base)]);
  });
  if (!rows.length) (research.copper_components || []).forEach(item => {
    const mass = item.unit_mass;
    rows.push([Object.values(item.applies_to_dimension_values || {}).join(" / ") || "全部型号", "(无子类别)", subsystems[item.parent_subsystem_id] || item.parent_subsystem_id, `${item.component_name} ${q(mass)}`, mass?.unit || "－", "－", "－", firstCitationTitle(item)]);
  });
  return renderTable(["型号/取值名称", "适用范围/子类别", "功能子系统/部件名称", "含铜部件清单及含铜量", "部件铜量单位", "该型号的铜使用强度(基准)", "强度单位", "案例/数据来源标题"], rows, ["155px", "125px", "170px", "260px", "110px", "150px", "110px", "190px"]);
}

function renderSection4(reduction) {
  const rows = (reduction.reduction_measures || []).map(item => [item.measure_id, item.mechanism_category, item.measure_name, item.mechanism, (item.target_component_ids || []).join("、"), item.applicable_scope || "(无子类别)", item.expected_reduction?.value, item.expected_reduction?.unit]);
  return renderTable(["措施ID", "减量化类别", "减量化措施", "减量原因", "作用对象(子系统/部件)", "适用范围/子类别", "绝对降铜量", "降铜量单位"], rows, ["80px", "150px", "210px", "230px", "190px", "130px", "110px", "110px"]);
}

function renderSection5(reduction) {
  const rows = (reduction.scenarios || []).map(item => [item.scenario_id, item.scenario_name, item.scenario_definition, item.applicable_scope || "(无子类别)", (item.included_measure_ids || []).join("、") || "无", item.full_implementation_reduction_pct?.value, item.measure_start_year, item.target_achievement_year]);
  return renderTable(["情景代码", "情景名称", "情景定位/定义", "适用范围/子类别", "纳入措施ID", "满实施减量幅度Δmax(%)", "措施启动年", "目标实现年"], rows, ["85px", "130px", "260px", "135px", "130px", "145px", "110px", "110px"]);
}

function renderSection6(run, reduction) {
  const rows = (reduction.trajectory || []).slice().sort((a, b) => Number(a.year) - Number(b.year) || String(a.scenario_id).localeCompare(String(b.scenario_id))).map(item => [item.year, item.applicable_scope || "(无子类别)", item.scenario_id, item.scenario_realization_rate_pct, item.unit_copper_intensity?.value, item.unit_copper_intensity?.unit, item.delta_vs_baseline?.value, Number(item.year) === Number(run.baseline_year) ? "基准年" : ""]);
  return renderTable(["年份", "适用范围/子类别", "情景代码", "情景实现率(%)", "单位铜强度", "强度单位", "较基准ΔCu", "备注"], rows, ["80px", "170px", "100px", "135px", "130px", "125px", "120px", "120px"]);
}

function renderTable(headers, rows, widths) {
  if (!rows.length) return `<div class="template-empty"><div><b>当前子表暂无数据</b><p>该运行尚未产出本节内容，或当前筛选条件没有匹配记录。</p></div></div>`;
  return `<div class="data-scroll"><table class="template-table"><colgroup>${widths.map(width => `<col style="width:${width}">`).join("")}</colgroup><thead><tr>${headers.map(header => `<th>${header}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td class="${cell === "" || cell === null || cell === undefined ? "muted" : ""}">${val(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function sectionRowCount(run, id) {
  const { result, research, reduction } = sourceParts(run);
  if (id === 1) return 8;
  if (id === 2) return (research.classification_dimensions || []).reduce((sum, item) => sum + (item.values || []).length, 0);
  if (id === 3) return reduction.model_baselines?.length || research.copper_components?.length || 0;
  if (id === 4) return (reduction.reduction_measures || []).length;
  if (id === 5) return (reduction.scenarios || []).length;
  return (reduction.trajectory || []).length;
}

function renderError(message) {
  $("template-tabs").innerHTML = "";
  $("template-view").innerHTML = `<div class="template-empty"><div><b>成果页暂时不可用</b><p>${esc(message)}</p></div></div>`;
}

init();
