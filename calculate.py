"""确定性计算层（零 LLM）
==================================================================
本模块是"减法算术"的唯一归属：所有可由输入参数唯一确定的数值
（减量前单位铜强度、情景Δmax、逐年实现率、强度路径）都在这里用
闭式公式计算，LLM 只负责提出带引用的参数（BaselineParams/
ScenarioParams/措施 expected_reduction），不负责算数。

核心公式：
  有效铜质量   m_i = unit_mass_i × regional_presence_i/100（presence缺省100）
  型号基线强度 I_v = Σ m_i / factor_v          （factor=单台对应功能单位数）
  构成权重     w_i = m_i / Σ m_j × 100         （同型号内合计=100）
  情景Δmax     Δ = Σ_c w_c × r_c               （r_c=该部件被纳入措施的组合降幅%）
    同部件多措施组合（COMBINE_MODE）：
      multiplicative: r_c = 100 × Π(1 - r_k/100) 的补，即 100×(1-Π(1-r_k/100))
      additive_cap:   r_c = min(Σ r_k, 100)
  逐年实现率   rate(y) 见 realization_rate（linear/s_curve/step）
  强度路径     I(y) = I_base × (1 - Δ/100 × rate(y)/100)

区域区分度的入口：regional_presence 与 market_share 由调研链按区域
标注，因此同一产品在不同区域的基线强度天然不同；证据不足区域采用
全球主流值（region_basis=global_fallback）并在 calc_trace/notes 留痕。
"""

from __future__ import annotations
import math
import re
from typing import Optional

from schemas import (
    BaselineParams, CalculationParams, ComponentWeight, IntensityTrajectoryPoint,
    ModelBaselineIntensity, QuantValue, Scenario, ScenarioParams,
    SCENARIO_CODE_FIXED_NAMES, SCENARIO_SEVERITY_ORDER,
)

# 路径终点年（模板口径：基准年–2035）
HORIZON_YEAR = 2035
# 同部件多措施组合规则："multiplicative"（默认，连乘）或 "additive_cap"（加性封顶100）
COMBINE_MODE = "multiplicative"
# s_curve logistic 陡度：k = S_CURVE_STEEPNESS/跨度，窗口端点≈4.7%/95.3%归一化
S_CURVE_STEEPNESS = 6.0


# ============================================================
# 基础取值辅助
# ============================================================

def presence_pct(comp: dict) -> float:
    """区域存在率：regional_presence 缺省视为 100%"""
    rp = comp.get("regional_presence") or {}
    v = rp.get("value")
    return float(v) if v is not None else 100.0


def effective_mass(comp: dict) -> tuple[float, str]:
    """有效铜质量 = unit_mass × presence/100，返回(值, 单位)"""
    um = comp.get("unit_mass") or {}
    mass = float(um.get("value") or 0.0) * presence_pct(comp) / 100.0
    return mass, (um.get("unit") or "kg")


def build_value_dimension_map(dimensions: list[dict]) -> dict[str, str]:
    """value_id -> dimension_id 映射（由图A的 classification_dimensions 构建）"""
    m: dict[str, str] = {}
    for d in dimensions or []:
        for v in d.get("values") or []:
            m[v.get("value_id")] = d.get("dimension_id")
    return m


def components_for_value(components: list[dict], value_id: str,
                         value_dim: str | None = None) -> list[dict]:
    """适用于某型号取值的部件。

    匹配规则：
      - 部件 applies 为空（全部取值通用）→ 适用；
      - value_id 直接出现在部件 applies 的取值中 → 适用；
      - 部件限定的维度与参考取值所在维度不同 → 部件对该维度不敏感，适用
        （型号=多维度取值组合，参考配置只选定单一维度取值时，其它维度的
         部件限定不应把部件排除——否则基线会错误地计为 0）；
      - 部件与参考取值同维度但取了其它值 → 不适用。"""
    out = []
    for c in components:
        applies = c.get("applies_to_dimension_values") or {}
        if not applies or value_id in applies.values():
            out.append(c)
        elif value_dim is not None and value_dim not in applies:
            out.append(c)
    return out


