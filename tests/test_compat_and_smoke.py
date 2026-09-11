"""旧 run JSON 兼容性 与 DemoLLM 新契约冒烟
==================================================================
1. webapp/data/runs/ 下的历史 run（旧 schema 产出）必须能用新 schema
   反序列化——所有新增字段带默认值，不允许破坏既有成果的查看/导出。
2. DemoLLM 必须支持图B 新输出契约（BaselineParams / ScenarioParams），
   保证无 API Key 时演示模式可跑通新流程。
"""

import glob
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"))

from schemas import ProductResearchOutput, ProductReductionModel

RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "webapp", "data", "runs")


def _run_files_with_result():
    out = []
    for f in sorted(glob.glob(os.path.join(RUNS_DIR, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("result"):
                out.append((f, d))
        except Exception:
            continue
    return out


def test_old_run_json_backcompat():
    """旧 schema 产出的 run 结果必须能被新 schema 验证（新字段全默认值）。

    注：archive01.json 是早期手工样例（连必填的 product_id 都没有，
    从来不是图A/图B 的产出物），不在兼容范围内。"""
    pairs = _run_files_with_result()
    assert pairs, "webapp/data/runs 下应存在带 result 的历史 run"
    checked = 0
    for f, d in pairs:
        result = d["result"]
        if not result.get("product_id"):
            continue  # 手工样例，非图产出物
        if "classification_dimensions" in result:
            out = ProductResearchOutput.model_validate(result)
            assert out.region_id  # 默认 "unknown" 或真实值
        elif "reduction_measures" in result:
            out = ProductReductionModel.model_validate(result)
            assert out.model_baselines == []  # 旧成果无基线，默认空表
        else:
            continue
        checked += 1
    assert checked >= 2, f"至少应验证2个历史run，实际 {checked}"


def test_demo_llm_new_contracts():
    from demo_llm import DemoLLM
    from langchain_core.messages import HumanMessage
    from schemas import BaselineParams, ScenarioParams

    llm = DemoLLM(product_name="空调")
    # 模拟 baseline persona 的真实拼装：取值行独立成行（"- v1 / 型号一"）
    ctx = ("型号取值列表：\n- v1 / 型号一\n含铜部件快照："
           "[{'component_id': 'cu1', 'measure_id': 'R01', 'unit_mass': {'value': 1.0}}]")
    bp = llm.with_structured_output(BaselineParams).invoke([
        HumanMessage(content=f"基准年: 2025\n{ctx}")])
    assert isinstance(bp, BaselineParams)
    assert bp.normalization_factors and bp.high_copper_references
    assert bp.normalization_factors[0].value_id == "v1"

    sps = llm.with_structured_output(list[ScenarioParams]).invoke([
        HumanMessage(content="基准年: 2025\n'measure_id': 'R01'\n'measure_id': 'R02'\n'measure_id': 'R03'")])
    assert len(sps) == 4
    assert [s.scenario_id for s in sps] == ["S0", "S1", "S2", "S3"]
    assert sps[0].included_measure_ids == []
    # ScenarioParams 契约本身不含 Δmax 字段（Δmax 由确定性代码计算）
    assert "full_implementation_reduction_pct" not in ScenarioParams.model_fields
