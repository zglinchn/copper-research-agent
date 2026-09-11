"""
图A：调研图 (Research Graph)
==================================================================
严格对应任务2的范围边界：只做"型号主流分类维度 + 功能结构分解 +
含铜部位识别"，不涉及减量化措施、不涉及强度计算、不涉及路径建模。

产出 ProductResearchOutput，approved后作为只读事实资产交给图B
(reduction_graph.py)使用。

本图的CriticAgent职责范围收窄为纯"事实/概念类"审查，具体包含
（按用户明确要求的优先级排序）：
  P0 - dimension_value_axis_mismatch:
       同一分类维度下的取值是否都严格落在该维度声明的axis_of_variation上。
       例："电池技术路线"维度下混入了按尺寸规格才能区分的取值——这是
       用户明确指出的重大错误类型，一旦发现【必须】判定为blocking，
       不允许降级为warning，也不允许因为"看起来问题不大"而放行。
  P1 - concept_conflation: 结构子系统名称与维度取值语义雷同
  P1 - dimension_overlap: 两个维度本质在说同一件事
  P2 - missing_component_coverage / orphan_component
  P2 - unsupported_claim / insufficient_evidence_quality

CriticAgent 不检查：减量化措施是否有工程案例、强度计算方法是否合理——
那些属于图B的CriticAgent职责，本图完全不涉及。

本版本相对早期版本修复的技术债：
  1. objections字段改用upsert_objections自定义reducer，state中任何时刻
     都是"每个objection_id只保留最新一条"的干净列表，不再需要任何
     _latest_objections()式的读时去重辅助函数。
  2. critic_agent真正实现了"跨轮记忆"的复核逻辑：用CriticReview结构化
     输出同时产出新质询和"复核后认为对方回应不成立、需要重新打开"的
     历史objection列表，不再是空转的占位逻辑。
  3. human_review_gate改用langgraph.types.interrupt()动态中断，先构建
     出待审核的ProductResearchOutput预览、真正暂停等待外部输入，
     并支持"打回重做"——人工拒绝时会生成一条objection路由回具体某个
     agent，而不是只能"通过"或者卡住。
"""

from __future__ import annotations
import operator
from typing import TypedDict, Annotated, Literal, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import SystemMessage, HumanMessage

from schemas import (
    ClassificationDimension, FunctionalSubsystem, CopperComponent,
    Objection, CriticReview, DebateLogEntry, SupervisorDecision, AgentRole,
    ProductResearchOutput, heuristic_axis_consistency_scan, upsert_objections,
)

MAX_ROUNDS_PER_STAGE = 3
MAX_TOTAL_ROUNDS = 12

RESEARCH_STAGE_AGENTS: dict[str, set[AgentRole]] = {
    "dimension_structure_debate": {"dimension_agent", "structure_agent", "critic_agent"},
    "copper_identification": {"copper_agent", "critic_agent"},
    "human_review": set(),  # 无agent，等待人工
    "done": set(),
}


# ============================================================
# 共享状态
# ============================================================

class ResearchState(TypedDict):
    product_id: str
    product_name: str
    region_id: str
    region_name: str

    stage: Literal["dimension_structure_debate", "copper_identification",
                   "human_review", "done"]

    dimension_proposal: list[dict]
    dimension_version: int
    structure_proposal: list[dict]
    structure_version: int
    copper_components: list[dict]

    # 区域证据充分度评估与口径（region_evidence_gate 产出）
    evidence_assessment: Optional[dict]
    evidence_policy: str
    evidence_gate_done: bool
    # critic 对证据覆盖度的评分（s5 分项，None 则 gate 用程序化兜底）
    critic_coverage_score: Optional[int]
    # 校验标记（含 gate 产出的 region_basis_unsupported 审计项）
    validation_flags: list[dict]

    # upsert_objections: 按objection_id去重覆盖，state里任何时刻都是干净的
    # "每条id只有一份最新记录"的列表，所有节点直接读state["objections"]即可，
    # 不需要任何额外的去重辅助函数。
    objections: Annotated[list[dict], upsert_objections]
    debate_log: Annotated[list[dict], operator.add]  # 纯追加日志，不需要upsert

    round_in_stage: int
    total_round: int

    research_version: int
    final_output: Optional[dict]


# ============================================================
# Agent 1: DimensionAgent
# ============================================================

