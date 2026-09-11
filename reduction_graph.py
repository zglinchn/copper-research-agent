"""
图B：减量化图 (Reduction Graph)
==================================================================
独立于图A(research_graph.py)运行，输入是图A产出并经人工approved的
ProductResearchOutput——本图只读取这份数据，绝不直接修改。

阶段0（baseline_quantification）：减量前单位铜强度归位
  模板Section3「含铜部位清单与单位铜强度证据」此前不属于任何图：
  图A边界明确排除强度计算，旧图B又假设基线已存在却无节点产出
  （integration硬编码0kg兜底）。本版本将其并入图B作stage 0：
    baseline_agent(LLM)   只提带引用的参数：功能单位、归一化换算因子、
                          每子类别高铜参考配置选择——不算任何数字；
    baseline_calc_node    确定性代码：调 calculate.compute_model_baselines
                          产出 model_baselines（强度+构成权重+calc_trace）。
  不单独建图的理由：数据依赖为严格链条 基线→措施绝对降铜量→Δmax→路径，
  拆图引入产物交接与版本错配风险；基线是"对已批准事实的确定性计算"，
  符合图B"消费事实+计算"的身份；复用现有supervisor/critic/objection/
  流式UI与run持久化。

定量阶段（quantification）：LLM 只产 ScenarioParams（不含Δmax），
  calculation_node 确定性代码调 calculate.run_full_calculation 算出
  Δmax/情景/逐年路径；排序违规以 parameter-level objection 打回
  quant_agent 调参数（不是调数字）。

若ReductionAgent/QuantAgent在工作中发现调研阶段存在缺口或疑似错误
（例如"这个含铜部位好像漏了"），不允许自己动手在state里加/改数据，
只能产出一条AmendmentRequest，交由人工/图A做局部重跑处理。

本图的CriticAgent职责范围与图A的CriticAgent完全不同，收窄为三个阶段
各自的检查清单：
  baseline_quantification阶段:
  - 归一化口径/高铜参考配置/功能单位是否有引用与理由
  - 代码确定性预检：因子覆盖参考配置、scope不重复、因子为正
  reduction_research阶段:
  - measure_without_case: 减量化措施是否有真实工程案例支撑
  - insufficient_evidence_quality: 案例/数据来源是否可信
  - scope_creep: 是否越权修改了调研阶段的事实结论
  quantification阶段:
  - incomplete_scenario_set / duplicate_scenario_entry（对ScenarioParams预检）
  - scope必须取自baseline阶段确定的子类别集合
  - Δmax排序在calculation_node计算后由代码复核，违规打回quant_agent
它完全不检查维度/结构相关的问题——那是图A的职责。

同一份approved调研成果可以被本图多次复用，跑不同情景假设，
不需要每次都重新触发图A的调研+人工审核流程。
"""

from __future__ import annotations
import operator
from typing import TypedDict, Annotated, Literal, Optional
from collections import Counter

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import SystemMessage

import calculate
from schemas import (
    BaselineParams, CalculationParams, CopperReductionMeasure, Objection,
    CriticReview, DebateLogEntry, SupervisorDecision, AgentRole,
    ProductResearchOutput, ProductReductionModel, AmendmentRequest,
    ScenarioParams, CountryMeasureReview, SCENARIO_SEVERITY_ORDER, upsert_objections,
)

MAX_ROUNDS_PER_STAGE = 3
MAX_TOTAL_ROUNDS = 14

REDUCTION_STAGE_AGENTS: dict[str, set[AgentRole]] = {
    "baseline_quantification": {"baseline_agent", "critic_agent"},
    "reduction_research": {"reduction_agent", "country_policy_agent", "critic_agent"},
    "quantification": {"quant_agent", "critic_agent"},
    "done": set(),
}


class ReductionState(TypedDict):
    product_id: str
    region_id: str
    region_name: str
    evidence_policy: str
    research_output: dict  # 只读！加载后的ProductResearchOutput，禁止被任何agent修改
    analysis_run_id: str
    baseline_year: int

    stage: Literal["baseline_quantification", "reduction_research",
                   "quantification", "done"]

    # stage 0：减量前单位铜强度
    baseline_params: Optional[dict]
    model_baselines: list[dict]
    baseline_calc_method: str

    reduction_measures: list[dict]
    country_measure_review: Optional[dict]
    scenario_params: list[dict]
    scenarios: list[dict]
    trajectory: list[dict]
    functional_unit_candidates: list[str]
    chosen_functional_unit: str
    baseline_unit_intensity: Optional[dict]
    calculation_params: Optional[dict]
    calculation_violations: list[str]
    baseline_retry_used: bool

    amendment_requests: Annotated[list[dict], operator.add]  # 纯追加，无需upsert
    objections: Annotated[list[dict], upsert_objections]
    debate_log: Annotated[list[dict], operator.add]

    round_in_stage: int
    total_round: int
    final_output: Optional[dict]


def _fallback_scenario_params(state: ReductionState) -> list[ScenarioParams]:
    """轮次耗尽的确定性兜底情景：按措施降幅升序嵌套分配 S1/S2/S3，
    逐级试算保证计算后Δmax严格递增；无法构造时抛错说明原因。
    （这是代码生成的保守缺省，非LLM调研产物，debate_log 中留痕）"""
    measures = state["reduction_measures"]
    refs = [b for b in state.get("model_baselines") or [] if b.get("is_reference")]
    if not measures or not refs:
        raise ValueError("轮次耗尽兜底失败：缺少措施或基线，无法构造情景，"
                         "请人工检查后重跑")
    ranked = sorted(measures, key=lambda m: (m.get("expected_reduction") or {}).get("value") or 0)
    ids = [m["measure_id"] for m in ranked]

    def delta(included):
        return calculate.combine_scenario_delta(refs[0], measures, included)

    s1, s2, s3 = ids[:1], ids[:1], ids
    if not (0 < delta(s1) < delta(s2) < delta(s3)):
        found = False
        for cut1 in range(1, len(ids)):
            for cut2 in range(cut1 + 1, len(ids) + 1):
                a, b = ids[:cut1], ids[:cut2]
                if 0 < delta(a) < delta(b) < delta(ids):
                    s1, s2, s3 = a, b, ids
                    found = True
                    break
            if found:
                break
        if not found:
            raise ValueError("轮次耗尽兜底失败：现有措施数据无法构造Δmax严格递增的"
                             "S1<S2<S3，请人工补充措施证据后重跑")

    def mk(sid, included):
        return ScenarioParams(
            scenario_id=sid,
            scenario_name={"S0": "基准情景", "S1": "普通减量",
                           "S2": "加速减量", "S3": "深度减量"}[sid],
            applicable_scope=None,
            scenario_definition=f"轮次耗尽系统兜底情景（{sid}）：按措施力度保守分配",
            included_measure_ids=list(included),
            measure_start_year=state["baseline_year"],
            target_achievement_year=calculate.HORIZON_YEAR,
            target_achievement_rate_pct=100,
            diffusion_method="linear",
            scenario_rationale="轮次耗尽系统兜底：非LLM调研产物，供确定性计算收尾；"
                               "正式结论请补充措施证据后重跑",
        )

    params = [mk("S0", []), mk("S1", s1), mk("S2", s2), mk("S3", s3)]
    # 自检：直接用与 calculation_node 完全相同的 run_full_calculation 链路
    # （包括基线选择逻辑），违规则直接报错，而不是打回 quant_agent
    # 重走相同兑底造成死循环
    probe = CalculationParams(
        functional_unit=state.get("chosen_functional_unit") or "",
        baseline_year=state["baseline_year"],
        horizon_year=calculate.HORIZON_YEAR,
        components=state["research_output"]["copper_components"],
        measures=measures,
        scenario_params=params,
        model_baselines=state.get("model_baselines") or [],
    )
    violations = calculate.run_full_calculation(probe)["violations"]
    if violations:
        raise ValueError("轮次耗尽兑底生成的情景未通过排序校验："
                         + "；".join(violations)
                         + "。请人工补充措施证据后重跑")
    return params


