"""calculate.py 确定性计算层单元测试
==================================================================
覆盖：presence 加权基线、构成权重和=100、归一化换算、同部件连乘/
跨部件加权、三种扩散曲线端点/中点性质、排序违规检出、路径全量行数、
措施绝对降铜量。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calculate
from schemas import (
    BaselineParams, CalculationParams, HighCopperReference, NormalizationFactor,
    QuantValue, ScenarioParams,
)

CITE = {"source_type": "other", "title": "测试引用", "confidence": "low"}


def _comp(cid, mass, presence=None, applies=None, basis="region_specific"):
    return {
        "component_id": cid, "component_name": f"部件{cid}",
        "parent_subsystem_id": "sub-1", "copper_form": "copper_winding",
        "function_of_copper": "导电", "mass_data_basis": "engineering_estimate",
        "unit_mass": {"value": mass, "unit": "kg"},
        "regional_presence": ({"value": presence, "unit": "%"} if presence is not None else None),
        "applies_to_dimension_values": applies or {},
        "region_basis": basis, "citations": [CITE],
    }


def _baseline_params():
    return BaselineParams(
        functional_unit_candidates=["kg/MWp", "kg/台"],
        functional_unit_rationale="测试：按功率归一",
        chosen_functional_unit="kg/MWp",
        normalization_factors=[
            NormalizationFactor(value_id="v1", value_name="型号一", factor=2.0,
                                unit_note="单台额定功率2MWp → 2MWp/台", citations=[CITE]),
        ],
        high_copper_references=[
            HighCopperReference(applicable_scope=None, chosen_value_id="v1",
                                rationale="测试：唯一型号即高铜参考", citations=[CITE]),
        ],
    )


COMPONENTS = [_comp("cu1", 10.0), _comp("cu2", 5.0, presence=60.0)]


def test_presence_weighted_baseline():
    """铝绕组份额40% → presence=60 → 有效质量 5×0.6=3，基线=(10+3)/2=6.5"""
    baselines = calculate.compute_model_baselines(COMPONENTS, _baseline_params())
    assert len(baselines) == 1
    b = baselines[0]
    assert b.intensity.value == pytest.approx(6.5)
    assert b.intensity.unit == "kg/MWp"
    assert b.is_reference and b.applicable_scope is None


def test_component_weights_sum_to_100():
    baselines = calculate.compute_model_baselines(COMPONENTS, _baseline_params())
    ws = [cw.weight_pct for cw in baselines[0].component_weights]
    assert sum(ws) == pytest.approx(100.0)
    assert ws[0] == pytest.approx(10 * 100 / 13, abs=0.01)
    assert ws[1] == pytest.approx(3 * 100 / 13, abs=0.01)


def test_global_fallback_marking():
    comps = [_comp("cu1", 10.0, basis="global_fallback"), _comp("cu2", 5.0)]
    baselines = calculate.compute_model_baselines(comps, _baseline_params())
    b = baselines[0]
    assert b.region_basis == "region_specific"  # 部分fallback→混合口径留痕
    assert "混合口径" in (b.region_basis_note or "")
    comps_all = [_comp("cu1", 10.0, basis="global_fallback")]
    b2 = calculate.compute_model_baselines(comps_all, _baseline_params())[0]
    assert b2.region_basis == "global_fallback"


def _measure(mid, targets, r):
    return {
        "measure_id": mid, "measure_name": mid, "target_component_ids": targets,
        "mechanism": "m", "mechanism_category": "material_substitution",
        "expected_reduction": {"value": r, "unit": "%"}, "maturity": "pilot",
        "earliest_feasible_year": 2027,
        "engineering_case": {"project_or_product_name": "p", "implementing_entity": "e",
                             "year": 2024, "outcome_description": "o",
                             "scale": "pilot_project", "citations": [CITE]},
    }


def test_same_component_multiplicative_and_cross_component_weighting():
    baselines = calculate.compute_model_baselines(COMPONENTS, _baseline_params())
    b = baselines[0]
    # 同部件两措施 10% 与 20% → 连乘 28%
    measures = [_measure("R1", ["cu1"], 10), _measure("R2", ["cu1"], 20)]
    delta = calculate.combine_scenario_delta(b, measures, ["R1", "R2"])
    assert delta == pytest.approx(b.component_weights[0].weight_pct / 100 * 28.0, abs=1e-3)
    # 跨部件：cu1 降10%、cu2 降0 → Δ = w1×10
    measures2 = [_measure("R1", ["cu1"], 10)]
    delta2 = calculate.combine_scenario_delta(b, measures2, ["R1"])
    assert delta2 == pytest.approx(b.component_weights[0].weight_pct / 100 * 10.0, abs=1e-3)


def _sp(sid, included, start=2025, target=2035, rate=100, diffusion="linear"):
    return ScenarioParams(
        scenario_id=sid, scenario_name={"S0": "基准情景", "S1": "普通减量",
                                        "S2": "加速减量", "S3": "深度减量"}[sid],
        applicable_scope=None, scenario_definition=f"{sid}定义",
        included_measure_ids=included, measure_start_year=start,
        target_achievement_year=target, target_achievement_rate_pct=rate,
        diffusion_method=diffusion, scenario_rationale="测试", citations=[CITE],
    )


def test_realization_rate_curves():
    lin = _sp("S1", [], diffusion="linear")
    assert calculate.realization_rate(2025, lin) == 0.0
    assert calculate.realization_rate(2030, lin) == pytest.approx(50.0)
    assert calculate.realization_rate(2035, lin) == 100.0
    stp = _sp("S1", [], diffusion="step")
    assert calculate.realization_rate(2034, stp) == 0.0
    assert calculate.realization_rate(2035, stp) == 100.0
    sc = _sp("S1", [], diffusion="s_curve")
    assert calculate.realization_rate(2025, sc) == 0.0
    assert calculate.realization_rate(2030, sc) == pytest.approx(50.0, abs=1e-6)
    assert calculate.realization_rate(2035, sc) == 100.0
    # 单调性
    rates = [calculate.realization_rate(y, sc) for y in range(2025, 2036)]
    assert all(a <= b for a, b in zip(rates, rates[1:]))


def test_full_calculation_ordering_and_trajectory():
    baselines = calculate.compute_model_baselines(COMPONENTS, _baseline_params())
    measures = [_measure("R1", ["cu1"], 10), _measure("R2", ["cu2"], 20)]
    params = CalculationParams(
        functional_unit="kg/MWp", baseline_year=2025, horizon_year=2035,
        components=COMPONENTS, measures=measures,
        scenario_params=[_sp("S0", [], target=2025, rate=0), _sp("S1", ["R1"]),
                         _sp("S2", ["R1", "R2"]), _sp("S3", ["R1", "R2"])],
        model_baselines=baselines,
    )
    # S2 与 S3 措施组合相同 → Δmax 相等 → 排序违规，trajectory 不产出
    res = calculate.run_full_calculation(params)
    assert res["violations"], "S2==S3 的Δmax 必须被检出为排序违规"
    assert res["trajectory"] == []

    # 修正：S3 纳入全部且 R2 力度更大
    measures[1]["expected_reduction"]["value"] = 20
    params.scenario_params[3] = _sp("S3", ["R1", "R2"])
    params.measures = [_measure("R1", ["cu1"], 10), _measure("R2", ["cu2"], 20),
                       _measure("R3", ["cu1", "cu2"], 15)]
    params.scenario_params[3] = _sp("S3", ["R1", "R2", "R3"])
    res = calculate.run_full_calculation(params)
    assert not res["violations"]
    d = res["deltas"]
    assert d[(None, "S0")] == 0.0
    assert d[(None, "S1")] < d[(None, "S2")] < d[(None, "S3")]
    # 路径全量：11年 × 4情景
    assert len(res["trajectory"]) == 11 * 4
    s0_points = [p for p in res["trajectory"] if p.scenario_id == "S0"]
    base = baselines[0].intensity.value
    assert all(p.unit_copper_intensity.value == pytest.approx(base) for p in s0_points)
    assert all(p.scenario_realization_rate_pct == 0.0 for p in s0_points)


def test_normalization_unit_conversion():
    """归一化换算正确性：部件质量 t、单台规格 → t Cu/MWp_DC 口径"""
    comps = [{
        "component_id": "cu1", "component_name": "焊带",
        "parent_subsystem_id": "sub-1", "copper_form": "copper_foil",
        "function_of_copper": "导电", "mass_data_basis": "teardown_measurement",
        "unit_mass": {"value": 0.5733, "unit": "t"},
        "regional_presence": None, "applies_to_dimension_values": {},
        "region_basis": "region_specific", "citations": [CITE],
    }]
    bp = _baseline_params()
    bp.chosen_functional_unit = "t Cu/MWp_DC"
    bp.normalization_factors[0].factor = 0.000635  # 单兙635Wp → 0.000635 MWp/兙
    bp.normalization_factors[0].unit_note = "单兙额定功率635Wp → 0.000635 MWp/兙"
    baselines = calculate.compute_model_baselines(comps, bp)
    b = baselines[0]
    # 0.5733 t / 0.000635 MWp/兙 = 902.83 t Cu/MWp_DC（换算链路正确性）
    assert b.intensity.value == pytest.approx(0.5733 / 0.000635, rel=1e-4)
    assert b.intensity.unit == "t Cu/MWp_DC"
    assert b.functional_unit == "t Cu/MWp_DC"


def test_cross_dimension_reference_not_excluded():
    """真实缺陷回归：高铜参考选了A维度取值，部件挂在B维度取值上——
    部件不应被排除（否则基线错误为0）"""
    comps = [_comp("cu1", 10.0, applies={"dim-1": "v1"})]
    bp = _baseline_params()
    # 参考配置选的是 dim-2 的取值 v-other（与部件限定维度不同）
    bp.normalization_factors[0].value_id = "v-other"
    bp.high_copper_references[0].chosen_value_id = "v-other"
    dims = [
        {"dimension_id": "dim-1", "values": [{"value_id": "v1", "value_name": "取值一"}]},
        {"dimension_id": "dim-2", "values": [{"value_id": "v-other", "value_name": "参考取值"}]},
    ]
    baselines = calculate.compute_model_baselines(comps, bp, dimensions=dims)
    b = baselines[0]
    assert b.component_weights, "跨维度参考配置不应把部件排除"
    assert b.intensity.value == pytest.approx(5.0)  # 10kg / factor 2.0


def test_same_dimension_other_value_excludes_component():
    """同维度取了别的值 → 部件不适用（排除规则仍生效）"""
    comps = [_comp("cu1", 10.0, applies={"dim-1": "v1"})]
    bp = _baseline_params()  # 参考配置 v1 同在 dim-1，但部件适用于 v1 → 适用
    dims = [{"dimension_id": "dim-1", "values": [{"value_id": "v1", "value_name": "取值一"}]}]
    baselines = calculate.compute_model_baselines(comps, bp, dimensions=dims)
    assert baselines[0].intensity.value == pytest.approx(5.0)
    # 若部件限定在 dim-1 的其它取值 v2 → 排除
    comps2 = [_comp("cu1", 10.0, applies={"dim-1": "v2"})]
    b2 = calculate.compute_model_baselines(comps2, bp, dimensions=dims)[0]
    assert b2.intensity.value == 0.0


def test_unknown_value_id_falls_back_to_all_components():
    """chosen_value_id 不在图A维度值中（value_dim=None）→ 配置性失败，
    兑底回退为全部部件，基线不得错误计为 0（真实线上缺陷回归）"""
    comps = [_comp("cu1", 10.0, applies={"dim-1": "v1"})]
    bp = _baseline_params()
    bp.normalization_factors[0].value_id = "v-ghost"
    bp.high_copper_references[0].chosen_value_id = "v-ghost"
    dims = [{"dimension_id": "dim-1", "values": [{"value_id": "v1", "value_name": "取值一"}]}]
    b = calculate.compute_model_baselines(comps, bp, dimensions=dims)[0]
    assert b.intensity.value == pytest.approx(5.0)
    assert "无法归属图A维度" in b.calc_trace


def test_measure_absolute_reduction():
    baselines = calculate.compute_model_baselines(COMPONENTS, _baseline_params())
    val, unit = calculate.measure_absolute_reduction(_measure("R1", ["cu1"], 10), baselines)
    assert val == pytest.approx(1.0)  # 10kg × 10%
    assert unit == "kg"