def _round(x: float, nd: int = 6) -> float:
    return round(x, nd)


# ============================================================
# 减量前单位铜强度（模板 Section 3）
# ============================================================

def compute_model_baselines(components: list[dict], baseline_params,
                            dimensions: list[dict] | None = None) -> list[ModelBaselineIntensity]:
    """按(型号取值×子类别)确定性计算减量前单位铜强度。

    baseline_params 可为 BaselineParams 或其 model_dump dict。
    dimensions 为图A的 classification_dimensions dump（用于部件适用的
    维度感知匹配，防止跨维度参考配置把部件错误排除）。
    产出规则：
      - 每个 HighCopperReference(scope, chosen_value) → 一条 is_reference=True 的行；
      - 有归一化因子但未被任何 scope 选为参考的型号 → 一条 scope=None 的普通行；
      - region_basis：行内全部有效部件均为 global_fallback 时该行标
        global_fallback，部分为 fallback 时保持 region_specific 但在
        region_basis_note 列明混合口径。
    """
    if isinstance(baseline_params, dict):
        baseline_params = BaselineParams.model_validate(baseline_params)
    factors = {f.value_id: f for f in baseline_params.normalization_factors}
    refs = list(baseline_params.high_copper_references)
    chosen_values = {r.chosen_value_id for r in refs}

    entries: list[ModelBaselineIntensity] = []
    fu = baseline_params.chosen_functional_unit
    value_dim = build_value_dimension_map(dimensions)
    for ref in refs:
        entries.append(_build_entry(components, factors, ref.chosen_value_id,
                                    ref.applicable_scope, is_reference=True, ref=ref,
                                    functional_unit=fu, value_dim=value_dim.get(ref.chosen_value_id)))
    for value_id in factors:
        if value_id not in chosen_values:
            entries.append(_build_entry(components, factors, value_id, None,
                                        is_reference=False, ref=None, functional_unit=fu,
                                        value_dim=value_dim.get(value_id)))
    # 稳定排序：scope(None最后) → value_id
    entries.sort(key=lambda e: (e.applicable_scope is not None,
                                e.applicable_scope or "", e.value_id))
    return entries


def _build_entry(components, factors, value_id, scope, is_reference, ref,
                 functional_unit: str = "", value_dim: str | None = None) -> ModelBaselineIntensity:
    factor_obj = factors[value_id]
    comps = components_for_value(components, value_id, value_dim)
    fallback_note = ""
    if not comps and value_dim is None:
        # 兜底：chosen_value_id 无法归属到任何图A维度（配置性失败）时，
        # 维度排除规则不可信——回退为通用部件，仍无则回退全部部件，
        # 保证基线不因匹配规则而错误计为 0。
        # （value_dim 已知时的空结果属于真实排除——如铝绕组参考不计入
        #   铜绕组部件——保持语义不变）
        comps = [c for c in components
                 if not (c.get("applies_to_dimension_values") or {})] or components
        fallback_note = "（参考取值无法归属图A维度，已回退为全部含铜部件口径）"
    masses = []
    for c in comps:
        m, unit = effective_mass(c)
        masses.append((c, m, unit))
    total = sum(m for _, m, _ in masses)

    weights: list[ComponentWeight] = []
    running = 0.0
    for idx, (c, m, unit) in enumerate(masses):
        if total > 0:
            w = _round(m / total * 100.0, 4) if idx < len(masses) - 1 else _round(100.0 - running, 4)
            running = _round(running + w, 4)
        else:
            w = 0.0
        weights.append(ComponentWeight(
            component_id=c.get("component_id", ""),
            component_name=c.get("component_name", ""),
            mass=_round(m, 6), mass_unit=unit, weight_pct=w,
        ))

    intensity_value = _round(total / factor_obj.factor, 6) if factor_obj.factor else 0.0
    # 强度单位直接采用选定的功能单位口径（如 kg/MWp、t Cu/MWp_DC），
    # 避免从自由文本 unit_note 里脆弱地解析单位
    unit = functional_unit or _intensity_unit(masses, factor_obj)

    fallback_ids = [c.get("component_id") for c, m, _ in masses
                    if m > 0 and c.get("region_basis") == "global_fallback"]
    all_fallback = bool(fallback_ids) and len(fallback_ids) == len([m for _, m, _ in masses if m > 0])
    region_basis = "global_fallback" if all_fallback else "region_specific"
    region_note = None
    if fallback_ids and not all_fallback:
        region_note = f"混合口径：部件{fallback_ids}采用全球主流替代值，其余为区域证据"
    elif all_fallback:
        region_note = "区域证据不足，整行采用全球主流型号替代"

    trace = (f"intensity = Σ(unit_mass×presence/100)/factor = "
             f"{_round(total, 6)}/{factor_obj.factor} = {intensity_value}；"
             f"factor口径: {factor_obj.unit_note}；部件数: {len(masses)}{fallback_note}")
    citations = list(factor_obj.citations)
    note = None
    if ref is not None:
        note = f"高铜参考配置选择理由：{ref.rationale}"
        citations += list(ref.citations)
    return ModelBaselineIntensity(
        value_id=value_id, value_name=factor_obj.value_name,
        applicable_scope=scope, functional_unit=functional_unit,
        intensity=QuantValue(value=intensity_value, unit=unit),
        component_weights=weights, is_reference=is_reference,
        high_copper_reference_note=note, region_basis=region_basis,
        region_basis_note=region_note, calc_trace=trace, citations=citations,
    )


