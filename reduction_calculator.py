"""铜减量化路径的确定性计算程序。

此文件不调用大模型。它仅接收已经审查的措施、情景计划与基准强度，按固定
公式生成情景总降幅和逐年单位铜强度路径。
"""

from __future__ import annotations

from math import prod
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from schemas import (
    CopperReductionMeasure,
    IntensityTrajectoryPoint,
    QuantValue,
    SCENARIO_CODE_FIXED_NAMES,
    Scenario,
    ScenarioPlan,
)


CALCULATION_METHOD = "确定性程序 v1：措施降幅按乘法叠加；扩散按固定函数计算。"


class CalculationInput(BaseModel):
    baseline_year: int
    trajectory_end_year: int = 2035
    functional_unit: str = Field(min_length=1)
    baseline_unit_intensity: QuantValue
    baseline_calc_method: str = Field(min_length=1)
    reduction_measures: list[CopperReductionMeasure]
    scenario_plans: list[ScenarioPlan]

    @model_validator(mode="after")
    def _year_and_baseline_valid(self):
        if self.trajectory_end_year < self.baseline_year:
            raise ValueError("路径终点年份不得早于基准年")
        if self.baseline_unit_intensity.value < 0:
            raise ValueError("基准单位铜强度不得为负")
        return self


class CalculationResult(BaseModel):
    scenarios: list[Scenario]
    trajectory: list[IntensityTrajectoryPoint]
    calculation_method: str = CALCULATION_METHOD


def _applicable_measure(measure: CopperReductionMeasure, scope: Optional[str]) -> bool:
    return measure.applicable_scope is None or measure.applicable_scope == scope


def _resolve_measures(plan: ScenarioPlan, measures: list[CopperReductionMeasure]) -> list[CopperReductionMeasure]:
    selected: list[CopperReductionMeasure] = []
    for measure_id in plan.included_measure_ids:
        matches = [m for m in measures if m.measure_id == measure_id and _applicable_measure(m, plan.applicable_scope)]
        if len(matches) != 1:
            raise ValueError(
                f"情景 {plan.scenario_id} / {plan.applicable_scope or '无子类别'} 的措施 {measure_id} "
                "不存在或对应多个适用范围"
            )
        measure = matches[0]
        if measure.expected_reduction is None or measure.expected_reduction.unit != "%":
            raise ValueError(f"措施 {measure_id} 缺少单位为 % 的预期降幅，不能进入固定计算")
        if not 0 <= measure.expected_reduction.value < 100:
            raise ValueError(f"措施 {measure_id} 的预期降幅必须在 [0, 100) 内")
        selected.append(measure)
    return selected


def _effective_reduction_pct(measures: list[CopperReductionMeasure]) -> float:
    """相对降幅按乘法叠加，避免把两个独立百分比直接相加超过 100%。"""
    return round((1 - prod(1 - m.expected_reduction.value / 100 for m in measures)) * 100, 8)


def _realization_rate(plan: ScenarioPlan, year: int) -> float:
    if plan.scenario_id == "S0" or year < plan.measure_start_year:
        return 0.0
    if plan.target_achievement_year <= plan.measure_start_year:
        return plan.target_achievement_rate_pct
    if year >= plan.target_achievement_year:
        return plan.target_achievement_rate_pct
    t = (year - plan.measure_start_year) / (plan.target_achievement_year - plan.measure_start_year)
    if plan.diffusion_method == "linear":
        factor = t
    elif plan.diffusion_method == "s_curve":
        factor = t * t * (3 - 2 * t)
    else:  # step
        factor = 1.0
    return round(plan.target_achievement_rate_pct * factor, 8)


def calculate(input_data: CalculationInput) -> CalculationResult:
    """从已审核输入确定性生成情景与每年强度路径。"""
    plans_by_scope: dict[Optional[str], list[ScenarioPlan]] = {}
    for plan in input_data.scenario_plans:
        plans_by_scope.setdefault(plan.applicable_scope, []).append(plan)

    scenarios: list[Scenario] = []
    trajectory: list[IntensityTrajectoryPoint] = []
    for scope, plans in plans_by_scope.items():
        if {p.scenario_id for p in plans} != {"S0", "S1", "S2", "S3"} or len(plans) != 4:
            raise ValueError(f"子类别 {scope or '无子类别'} 必须恰有 S0-S3 四个情景计划")
        for plan in sorted(plans, key=lambda p: p.scenario_id):
            selected = _resolve_measures(plan, input_data.reduction_measures)
            reduction_pct = 0.0 if plan.scenario_id == "S0" else _effective_reduction_pct(selected)
            scenario = Scenario(
                scenario_id=plan.scenario_id,
                scenario_name=SCENARIO_CODE_FIXED_NAMES[plan.scenario_id],
                applicable_scope=scope,
                scenario_definition=plan.scenario_definition,
                included_measure_ids=plan.included_measure_ids,
                full_implementation_reduction_pct=QuantValue(value=reduction_pct, unit="%"),
                measure_start_year=plan.measure_start_year,
                target_achievement_year=plan.target_achievement_year,
                target_achievement_rate_pct=plan.target_achievement_rate_pct,
                diffusion_method=plan.diffusion_method,
                scenario_rationale=plan.scenario_rationale,
                citations=plan.citations,
            )
            scenarios.append(scenario)
            for year in range(input_data.baseline_year, input_data.trajectory_end_year + 1):
                rate = _realization_rate(plan, year)
                intensity = round(input_data.baseline_unit_intensity.value * (1 - reduction_pct / 100 * rate / 100), 8)
                trajectory.append(IntensityTrajectoryPoint(
                    year=year,
                    scenario_id=plan.scenario_id,
                    applicable_scope=scope,
                    scenario_realization_rate_pct=rate,
                    unit_copper_intensity=QuantValue(value=intensity, unit=input_data.baseline_unit_intensity.unit),
                    functional_unit=input_data.functional_unit,
                    delta_vs_baseline=QuantValue(
                        value=round(intensity - input_data.baseline_unit_intensity.value, 8),
                        unit=input_data.baseline_unit_intensity.unit,
                    ),
                    applied_measure_ids=plan.included_measure_ids,
                ))
        reductions = [
            next(s.full_implementation_reduction_pct.value for s in scenarios
                 if s.applicable_scope == scope and s.scenario_id == code)
            for code in ("S0", "S1", "S2", "S3")
        ]
        if not reductions[0] == 0 or not reductions[0] < reductions[1] < reductions[2] < reductions[3]:
            raise ValueError(f"子类别 {scope or '无子类别'} 的确定性综合降幅不满足 S0<S1<S2<S3")
    return CalculationResult(scenarios=scenarios, trajectory=trajectory)