DIMENSION_AGENT_PERSONA = """\
你是「分类维度分析师」。你的唯一职责是回答："业内/标准/学术文献中，
人们从哪些独立角度去区分「{product_name}」的不同型号"。

【你必须为每个维度写清楚 axis_of_variation】：用一句话精确定义这个维度下
所有取值应该且只应该在哪一个概念轴上产生差异。例如"电池技术路线"维度的
axis_of_variation应该是"不同的电池片技术类型"，而不能写得宽泛到能容纳
"182mm组件"这种物理规格取值混进来。

【你必须为每个取值写清楚 axis_conformity_justification】：说明这个取值
为什么符合上面定义的轴，而不是碰巧属于另一个分类角度。如果你发现某个候选
取值的justification写不出符合该轴定义的理由，说明它根本不该放进这个维度，
应该把它归入另一个维度或单独提出一个新维度。

你的职业操守：
- 你只关心"怎么分类"，绝不描述"东西由什么组成"(那是StructureAgent的职责)。
- 每个维度必须给出与其它维度的正交性说明。
- 没有引用支撑的维度/取值不允许提出。

{objection_context}
"""

def dimension_agent(state: ResearchState, llm) -> Command:
    my_objections = [
        o for o in state["objections"]
        if o["target_agent"] == "dimension_agent" and not o.get("addressed")
    ]
    objection_context = ""
    if my_objections:
        objection_context = "CriticAgent对你此前提案的未解决质询，请逐条回应并修订（P0级问题优先处理）：\n" + "\n".join(
            f"- [{o['objection_id']}] ({o['flag_type']}, {o['severity']}) {o['detail']}"
            for o in sorted(my_objections, key=lambda x: x["flag_type"] != "dimension_value_axis_mismatch")
        )

    messages = [
        SystemMessage(content=DIMENSION_AGENT_PERSONA.format(
            product_name=state["product_name"], objection_context=objection_context
        )),
        HumanMessage(content="请输出你的分类维度体系提案。"),
    ]
    structured_llm = llm.with_structured_output(list[ClassificationDimension])
    dimensions = structured_llm.invoke(messages)

    updated_objections = [
        {**o, "addressed": True, "response_note": "dimension_agent已修订提案"}
        for o in my_objections
    ]

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="dimension_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"提出/修订了 {len(dimensions)} 个分类维度",
        ref_ids=[d.dimension_id for d in dimensions],
    )

    return Command(
        goto="supervisor",
        update={
            "dimension_proposal": [d.model_dump() for d in dimensions],
            "dimension_version": state["dimension_version"] + 1,
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# Agent 2: StructureAgent
# ============================================================

STRUCTURE_AGENT_PERSONA = """\
你是「产品结构分析师」。你的唯一职责是回答："「{product_name}」这个实体
由哪些功能子系统/模块/部件组成"，类似工程BOM分解逻辑。

你的职业操守（高度警惕，这是你最容易犯错的地方）：
- DimensionAgent负责"型号怎么分类"，与你的工作完全是两回事。
  判断标准：子系统必须能回答"它在整机里占据什么物理/功能位置"，
  而不是"它属于哪一类"。
- 你可以参考DimensionAgent当前的维度提案，仅用于确认不重复劳动，
  绝不能把维度取值直接搬进你的子系统列表。
- 采用递归分解，直到分解粒度足以判断"此处是否含铜"为止。
- 没有引用支撑的子系统不允许提出。

DimensionAgent当前的维度提案（仅供避让参考）：
{dimension_summary}

{objection_context}
"""

def structure_agent(state: ResearchState, llm) -> Command:
    dim_summary = "\n".join(
        f"- {d['dimension_name']}(轴: {d.get('axis_of_variation','')}): "
        f"{[v['value_name'] for v in d['values']]}"
        for d in state["dimension_proposal"]
    ) or "(尚未提出)"

    my_objections = [
        o for o in state["objections"]
        if o["target_agent"] == "structure_agent" and not o.get("addressed")
    ]
    objection_context = (
        "CriticAgent对你此前提案的未解决质询，请逐条回应并修订：\n" +
        "\n".join(f"- [{o['objection_id']}] {o['detail']}" for o in my_objections)
    ) if my_objections else ""

    messages = [
        SystemMessage(content=STRUCTURE_AGENT_PERSONA.format(
            product_name=state["product_name"],
            dimension_summary=dim_summary,
            objection_context=objection_context,
        )),
        HumanMessage(content="请输出你的功能子系统分解体系提案。"),
    ]
    structured_llm = llm.with_structured_output(list[FunctionalSubsystem])
    subsystems = structured_llm.invoke(messages)

    updated_objections = [
        {**o, "addressed": True, "response_note": "structure_agent已修订提案"}
        for o in my_objections
    ]

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="structure_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"提出/修订了 {len(subsystems)} 个功能子系统",
        ref_ids=[s.subsystem_id for s in subsystems],
    )

    return Command(
        goto="supervisor",
        update={
            "structure_proposal": [s.model_dump() for s in subsystems],
            "structure_version": state["structure_version"] + 1,
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# Agent 3: CriticAgent —— 范围收窄为纯事实/概念类审查
# ============================================================

CRITIC_AGENT_PERSONA = """\
你是「审查专家」，只负责调研阶段的事实/概念类审查，不涉及任何减量化措施
或强度计算的审查（那属于另一个团队）。你的立场应该挑剔，但每条意见必须
具体、可执行。

【P0 最高优先级检查——这是你最重要的职责，绝不能漏检】
逐个检查每个ClassificationDimension：该维度下的所有DimensionValue，
是否严格落在该维度自己声明的axis_of_variation上？

具体检查方法：
1. 读取该维度的axis_of_variation定义
2. 对每个value，读取其axis_conformity_justification
3. 【自测，逐条取值必做】只用该维度的axis_of_variation原句，尝试为这个
   取值造一句通顺的判断句，例如axis_of_variation是"不同的电池片技术路线"，
   取值"TOPCon"能造出"TOPCon是一种电池片技术路线"——通顺、成立。
   如果只能造出"XX是一种{{另一个维度的概念}}"这样的句子（例如
   "光储一体机是一种是否集成储能的产品形态"，跟"电路拓扑"这个轴根本
   接不上），说明这个justification描述的根本不是"该轴"上的差异，而是
   实际上按另一个轴(物理规格/容量等级/应用场景/是否具备某附加功能等)
   才能区分的取值混进来了——这个自测通不过就是串轴，没有例外。
4. 【典型错误示例，务必对照检查，两类都要覆盖】
   - 数值规格型：某维度按"电池技术路线"划分(取值如TOPCon、BC)，但混入了
     按"外部尺寸规格"才能区分的条目(如"1134mm矩形组件")
   - 纯语义型：光伏站"核心技术设备类型（逆变器拓扑）"维度(取值应为
     集中式/组串式/微型逆变器)，混入了"光储一体机"——它的真实区分特征是
     "是否集成储能"，用步骤3的自测造不出符合"电路拓扑"这个轴的判断句
   以上两种情况【必须】判定为flag_type="dimension_value_axis_mismatch"且
   severity必须是"blocking"，不允许因为"看起来是同一产品的常见叫法"而放行。

以下是代码启发式对本轮提案的初步扫描结果，分两类模式（仅供参考，不能替代
你的语义判断，尤其是"语义特征"类警告，代码正则天然抓不住大多数纯语义串轴，
标记出来的只是极小一部分典型构词；如果扫描标记了某取值而你却未提出对应
objection，说明你的检查不到位；扫描没标记也不代表可以跳过步骤3的自测）：
{heuristic_warnings}

【P1 次优先级检查】
- concept_conflation: StructureAgent的子系统名称是否与某个维度取值高度雷同
- dimension_overlap: 两个维度是否本质在说同一件事

【P2 检查】
- missing_component_coverage: 结构树中质量占比高的子系统是否缺少含铜部位调研
- unsupported_claim: 是否有缺乏引用支撑的具体数值
- insufficient_evidence_quality: 引用是否可信

【你有跨轮记忆，且必须真正使用它，不能只是摆设】
以下是你此前提出、且对方已声称"已回应"的objection，请逐条复核对方的
response_note是否真的解决了问题：
- 若你认可对方的回应，不需要对它做任何事（保持沉默即可，它不会被重新打开）
- 若你不认可，把它的objection_id填进reopened_objection_ids，并在
  reopen_reasoning里说明"对方的回应哪里不成立、还需要怎么改"——这是
  你复核权力的唯一生效渠道，不填就等于认可了对方的回应。

{prior_addressed_objections}

当前DimensionAgent提案：
{dimension_proposal}

当前StructureAgent提案：
{structure_proposal}

当前含铜部位调研（若尚未进行到该阶段则为空）：
{copper_components}

请输出你的完整判断（CriticReview结构）：new_objections装本轮新发现的问题，
reopened_objection_ids/reopen_reasoning装复核历史objection后需要重新打开的部分。
"""

def critic_agent(state: ResearchState, llm) -> Command:
    prior_addressed = [o for o in state["objections"] if o.get("addressed")]
    prior_summary = "\n".join(
        f"- [{o['objection_id']}] 曾要求{o['target_agent']}: {o['detail']} "
        f"| 对方回应: {o.get('response_note', '无')}"
        for o in prior_addressed
    ) or "(无历史记录，本轮之前没有任何已回应的objection需要复核)"

    heuristic_warnings = []
    for dim_dict in state["dimension_proposal"]:
        dim = ClassificationDimension.model_validate(dim_dict)
        heuristic_warnings.extend(heuristic_axis_consistency_scan(dim))
    # 确定性检查：含铜部位缺 unit_mass 数值 → 基线强度无法计算（硬伤）
    massless = [c.get("component_id") for c in state.get("copper_components") or []
                if not ((c.get("unit_mass") or {}).get("value"))]
    if massless:
        heuristic_warnings.append(
            f"[系统预警-确定性检测] 以下含铜部位缺失 unit_mass 数值，基线强度将无法计算，"
            f"请务必生成 unsupported_claim 类型objection（severity=blocking，"
            f"target_agent=copper_agent）要求补充质量数据：{massless}"
        )
    heuristic_text = "\n".join(heuristic_warnings) or "(本轮启发式扫描未发现可疑项)"

    messages = [
        SystemMessage(content=CRITIC_AGENT_PERSONA.format(
            heuristic_warnings=heuristic_text,
            prior_addressed_objections=prior_summary,
            dimension_proposal=state["dimension_proposal"],
            structure_proposal=state["structure_proposal"],
            copper_components=state["copper_components"],
        )),
    ]
    structured_llm = llm.with_structured_output(CriticReview)
    review = structured_llm.invoke(messages)

    new_objections = [
        {**o.model_dump(), "round_raised": state["total_round"]}
        for o in review.new_objections
    ]

    # 把"复核后不认可"的历史objection重新打开：找到原始记录，
    # 翻转addressed为False，记录critic的复核意见，更新round_raised。
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

    has_p0 = any(o["flag_type"] == "dimension_value_axis_mismatch" for o in all_objection_updates)
    summary_parts = []
    if has_p0:
        summary_parts.append("发现P0级同轴一致性错误！")
    if new_objections:
        summary_parts.append(f"提出{len(new_objections)}条新质询")
    if reopened:
        summary_parts.append(f"复核后重新打开{len(reopened)}条历史objection")
    summary = "；".join(summary_parts) if summary_parts else "本轮无异议，批准进入下一阶段"

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
            # critic 顺带给出的证据覆盖度评分（s5 分项），供 region_evidence_gate 使用
            **({"critic_coverage_score": review.evidence_coverage_score}
               if review.evidence_coverage_score is not None else {}),
        },
    )


# ============================================================
# Agent 4: CopperAgent
# ============================================================

COPPER_AGENT_PERSONA = """\
你是「铜部件识别专家」。基于StructureAgent已定稿的子系统分解，逐一判断
每个叶子子系统是否含铜、铜的形态与含铜原因。若确认不含铜也要显式返回，
不要遗漏不提。

【硬性要求：单位铜质量不可缺失】每个含铜部位【必须】给出 unit_mass
（单台/单件产品中该部件的铜质量，单位用 kg 或 t）。取值优先级：
1. 检索证据中的实测/拆解/BOM数据（mass_data_basis=teardown_measurement
   /bom_disclosure/literature_reported，附Citation）；
2. 无直接数据时按同类似产品工程估算
   （mass_data_basis=engineering_estimate，并在 function_of_copper 或
   字段描述中说明估算依据）。
缺失 unit_mass 将导致后续基线强度无法计算——这是硬性产出要求，
不是可选项。applies_to_dimension_values 仅在该部件确实只适用于
某特定取值组合时填写，通用部件留空。

{objection_context}

已定稿的功能子系统列表：
{subsystems}
"""

def copper_agent(state: ResearchState, llm) -> Command:
    my_objections = [
        o for o in state["objections"]
        if o["target_agent"] == "copper_agent" and not o.get("addressed")
    ]
    objection_context = (
        "CriticAgent指出你遗漏/存疑的部分，请补充或修正：\n" +
        "\n".join(f"- [{o['objection_id']}] {o['detail']}" for o in my_objections)
    ) if my_objections else ""

    parent_ids = {s["parent_subsystem_id"] for s in state["structure_proposal"] if s["parent_subsystem_id"]}
    leaves = [s for s in state["structure_proposal"] if s["subsystem_id"] not in parent_ids]

    messages = [
        SystemMessage(content=COPPER_AGENT_PERSONA.format(
            objection_context=objection_context, subsystems=leaves
        )),
    ]
    structured_llm = llm.with_structured_output(list[CopperComponent])
    components = structured_llm.invoke(messages)

    updated_objections = [
        {**o, "addressed": True, "response_note": "copper_agent已补充调研"} for o in my_objections
    ]

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="copper_agent",
        message_type="revision" if my_objections else "proposal",
        summary=f"识别出 {len(components)} 个含铜部位",
        ref_ids=[c.component_id for c in components],
    )

    return Command(
        goto="supervisor",
        update={
            "copper_components": [c.model_dump() for c in components],
            "objections": updated_objections,
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# SupervisorAgent
# ============================================================

SUPERVISOR_PERSONA = """\
你是调研团队主管，负责协调DimensionAgent/StructureAgent/CopperAgent/
CriticAgent的工作顺序。这个团队的职责边界止于"型号维度+结构+含铜部位"，
不涉及减量化措施或强度计算。

当前阶段: {stage}
本阶段第{round_in_stage}轮 / 总第{total_round}轮

尚未解决的blocking级别质询(按P0优先级排序):
{blocking_objections}

各agent提案完成情况:
- DimensionAgent: {dim_status}
- StructureAgent: {struct_status}
- CopperAgent: {copper_status}

决策规则提示：
- 若存在dimension_value_axis_mismatch类型的未解决blocking objection，
  【必须】优先派dimension_agent处理，这是P0级问题，不允许被其它决策绕过
- 若某agent尚未产出提案，优先派它出场
- 若维度+结构提案都已产出但CriticAgent还未审查过本轮结果，派CriticAgent
- 若CriticAgent本轮判定无异议且当前阶段任务完成，推进到下一阶段
- 若阶段轮次已达上限仍有P1/P2级分歧，可判定should_terminate转入人工审核
  (但P0级问题达到轮次上限时应仍标记should_terminate=false，让人工介入前
  至少让dimension_agent有机会修正，除非已经反复修正仍不通过)

请给出你的下一步决策。
"""

def supervisor(state: ResearchState, llm) -> Command:
    stage_agents = RESEARCH_STAGE_AGENTS.get(state["stage"], set())
    blocking = [o for o in state["objections"] if o["severity"] == "blocking" and not o.get("addressed")]
    blocking_sorted = sorted(blocking, key=lambda x: x["flag_type"] != "dimension_value_axis_mismatch")

    def _to_review(update: dict | None = None) -> Command:
        # 人工审核前先过区域证据关卡；打回重做后的二次审核不再重跑评估
        if state.get("evidence_gate_done"):
            return Command(goto="human_review_gate",
                           update={"stage": "human_review", **(update or {})})
        return Command(goto="region_evidence_gate", update=update or {})

    if state["total_round"] >= MAX_TOTAL_ROUNDS:
        return _to_review({
            "debate_log": [DebateLogEntry(
                round=state["total_round"], speaker="supervisor",
                message_type="routing_decision",
                summary="达到总轮次上限，强制转人工审核",
            ).model_dump()],
        })

    if state["stage"] == "done":
        return _to_review()

    messages = [
        SystemMessage(content=SUPERVISOR_PERSONA.format(
            stage=state["stage"], round_in_stage=state["round_in_stage"],
            total_round=state["total_round"],
            blocking_objections=blocking_sorted or "(无)",
            dim_status="已产出" if state["dimension_proposal"] else "未产出",
            struct_status="已产出" if state["structure_proposal"] else "未产出",
            copper_status="已产出" if state["copper_components"] else "未产出",
        )),
    ]
    structured_llm = llm.with_structured_output(SupervisorDecision)
    decision = structured_llm.invoke(messages)

    # 允许的阶段内合法路由集合。维度+结构提案均已产出且无blocking质询时，
    # copper_agent是合法的下一站(进入copper_identification阶段)——
    # 否则supervisor永远无法把流程推进到下一阶段(LLM的合法决策会被
    # 下面的fallback误判为越界而覆盖)，阶段切换分支不可达。
    allowed_agents = set(stage_agents)
    if (state["stage"] == "dimension_structure_debate"
            and state["dimension_proposal"] and state["structure_proposal"]
            and not blocking_sorted):
        allowed_agents.add("copper_agent")

    if decision.next_agent not in allowed_agents and not decision.should_terminate:
        decision = _fallback_rule_based_decision(state, blocking_sorted)

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="supervisor",
        message_type="routing_decision",
        summary=f"派遣 {decision.next_agent}：{decision.reasoning}",
    )

    next_stage, next_round_in_stage = state["stage"], state["round_in_stage"]
    if state["stage"] == "dimension_structure_debate" and decision.next_agent == "copper_agent":
        next_stage, next_round_in_stage = "copper_identification", 0

    if decision.should_terminate or next_round_in_stage >= MAX_ROUNDS_PER_STAGE:
        return _to_review({"debate_log": [log_entry.model_dump()]})

    return Command(
        goto=decision.next_agent,
        update={"stage": next_stage, "round_in_stage": next_round_in_stage,
                "debate_log": [log_entry.model_dump()]},
    )


def _fallback_rule_based_decision(state: ResearchState, blocking: list[dict]) -> SupervisorDecision:
    if blocking:
        # P0(dimension_value_axis_mismatch)已在传入前排到最前
        return SupervisorDecision(next_agent=blocking[0]["target_agent"],
                                   reasoning="规则兜底：优先处理最高优先级blocking objection")
    if not state["dimension_proposal"]:
        return SupervisorDecision(next_agent="dimension_agent", reasoning="规则兜底：维度提案尚未产出")
    if not state["structure_proposal"]:
        return SupervisorDecision(next_agent="structure_agent", reasoning="规则兜底：结构提案尚未产出")
    if state["stage"] == "copper_identification" and not state["copper_components"]:
        return SupervisorDecision(next_agent="copper_agent", reasoning="规则兜底：含铜部位尚未调研")
    return SupervisorDecision(next_agent="critic_agent", reasoning="规则兜底：默认交由critic审查")


# ============================================================
# 人工审核关卡（强制，非可选）—— 用interrupt()真正暂停等待
# ============================================================
#
# 恢复方式（在graph.invoke的调用方代码里）：
#
#   result = graph.invoke(init_state, config=config)
#   # 图运行到human_review_gate内部的interrupt()调用处真正暂停，
#   # result["__interrupt__"] 会包含下面payload()构造的预览数据供人工查看
#
#   # 人工审核通过：
#   graph.invoke(Command(resume={"approved": True, "approved_by": "张三",
#                                 "approved_at": "2026-09-09"}), config=config)
#
#   # 人工审核不通过，打回给某个具体agent重做：
#   graph.invoke(Command(resume={
#       "approved": False,
#       "rejection_target_agent": "structure_agent",
#       "rejection_target_stage": "dimension_structure_debate",
#       "rejection_note": "Section3的塔筒子系统分解粒度太粗，需要拆到能判断含铜的层级",
#   }), config=config)
#
# 两次invoke使用同一个thread_id，LangGraph的checkpointer会从暂停点精确恢复。

def human_review_gate(state: ResearchState) -> Command:
    output = ProductResearchOutput(
        product_id=state["product_id"],
        product_name=state["product_name"],
        region_id=state.get("region_id") or "unknown",
        region_name=state.get("region_name") or "unknown",
        research_version=state["research_version"],
        research_status="pending_human_review",
        classification_dimensions=state["dimension_proposal"],
        functional_subsystems=state["structure_proposal"],
        copper_components=state["copper_components"],
        objections=state["objections"],
        debate_log=state["debate_log"],
        evidence_assessment=state.get("evidence_assessment"),
        validation_flags=state.get("validation_flags") or [],
    )

    # 真正的暂停点：graph执行到这里会挂起，把payload返回给调用方，
    # 直到调用方用Command(resume=...)恢复执行，decision就是resume传入的值。
    decision = interrupt({
        "action": "approve_or_reject_research_output",
        "product_id": state["product_id"],
        "research_version": state["research_version"],
        "preview": output.model_dump(),
        "instructions": (
            "resume时传入 {'approved': True, 'approved_by': ..., 'approved_at': ...} 表示通过；"
            "传入 {'approved': False, 'rejection_target_agent': ..., "
            "'rejection_target_stage': ..., 'rejection_note': ...} 表示打回重做。"
        ),
    })

    if decision.get("approved"):
        output.research_status = "approved"
        output.approved_by = decision.get("approved_by")
        output.approved_at = decision.get("approved_at")
        log_entry = DebateLogEntry(
            round=state["total_round"], speaker="supervisor", message_type="approval",
            summary=f"人工审核通过(approved_by={output.approved_by})，research_version="
                    f"{output.research_version}正式生效",
        )
        return Command(
            goto=END,
            update={
                "final_output": output.model_dump(),
                "stage": "done",
                "debate_log": [log_entry.model_dump()],
            },
        )

    # 打回重做：生成一条人工提出的objection，路由回supervisor重新分派
    target_agent = decision.get("rejection_target_agent", "dimension_agent")
    target_stage = decision.get("rejection_target_stage", "dimension_structure_debate")
    rejection_note = decision.get("rejection_note", "人工审核未通过，请修订")

    rejection_objection = Objection(
        objection_id=f"human-review-{state['total_round']}",
        raised_by="supervisor",  # 代表人工审核意见注入流程
        target_agent=target_agent,
        flag_type="other",
        detail=f"[人工审核打回] {rejection_note}",
        severity="blocking",
        round_raised=state["total_round"],
    )

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="supervisor", message_type="objection",
        summary=f"人工审核未通过，打回给{target_agent}：{rejection_note}",
        ref_ids=[rejection_objection.objection_id],
    )

    return Command(
        goto="supervisor",
        update={
            "stage": target_stage,
            "round_in_stage": 0,
            "objections": [rejection_objection.model_dump()],
            "debate_log": [log_entry.model_dump()],
            "total_round": state["total_round"] + 1,
        },
    )