def _intensity_unit(masses, factor_obj) -> str:
    """兜底单位解析：从 unit_note（如'单台额定功率2MWp → 2MWp/台'）取
    箭头后分式的分子作为功能单位词，拼成 质量单位/功能单位。"""
    mass_unit = masses[0][2] if masses else "kg"
    note = factor_obj.unit_note or ""
    tail = note.split("→")[-1] if "→" in note else note
    fu = tail.split("/")[0].strip() if "/" in tail else ""
    fu = re.sub(r"^[0-9.]+", "", fu)  # '2MWp' → 'MWp'
    return f"{mass_unit}/{fu}" if fu else mass_unit


def compute_baseline_intensity(model_baselines: list[ModelBaselineIntensity]) -> dict[Optional[str], QuantValue]:
    """每个子类别的基线强度：优先取 is_reference 行；无参考行时取强度最大者
    （高铜参考原则），供情景Δmax与路径计算使用。"""
    out: dict[Optional[str], QuantValue] = {}
    scopes = {b.applicable_scope for b in model_baselines}
    for scope in scopes:
        entries = [b for b in model_baselines if b.applicable_scope == scope]
        refs = [b for b in entries if b.is_reference]
        pick = refs[0] if refs else max(entries, key=lambda b: b.intensity.value)
        out[scope] = pick.intensity
    return out


# ============================================================
# 情景 Δmax（模板 Section 5）
# ============================================================

def combine_component_reduction(reductions_pct: list[float]) -> float:
    """同部件多措施组合降幅：默认连乘 Π(1-r/100) 取补；可切换加性封顶。"""
    if not reductions_pct:
        return 0.0
    if COMBINE_MODE == "additive_cap":
        return min(sum(reductions_pct), 100.0)
    remain = 1.0
    for r in reductions_pct:
        remain *= (1.0 - r / 100.0)
    return 100.0 * (1.0 - remain)


def combine_scenario_delta(baseline, measures: list[dict],
                           included_measure_ids: list[str]) -> float:
    """Δmax = Σ_c w_c/100 × r_c：按参考配置的部件质量权重加权组合降幅。
    baseline 可为 ModelBaselineIntensity 或其 model_dump dict。"""
    if isinstance(baseline, dict):
        from schemas import ModelBaselineIntensity as _MBI
        baseline = _MBI.model_validate(baseline)
    by_id = {m.get("measure_id"): m for m in measures}
    delta = 0.0
    for cw in baseline.component_weights:
        rs = []
        for mid in included_measure_ids:
            m = by_id.get(mid)
            if not m:
                continue
            if cw.component_id in (m.get("target_component_ids") or []):
                er = (m.get("expected_reduction") or {}).get("value")
                if er is not None:
                    rs.append(float(er))
        delta += cw.weight_pct / 100.0 * combine_component_reduction(rs)
    return _round(delta, 4)


