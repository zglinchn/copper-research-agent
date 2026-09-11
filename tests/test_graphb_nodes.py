"""图B 节点级单元测试（不发起整图运行）
==================================================================
直接调用 baseline_calc_node / calculation_node / integration_node /
supervisor 的确定性路由分支，验证：
  - 基线计算节点产出 model_baselines 与主基线强度；
  - 计算节点在排序违规时打回 quant_agent、通过时产出 scenarios/trajectory；
  - integration 缺产出即报错（消灭 0kg 硬编码兜底）；
  - supervisor 在 critic 审查通过后强制路由到确定性计算节点。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import reduction_graph as rg
from schemas import Citation

CITE = Citation(source_type="other", title="测试引用", confidence="low").model_dump()


def _research():
    return {
        "product_id": "pv_station", "product_name": "光伏电站",
        "region_id": "China", "region_name": "中国",
        "research_version": 1, "research_status": "approved",
        "classification_dimensions": [{
            "dimension_id": "dim-1", "dimension_name": "技术路线",
            "dimension_category": "technology_route",
            "axis_of_variation": "不同电池技术", "definition_rationale": "r",
            "orthogonality_check": "o",
            "values": [{"value_id": "v1", "value_name": "型号一",
                        "axis_conformity_justification": "j", "citations": [CITE]}],
            "evidence": [CITE],
        }],
        "functional_subsystems": [{
            "subsystem_id": "sub-1", "subsystem_name": "核心系统",
            "decomposition_level": 0, "function_description": "f", "evidence": [CITE],
        }],
        "copper_components": [{
            "component_id": "cu1", "parent_subsystem_id": "sub-1",
            "component_name": "绕组", "copper_form": "copper_winding",
            "function_of_copper": "导电", "mass_data_basis": "engineering_estimate",
            "unit_mass": {"value": 10, "unit": "kg"}, "citations": [CITE],
        }],
    }


def _state(**over):
    base = {
        "product_id": "pv_station", "region_id": "China", "region_name": "中国",
        "evidence_policy": "region_specific",
        "research_output": _research(), "analysis_run_id": "run-test",
        "baseline_year": 2025, "stage": "baseline_quantification",
        "baseline_params": None, "model_baselines": [], "baseline_calc_method": "",
        "reduction_measures": [], "scenario_params": [], "scenarios": [],
        "trajectory": [], "functional_unit_candidates": [], "chosen_functional_unit": "",
        "baseline_unit_intensity": None, "calculation_params": None,
        "calculation_violations": [],
        "amendment_requests": [], "objections": [], "debate_log": [],
        "round_in_stage": 0, "total_round": 0, "final_output": None,
    }
    base.update(over)
    return base


def _baseline_params():
    return {
        "functional_unit_candidates": ["kg/MWp"],
        "functional_unit_rationale": "测试",
        "chosen_functional_unit": "kg/MWp",
        "normalization_factors": [{
            "value_id": "v1", "value_name": "型号一", "factor": 2.0,
            "unit_note": "单台2MWp → 2MWp/台", "citations": [CITE],
        }],
        "high_copper_references": [{
            "applicable_scope": None, "chosen_value_id": "v1",
            "rationale": "测试高铜参考", "citations": [CITE],
        }],
        "citations": [CITE],
    }


def _measure(mid, r):
    return {
        "measure_id": mid, "measure_name": mid, "target_component_ids": ["cu1"],
        "mechanism": "m", "mechanism_category": "material_substitution",
        "expected_reduction": {"value": r, "unit": "%"}, "maturity": "pilot",
        "earliest_feasible_year": 2027,
        "engineering_case": {"project_or_product_name": "p", "implementing_entity": "e",
                             "year": 2024, "outcome_description": "o",
                             "scale": "pilot_project", "citations": [CITE]},
        "additional_citations": [CITE],
    }


def _sp(sid, included, rate=100):
    return {
        "scenario_id": sid, "scenario_name": {"S0": "基准情景", "S1": "普通减量",
                                              "S2": "加速减量", "S3": "深度减量"}[sid],
        "applicable_scope": None, "scenario_definition": "d",
        "included_measure_ids": included, "measure_start_year": 2025,
        "target_achievement_year": 2035, "target_achievement_rate_pct": rate,
        "diffusion_method": "linear", "scenario_rationale": "r", "citations": [CITE],
    }


def test_baseline_calc_node_produces_baselines():
    st = _state(baseline_params=_baseline_params())
    cmd = rg.baseline_calc_node(st)
    upd = cmd.update
    assert upd["stage"] == "reduction_research"
    assert len(upd["model_baselines"]) == 1
    assert upd["model_baselines"][0]["intensity"]["value"] == pytest.approx(5.0)  # 10kg/2MWp
    assert upd["baseline_unit_intensity"]["value"] == pytest.approx(5.0)
    assert "compute_model_baselines" in upd["baseline_calc_method"]


def test_baseline_calc_node_hard_check_rejects():
    bp = _baseline_params()
    bp["high_copper_references"][0]["chosen_value_id"] = "v-missing"
    st = _state(baseline_params=bp)
    cmd = rg.baseline_calc_node(st)
    upd = cmd.update
    assert "objections" in upd and upd["objections"][0]["severity"] == "blocking"
    assert upd["objections"][0]["target_agent"] == "baseline_agent"


def _calc_state():
    st = _state(
        stage="quantification",
        baseline_params=_baseline_params(),
        model_baselines=rg.baseline_calc_node(_state(baseline_params=_baseline_params())).update["model_baselines"],
        baseline_unit_intensity={"value": 5.0, "unit": "kg/MWp"},
        reduction_measures=[_measure("R1", 10), _measure("R2", 20), _measure("R3", 30)],
    )
    return st


def test_calculation_node_success_and_violation():
    st = _calc_state()
    st["scenario_params"] = [_sp("S0", []), _sp("S1", ["R1"]),
                             _sp("S2", ["R1", "R2"]), _sp("S3", ["R1", "R2", "R3"])]
    cmd = rg.calculation_node(st)
    upd = cmd.update
    assert upd["stage"] == "done"
    deltas = {s["scenario_id"]: s["full_implementation_reduction_pct"]["value"]
              for s in upd["scenarios"]}
    assert deltas["S0"] == 0.0 < deltas["S1"] < deltas["S2"] < deltas["S3"]
    assert len(upd["trajectory"]) == 11 * 4

    # 排序违规：S2/S3 组合相同
    st2 = _calc_state()
    st2["scenario_params"] = [_sp("S0", []), _sp("S1", ["R1"]),
                              _sp("S2", ["R1", "R2"]), _sp("S3", ["R1", "R2"])]
    cmd2 = rg.calculation_node(st2)
    upd2 = cmd2.update
    assert upd2["calculation_violations"]
    assert upd2["objections"][0]["flag_type"] == "scenario_severity_ordering_violation"
    assert upd2["round_in_stage"] == 0


def test_integration_requires_computed_outputs():
    with pytest.raises(ValueError):
        rg.integration_node(_state())
    st = _calc_state()
    st["scenario_params"] = [_sp("S0", []), _sp("S1", ["R1"]),
                             _sp("S2", ["R1", "R2"]), _sp("S3", ["R1", "R2", "R3"])]
    upd = rg.calculation_node(st).update
    st.update({k: v for k, v in upd.items() if k != "debate_log"})
    out = rg.integration_node(st)["final_output"]
    assert out["model_baselines"], "final_output 必须携带 model_baselines"
    assert out["baseline_unit_intensity"]["value"] == pytest.approx(5.0)
    assert out["region_id"] == "China"

    blocked = dict(st)
    blocked["objections"] = [{"objection_id": "x", "raised_by": "critic_agent",
                              "target_agent": "reduction_agent", "flag_type": "other",
                              "detail": "未解决", "severity": "blocking",
                              "round_raised": 1, "addressed": False}]
    with pytest.raises(ValueError, match="blocking objection"):
        rg.integration_node(blocked)

    fallback = dict(st)
    fallback["scenarios"] = [dict(s, scenario_rationale="轮次耗尽系统兜底")
                             for s in st["scenarios"]]
    with pytest.raises(ValueError, match="程序兜底"):
        rg.integration_node(fallback)


class _StubLLM:
    """supervisor 路由测试用：返回固定决策"""
    def __init__(self, decision):
        self.decision = decision

    def with_structured_output(self, schema):
        outer = self

        class _SO:
            def invoke(self, messages):
                return outer.decision
        return _SO()


def test_supervisor_forced_routes_to_calc_nodes():
    from schemas import SupervisorDecision
    st = _state(baseline_params=_baseline_params(), round_in_stage=1)
    cmd = rg.supervisor(st, _StubLLM(SupervisorDecision(
        next_agent="critic_agent", reasoning="不应被采用")))
    assert cmd.goto == "baseline_calc_node"

    st2 = _calc_state()
    st2["scenario_params"] = [_sp("S0", []), _sp("S1", ["R1"]),
                              _sp("S2", ["R1", "R2"]), _sp("S3", ["R1", "R2", "R3"])]
    st2["round_in_stage"] = 1
    cmd2 = rg.supervisor(st2, _StubLLM(SupervisorDecision(
        next_agent="critic_agent", reasoning="不应被采用")))
    assert cmd2.goto == "calculation_node"