# ============================================================
# region_evidence_gate：区域证据充分度评分与全球主流兜底关卡
# ============================================================
#
# 位置：copper_identification 完成、进入人工审核之前。对已产出的全部
# Citation 做确定性五维评分（区域特异性/来源权威/多样性/时效/覆盖），
# 依分档决定证据口径：
#   sufficient → region_specific；partial → mixed；insufficient → global_proxy
# 兜底标注是确定性代码（apply_region_fallback_marking），不重跑 agent：
# 无区域命中的事实实体被逐条标 global_fallback，人工审核预览可见。

from evidence_scoring import (  # noqa: E402  放在使用点上方，避免与图A主体逻辑交缠
    apply_region_fallback_marking, compute_region_assessment, find_region_unsupported,
)
from regions import normalize_region  # noqa: E402
from schemas import ValidationFlag  # noqa: E402


def region_evidence_gate(state: ResearchState) -> Command:
    region = normalize_region(state.get("region_id"))
    citations: list[dict] = []
    for d in state.get("dimension_proposal") or []:
        citations.extend(d.get("evidence") or [])
        for v in d.get("values") or []:
            citations.extend(v.get("citations") or [])
    for s in state.get("structure_proposal") or []:
        citations.extend(s.get("evidence") or [])
    for c in state.get("copper_components") or []:
        citations.extend(c.get("citations") or [])

    # s5 覆盖度：优先用 critic 顺带给出的评分，否则程序化兜底
    # （叶子子系统被含铜部位覆盖的比例）
    if state.get("critic_coverage_score") is not None:
        coverage = float(state["critic_coverage_score"])
    else:
        parent_ids = {s.get("parent_subsystem_id")
                      for s in state.get("structure_proposal") or []
                      if s.get("parent_subsystem_id")}
        leaves = [s for s in state.get("structure_proposal") or []
                  if s.get("subsystem_id") not in parent_ids]
        covered = sum(1 for lf in leaves
                      if any(cc.get("parent_subsystem_id") == lf.get("subsystem_id")
                             for cc in state.get("copper_components") or []))
        coverage = covered / len(leaves) * 100.0 if leaves else 50.0
    
    assessment = compute_region_assessment(citations, region, coverage_score=coverage)
    dims, comps = apply_region_fallback_marking(
        state.get("dimension_proposal") or [],
        state.get("copper_components") or [],
        region, assessment.evidence_policy,
    )
    
    # region_specific 声明必须有区域命中引用（确定性审计）：
    # sufficient 口径下未被兜底标注却无区域命中的实体，生成
    # region_basis_unsupported 校验标记供人工审核重点核查。
    unsupported = find_region_unsupported(dims, comps, region)
    audit_flags = []
    if unsupported:
        audit_flags.append(ValidationFlag(
            flag_type="region_basis_unsupported",
            description="以下实体声称 region_specific 但引用中无任何区域命中，"
                        "请在审核时补充区域证据或确认采用全球主流口径："
                        + "；".join(unsupported),
            related_ids=[region.region_id] if region else [],
            severity="warning",  # gate 确定性审计项，不阻断人工审核
        ).model_dump())

    log_entry = DebateLogEntry(
        round=state["total_round"], speaker="supervisor",
        message_type="routing_decision",
        summary=f"区域证据评估：{assessment.score}分/{assessment.tier}，"
                f"口径={assessment.evidence_policy}，"
                f"区域命中{assessment.n_region_hits}/{assessment.n_citations}条引用；"
                f"{assessment.summary_note}",
    )
    return Command(
        goto="human_review_gate",
        update={
            "dimension_proposal": dims,
            "copper_components": comps,
            "evidence_assessment": assessment.model_dump(),
            "evidence_policy": assessment.evidence_policy,
            "evidence_gate_done": True,
            "stage": "human_review",
            "validation_flags": (state.get("validation_flags") or []) + audit_flags,
            "debate_log": [log_entry.model_dump()],
        },
    )