def _system_objection(state: ReductionState, target_agent: AgentRole,
                      flag_type: str, detail: str) -> Objection:
    """确定性预检/计算节点发现问题时，以系统名义注入blocking质询。

    与critic persona中"代码硬检结果必须据此生成objection"的机制同源：
    确定性代码能直接判定的问题，不依赖LLM是否"看见"。
    """
    return Objection(
        objection_id=f"sys-{target_agent}-{state['total_round']}-{flag_type}",
        raised_by="critic_agent",  # 代表确定性预检意见注入流程
        target_agent=target_agent,
        flag_type=flag_type,
        detail=detail,
        severity="blocking",
        round_raised=state["total_round"],
    )


# ============================================================
# 加载器：读取只读的调研成果，做前置校验 + 区域口径注入
# ============================================================

def load_research_output(state: ReductionState) -> Command:
    research = ProductResearchOutput.model_validate(state["research_output"])
    if research.research_status != "approved":
        raise ValueError(
            f"研究成果 v{research.research_version} 状态为「{research.research_status}」，"
            f"未经批准不允许进入减量化分析阶段。"
        )
    if research.geography is None or research.country_profile is None:
        raise ValueError("调研成果缺少严格国家范围或国家研究画像，不能进入国家级减量化分析")
    if research.evidence_assessment and research.evidence_assessment.tier == "insufficient":
        raise ValueError("调研成果的国家证据等级为insufficient，不能进入减量化分析")
    # 区域与证据口径以调研成果为权威源（runner 预填值仅作兜底）
    region_id = research.region_id if research.region_id != "unknown" \
        else (state.get("region_id") or "unknown")
    region_name = research.region_name if research.region_name != "unknown" \
        else (state.get("region_name") or "unknown")
    evidence_policy = (research.evidence_assessment.evidence_policy
                       if research.evidence_assessment
                       else (state.get("evidence_policy") or "region_specific"))
    log_entry = DebateLogEntry(
        round=0, speaker="supervisor", message_type="routing_decision",
        summary=f"已加载approved调研成果 v{research.research_version}，共"
                f"{len(research.copper_components)}个含铜部位；区域="
                f"{region_name}，证据口径={evidence_policy}，进入基线定量",
    )
    return Command(goto="supervisor", update={
        "region_id": region_id,
        "region_name": region_name,
        "evidence_policy": evidence_policy,
        "debate_log": [log_entry.model_dump()],
    })


# ============================================================
# Stage 0：baseline_agent（只提参数）+ baseline_calc_node（只算数）
# ============================================================

BASELINE_AGENT_PERSONA = """\
你是「定量分析师」，第0步任务：为"减量前单位铜强度"建立归一化口径。
你只提出带引用的参数，绝不做任何数值汇总计算——基线强度、构成权重
由确定性代码在你给出的参数上计算（你算的数不会被采纳）。

你必须输出 BaselineParams：
1. functional_unit_candidates / functional_unit_rationale / chosen_functional_unit：
   功能单位候选与选择（如 t Cu/MWp_DC、kg/台），理由需说明为何该口径可比；
2. normalization_factors：为下列每个主流型号取值给一个归一化换算因子，
   factor=单台/单件产品对应的功能单位数量（如单台635Wp→0.000635 MWp/台），
   unit_note写清换算口径，且必须附Citation（厂商规格书/标准/权威报告）；
3. high_copper_references：每个applicable_scope（子类别；无子类别则仅一条
   applicable_scope=None）恰好一条高铜参考配置选择：chosen_value_id+
   rationale+Citation。参考配置是该子类别减铜潜力的统一比较基准(S0基准)。

【区域口径】本次研究区域：{region_name}（区域证据口径：{evidence_policy}）。
若某区域已普遍采用铝代铜/铝绕组（部件regional_presence<100或
region_basis=global_fallback），基线即反映该区域真实材料结构；
"区域现状已含替代"的部分计入S0基准，禁止再计为减量潜力。

型号取值列表（value_id/value_name，normalization_factors与
high_copper_references中的ID必须取自这里）：
{dimension_values}

含铜部件快照（只读）：
{copper_components}

{objection_context}
"""


