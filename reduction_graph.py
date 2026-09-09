"""
图B：减量化图 (Reduction Graph)
==================================================================
独立于图A(research_graph.py)运行，输入是图A产出并经人工approved的
ProductResearchOutput——本图只读取这份数据，绝不直接修改。

若ReductionAgent/QuantAgent在工作中发现调研阶段存在缺口或疑似错误
（例如"这个含铜部位好像漏了"），不允许自己动手在state里加/改数据，
只能产出一条AmendmentRequest，交由人工/图A做局部重跑处理。

本图的CriticAgent职责范围与图A的CriticAgent完全不同，收窄为两个阶段
各自的检查清单：
  reduction_research阶段:
  - measure_without_case: 减量化措施是否有真实工程案例支撑
  - insufficient_evidence_quality: 案例/数据来源是否可信
  - scope_creep: 是否越权修改了调研阶段的事实结论
  quantification阶段:
  - incomplete_scenario_set: 每个applicable_scope下是否凑齐S0-S3四档情景
  - duplicate_scenario_entry: 同一子类别下是否重复录入了同一情景代码
  - scenario_severity_ordering_violation: Δmax(S0)=0<Δmax(S1)<Δmax(S2)<Δmax(S3)
    是否被打破——这是用户明确要求的硬约束，critic在此阶段用代码做确定性
    预检(见_scenario_ordering_hard_check)，不完全依赖LLM判断
它完全不检查维度/结构相关的问题——那是图A的职责，两个团队的critic
prompt和检查清单是刻意分开写的，避免同一个critic承担认知负荷过重
的检查清单而顾此失彼。

同一份approved调研成果可以被本图多次复用，跑不同情景假设，
不需要每次都重新触发图A的调研+人工审核流程。

本版本相对早期版本修复的技术债：
  1. objections字段改用upsert_objections自定义reducer(与图A共用同一个
     reducer函数)，不再需要_latest_objections()式的读时去重。
  2. reduction_agent真正实现了双输出：一次结构化调用同时产出
     CopperReductionMeasure列表和AmendmentRequest列表(用本文件内定义的
     ReductionAgentOutput包装模型承接)，不再是"amendment_requests恒为
     空列表"的占位实现。
  3. critic_agent在两个阶段都真正实现了"跨轮记忆复核"逻辑(CriticReview)，
     并对quantification阶段做了代码层面的确定性预检，逻辑与
     schemas.py里ProductReductionModel的最终校验完全一致，提前拦截
     而不必等到最终整合才发现。
  4. 情景重复录入(同一子类别下同一scenario_id出现多次)加入了检测。
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

from schemas import (
    CopperReductionMeasure, IntensityTrajectoryPoint, Scenario,
    Objection, CriticReview, DebateLogEntry, SupervisorDecision, AgentRole,
    ProductResearchOutput, ProductReductionModel, AmendmentRequest,
    SCENARIO_SEVERITY_ORDER, upsert_objections,
)

MAX_ROUNDS_PER_STAGE = 3
MAX_TOTAL_ROUNDS = 10

REDUCTION_STAGE_AGENTS: dict[str, set[AgentRole]] = {
    "reduction_research": {"reduction_agent", "critic_agent"},
    "quantification": {"quant_agent", "critic_agent"},
    "done": set(),
}


class ReductionState(TypedDict):
    product_id: str
    research_output: dict  # 只读！加载后的ProductResearchOutput，禁止被任何agent修改
    analysis_run_id: str
    baseline_year: int

    stage: Literal["reduction_research", "quantification", "done"]

    reduction_measures: list[dict]
    scenarios: list[dict]
    trajectory: list[dict]
    functional_unit_candidates: list[str]
    chosen_functional_unit: str
    baseline_unit_intensity: Optional[dict]

    amendment_requests: Annotated[list[dict], operator.add]  # 纯追加，无需upsert
    objections: Annotated[list[dict], upsert_objections]
    debate_log: Annotated[list[dict], operator.add]

    round_in_stage: int
    total_round: int
    final_output: Optional[dict]


# ============================================================
# 加载器：读取只读的调研成果，做前置校验
# ============================================================

def load_research_output(state: ReductionState) -> Command:
    research = ProductResearchOutput.model_validate(state["research_output"])
    if research.research_status != "approved":
        raise ValueError(
            f"研究成果 v{research.research_version} 状态为「{research.research_status}」，"
            f"未经批准不允许进入减量化分析阶段。"
        )
    log_entry = DebateLogEntry(
        round=0, speaker="supervisor", message_type="routing_decision",
        summary=f"已加载approved调研成果 v{research.research_version}，共"
                f"{len(research.copper_components)}个含铜部位，进入减量化研究",
    )
    return Command(goto="supervisor", update={"debate_log": [log_entry.model_dump()]})


# ============================================================
# Agent 1: ReductionAgent —— 真正的双输出(措施 + 修订请求)
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

{objection_context}

已批准的含铜部位（只读，来自research_version={research_version}）：
{copper_components}
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
        )),
    ]
    structured_llm = llm.with_structured_output(ReductionAgentOutput)
    output = structured_llm.invoke(messages)

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
        summary=f"提出 {len(output.measures)} 条减量化措施" + (
            f"，另提交 {len(amendment_requests)} 条调研修订请求"
            if amendment_requests else ""
        ),
        ref_ids=[m.measure_id for m in output.measures],
    )

    return Command(
        goto="supervisor",
        update={
            "reduction_measures": [m.model_dump() for m in output.measures],
            "amendment_requests": [a.model_dump() for a in amendment_requests],
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# 情景排序/完整性/重复录入的确定性预检
# （逻辑与schemas.py的ProductReductionModel._scenario_severity_ordering
#   完全一致，在critic阶段提前拦截，不必等到最终整合才发现）
# ============================================================

def _scenario_ordering_hard_check(scenarios: list[dict]) -> list[str]:
    warnings: list[str] = []
    by_scope: dict[str, list[dict]] = {}
    for s in scenarios:
        scope_key = s.get("applicable_scope") or "(无子类别)"
        by_scope.setdefault(scope_key, []).append(s)

    for scope_label, scen_list in by_scope.items():
        id_counts = Counter(s["scenario_id"] for s in scen_list)
        duplicated = [code for code, cnt in id_counts.items() if cnt > 1]
        if duplicated:
            warnings.append(
                f"[系统预警-确定性检测] 子类别「{scope_label}」情景代码重复出现: {duplicated}，"
                f"请生成duplicate_scenario_entry类型的objection，target_agent为quant_agent。"
            )
            continue

        scen_map = {s["scenario_id"]: s for s in scen_list}
        missing = [c for c in SCENARIO_SEVERITY_ORDER if c not in scen_map]
        if missing:
            warnings.append(
                f"[系统预警-确定性检测] 子类别「{scope_label}」缺少情景: {missing}，"
                f"S0-S3四档必须齐全，请生成incomplete_scenario_set类型的objection。"
            )
            continue

        values = [scen_map[c]["full_implementation_reduction_pct"]["value"] for c in SCENARIO_SEVERITY_ORDER]
        if not (values[0] < values[1] < values[2] < values[3]):
            warnings.append(
                f"[系统预警-确定性检测] 子类别「{scope_label}」Δmax未严格递增: "
                f"S0={values[0]} S1={values[1]} S2={values[2]} S3={values[3]}，"
                f"请生成scenario_severity_ordering_violation类型的objection，"
                f"target_agent应为quant_agent。"
            )
    return warnings


# ============================================================
# Agent 2: CriticAgent —— 按阶段切换检查清单，两阶段都有真实复核逻辑
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

REDUCTION_STAGE_CHECKLIST = """\
本阶段检查清单：
1. measure_without_case: 每条措施是否有真实的EngineeringCase，
   若maturity="commercial"却没有真实商业化项目佐证，视为blocking
