"""演示模式 LLM（未配置 OPENAI_API_KEY 时的本地回退）
==================================================================
实现与 langchain 聊天模型相同的 with_structured_output 接口，
按 schema 类型生成"结构合理但内容为通用占位"的演示数据，用途：
  1. 无 API Key 时也能端到端跑通图A/图B全流程，验证网页端交互；
  2. 作为前后端联调的固定样本。

设计约束（与 schemas.py 的反硬编码原则一致）：
  - 生成的维度/子系统/含铜部位全部使用"元级模板"（技术路线/容量等级/
    应用场景等 DimensionCategory 元类别 + 中性占位取值），
    不包含任何产品专属先验（如"光伏板+BOS"这类人工经验划分）。
  - 所有引用统一标注为"演示模式占位引用"，confidence=low，
    正式调研必须在配置真实 LLM 后运行。
"""

from __future__ import annotations
import re
import typing

from langchain_core.messages import SystemMessage

from schemas import (
    ClassificationDimension, DimensionCategory, DimensionValue,
    FunctionalSubsystem, CopperComponent, QuantValue, Citation,
    CriticReview, SupervisorDecision,
    CopperReductionMeasure, EngineeringCase, Scenario,
    IntensityTrajectoryPoint, BaselineParams, NormalizationFactor,
    HighCopperReference, ScenarioParams,
)
from reduction_graph import ReductionAgentOutput

_DEMO_CITE = Citation(
    source_type="other",
    title="演示模式占位引用（未执行真实网络检索）",
    publisher="本地演示运行",
    confidence="low",
    excerpt_note="演示数据：仅用于验证流程与页面结构，正式使用请在配置 OPENAI_API_KEY 后运行在线调研。",
)


class DemoLLM:
    """模拟 llm.with_structured_output(schema).invoke(messages) 契约"""

    def __init__(self, product_name: str = "目标产品"):
        self.product_name = product_name

    def with_structured_output(self, schema):
        return _DemoStructured(self, schema)


def _unwrap_list(schema):
    origin = typing.get_origin(schema)
    if origin in (list, typing.List):
        return typing.get_args(schema)[0]
    return None