def baseline_agent(state: ReductionState, llm) -> Command:
    research = state["research_output"]
    my_objections = [
        o for o in state["objections"]
        if o["target_agent"] == "baseline_agent" and not o.get("addressed")
    ]
    objection_context = (
        "CriticAgent/确定性预检对你此前基线参数的质询，请逐条修订：\n" +
        "\n".join(f"- [{o['objection_id']}] {o['detail']}" for o in my_objections)
    ) if my_objections else ""

    dim_values = []
    for d in research.get("classification_dimensions", []):
        for v in d.get("values", []):
            dim_values.append(f"- {v.get('value_id')} / {v.get('value_name')}")
    messages = [
        SystemMessage(content=BASELINE_AGENT_PERSONA.format(
            region_name=state.get("region_name") or "未知",
            evidence_policy=state.get("evidence_policy") or "region_specific",
            dimension_values="\n".join(dim_values) or "(无)",
            copper_components=research["copper_components"],
            objection_context=objection_context,
        )),
    ]
    structured_llm = llm.with_structured_output(BaselineParams)
    params = structured_llm.invoke(messages)

    updated_objections = [
        {**o, "addressed": True, "response_note": "baseline_agent已修订参数"}
        for o in my_objections
    ]
    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="quant_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"提出基线归一化参数：功能单位={params.chosen_functional_unit}，"
                f"{len(params.normalization_factors)}个换算因子，"
                f"{len(params.high_copper_references)}个高铜参考配置",
    )
    return Command(
        goto="supervisor",
        update={
            "baseline_params": params.model_dump(),
            "functional_unit_candidates": params.functional_unit_candidates,
            "chosen_functional_unit": params.chosen_functional_unit,
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


def _baseline_hard_check(state: ReductionState) -> list[str]:
    """baseline_params 的确定性预检：因子覆盖参考配置、scope唯一、因子为正"""
    warnings: list[str] = []
    bp = state.get("baseline_params") or {}
    factors = {f.get("value_id"): f for f in bp.get("normalization_factors", [])}
    refs = bp.get("high_copper_references", [])
    scopes = [r.get("applicable_scope") for r in refs]
    dup_scopes = [s for s, c in Counter(scopes).items() if c > 1]
    if dup_scopes:
        warnings.append(f"high_copper_references 的 applicable_scope 重复: {dup_scopes}，"
                        f"每个子类别只能有一个高铜参考配置")
    if not refs:
        warnings.append("high_copper_references 为空：至少需要一条"
                        "（无子类别产品填 applicable_scope=None）")
    for r in refs:
        vid = r.get("chosen_value_id")
        if vid not in factors:
            warnings.append(f"高铜参考配置 {vid}(scope={r.get('applicable_scope')}) "
                            f"缺少归一化换算因子，无法计算基线强度")
    for f in bp.get("normalization_factors", []):
        if not f.get("factor") or f.get("factor", 0) <= 0:
            warnings.append(f"型号 {f.get('value_id')} 的换算因子必须为正数")
        # 因子缺 Citation 属证据质量问题（影响可信度不影响可计算性），
        # 由 critic 语义层审查，不作为硬检阻断项——线上实测对部分模型
        # 该要求过苛会导致基线阶段反复卡死
    return warnings


def baseline_calc_node(state: ReductionState) -> Command:
    """确定性节点：在 baseline_params 上计算减量前单位铜强度（模板Section3）。"""
    research = state["research_output"]
    warnings = _baseline_hard_check(state)
    if warnings:
        if state.get("baseline_retry_used"):
            # 已打回重试过一次仍失败 → 诚实报错，避免 objection 循环
            raise ValueError("基线参数连续两次未通过确定性预检："
                             + "；".join(warnings))
        obj = _system_objection(
            state, "baseline_agent", "insufficient_evidence_quality",
            "[确定性预检] 基线参数不可计算：\n" + "\n".join(warnings))
        return Command(
            goto="supervisor",
            update={
                "objections": [obj.model_dump()],
                "baseline_retry_used": True,
                "round_in_stage": 0,
                "debate_log": [DebateLogEntry(
                    round=state["total_round"], speaker="quant_agent",
                    message_type="objection",
                    summary=f"基线参数确定性预检失败({len(warnings)}项)，打回baseline_agent",
                ).model_dump()],
                "total_round": state["total_round"] + 1,
            },
        )

    baselines = calculate.compute_model_baselines(
        research["copper_components"], state["baseline_params"],
        dimensions=research.get("classification_dimensions"))
    baselines_by_scope = calculate.compute_baseline_intensity(baselines)
    primary = baselines_by_scope.get(None) or next(iter(baselines_by_scope.values()))
    bp = BaselineParams.model_validate(state["baseline_params"])
    trace = (f"calculate.compute_model_baselines: intensity=Σ(unit_mass×presence/100)/factor；"
             f"functional_unit={bp.chosen_functional_unit}；"
             f"baselines={len(baselines)}；scopes={sorted((str(s) for s in baselines_by_scope), key=str)}")
    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="quant_agent",
        message_type="routing_decision",
        summary=f"确定性计算完成：{len(baselines)}条型号基线，"
                f"主基线强度={primary.value} {primary.unit}",
    )
    return Command(
        goto="supervisor",
        update={
            "model_baselines": [b.model_dump() for b in baselines],
            "baseline_unit_intensity": primary.model_dump(),
            "baseline_calc_method": trace,
            "baseline_retry_used": False,
            "stage": "reduction_research",
            "round_in_stage": 0,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# Agent：ReductionAgent —— 真正的双输出(措施 + 修订请求)
# ============================================================

class ReductionAgentOutput(BaseModel):
    """ReductionAgent单次结构化调用的完整输出契约。
    用一个包装模型同时承接措施和修订请求，而不是分两次调用或用
    空列表占位——这样LLM在同一次推理里就能"一边提措施、一边意识到
    调研数据有缺口"，两者产生于同一个上下文，逻辑上更一致。
    """
    measures: list[CopperReductionMeasure] = Field(default_factory=list)
    amendment_requests: list[AmendmentRequest] = Field(
        default_factory=list,
        description="若在提出措施过程中发现已批准的调研数据(含铜部位清单)似乎"
                    "有遗漏或错误，不要擅自在measures里编造/修改事实数据，"
                    "而是在这里提交一条修订请求，交由调研团队处理。"
                    "没有发现问题时这里保持空列表。"
    )


REDUCTION_AGENT_PERSONA = """\
你是「铜减量化技术顾问」，你的信誉建立在"言之有据"上——绝不允许为凑数
编造工程案例。每条措施必须附真实的EngineeringCase(项目名/实施方/年份/
效果/引用)，若某技术仅停留在实验室阶段，如实标注maturity="lab"，
不要包装成已商业化。

【重要边界】你只能基于以下已批准的含铜部位数据提出措施，绝不能自行新增
或修改含铜部位的事实数据。如果你在调研措施过程中发现这份数据似乎有遗漏
或错误（例如某个应该含铜的部位没有被列出、或某个部位的铜量数据明显与
你查到的工程资料矛盾），不要擅自在你的措施输出中添加/修改事实数据，
而是通过amendment_requests字段提出修订请求，交由调研团队处理——
这是你发现调研缺口时唯一被允许的反馈渠道。

【expected_reduction 口径】填写"目标部件铜质量降低%"（0-100），
确定性代码会按基线构成权重把它换算为情景Δmax与绝对降铜量；
同一部件多条措施的效果由代码连乘组合，你不需要也不允许自行汇总。

【区域口径】研究区域：{region_name}。若某替代路线在该区域已是主流现状
（体现在基线 regional_presence/region_basis 中），它已计入S0基准，
禁止再把它包装成减量措施重复计算。

已批准的含铜部位（只读，来自research_version={research_version}）：
{copper_components}

减量前单位铜强度基线（只读，确定性计算产出，措施口径须与之一致）：
{model_baselines}

{objection_context}
"""


def reduction_agent(state: ReductionState, llm) -> Command:
    research = state["research_output"]
    my_objections = [
        o for o in state["objections"]
        if o["target_agent"] == "reduction_agent" and not o.get("addressed")
    ]
    objection_context = (
        "CriticAgent指出以下措施缺乏案例支撑或存在越权问题，请修正：\n" +
        "\n".join(f"- [{o['objection_id']}] {o['detail']}" for o in my_objections)
    ) if my_objections else ""

    messages = [
        SystemMessage(content=REDUCTION_AGENT_PERSONA.format(
            objection_context=objection_context,
            research_version=research["research_version"],
            copper_components=research["copper_components"],
            model_baselines=state.get("model_baselines") or "(尚未计算)",
            region_name=state.get("region_name") or "未知",
        )),
    ]
    structured_llm = llm.with_structured_output(ReductionAgentOutput)
    output = structured_llm.invoke(messages)

    # 措施单调不减：修订是“修正/新增”而非“覆盖”。若本轮输出比上一轮
    # 少（LLM 修订时常见地丢失措施），用上一轮措施补齐，避免轮次耗尽时
    # 措施数不足以构造 S1<S2<S3 严格递增情景。
    prev_measures = {m["measure_id"]: m for m in state.get("reduction_measures") or []}
    new_measures = {m.measure_id: m.model_dump() for m in output.measures}
    disallowed = {a["measure_id"] for a in
                  (state.get("country_measure_review") or {}).get("assessments", [])
                  if not a.get("applicable_in_country")}
    restored = [mid for mid in prev_measures
                if mid not in new_measures and mid not in disallowed]
    merged_measures = list(new_measures.values()) + \
        [prev_measures[mid] for mid in restored]

    # target_product_id/target_research_version不能让LLM自己猜——这两个是
    # 精确的系统标识符，必须由代码从当前运行环境注入真实值，防止LLM编造
    # 或记错版本号导致AmendmentRequest指向错误的调研版本。
    amendment_requests = [
        a.model_copy(update={
            "requested_by": "reduction_agent",
            "target_product_id": state["product_id"],
            "target_research_version": research["research_version"],
        })
        for a in output.amendment_requests
    ]

    updated_objections = [
        {**o, "addressed": True, "response_note": "reduction_agent已修正"} for o in my_objections
    ]

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="reduction_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"提出 {len(output.measures)} 条减量化措施"
                + (f"（修订合并后保留 {len(merged_measures)} 条，恢复被丢弃: {restored}）" if restored else "")
                + (f"，另提交 {len(amendment_requests)} 条调研修订请求"
                    if amendment_requests else ""),
        ref_ids=[m.measure_id for m in output.measures],
    )

    return Command(
        goto="supervisor",
        update={
            "reduction_measures": merged_measures,
            "country_measure_review": None,
            "amendment_requests": [a.model_dump() for a in amendment_requests],
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# Agent：CountryPolicyAgent —— 国家措施适用性核查
# ============================================================

COUNTRY_POLICY_AGENT_PERSONA = """\
你是「国家措施适用性研究员」。逐条核查以下铜减量措施在 {country_name}
是否已有应用、试点、限制或禁止条件。country_id 必须填写 {country_id}。
不得用全球案例证明本国可实施；每条措施必须附目标国家来源，并填写证据年份、
统计口径和URL。若证据无法证明适用，applicable_in_country=false。

待核查措施：
{measures}
"""


def country_policy_agent(state: ReductionState, llm) -> Command:
    geography = state["research_output"]["geography"]
    review = llm.with_structured_output(CountryMeasureReview).invoke([
        SystemMessage(content=COUNTRY_POLICY_AGENT_PERSONA.format(
            country_name=geography["country_name"], country_id=geography["country_id"],
            measures=state["reduction_measures"],
        ))
    ])
    known = {m["measure_id"] for m in state["reduction_measures"]}
    assessed = {a.measure_id for a in review.assessments}
    if assessed != known:
        raise ValueError(f"国家措施核查未逐条覆盖全部措施：缺少{sorted(known-assessed)}，"
                         f"多出{sorted(assessed-known)}")
    objections = []
    for a in review.assessments:
        if not a.applicable_in_country:
            objections.append(_system_objection(
                state, "reduction_agent", "insufficient_evidence_quality",
                f"措施{a.measure_id}缺少目标国家适用性：{a.national_constraints}"))
        else:
            measure = next(m for m in state["reduction_measures"]
                           if m["measure_id"] == a.measure_id)
            measure["additional_citations"] = [
                *(measure.get("additional_citations") or []),
                *[c.model_dump() for c in a.citations],
            ]
    return Command(goto="supervisor", update={
        "country_measure_review": review.model_dump(),
        "objections": [o.model_dump() for o in objections],
        "debate_log": [DebateLogEntry(
            round=state["total_round"], speaker="country_policy_agent",
            message_type="objection" if objections else "approval",
            summary=f"完成{len(review.assessments)}条措施的国家适用性核查",
        ).model_dump()],
        "total_round": state["total_round"] + 1,
    })


# ============================================================
# 确定性预检：基线/情景参数
# ============================================================

def _scenario_params_hard_check(state: ReductionState) -> list[str]:
    """对 ScenarioParams（计算前）的确定性预检"""
    warnings: list[str] = []
    params = state.get("scenario_params") or []
    baseline_scopes = {b.get("applicable_scope") for b in state.get("model_baselines") or []}
    by_scope: dict[Optional[str], list[dict]] = {}
    for p in params:
        by_scope.setdefault(p.get("applicable_scope"), []).append(p)
    if not params:
        warnings.append("尚未产出任何ScenarioParams")
    for scope, items in by_scope.items():
        label = scope or "(无子类别)"
        if scope not in baseline_scopes:
            warnings.append(f"子类别「{label}」不在baseline阶段确定的子类别集合"
                            f"{sorted((str(s) for s in baseline_scopes), key=str)}中")
        ids = [p.get("scenario_id") for p in items]
        dups = [i for i, c in Counter(ids).items() if c > 1]
        if dups:
            warnings.append(f"子类别「{label}」情景代码重复: {dups}，"
                            f"请生成duplicate_scenario_entry类型的objection，target_agent为quant_agent")
            continue
        missing = [c for c in SCENARIO_SEVERITY_ORDER if c not in ids]
        if missing:
            warnings.append(f"子类别「{label}」缺少情景: {missing}，"
                            f"S0-S3四档必须齐全，请生成incomplete_scenario_set类型的objection")
            continue
        s0 = next(p for p in items if p.get("scenario_id") == "S0")
        if s0.get("included_measure_ids"):
            warnings.append(f"子类别「{label}」S0纳入了措施——基准情景必须为空")
    for p in params:
        if p.get("target_achievement_year", 0) < p.get("measure_start_year", 0):
            warnings.append(f"情景{p.get('scenario_id')}(scope={p.get('applicable_scope')})"
                            f"目标年早于启动年")
    return warnings


# ============================================================
# CriticAgent —— 按阶段切换检查清单
# ============================================================

CRITIC_AGENT_PERSONA = """\
你是「审查专家」，只负责减量化图(图B)内部的审查，完全不涉及维度/结构相关
问题的检查（那是调研团队的职责，本阶段不应该出现，若出现说明有人越权）。

当前阶段: {stage}

{checklist}

【你有跨轮记忆，且必须真正使用它】以下是你此前提出、且对方已声称"已回应"
的objection，请逐条复核对方的response_note是否真的解决了问题：
- 若认可，不需要对它做任何事
- 若不认可，把它的objection_id填进reopened_objection_ids，并在
  reopen_reasoning里说明理由——这是你复核权力的唯一生效渠道
{prior_addressed_objections}

请输出你的完整判断（CriticReview结构）。
"""

BASELINE_STAGE_CHECKLIST = """\
本阶段检查清单（baseline_quantification，减量前单位铜强度口径）：
1. 归一化换算因子是否附Citation？unit_note口径是否足以复现换算？
2. 每个子类别的高铜参考配置选择是否有理由？是否符合"高铜技术参考基准
   （统一比较减铜潜力）"的口径，而不是随手选了一个低铜配置？
3. 功能单位选择是否有理由？是否与该产品主流强度口径一致？
4. 【确定性预检】以下是代码对baseline_params的复核结果，若有内容你
   【必须】据此生成对应objection（target_agent=baseline_agent）：
{hard_check_warnings}

当前BaselineParams提案：
{baseline_params}
"""

REDUCTION_STAGE_CHECKLIST = """\
本阶段检查清单：
1. measure_without_case: 每条措施是否有真实的EngineeringCase，
   若maturity="commercial"却没有真实商业化项目佐证，视为blocking
2. insufficient_evidence_quality: 引用来源是否可信、是否自相矛盾
3. scope_creep: ReductionAgent是否越权修改了含铜部位的事实数据
   （对照下方"已批准的含铜部位快照"，若发现措施引用的target_component_ids
   指向了快照中不存在的部件，判定为blocking的scope_creep）
4. trade_offs是否如实报告，是否存在只报喜不报忧的可疑迹象
5. expected_reduction是否按"目标部件铜质量降低%"口径填写（0-100的%），
   而不是把绝对量或情景幅度填进来

已批准的含铜部位快照（只读基准，用于比对是否被篡改）：
{copper_components_snapshot}

当前ReductionAgent提案：
{reduction_measures}
"""

QUANTIFICATION_STAGE_CHECKLIST = """\
本阶段检查清单（审查对象是ScenarioParams参数，Δmax由代码计算、不在此审查）：
1. incomplete_scenario_set / duplicate_scenario_entry: 见下方确定性预检
2. 每个情景的scenario_rationale是否有引用支撑，是否只是空泛描述
3. diffusion_method的选择是否有依据说明，还是随意指定
4. 措施组合分配是否合理：若某情景纳入的措施集合明显弱于更低档情景，
   代码计算后Δmax排序会违规——你可以提前指出，但最终以计算结果为准
5. S0是否严格为空措施集合（基准情景）

以下是代码对本轮情景参数提案的确定性预检结果（若有内容，你【必须】据此
生成对应类型的objection，不能视而不见）：
{hard_check_warnings}

当前情景参数提案（ScenarioParams，不含Δmax）：
{scenario_params}

子类别集合（baseline阶段确定）：{baseline_scopes}
"""


def critic_agent(state: ReductionState, llm) -> Command:
    prior_addressed = [o for o in state["objections"] if o.get("addressed")]
    prior_summary = "\n".join(
        f"- [{o['objection_id']}] 曾要求{o['target_agent']}: {o['detail']} "
        f"| 回应: {o.get('response_note', '无')}"
        for o in prior_addressed
    ) or "(无历史记录，本轮之前没有任何已回应的objection需要复核)"

    if state["stage"] == "baseline_quantification":
        hard_warnings = _baseline_hard_check(state)
        checklist = BASELINE_STAGE_CHECKLIST.format(
            hard_check_warnings="\n".join(hard_warnings) or "(未发现确定性问题)",
            baseline_params=state.get("baseline_params") or "(尚未产出)",
        )
    elif state["stage"] == "reduction_research":
        valid_component_ids = {c["component_id"] for c in state["research_output"]["copper_components"]}
        hard_violations = [
            m for m in state["reduction_measures"]
            if any(cid not in valid_component_ids for cid in m.get("target_component_ids", []))
        ]
        checklist = REDUCTION_STAGE_CHECKLIST.format(
            copper_components_snapshot=state["research_output"]["copper_components"],
            reduction_measures=state["reduction_measures"],
        )
        if hard_violations:
            checklist += (
                f"\n\n【系统预警-确定性检测】以下措施引用了不存在于已批准快照中的"
                f"component_id，这是可以直接判定的scope_creep，请务必生成对应objection: "
                f"{[m['measure_id'] for m in hard_violations]}"
            )
    else:  # quantification
        hard_warnings = _scenario_params_hard_check(state)
        checklist = QUANTIFICATION_STAGE_CHECKLIST.format(
            hard_check_warnings="\n".join(hard_warnings) or "(未发现确定性问题)",
            scenario_params=state.get("scenario_params") or "(尚未产出)",
            baseline_scopes=sorted((str(b.get("applicable_scope"))
                                    for b in state.get("model_baselines") or []), key=str),
        )

    messages = [
        SystemMessage(content=CRITIC_AGENT_PERSONA.format(
            stage=state["stage"], checklist=checklist, prior_addressed_objections=prior_summary,
        )),
    ]
    structured_llm = llm.with_structured_output(CriticReview)
    review = structured_llm.invoke(messages)

    new_objections = [
        {**o.model_dump(), "round_raised": state["total_round"]}
        for o in review.new_objections
    ]

    prior_by_id = {o["objection_id"]: o for o in state["objections"]}
    reopened = []
    for oid in review.reopened_objection_ids:
        original = prior_by_id.get(oid)
        if original is None:
            continue  # critic复核了一个不存在的id，忽略而不是崩溃
        reason = review.reopen_reasoning.get(oid, "critic复核后认为回应不成立，但未给出具体理由")
        reopened.append({
            **original,
            "addressed": False,
            "critic_accepts_response": False,
            "detail": f"{original['detail']}\n[critic复核意见]: {reason}",
            "round_raised": state["total_round"],
        })

    all_objection_updates = new_objections + reopened

    summary_parts = []
    if new_objections:
        summary_parts.append(f"提出{len(new_objections)}条新质询")
    if reopened:
        summary_parts.append(f"复核后重新打开{len(reopened)}条历史objection")
    default_approval = {
        "baseline_quantification": "本轮无异议，批准进入确定性基线计算",
        "reduction_research": "本轮无异议，批准进入定量建模",
        "quantification": "本轮无异议，批准进入确定性情景/路径计算",
    }[state["stage"]]
    summary = "；".join(summary_parts) if summary_parts else default_approval

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="critic_agent",
        message_type="objection" if all_objection_updates else "approval",
        summary=summary,
        ref_ids=[o["objection_id"] for o in all_objection_updates],
    )

    return Command(
        goto="supervisor",
        update={
            "objections": all_objection_updates,
            "debate_log": [log_entry.model_dump()],
            "round_in_stage": state["round_in_stage"] + 1,
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# QuantAgent：只产 ScenarioParams（Δmax 交给代码）
# ============================================================

SCENARIO_DEFINITION_PROMPT = """\
你是「定量分析师」，第二步任务：基于已获批准的减量化措施与基线，为每个
applicable_scope(子类别)分别设定固定的4档情景参数：S0/S1/S2/S3。
你只输出 ScenarioParams（不含Δmax）——Δmax由确定性代码按"措施组合×
基线构成权重"计算，你填的任何Δmax都不会被采纳。

【子类别集合固定】只能使用以下baseline阶段确定的子类别（含None）：
{baseline_scopes}

【重要：情景框架是固定的，不是你的调研/创作对象】
- S0「基准情景」：不纳入任何措施，included_measure_ids必须为空
- S1「普通减量」：力度最小的实质减量情景
- S2「加速减量」：力度中等
- S3「深度减量」：力度最大
这4个情景代码和名称必须原样使用，不允许增减情景数量、不允许改名、
不允许只出2-3档。每个applicable_scope下都必须凑齐这4档，且每档只能一次。

【S1/S2/S3的措施组合不要求父子集关系】
互斥措施可以分别归属不同情景。但硬约束是：代码按组合算出的Δmax必须
严格递增 Δmax(S0)=0 < Δmax(S1) < Δmax(S2) < Δmax(S3)。若你的组合算出来
S2不如S1，代码会报排序违规并打回——请重新分配措施组合（不是改数字）。

若某措施在不同子类别下效果不同，对应情景也应按子类别拆成多条记录
(同一scenario_id，不同applicable_scope)。

已获批准的减量化措施: {reduction_measures}
基线（只读）: {model_baselines}
基准年: {baseline_year}

{objection_context}
"""


def quant_agent(state: ReductionState, llm) -> Command:
    my_objections = [
        o for o in state["objections"]
        if o["target_agent"] == "quant_agent" and not o.get("addressed")
    ]
    objection_context = ""
    if my_objections:
        sorted_obj = sorted(
            my_objections,
            key=lambda x: x["flag_type"] not in (
                "scenario_severity_ordering_violation", "incomplete_scenario_set",
                "duplicate_scenario_entry",
            ),
        )
        objection_context = "\n\nCriticAgent/确定性计算对你此前情景参数的未解决质询，" \
            "请优先处理排序/完整性类问题，逐条修订（调整措施组合与参数，不是改数字）：\n" + \
            "\n".join(f"- [{o['objection_id']}] ({o['flag_type']}) {o['detail']}" for o in sorted_obj)

    scenario_messages = [
        SystemMessage(content=SCENARIO_DEFINITION_PROMPT.format(
            reduction_measures=state["reduction_measures"],
            model_baselines=state.get("model_baselines") or "(无)",
            baseline_scopes=sorted((str(b.get("applicable_scope"))
                                    for b in state.get("model_baselines") or []), key=str),
            baseline_year=state["baseline_year"],
            objection_context=objection_context,
        )),
    ]
    scenario_llm = llm.with_structured_output(list[ScenarioParams])
    scenario_params = scenario_llm.invoke(scenario_messages)

    updated_objections = [
        {**o, "addressed": True, "response_note": "quant_agent已重新分配情景措施/参数"}
        for o in my_objections
    ]

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="quant_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"{'修订' if my_objections else '定义'} {len(scenario_params)} 组情景参数"
                f"（Δmax待确定性计算）",
    )

    return Command(
        goto="supervisor",
        update={
            "scenario_params": [s.model_dump() for s in scenario_params],
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# calculation_node：确定性情景Δmax/路径计算（零LLM）
# ============================================================

def calculation_node(state: ReductionState) -> Command:
    research = state["research_output"]
    params = CalculationParams(
        functional_unit=state.get("chosen_functional_unit") or "",
        normalization_note=state.get("baseline_calc_method") or "",
        baseline_year=state["baseline_year"],
        horizon_year=calculate.HORIZON_YEAR,
        components=research["copper_components"],
        measures=state["reduction_measures"],
        scenario_params=[ScenarioParams.model_validate(p)
                         for p in state.get("scenario_params") or []],
        model_baselines=[b if hasattr(b, "model_dump") else b
                         for b in _validated_baselines(state)],
    )
    result = calculate.run_full_calculation(params)

    if result["violations"]:
        if state.get("calculation_violations"):
            # quant_agent 修订一轮后仍违规 → 改用代码生成的保守情景
            # （scope=None 单组，自检通过）收尾，保证产出可落盘；
            # LLM 情景参数保留在 state/日志中供审计
            try:
                fallback = _fallback_scenario_params(state)
            except ValueError as exc:
                raise ValueError(f"情景参数连续两轮未通过校验且兑底失败：{exc}")
            params = params.model_copy(update={"scenario_params": fallback})
            result = calculate.run_full_calculation(params)
            if result["violations"]:
                raise ValueError("兑底情景仍未通过校验："
                                 + "；".join(result["violations"]))
            result["calc_trace"] += "；[fallback] quant_agent情景两轮违规，已切换代码生成保守情景收尾"
        else:
            first = result["violations"][0]
            if "重复" in first:
                flag = "duplicate_scenario_entry"
            elif "缺少" in first:
                flag = "incomplete_scenario_set"
            else:
                flag = "scenario_severity_ordering_violation"
            obj = _system_objection(
                state, "quant_agent", flag,
                "[确定性计算] 计算后Δmax校验未通过：\n" + "\n".join(result["violations"]))
            return Command(
                goto="supervisor",
                update={
                    "calculation_params": params.model_dump(),
                    "calculation_violations": result["violations"],
                    "objections": [obj.model_dump()],
                    "round_in_stage": 0,
                    "debate_log": [DebateLogEntry(
                        round=state["total_round"], speaker="quant_agent",
                        message_type="objection",
                        summary=f"确定性计算检出{len(result['violations'])}项排序/完整性违规，"
                                f"打回quant_agent调整措施组合",
                    ).model_dump()],
                    "total_round": state["total_round"] + 1,
                },
            )

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="quant_agent",
        message_type="routing_decision",
        summary=f"确定性计算完成：{len(result['scenarios'])}个情景(Δmax代码算出)、"
                f"{len(result['trajectory'])}个路径点；{result['calc_trace']}",
    )
    return Command(
        goto="supervisor",
        update={
            "calculation_params": params.model_dump(),
            "calculation_violations": [],
            "scenarios": [s.model_dump() for s in result["scenarios"]],
            "trajectory": [t.model_dump() for t in result["trajectory"]],
            "stage": "done",
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


def _validated_baselines(state: ReductionState):
    from schemas import ModelBaselineIntensity
    return [ModelBaselineIntensity.model_validate(b)
            for b in state.get("model_baselines") or []]


# ============================================================
# SupervisorAgent
# ============================================================

SUPERVISOR_PERSONA = """\
你是减量化团队主管，协调BaselineAgent/ReductionAgent/CriticAgent/QuantAgent。
这个团队只读消费调研团队已批准的事实数据，不涉及维度/结构问题的判断。
所有确定性计算（基线强度/Δmax/路径）由代码节点执行，不派给任何agent。

当前阶段: {stage}
本阶段第{round_in_stage}轮 / 总第{total_round}轮
尚未解决的blocking质询: {blocking_objections}
BaselineAgent: {baseline_status}
ReductionAgent: {reduction_status}
QuantAgent: {scenario_status}

决策规则：
- baseline_quantification阶段：若尚未产出归一化参数，派baseline_agent；
  若已产出但本轮未经critic审查，派critic_agent（审查通过后代码节点
  会自动执行基线计算，不需要你派遣）
- reduction_research阶段：若存在scope_creep等blocking objection，优先派
  reduction_agent修正；若尚未产出措施，派reduction_agent；若措施已产出但
  本轮未经critic审查，派critic_agent；若critic本轮无异议，推进到quant_agent
- quantification阶段：若存在排序/完整性类blocking objection，【必须】派
  quant_agent重新分配措施组合；若尚未产出情景参数，派quant_agent；
  若已产出但本轮未经critic审查，派critic_agent；若critic本轮无异议，
  代码节点会自动执行情景/路径计算并进入整合
"""


def supervisor(state: ReductionState, llm) -> Command:
    stage_agents = REDUCTION_STAGE_AGENTS.get(state["stage"], set())
    # blocking 只按当前阶段的责任agent过滤：跨阶段遗留的历史质询
    # （如 baseline 阶段的归一化因子质询）保留在 state 供审计，
    # 但不再阻塞后续阶段的路由
    blocking = [o for o in state["objections"]
                if o["severity"] == "blocking" and not o.get("addressed")
                and o.get("target_agent") in stage_agents]
    blocking_sorted = sorted(
        blocking,
        key=lambda x: x["flag_type"] not in (
            "scenario_severity_ordering_violation", "incomplete_scenario_set",
            "duplicate_scenario_entry", "scope_creep",
        ),
    )

    # 轮次耗尽兜底：不能直接进 integration——未经过确定性计算的中间产物
    # 会被 integration 守护拦下（诚实失败）。应先用已有参数走完确定性
    # 计算；reduction_research 阶段耗尽时代码生成保守情景分配。
    def _finish_with_calc(reason: str) -> Command:
        if state["stage"] == "quantification" and state.get("scenario_params"):
            return Command(goto="calculation_node", update={
                "debate_log": [DebateLogEntry(
                    round=state["total_round"], speaker="supervisor",
                    message_type="routing_decision", summary=reason,
                ).model_dump()],
            })
        if state["stage"] == "baseline_quantification" and state.get("baseline_params"):
            return Command(goto="baseline_calc_node", update={
                "stage": "reduction_research", "round_in_stage": 0,
                "debate_log": [DebateLogEntry(
                    round=state["total_round"], speaker="supervisor",
                    message_type="routing_decision", summary=reason,
                ).model_dump()],
            })
        if state["stage"] == "reduction_research" and state.get("reduction_measures"):
            fallback = _fallback_scenario_params(state)
            return Command(goto="calculation_node", update={
                "stage": "quantification",
                "scenario_params": [p.model_dump() for p in fallback],
                "debate_log": [DebateLogEntry(
                    round=state["total_round"], speaker="supervisor",
                    message_type="routing_decision",
                    summary=reason + "；已由代码生成保守情景分配（非LLM调研产物）",
                ).model_dump()],
            })
        return Command(goto="integration", update={
            "debate_log": [DebateLogEntry(
                round=state["total_round"], speaker="supervisor",
                message_type="routing_decision",
                summary=reason + "；无可计算参数，交由integration守护报错",
            ).model_dump()],
        })
    
    if state["total_round"] >= MAX_TOTAL_ROUNDS:
        return _finish_with_calc("达到总轮次上限")

    if state["stage"] == "done":
        return Command(goto="integration")

    if (state["stage"] == "reduction_research" and state.get("reduction_measures")
            and not state.get("country_measure_review") and not blocking_sorted):
        return Command(goto="country_policy_agent", update={
            "debate_log": [DebateLogEntry(
                round=state["total_round"], speaker="supervisor",
                message_type="routing_decision", summary="派遣国家措施适用性核查",
            ).model_dump()],
        })
    if (state["stage"] == "reduction_research" and state.get("country_measure_review")
            and state["round_in_stage"] < 1 and not blocking_sorted):
        return Command(goto="critic_agent")

    # 代码强制路由：critic已审查且无blocking时，确定性计算节点自动执行，
    # 不经过LLM决策（LLM的next_agent枚举里也没有代码节点）。
    if state["stage"] == "baseline_quantification" and state.get("baseline_params") \
            and state["round_in_stage"] >= 1 and not blocking_sorted:
        return Command(goto="baseline_calc_node", update={
            "debate_log": [DebateLogEntry(
                round=state["total_round"], speaker="supervisor",
                message_type="routing_decision",
                summary="基线参数已通过critic审查，派遣确定性计算节点baseline_calc_node",
            ).model_dump()],
        })
    if state["stage"] == "quantification" and state.get("scenario_params") \
            and state["round_in_stage"] >= 1 and not blocking_sorted:
        return Command(goto="calculation_node", update={
            "debate_log": [DebateLogEntry(
                round=state["total_round"], speaker="supervisor",
                message_type="routing_decision",
                summary="情景参数已通过critic审查，派遣确定性计算节点calculation_node",
            ).model_dump()],
        })

    messages = [
        SystemMessage(content=SUPERVISOR_PERSONA.format(
            stage=state["stage"], round_in_stage=state["round_in_stage"],
            total_round=state["total_round"], blocking_objections=blocking_sorted or "(无)",
            baseline_status="已产出" if state.get("baseline_params") else "未产出",
            reduction_status="已产出" if state["reduction_measures"] else "未产出",
            scenario_status="已产出" if state.get("scenario_params") else "未产出",
        )),
    ]
    structured_llm = llm.with_structured_output(SupervisorDecision)
    decision = structured_llm.invoke(messages)

    # 允许的阶段内合法路由集合。各阶段产物已产出且无blocking质询时，
    # 下一阶段的入口agent是合法下一站——否则supervisor永远无法把流程
    # 推进到下一阶段(LLM的合法决策会被下面的fallback误判为越界而覆盖)。
    allowed_agents = set(stage_agents)
    if (state["stage"] == "reduction_research"
            and state["reduction_measures"] and not blocking_sorted):
        allowed_agents.add("quant_agent")

    if decision.next_agent not in allowed_agents and not decision.should_terminate:
        decision = _fallback_rule_based_decision(state, blocking_sorted)

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="supervisor", message_type="routing_decision",
        summary=f"派遣 {decision.next_agent}：{decision.reasoning}",
    )

    next_stage, next_round_in_stage = state["stage"], state["round_in_stage"]
    if state["stage"] == "reduction_research" and decision.next_agent == "quant_agent":
        next_stage, next_round_in_stage = "quantification", 0

    if decision.should_terminate or next_round_in_stage >= MAX_ROUNDS_PER_STAGE:
        if state["stage"] == "quantification" and state.get("scenario_params"):
            return Command(goto="calculation_node", update={
                "stage": next_stage, "debate_log": [log_entry.model_dump()],
            })
        if state["stage"] == "baseline_quantification" and state.get("baseline_params"):
            return Command(goto="baseline_calc_node", update={
                "stage": "reduction_research", "round_in_stage": 0,
                "debate_log": [log_entry.model_dump()],
            })
        if state["stage"] == "reduction_research" and state.get("reduction_measures"):
            fallback = _fallback_scenario_params(state)
            return Command(goto="calculation_node", update={
                "stage": "quantification",
                "scenario_params": [p.model_dump() for p in fallback],
                "debate_log": [log_entry.model_dump()],
            })
        return Command(goto="integration", update={
            "stage": next_stage, "debate_log": [log_entry.model_dump()],
        })

    return Command(
        goto=decision.next_agent,
        update={"stage": next_stage, "round_in_stage": next_round_in_stage,
                "debate_log": [log_entry.model_dump()]},
    )


def _fallback_rule_based_decision(state: ReductionState, blocking: list[dict]) -> SupervisorDecision:
    if blocking:
        target = blocking[0]["target_agent"]
        return SupervisorDecision(next_agent=target, reasoning="规则兜底：优先处理最高优先级blocking objection")
    if state["stage"] == "baseline_quantification":
        if not state.get("baseline_params"):
            return SupervisorDecision(next_agent="baseline_agent", reasoning="规则兜底：基线归一化参数尚未产出")
        return SupervisorDecision(next_agent="critic_agent", reasoning="规则兜底：默认交由critic审查基线参数")
    if state["stage"] == "reduction_research":
        if not state["reduction_measures"]:
            return SupervisorDecision(next_agent="reduction_agent", reasoning="规则兜底：措施尚未产出")
        if not state.get("country_measure_review"):
            return SupervisorDecision(next_agent="country_policy_agent", reasoning="规则兜底：国家适用性尚未核查")
        return SupervisorDecision(next_agent="critic_agent", reasoning="规则兜底：默认交由critic审查措施")
    # quantification
    if not state.get("scenario_params"):
        return SupervisorDecision(next_agent="quant_agent", reasoning="规则兜底：情景参数尚未产出")
    return SupervisorDecision(next_agent="critic_agent", reasoning="规则兜底：默认交由critic审查情景参数")


def integration_node(state: ReductionState):
    if not state.get("model_baselines"):
        raise ValueError("integration缺少model_baselines：baseline_quantification阶段未产出，"
                         "不允许落final_output（消灭0kg硬编码兜底）")
    if not state.get("scenarios") or not state.get("trajectory"):
        raise ValueError("integration缺少scenarios/trajectory：calculation_node未成功执行，"
                         "不允许落final_output")
    assessment = state["research_output"].get("evidence_assessment") or {}
    if assessment.get("tier") == "insufficient":
        raise ValueError("国家证据等级为insufficient，不能生成国家级铜减量化模型")
    unresolved = [o for o in state.get("objections") or []
                  if o.get("severity") == "blocking" and not o.get("addressed")]
    if unresolved:
        raise ValueError(f"仍有{len(unresolved)}条未解决blocking objection，不能标记完成")
    if any("轮次耗尽系统兜底" in (s.get("scenario_rationale") or "")
           for s in state.get("scenarios") or []):
        raise ValueError("情景为轮次耗尽后的程序兜底，不属于可验收研究成果")
    if state["research_output"].get("geography") and not state.get("country_measure_review"):
        raise ValueError("缺少国家措施适用性核查，不能标记完成")
    geography = state["research_output"].get("geography")
    if geography:
        from regions import resolve_country
        from evidence_scoring import citation_region_hit
        country = resolve_country(geography.get("country_id"))
        missing_scenario_evidence = [
            s.get("scenario_id") for s in state.get("scenarios") or []
            if s.get("scenario_id") != "S0" and not any(
                citation_region_hit(c, country) for c in s.get("citations") or [])
        ]
        if missing_scenario_evidence:
            raise ValueError("以下情景缺少目标国家证据，不能标记完成："
                             + ", ".join(missing_scenario_evidence))
    bp = BaselineParams.model_validate(state["baseline_params"]) if state.get("baseline_params") else None
    model = ProductReductionModel(
        product_id=state["product_id"],
        region_id=state.get("region_id") or "unknown",
        region_name=state.get("region_name") or "unknown",
        geography=state["research_output"].get("geography"),
        country_profile=state["research_output"].get("country_profile"),
        country_measure_review=state.get("country_measure_review"),
        based_on_research_version=state["research_output"]["research_version"],
        analysis_run_id=state["analysis_run_id"],
        functional_unit_candidates=state.get("functional_unit_candidates") or
                                   (bp.functional_unit_candidates if bp else []),
        functional_unit_rationale=bp.functional_unit_rationale if bp else "见baseline_agent",
        chosen_functional_unit=state.get("chosen_functional_unit") or
                               (bp.chosen_functional_unit if bp else ""),
        baseline_year=state["baseline_year"],
        baseline_unit_intensity=state["baseline_unit_intensity"],
        baseline_calc_method=state.get("baseline_calc_method") or "见calculate.py",
        reduction_measures=state["reduction_measures"],
        scenarios=state["scenarios"],
        trajectory=state["trajectory"],
        model_baselines=state["model_baselines"],
        # 区域证据评估从父调研成果继承（前端区域证据卡/导出口径使用）
        evidence_assessment=(state["research_output"].get("evidence_assessment")
                             if isinstance(state["research_output"], dict) else None),
        amendment_requests=state["amendment_requests"],
        objections=state["objections"],
        debate_log=state["debate_log"],
    )
    return {"final_output": model.model_dump()}


# ============================================================
# 图编译
# ============================================================

def build_reduction_graph(llm, checkpointer=None):
    g = StateGraph(ReductionState)

    g.add_node("loader", load_research_output)
    g.add_node("supervisor", lambda s: supervisor(s, llm))
    g.add_node("baseline_agent", lambda s: baseline_agent(s, llm))
    g.add_node("baseline_calc_node", baseline_calc_node)
    g.add_node("reduction_agent", lambda s: reduction_agent(s, llm))
    g.add_node("country_policy_agent", lambda s: country_policy_agent(s, llm))
    g.add_node("critic_agent", lambda s: critic_agent(s, llm))
    g.add_node("quant_agent", lambda s: quant_agent(s, llm))
    g.add_node("calculation_node", calculation_node)
    g.add_node("integration", integration_node)

    g.add_edge(START, "loader")
    g.add_edge("integration", END)

    # 同图A：from_conn_string返回上下文管理器，改为持有长连接。
    if checkpointer is None:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect("reduction_graph_checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    return g.compile(checkpointer=checkpointer)