2. insufficient_evidence_quality: 引用来源是否可信、是否自相矛盾
3. scope_creep: ReductionAgent是否越权修改了含铜部位的事实数据
   （对照下方"已批准的含铜部位快照"，若发现措施引用的target_component_ids
   指向了快照中不存在的部件，判定为blocking的scope_creep）
4. trade_offs是否如实报告，是否存在只报喜不报忧的可疑迹象

已批准的含铜部位快照（只读基准，用于比对是否被篡改）：
{copper_components_snapshot}

当前ReductionAgent提案：
{reduction_measures}
"""

QUANTIFICATION_STAGE_CHECKLIST = """\
本阶段检查清单（不检查措施本身是否有案例，那是上一阶段已经过关的内容）：
1. incomplete_scenario_set: 每个applicable_scope下是否都凑齐了S0/S1/S2/S3
   四档情景，缺一不可
2. duplicate_scenario_entry: 同一applicable_scope下是否重复出现了同一
   scenario_id
3. scenario_severity_ordering_violation: 【最高优先级】同一applicable_scope下
   Δmax是否满足严格递增 Δmax(S0)=0 < Δmax(S1) < Δmax(S2) < Δmax(S3)。
   注意：S1/S2/S3包含的measure_id不要求构成父子集关系（互斥措施可以分别
   归属不同情景），你只需要看最终算出的Δmax数值是否严格递增，不要因为
   "S3的措施集合不是S2的超集"就误判为错误——那不是错误。