def assemble_scenarios(scenario_params: list[ScenarioParams],
                       deltas: dict[tuple[Optional[str], str], float]) -> list[Scenario]:
    """把 ScenarioParams + 计算Δmax 组装为完整 Scenario（Δmax 不再由 LLM 给）。"""
    out = []
    for sp in scenario_params:
        dmax = deltas.get((sp.applicable_scope, sp.scenario_id), 0.0)
        out.append(Scenario(
            scenario_id=sp.scenario_id,
            scenario_name=SCENARIO_CODE_FIXED_NAMES[sp.scenario_id],
            applicable_scope=sp.applicable_scope,
            scenario_definition=sp.scenario_definition,
            included_measure_ids=list(sp.included_measure_ids),
            full_implementation_reduction_pct=QuantValue(value=dmax, unit="%"),
            measure_start_year=sp.measure_start_year,
            target_achievement_year=sp.target_achievement_year,
            target_achievement_rate_pct=sp.target_achievement_rate_pct,
            diffusion_method=sp.diffusion_method,
            scenario_rationale=sp.scenario_rationale,
            notes=sp.notes,
            region_basis="region_specific",
            citations=list(sp.citations),
        ))
    return out


def check_ordering(scenarios: list[Scenario]) -> list[str]:
    """计算后Δmax的排序/完整性/重复预检，返回违规描述列表（空=通过）。"""
    violations: list[str] = []
    by_scope: dict[Optional[str], list[Scenario]] = {}
    for s in scenarios:
        by_scope.setdefault(s.applicable_scope, []).append(s)
    for scope, items in by_scope.items():
        label = scope or "(无子类别)"
        ids = [s.scenario_id for s in items]
        dups = {i for i in ids if ids.count(i) > 1}
        if dups:
            violations.append(f"子类别「{label}」情景代码重复: {sorted(dups)}")
            continue
        missing = [c for c in SCENARIO_SEVERITY_ORDER if c not in ids]
        if missing:
            violations.append(f"子类别「{label}」缺少情景: {missing}")
            continue
        vals = [next(s.full_implementation_reduction_pct.value for s in items
                     if s.scenario_id == c) for c in SCENARIO_SEVERITY_ORDER]
        if not (vals[0] < vals[1] < vals[2] < vals[3]):
            violations.append(
                f"子类别「{label}」计算后Δmax未严格递增: "
                f"S0={vals[0]} S1={vals[1]} S2={vals[2]} S3={vals[3]}，"
                f"请调整措施组合或参数（不是修改数字）")
    return violations


# ============================================================
# 逐年实现率与强度路径（模板 Section 6）
# ============================================================

def realization_rate(year: int, sp: ScenarioParams) -> float:
    """扩散曲线闭式公式：linear/s_curve(logistic)/step；other 按 linear 兜底。"""
    start, target = sp.measure_start_year, sp.target_achievement_year
    rate = sp.target_achievement_rate_pct
    if year <= start:
        return 0.0
    if year >= target:
        return float(rate)
    span = max(target - start, 1)
    if sp.diffusion_method == "step":
        return 0.0
    if sp.diffusion_method == "s_curve":
        mid = (start + target) / 2.0
        k = S_CURVE_STEEPNESS / span
        f = lambda y: 1.0 / (1.0 + math.exp(-k * (y - mid)))  # noqa: E731
        norm = (f(year) - f(start)) / (f(target) - f(start))
        return _round(rate * norm, 4)
    return _round(rate * (year - start) / span, 4)  # linear / other 兜底