class _DemoStructured:
    def __init__(self, llm: DemoLLM, schema):
        self.llm = llm
        self.schema = schema

    def invoke(self, messages):
        text = "\n".join(
            (m.content if isinstance(m.content, str) else str(m.content))
            for m in messages
            if not isinstance(m, SystemMessage) or True
        )
        elem = _unwrap_list(self.schema)

        if elem is ClassificationDimension:
            return self._demo_dimensions()
        if elem is FunctionalSubsystem:
            return self._demo_subsystems()
        if elem is CopperComponent:
            return self._demo_copper(text)
        if elem is Scenario:
            return self._demo_scenarios(text)
        if elem is ScenarioParams:
            return self._demo_scenario_params(text)
        if elem is IntensityTrajectoryPoint:
            return self._demo_trajectory(text)
        if self.schema is BaselineParams:
            return self._demo_baseline_params(text)
        if self.schema is CriticReview:
            return CriticReview()  # 演示模式：critic始终一轮通过
        if self.schema is SupervisorDecision:
            return self._demo_decision(text)
        if self.schema is ReductionAgentOutput:
            return self._demo_reduction(text)
        raise NotImplementedError(f"演示模式不支持schema: {self.schema}")

    # --------------------------------------------------------
    # 维度体系：元类别级模板，不含产品先验
    # --------------------------------------------------------
    def _demo_dimensions(self):
        p = self.llm.product_name
        templates = [
            ("技术路线", DimensionCategory.TECHNOLOGY_ROUTE,
             "不同的核心技术路线", ["主流技术路线A", "主流技术路线B", "新兴技术路线C"]),
            ("物理规格", DimensionCategory.PHYSICAL_SPEC,
             "不同的外形/规格标准", ["标准规格型", "紧凑规格型"]),
            ("容量功率等级", DimensionCategory.CAPACITY_POWER,
             "不同的容量或功率等级", ["小容量等级", "中容量等级", "大容量等级"]),
            ("应用场景", DimensionCategory.APPLICATION_SCENARIO,
             "不同的典型应用场景", ["场景一（当前主流）", "场景二（新兴）"]),
        ]
        dims = []
        for i, (name, cat, axis, values) in enumerate(templates):
            dims.append(ClassificationDimension(
                dimension_id=f"dim-{i + 1}",
                dimension_name=f"{p}·{name}",
                dimension_category=cat,
                axis_of_variation=f"{p}的{axis}",
                definition_rationale="演示数据：区分了不同取值并影响后续含铜强度测算。",
                orthogonality_check="演示数据：与其它维度相互独立，无语义重叠。",
                values=[
                    DimensionValue(
                        value_id=f"dim-{i + 1}-v{j + 1}",
                        value_name=v,
                        axis_conformity_justification=f"按「{axis}」这一分类轴划分的取值。",
                        prevalence_desc="当前主流" if j == 0 else None,
                        citations=[_DEMO_CITE],
                    )
                    for j, v in enumerate(values)
                ],
                evidence=[_DEMO_CITE],
            ))
        return dims

    # --------------------------------------------------------
    # 结构分解：通用三级BOM占位（叶子可判断含铜）
    # --------------------------------------------------------
    def _demo_subsystems(self):
        p = self.llm.product_name
        spec = [
            ("sub-1", f"{p}核心功能系统", 0, None),
            ("sub-1-1", "主工作单元", 1, "sub-1"),
            ("sub-1-2", "电气接口单元", 1, "sub-1"),
            ("sub-2", f"{p}电气系统", 0, None),
            ("sub-2-1", "输配电单元", 1, "sub-2"),
            ("sub-2-2", "控制与线束", 1, "sub-2"),
            ("sub-3", f"{p}结构支撑系统", 0, None),
            ("sub-3-1", "承力结构件", 1, "sub-3"),
            ("sub-4", f"{p}辅助系统", 0, None),
            ("sub-4-1", "散热与连接件", 1, "sub-4"),
        ]
        return [
            FunctionalSubsystem(
                subsystem_id=sid, subsystem_name=name,
                parent_subsystem_id=parent, decomposition_level=level,
                function_description="演示数据：功能描述占位，正式内容由在线调研产生。",
                evidence=[_DEMO_CITE],
            )
            for sid, name, level, parent in spec
        ]

    # --------------------------------------------------------
    # 含铜部位：挂在结构树叶子节点上
    # --------------------------------------------------------
    def _demo_copper(self, text: str):
        ids = re.findall(r"'subsystem_id': '([^']+)'", text)
        parent_ids = set(re.findall(r"'parent_subsystem_id': '([^']+)'", text))
        leaves = [i for i in ids if i not in parent_ids] or ["sub-1-1"]
        return [
            CopperComponent(
                component_id=f"cu-{sid}",
                parent_subsystem_id=sid,
                component_name="导电/电磁功能部件（演示占位）",
                copper_form="copper_winding",
                function_of_copper="演示数据：利用铜的导电性（占位描述）。",
                unit_mass=QuantValue(value=1.0, unit="kg（演示占位）"),
                mass_data_basis="engineering_estimate",
                citations=[_DEMO_CITE],
            )
            for sid in leaves
        ]

    # --------------------------------------------------------
    # 减量化措施（图B）
    # --------------------------------------------------------
    def _demo_reduction(self, text: str):
        comp_ids = re.findall(r"'component_id': '([^']+)'", text)
        targets = comp_ids[:2] or []
        case = EngineeringCase(
            project_or_product_name="演示工程案例（占位）",
            implementing_entity="演示实施方",
            year=2024,
            outcome_description="演示数据：占位的减量效果描述。",
            scale="pilot_project",
            citations=[_DEMO_CITE],
        )
        measures = [
            CopperReductionMeasure(
                measure_id="R01", measure_name="材料替代（演示占位）",
                target_component_ids=targets,
                mechanism="演示数据：材料替代机理占位描述。",
                mechanism_category="material_substitution",
                expected_reduction=QuantValue(value=8, unit="%"),
                trade_offs="演示数据：性能/成本权衡占位描述。",
                maturity="pilot", earliest_feasible_year=2027,
                engineering_case=case, additional_citations=[_DEMO_CITE],
            ),
            CopperReductionMeasure(
                measure_id="R02", measure_name="结构优化（演示占位）",
                target_component_ids=targets,
                mechanism="演示数据：结构优化机理占位描述。",
                mechanism_category="structural_optimization",
                expected_reduction=QuantValue(value=5, unit="%"),
                trade_offs="演示数据：可靠性权衡占位描述。",
                maturity="lab", earliest_feasible_year=2030,
                engineering_case=case, additional_citations=[_DEMO_CITE],
            ),
            CopperReductionMeasure(
                measure_id="R03", measure_name="工艺改进（演示占位）",
                target_component_ids=targets[:1] or targets,
                mechanism="演示数据：工艺改进机理占位描述。",
                mechanism_category="manufacturing_process",
                expected_reduction=QuantValue(value=3, unit="%"),
                trade_offs="演示数据：良率权衡占位描述。",
                maturity="lab", earliest_feasible_year=2031,
                engineering_case=case, additional_citations=[_DEMO_CITE],
            ),
        ]
        return ReductionAgentOutput(measures=measures, amendment_requests=[])

    # --------------------------------------------------------
    # 情景定义：固定S0-S3框架，满足严格递增硬约束
    # --------------------------------------------------------
    def _demo_scenarios(self, text: str):
        m = re.search(r"基准年:\s*(\d{4})", text)
        base_year = int(m.group(1)) if m else 2025
        specs = [
            ("S1", "普通减量", 5, 80),
            ("S2", "加速减量", 10, 90),
            ("S3", "深度减量", 15, 100),
        ]
        scenarios = [Scenario(
            scenario_id="S0", scenario_name="基准情景", applicable_scope=None,
            scenario_definition="高铜技术参考配置，不额外实施减铜措施（演示占位）",
            included_measure_ids=[],
            full_implementation_reduction_pct=QuantValue(value=0, unit="%"),
            measure_start_year=base_year, target_achievement_year=base_year,
            target_achievement_rate_pct=0, diffusion_method="linear",
            scenario_rationale="演示数据：S0固定为比较基准。", citations=[_DEMO_CITE],
        )]
        for sid, name, dmax, rate in specs:
            scenarios.append(Scenario(
                scenario_id=sid, scenario_name=name, applicable_scope=None,
                scenario_definition=f"演示数据：{name}情景定义占位",
                included_measure_ids=[],
                full_implementation_reduction_pct=QuantValue(value=dmax, unit="%"),
                measure_start_year=base_year, target_achievement_year=2035,
                target_achievement_rate_pct=rate, diffusion_method="linear",
                scenario_rationale="演示数据：情景依据占位描述。", citations=[_DEMO_CITE],
            ))
        return scenarios

    # --------------------------------------------------------
    # 基线归一化参数（图B stage 0）：只提参数不算数
    # --------------------------------------------------------
    def _demo_baseline_params(self, text: str) -> BaselineParams:
        # 兼容两种格式：persona 中的 "- v1 / 型号一" 行 与 dict dump 的 'value_id': 'v1'
        value_ids = list(dict.fromkeys(
            re.findall(r"^-\s*([\w-]+)\s*/", text, re.M)
            + re.findall(r"'value_id': '([^']+)'", text))) or ["v-demo"]
        return BaselineParams(
            functional_unit_candidates=["kg/演示功能单位"],
            functional_unit_rationale="演示数据：占位功能单位口径。",
            chosen_functional_unit="kg/演示功能单位",
            normalization_factors=[
                NormalizationFactor(
                    value_id=vid, value_name=f"型号{vid}", factor=1.0,
                    unit_note="演示：单台=1演示功能单位 → 1演示功能单位/台",
                    citations=[_DEMO_CITE],
                )
                for vid in value_ids
            ],
            high_copper_references=[
                HighCopperReference(
                    applicable_scope=None, chosen_value_id=value_ids[0],
                    rationale="演示数据：首个型号作为高铜参考配置占位。",
                    citations=[_DEMO_CITE],
                ),
            ],
            citations=[_DEMO_CITE],
        )

    # --------------------------------------------------------
    # 情景参数（图B quantification）：只含参数，Δmax 交给代码
    # --------------------------------------------------------
    def _demo_scenario_params(self, text: str):
        m = re.search(r"基准年:\s*(\d{4})", text)
        base_year = int(m.group(1)) if m else 2025
        measure_ids = list(dict.fromkeys(re.findall(r"'measure_id': '([^']+)'", text)))
        m1 = measure_ids[:1]
        m2 = measure_ids[:2]
        m3 = measure_ids[:3] or measure_ids
        specs = [
            ("S1", m1, 80),
            ("S2", m2, 90),
            ("S3", m3, 100),
        ]
        params = [ScenarioParams(
            scenario_id="S0", scenario_name="基准情景", applicable_scope=None,
            scenario_definition="高铜技术参考配置，不额外实施减铜措施（演示占位）",
            included_measure_ids=[], measure_start_year=base_year,
            target_achievement_year=base_year, target_achievement_rate_pct=0,
            diffusion_method="linear", scenario_rationale="演示数据：S0固定为比较基准。",
            citations=[_DEMO_CITE],
        )]
        for sid, included, rate in specs:
            params.append(ScenarioParams(
                scenario_id=sid,
                scenario_name={"S1": "普通减量", "S2": "加速减量", "S3": "深度减量"}[sid],
                applicable_scope=None,
                scenario_definition=f"演示数据：{sid}情景定义占位",
                included_measure_ids=included,
                measure_start_year=base_year, target_achievement_year=2035,
                target_achievement_rate_pct=rate, diffusion_method="linear",
                scenario_rationale="演示数据：情景依据占位描述。", citations=[_DEMO_CITE],
            ))
        return params

    # --------------------------------------------------------
    # 强度路径：每年×每情景一个点，与Δmax×实现率保持数学一致
    # --------------------------------------------------------
    def _demo_trajectory(self, text: str):
        m = re.search(r"基准年:\s*(\d{4})", text)
        base_year = int(m.group(1)) if m else 2025
        scen_ids = list(dict.fromkeys(re.findall(r"'scenario_id': '([^']+)'", text))) or ["S0"]
        dmax = {"S0": 0, "S1": 5, "S2": 10, "S3": 15}
        base_intensity = 1.0
        points = []
        for sid in scen_ids:
            for year, rate in [(base_year, 0), (2030, 50), (2035, 100)]:
                intensity = base_intensity * (1 - dmax.get(sid, 0) / 100 * rate / 100)
                points.append(IntensityTrajectoryPoint(
                    year=year, scenario_id=sid, applicable_scope=None,
                    scenario_realization_rate_pct=rate,
                    unit_copper_intensity=QuantValue(
                        value=round(intensity, 4), unit="kg/演示功能单位",
                    ),
                    functional_unit="演示功能单位",
                    delta_vs_baseline=QuantValue(
                        value=round(intensity - base_intensity, 4), unit="kg/演示功能单位",
                    ),
                ))
        return points

    # --------------------------------------------------------
    # Supervisor 路由决策：按提示词中的阶段/产出状态做确定性决策
    # --------------------------------------------------------
    def _demo_decision(self, text: str) -> SupervisorDecision:
        m = re.search(r"当前阶段:\s*(\w+)", text)
        stage = m.group(1) if m else ""
        m = re.search(r"本阶段第(\d+)轮", text)
        round_in_stage = int(m.group(1)) if m else 0

        if stage == "dimension_structure_debate":
            if "DimensionAgent: 未产出" in text:
                return SupervisorDecision(next_agent="dimension_agent", reasoning="演示：维度提案尚未产出")
            if "StructureAgent: 未产出" in text:
                return SupervisorDecision(next_agent="structure_agent", reasoning="演示：结构提案尚未产出")
            if round_in_stage < 1:
                return SupervisorDecision(next_agent="critic_agent", reasoning="演示：维度与结构已产出，交由critic审查")
            return SupervisorDecision(next_agent="copper_agent", reasoning="演示：维度与结构已通过审查，进入含铜部位识别")

        if stage == "copper_identification":
            if "CopperAgent: 未产出" in text:
                return SupervisorDecision(next_agent="copper_agent", reasoning="演示：含铜部位尚未调研")
            if round_in_stage < 1:
                return SupervisorDecision(next_agent="critic_agent", reasoning="演示：含铜部位已产出，交由critic审查")
            return SupervisorDecision(next_agent="critic_agent", should_terminate=True,
                                       reasoning="演示：调研阶段完成，转入人工审核",
                                       termination_reason="演示：调研阶段完成，转入人工审核")

        if stage == "baseline_quantification":
            if "BaselineAgent: 未产出" in text:
                return SupervisorDecision(next_agent="baseline_agent", reasoning="演示：基线归一化参数尚未产出")
            if round_in_stage < 1:
                return SupervisorDecision(next_agent="critic_agent", reasoning="演示：基线参数已产出，交由critic审查")
            return SupervisorDecision(next_agent="critic_agent", should_terminate=True,
                                       reasoning="演示：基线参数审查通过，进入确定性基线计算",
                                       termination_reason="演示：基线参数审查通过")

        if stage == "reduction_research":
            if "ReductionAgent: 未产出" in text:
                return SupervisorDecision(next_agent="reduction_agent", reasoning="演示：减量化措施尚未产出")
            if round_in_stage < 1:
                return SupervisorDecision(next_agent="critic_agent", reasoning="演示：措施已产出，交由critic审查")
            return SupervisorDecision(next_agent="quant_agent", reasoning="演示：措施审查通过，进入定量建模")

        if stage == "quantification":
            if "QuantAgent: 未产出" in text:
                return SupervisorDecision(next_agent="quant_agent", reasoning="演示：情景参数尚未产出")
            if round_in_stage < 1:
                return SupervisorDecision(next_agent="critic_agent", reasoning="演示：情景参数已产出，交由critic审查排序")
            return SupervisorDecision(next_agent="critic_agent", should_terminate=True,
                                       reasoning="演示：情景参数审查通过，进入确定性情景/路径计算",
                                       termination_reason="演示：情景参数审查通过，进入计算")

        return SupervisorDecision(next_agent="critic_agent", should_terminate=True,
                                   reasoning="演示：流程结束",
                                   termination_reason="演示：流程结束")