4. scenario_rationale/scenario_definition是否有引用支撑，是否只是空泛描述
5. diffusion_method的选择是否有依据说明，还是随意指定

以下是代码对本轮情景提案的确定性预检结果（若有内容，你【必须】据此生成
对应类型的objection，不能视而不见）：
{hard_check_warnings}

当前情景提案：
{scenarios}

当前强度路径（若尚未生成则为空）：
{trajectory}
"""

def critic_agent(state: ReductionState, llm) -> Command:
    prior_addressed = [o for o in state["objections"] if o.get("addressed")]
    prior_summary = "\n".join(
        f"- [{o['objection_id']}] 曾要求{o['target_agent']}: {o['detail']} "
        f"| 回应: {o.get('response_note', '无')}"
        for o in prior_addressed
    ) or "(无历史记录，本轮之前没有任何已回应的objection需要复核)"

    if state["stage"] == "reduction_research":
        valid_component_ids = {c["component_id"] for c in state["research_output"]["copper_components"]}
        reduction_measures = state["reduction_measures"]
        hard_violations = [
            m for m in reduction_measures
            if any(cid not in valid_component_ids for cid in m.get("target_component_ids", []))
        ]
        checklist = REDUCTION_STAGE_CHECKLIST.format(
            copper_components_snapshot=state["research_output"]["copper_components"],
            reduction_measures=reduction_measures,
        )
        if hard_violations:
            checklist += (
                f"\n\n【系统预警-确定性检测】以下措施引用了不存在于已批准快照中的"
                f"component_id，这是可以直接判定的scope_creep，请务必生成对应objection: "
                f"{[m['measure_id'] for m in hard_violations]}"
            )
    else:  # quantification
        hard_warnings = _scenario_ordering_hard_check(state["scenarios"])
        checklist = QUANTIFICATION_STAGE_CHECKLIST.format(
            hard_check_warnings="\n".join(hard_warnings) or "(未发现确定性问题)",
            scenarios=state["scenarios"],
            trajectory=state["trajectory"],
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
            continue
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
    summary = "；".join(summary_parts) if summary_parts else (
        "本轮无异议，批准进入定量建模" if state["stage"] == "reduction_research" else "本轮无异议，批准进入整合"
    )

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
# Agent 3: QuantAgent
# ============================================================

SCENARIO_DEFINITION_PROMPT = """\
你是「定量分析师」，第一步任务：基于已获批准的减量化措施，为每个
applicable_scope(子类别)分别设定固定的4档情景：S0/S1/S2/S3。

【重要：情景框架是固定的，不是你的调研/创作对象】
- S0「基准情景」：不纳入任何措施，Δmax固定为0，included_measure_ids必须为空
- S1「普通减量」：力度最小的实质减量情景
- S2「加速减量」：力度中等
- S3「深度减量」：力度最大
这4个情景代码和名称必须原样使用，不允许增减情景数量、不允许改名、
不允许只出2-3档。每个applicable_scope(如"陆上"/"海上"，无子类别则为None)
下都必须凑齐这4档，且每档只能出现一次。

【S1/S2/S3的措施组合不要求父子集关系】
你不需要让S3包含S2的全部措施、S2包含S1的全部措施——如果某些措施互斥
(如两种材料替代方案不能同时采用)，S3完全可以选用一套与S2不同的措施组合。
但有一个硬约束：无论怎么组合措施，最终算出的综合降铜幅度(Δmax)必须满足
严格递增—— Δmax(S0)=0 < Δmax(S1) < Δmax(S2) < Δmax(S3)。
如果你发现按某种措施组合方式算出来S2的Δmax反而不如S1，说明这套组合分配
不合理，需要重新分配措施到各情景，直到排序关系成立为止。