def build_trajectory(params: CalculationParams,
                     baselines_by_scope: dict[Optional[str], QuantValue],
                     deltas: dict[tuple[Optional[str], str], float]) -> list[IntensityTrajectoryPoint]:
    """逐年×逐情景×逐子类别全量路径：I(y) = I_base × (1 - Δ/100 × rate/100)。"""
    points = []
    horizon = params.horizon_year or HORIZON_YEAR
    for sp in params.scenario_params:
        base = baselines_by_scope.get(sp.applicable_scope)
        if base is None:
            continue
        dmax = deltas.get((sp.applicable_scope, sp.scenario_id), 0.0)
        for year in range(params.baseline_year, horizon + 1):
            rate = realization_rate(year, sp)
            intensity = base.value * (1.0 - dmax / 100.0 * rate / 100.0)
            points.append(IntensityTrajectoryPoint(
                year=year, scenario_id=sp.scenario_id,
                applicable_scope=sp.applicable_scope,
                scenario_realization_rate_pct=rate,
                unit_copper_intensity=QuantValue(value=_round(intensity, 6), unit=base.unit),
                functional_unit=params.functional_unit,
                delta_vs_baseline=QuantValue(value=_round(intensity - base.value, 6), unit=base.unit),
                applied_measure_ids=list(sp.included_measure_ids),
            ))
    points.sort(key=lambda p: (p.scenario_id, p.applicable_scope or "", p.year))
    return points


# ============================================================
# 措施绝对降铜量（模板 Section 4 列）
# ============================================================

def measure_absolute_reduction(measure: dict,
                               model_baselines) -> tuple[float, str]:
    """绝对降铜量 = Σ(参考配置中目标部件有效质量 × expected_reduction/100)。

    取措施 applicable_scope 对应的参考基线；无匹配 scope 时退到 scope=None。
    model_baselines 可为 ModelBaselineIntensity 列表或其 model_dump dict 列表。
    """
    def g(b, key, default=None):
        return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)

    scope = measure.get("applicable_scope")
    refs = [b for b in model_baselines if g(b, "is_reference") and g(b, "applicable_scope") == scope]
    if not refs:
        refs = [b for b in model_baselines if g(b, "is_reference") and g(b, "applicable_scope") is None]
    if not refs:
        refs = list(model_baselines)
    r = (measure.get("expected_reduction") or {}).get("value")
    if r is None or not refs:
        return 0.0, "kg"
    targets = set(measure.get("target_component_ids") or [])
    total = 0.0
    unit = "kg"
    for b in refs[:1]:
        for cw in g(b, "component_weights") or []:
            cid = cw.get("component_id") if isinstance(cw, dict) else cw.component_id
            if cid in targets:
                total += (cw.get("mass") if isinstance(cw, dict) else cw.mass) * float(r) / 100.0
                unit = cw.get("mass_unit") if isinstance(cw, dict) else cw.mass_unit
    return _round(total, 6), unit


def run_full_calculation(params: CalculationParams) -> dict:
    """一站式确定性计算：基线→Δmax→情景→路径→排序预检。

    返回 {model_baselines 已在params中, baselines_by_scope, deltas,
          scenarios, trajectory, violations, calc_trace}。
    """
    baselines_by_scope = compute_baseline_intensity(params.model_baselines)
    deltas: dict[tuple[Optional[str], str], float] = {}
    for sp in params.scenario_params:
        base_entry = next((b for b in params.model_baselines
                           if b.applicable_scope == sp.applicable_scope and b.is_reference), None)
        if base_entry is None:
            entries = [b for b in params.model_baselines
                       if b.applicable_scope == sp.applicable_scope]
            base_entry = max(entries, key=lambda b: b.intensity.value) if entries else None
        if base_entry is None:
            continue
        deltas[(sp.applicable_scope, sp.scenario_id)] = combine_scenario_delta(
            base_entry, params.measures, sp.included_measure_ids)
    scenarios = assemble_scenarios(params.scenario_params, deltas)
    violations = check_ordering(scenarios)
    trajectory = build_trajectory(params, baselines_by_scope, deltas) if not violations else []
    trace = (f"calculate.run_full_calculation: baselines={len(params.model_baselines)}, "
             f"scenarios={len(scenarios)}, trajectory={len(trajectory)}, "
             f"combine_mode={COMBINE_MODE}, horizon={params.horizon_year}")
    return {
        "baselines_by_scope": baselines_by_scope,
        "deltas": deltas,
        "scenarios": scenarios,
        "trajectory": trajectory,
        "violations": violations,
        "calc_trace": trace,
    }
