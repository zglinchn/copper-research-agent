"""
同轴一致性(P0)回归测试
======================
把两次真实暴露过的"串轴"案例固化成测试输入，防止同类问题在改动
CRITIC_AGENT_PERSONA / heuristic_axis_consistency_scan 后再次滑过：

1. 光伏站"核心技术设备类型（逆变器拓扑）"维度混入"光储一体机" —— 纯语义
   层面的串轴，一次真实LangGraph运行里critic没拦下来。
2. 手工版风电Excel："产品种类/技术路线"列头下实际填的是"陆上"/"海上"
   （应用场景维度，被错误声称成技术路线维度）—— 这一类错误连词面上都
   没有物理规格或"一体机"式构词特征，两条正则启发式都注定抓不住，
   必须靠critic自己的语义自测（axis_of_variation原句造句法）来判断。
   这里把它写成"预期失败"用例，用意是提醒未来的维护者：heuristic_axis_
   consistency_scan不是万能的，这类case永远需要LLM语义判断兜底，
   不要误以为加了正则就不需要critic自测了。

运行方式：pytest tests/test_axis_consistency_regression.py -v
（需要 pydantic 已安装；本次编写环境无网络，未能实际跑通，仅做过
 底层正则逻辑的独立验证，请在你自己的环境里跑一遍确认。）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pydantic import ValidationError

from schemas import (
    ClassificationDimension,
    DimensionValue,
    DimensionCategory,
    Citation,
    Objection,
    heuristic_axis_consistency_scan,
)


def _cite(title="占位引用"):
    return Citation(source_type="other", title=title, confidence="low")


# ============================================================
# 案例1：光伏站"逆变器拓扑"维度混入"光储一体机"（真实实测暴露的漏检）
# ============================================================

def _pv_inverter_topology_dimension_with_leak():
    return ClassificationDimension(
        dimension_id="D-PV-INV",
        dimension_name="核心技术设备类型（逆变器拓扑）",
        dimension_category=DimensionCategory.TECHNOLOGY_ROUTE,
        axis_of_variation="逆变器内部电路是集中式/组串式/微型拓扑",
        definition_rationale="不同拓扑的功率器件与散热/成本结构不同",
        orthogonality_check="与'是否集成储能'维度相互独立",
        values=[
            DimensionValue(
                value_id="V1", value_name="集中式逆变器",
                axis_conformity_justification="一种电路拓扑",
                citations=[_cite()],
            ),
            DimensionValue(
                value_id="V2", value_name="组串式逆变器",
                axis_conformity_justification="一种电路拓扑",
                citations=[_cite()],
            ),
            # 真正的问题条目：定义特征是"是否集成储能"，不是电路拓扑
            DimensionValue(
                value_id="V3", value_name="光储一体机（储能逆变器）",
                axis_conformity_justification="集成了储能功能的逆变器产品形态",
                citations=[_cite()],
            ),
        ],
    )


def test_semantic_heuristic_catches_pv_storage_combo_leak():
    """语义特征启发式(_SEMANTIC_FEATURE_TOKEN)必须标记"光储一体机"这条取值。

    这是本次改进新增的能力：改进前的heuristic_axis_consistency_scan只有
    物理规格正则，对这种纯语义串轴完全没有反应，必须靠这次新增的
    _SEMANTIC_FEATURE_TOKEN分支补上。
    """
    dim = _pv_inverter_topology_dimension_with_leak()
    warnings = heuristic_axis_consistency_scan(dim)
    assert any("光储一体机" in w and "语义特征" in w for w in warnings), (
        "改进后的heuristic_axis_consistency_scan应该能标记出'光储一体机'这条"
        "语义层面串轴的取值，若这个断言失败说明语义特征正则又失效了"
    )


def test_semantic_heuristic_does_not_flag_clean_topology_values():
    """干净的同轴取值（集中式/组串式）不应该被误伤。"""
    dim = _pv_inverter_topology_dimension_with_leak()
    warnings = heuristic_axis_consistency_scan(dim)
    flagged_names = "".join(warnings)
    assert "集中式逆变器" not in flagged_names
    assert "组串式逆变器" not in flagged_names


# ============================================================
# 案例2：手工版风电Excel —— "产品种类/技术路线"列头下填的是陆上/海上
# ============================================================

def _wind_turbine_mislabeled_dimension():
    """复现手工版Excel的真实错误：dimension_category声称是技术路线，
    取值却是应用场景（陆上/海上）。这条取值本身不含任何物理规格数字，
    也不含"一体机/集成式"这类构词特征，两条正则启发式都不会响应——
    这是预期的、需要被记录下来的heuristic盲区，不是新bug。
    """
    return ClassificationDimension(
        dimension_id="D-WIND-BAD",
        dimension_name="产品种类/技术路线",
        dimension_category=DimensionCategory.TECHNOLOGY_ROUTE,  # 声称是技术路线
        axis_of_variation="不同的传动/发电技术路线（如永磁直驱、双馈异步）",
        definition_rationale="技术路线决定齿轮箱/励磁系统等核心部件差异",
        orthogonality_check="与安装环境维度相互独立",
        values=[
            # 真实的错误：这两个取值实际是"陆上/海上"应用场景，不是技术路线
            DimensionValue(
                value_id="V1", value_name="陆上",
                axis_conformity_justification="安装在陆地环境",
                citations=[_cite()],
            ),
            DimensionValue(
                value_id="V2", value_name="海上",
                axis_conformity_justification="安装在海洋环境",
                citations=[_cite()],
            ),
        ],
    )


def test_heuristic_scan_cannot_catch_the_excel_style_mislabel():
    """记录已知盲区：这类"列头声称的轴 vs 实际取值的轴"完全错位、但取值
    名称本身既无规格数字也无'一体机'式构词特征的情况，两条正则启发式都
    抓不住，返回空列表是【预期行为】而不是需要修的bug。

    这个测试的意义不是要求scan通过，而是明确记录"这里必须靠critic的
    语义自测兜底，不能指望以后有人加几个正则就把这类case也覆盖了"——
    多数这一类的取值名称（"陆上"/"海上"）本身完全正常，只有结合
    dimension_category和axis_of_variation的声明才能看出矛盾，这天然
    需要语义理解，而不是字符串模式匹配能解决的问题。
    """
    dim = _wind_turbine_mislabeled_dimension()
    warnings = heuristic_axis_consistency_scan(dim)
    assert warnings == [], (
        "如果这个断言开始失败(即scan开始标记'陆上'/'海上')，说明有人加了"
        "更激进的正则规则，请同步检查是否会对其它产品的正常应用场景维度"
        "（如输配电的架空/地埋）造成误伤"
    )


# ============================================================
# Schema硬约束：dimension_value_axis_mismatch 不允许被降级为warning
# ============================================================

def test_axis_mismatch_objection_cannot_be_downgraded_to_warning():
    """即便critic的LLM判断或人为传参失误把severity写成warning，
    Objection的model_validator也必须在构造时就拒绝，这是防止P0检查被
    静默降级的最后一道代码防线（对应用户'不允许critic降级为warning'的
    强约束）。
    """
    with pytest.raises(ValidationError):
        Objection(
            objection_id="OBJ-1",
            raised_by="critic_agent",
            target_agent="dimension_agent",
            flag_type="dimension_value_axis_mismatch",
            detail=(
                "维度「核心技术设备类型（逆变器拓扑）」声称的轴是"
                "'集中式/组串式/微型拓扑'，但取值「光储一体机（储能逆变器）」"
                "实际的区分特征是'是否集成储能'，属于另一个轴，应移出本维度"
            ),
            severity="warning",  # 非法：这个flag_type必须是blocking
            round_raised=1,
        )


def test_axis_mismatch_objection_accepts_blocking():
    """正确用法：severity="blocking" 应该能正常构造，不应被误伤拦下。"""
    obj = Objection(
        objection_id="OBJ-2",
        raised_by="critic_agent",
        target_agent="dimension_agent",
        flag_type="dimension_value_axis_mismatch",
        detail=(
            "维度「核心技术设备类型（逆变器拓扑）」声称的轴是"
            "'集中式/组串式/微型拓扑'，但取值「光储一体机（储能逆变器）」"
            "实际的区分特征是'是否集成储能'，属于另一个轴，应移出本维度"
        ),
        severity="blocking",
        round_raised=1,
    )
    assert obj.severity == "blocking"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