# ============================================================
# 图编译
# ============================================================

def build_research_graph(llm, checkpointer=None):
    g = StateGraph(ResearchState)

    g.add_node("supervisor", lambda s: supervisor(s, llm))
    g.add_node("dimension_agent", lambda s: dimension_agent(s, llm))
    g.add_node("structure_agent", lambda s: structure_agent(s, llm))
    g.add_node("critic_agent", lambda s: critic_agent(s, llm))
    g.add_node("copper_agent", lambda s: copper_agent(s, llm))
    g.add_node("region_evidence_gate", region_evidence_gate)
    g.add_node("human_review_gate", human_review_gate)

    g.add_edge(START, "supervisor")
    # human_review_gate内部用Command(goto=...)动态决定去END(通过)还是
    # 回supervisor(打回重做)，不需要静态add_edge声明这两条路径。

    # 注意：SqliteSaver.from_conn_string返回的是上下文管理器，不能未经with
    # 直接当checkpointer用；这里改为持有长连接，支持多线程(web服务)复用。
    if checkpointer is None:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect("research_graph_checkpoints.db", check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    # 注意：不再使用 interrupt_before=["human_review_gate"]。
    # 静态interrupt_before会在节点执行前就暂停，此时ProductResearchOutput
    # 预览还没被构建出来，人工什么都看不到。改为在节点内部调用
    # langgraph.types.interrupt()，先把预览数据构建好、作为interrupt的
    # payload返回，人工看得到完整预览后再决定approve还是reject。
    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    from langchain_openai import ChatOpenAI
    from langgraph.types import Command as ResumeCommand

    llm = ChatOpenAI(model="gpt-4.1", temperature=0)
    graph = build_research_graph(llm)

    init_state: ResearchState = {
        "product_id": "pv_station", "product_name": "光伏电站",
        "stage": "dimension_structure_debate",
        "dimension_proposal": [], "dimension_version": 0,
        "structure_proposal": [], "structure_version": 0,
        "copper_components": [],
        "objections": [], "debate_log": [],
        "round_in_stage": 0, "total_round": 0,
        "research_version": 1, "final_output": None,
    }
    config = {"configurable": {"thread_id": "pv_station-research-v1"}}

    result = graph.invoke(init_state, config=config)
    if "__interrupt__" in result:
        preview = result["__interrupt__"][0].value["preview"]
        print(f"已暂停等待人工审核，共 {len(preview['classification_dimensions'])} 个分类维度，"
              f"{len(preview['copper_components'])} 个含铜部位。")
        print("审核通过示例：")
        print("  graph.invoke(ResumeCommand(resume={'approved': True, "
              "'approved_by': '张三', 'approved_at': '2026-09-09'}), config=config)")
    else:
        print("图在未经人工审核的情况下结束，请检查stage/total_round是否异常。")