若某措施在不同子类别下效果不同，对应情景也应按子类别拆成多条记录
(同一scenario_id，不同applicable_scope)，而不是把子类别编码进scenario_id本身。

已获批准的减量化措施: {reduction_measures}
基准年: {baseline_year}
"""

TRAJECTORY_MODELING_PROMPT = """\
你是「定量分析师」，第二步任务：基于已定义的情景，建模从基准年到2035年
每年的单位铜强度路径。

对每个(年份 × 情景 × 子类别)组合输出一个IntensityTrajectoryPoint：
- scenario_realization_rate_pct 按情景的diffusion_method(线性/S型/阶跃)
  从measure_start_year渐进到target_achievement_year推算，需在字段外的
  推理中说明具体推算方式
- unit_copper_intensity 需与情景定义的Δmax、实现率保持数学一致
  (强度 = 基准 × [1 - Δmax × 实现率])
- delta_vs_baseline 是与基准值的差值

已定义的情景: {scenarios}
含铜部位/基准强度(只读): {copper_components}
基准年: {baseline_year}
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
        objection_context = "\n\nCriticAgent对你此前提案的未解决质询，请优先处理排序/完整性类问题，逐条修订：\n" + \
            "\n".join(f"- [{o['objection_id']}] ({o['flag_type']}) {o['detail']}" for o in sorted_obj)

    scenario_messages = [
        SystemMessage(content=SCENARIO_DEFINITION_PROMPT.format(
            reduction_measures=state["reduction_measures"],
            baseline_year=state["baseline_year"],
        ) + objection_context),
    ]
    scenario_llm = llm.with_structured_output(list[Scenario])
    scenarios = scenario_llm.invoke(scenario_messages)

    trajectory_messages = [
        SystemMessage(content=TRAJECTORY_MODELING_PROMPT.format(
            scenarios=[s.model_dump() for s in scenarios],
            copper_components=state["research_output"]["copper_components"],
            baseline_year=state["baseline_year"],
        )),
    ]
    trajectory_llm = llm.with_structured_output(list[IntensityTrajectoryPoint])
    trajectory = trajectory_llm.invoke(trajectory_messages)

    updated_objections = [
        {**o, "addressed": True, "response_note": "quant_agent已重新分配情景措施/调整Δmax"}
        for o in my_objections
    ]

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="quant_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"{'修订' if my_objections else '定义'} {len(scenarios)} 个情景，"
                f"生成 {len(trajectory)} 个强度路径点",
    )

    return Command(
        goto="supervisor",
        update={
            "scenarios": [s.model_dump() for s in scenarios],
            "trajectory": [t.model_dump() for t in trajectory],
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# SupervisorAgent
# ============================================================

SUPERVISOR_PERSONA = """\
你是减量化团队主管，协调ReductionAgent/CriticAgent/QuantAgent。
这个团队只读消费调研团队已批准的事实数据，不涉及维度/结构问题的判断。

当前阶段: {stage}
本阶段第{round_in_stage}轮 / 总第{total_round}轮
尚未解决的blocking质询: {blocking_objections}
ReductionAgent: {reduction_status}
QuantAgent(情景+路径): {scenario_status}

决策规则：
- reduction_research阶段：若存在scope_creep等blocking objection，优先派
  reduction_agent修正；若尚未产出措施，派reduction_agent；若措施已产出但
  本轮未经critic审查，派critic_agent；若critic本轮无异议，推进到quant_agent
  (同时阶段切换为quantification)
- quantification阶段：若存在scenario_severity_ordering_violation/
  incomplete_scenario_set/duplicate_scenario_entry等blocking objection，
  【必须】派quant_agent重新分配措施/调整Δmax，不允许绕过；若尚未产出情景，
  派quant_agent；若情景已产出但本轮未经critic审查，派critic_agent；
  若critic本轮无异议，进入整合
"""

def supervisor(state: ReductionState, llm) -> Command:
    stage_agents = REDUCTION_STAGE_AGENTS.get(state["stage"], set())
    blocking = [o for o in state["objections"] if o["severity"] == "blocking" and not o.get("addressed")]
    blocking_sorted = sorted(
        blocking,
        key=lambda x: x["flag_type"] not in (
            "scenario_severity_ordering_violation", "incomplete_scenario_set",
            "duplicate_scenario_entry", "scope_creep",
        ),
    )

    if state["total_round"] >= MAX_TOTAL_ROUNDS:
        return Command(goto="integration", update={
            "debate_log": [DebateLogEntry(
                round=state["total_round"], speaker="supervisor",
                message_type="routing_decision", summary="达到总轮次上限，强制进入整合",
            ).model_dump()],
        })

    if state["stage"] == "done":
        return Command(goto="integration")

    messages = [
        SystemMessage(content=SUPERVISOR_PERSONA.format(
            stage=state["stage"], round_in_stage=state["round_in_stage"],
            total_round=state["total_round"], blocking_objections=blocking_sorted or "(无)",
            reduction_status="已产出" if state["reduction_measures"] else "未产出",
            scenario_status="已产出" if state["scenarios"] else "未产出",
        )),
    ]
    structured_llm = llm.with_structured_output(SupervisorDecision)
    decision = structured_llm.invoke(messages)

    # 允许的阶段内合法路由集合。reduction_research阶段中措施已产出且无
    # blocking质询时，quant_agent是合法的下一站(进入quantification阶段)——
    # 否则supervisor永远无法把流程推进到定量阶段，阶段切换分支不可达。
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
    elif state["stage"] == "quantification" and decision.should_terminate:
        next_stage = "done"

    if decision.should_terminate or next_round_in_stage >= MAX_ROUNDS_PER_STAGE:
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
    if state["stage"] == "reduction_research":
        if not state["reduction_measures"]:
            return SupervisorDecision(next_agent="reduction_agent", reasoning="规则兜底：措施尚未产出")
        return SupervisorDecision(next_agent="critic_agent", reasoning="规则兜底：默认交由critic审查措施")
    else:  # quantification
        if not state["scenarios"]:
            return SupervisorDecision(next_agent="quant_agent", reasoning="规则兜底：情景尚未产出")
        return SupervisorDecision(next_agent="critic_agent", reasoning="规则兜底：默认交由critic审查情景排序")


def integration_node(state: ReductionState):
    model = ProductReductionModel(
        product_id=state["product_id"],
        based_on_research_version=state["research_output"]["research_version"],
        analysis_run_id=state["analysis_run_id"],
        functional_unit_candidates=state.get("functional_unit_candidates", []),
        functional_unit_rationale="见quant_agent推理过程",
        chosen_functional_unit=state.get("chosen_functional_unit", ""),
        baseline_year=state["baseline_year"],
        baseline_unit_intensity=state.get("baseline_unit_intensity") or {"value": 0, "unit": "kg"},
        baseline_calc_method="见quant_agent",
        reduction_measures=state["reduction_measures"],
        scenarios=state["scenarios"],
        trajectory=state["trajectory"],
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
    g.add_node("reduction_agent", lambda s: reduction_agent(s, llm))
    g.add_node("critic_agent", lambda s: critic_agent(s, llm))
    g.add_node("quant_agent", lambda s: quant_agent(s, llm))
    g.add_node("integration", integration_node)

    g.add_edge(START, "loader")
    g.add_edge("integration", END)

    # 同图A：from_conn_string返回上下文管理器，改为持有长连接。
    if checkpointer is None:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect("reduction_graph_checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    import json
    from langchain_openai import ChatOpenAI

    # 实际使用中从持久化存储(数据库/文件)加载approved的研究成果，
    # 例如从图A的final_output(见research_graph.py的human_review_gate approval分支)落盘后读取。
    with open("approved_research_pv_station_v1.json") as f:
        research_output = json.load(f)

    llm = ChatOpenAI(model="gpt-4.1", temperature=0)
    graph = build_reduction_graph(llm)

    init_state: ReductionState = {
        "product_id": "pv_station",
        "research_output": research_output,
        "analysis_run_id": "run-2026-conservative-scenario",
        "baseline_year": 2024,
        "stage": "reduction_research",
        "reduction_measures": [], "scenarios": [], "trajectory": [],
        "functional_unit_candidates": [], "chosen_functional_unit": "",
        "baseline_unit_intensity": None,
        "amendment_requests": [], "objections": [], "debate_log": [],
        "round_in_stage": 0, "total_round": 0, "final_output": None,
    }
    result = graph.invoke(init_state, config={"configurable": {"thread_id": "pv_station-reduction-run1"}})
    print(f"完成，共 {result['total_round']} 轮，"
          f"{len(result['amendment_requests'])} 条调研修订请求待处理")
