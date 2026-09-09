"""
铜产品减量化模型智能体 - 数据结构定义
==========================================
核心设计原则：
1. "分类维度体系"（型号怎么分类）与"结构分解体系"（产品由什么组成）
   是两棵完全独立、结构对称的树，任何字段不能跨树引用错位。
2. 不预置任何产品专属枚举值——只预置"维度类别"这种元级枚举做兜底/校验用，
   具体维度名称、取值、子系统名称全部由智能体调研产生。
3. 每一条可能影响结论的信息都必须带 Citation，杜绝无来源臆断。
4. 用独立的 ValidationFlag 实体承载"概念混淆/维度重叠/覆盖缺漏"等错误类型，
   把"防止认知混乱"做成可编程校验，而不是依赖 LLM 的自觉性。
"""

from __future__ import annotations
import re
from pydantic import BaseModel, Field, model_validator
from typing import Literal, Optional
from enum import Enum


# ============================================================
# 基础原子类型
# ============================================================

class Citation(BaseModel):
    """任何非平凡结论都应能追溯到至少一条引用"""
    source_type: Literal[
        "standard",              # 国标/行标/IEC等
        "patent",
        "academic_paper",
        "industry_report",       # 行业协会/研究机构报告
        "teardown_report",       # 拆解报告(如iFixit/权威拆解机构)
        "manufacturer_datasheet",# 厂商技术规格书
        "bom_disclosure",        # 公开BOM/环境声明(EPD)
        "news",
        "other",
    ]
    title: str
    publisher: Optional[str] = None
    url: Optional[str] = None
    publish_date: Optional[str] = None  # ISO格式或"未知"
    confidence: Literal["high", "medium", "low"]
    excerpt_note: Optional[str] = Field(
        default=None,
        description="引用支持的具体论点摘要(用自己的话转述，不做原文长引用)"
    )


class QuantValue(BaseModel):
    value: float
    unit: str
    uncertainty_pct: Optional[float] = None
    value_type: Literal["point_estimate", "range_midpoint", "reported_exact"] = "point_estimate"
    range_low: Optional[float] = None
    range_high: Optional[float] = None


class DimensionCategory(str, Enum):
    """维度类别——仅作为元级分类兜底，不代表具体维度名"""
    TECHNOLOGY_ROUTE = "technology_route"          # 技术路线，如电池技术
    PHYSICAL_SPEC = "physical_spec"                # 物理规格/外形尺寸标准
    CAPACITY_POWER = "capacity_power"              # 容量/功率等级
    VOLTAGE_CLASS = "voltage_class"                # 电压等级(输配电类产品常见)
    APPLICATION_SCENARIO = "application_scenario"  # 应用场景(如陆上/海上)
    INSTALLATION_METHOD = "installation_method"    # 安装/敷设方式
    REGIONAL_STANDARD = "regional_standard"        # 区域标准/市场
    MATERIAL_ARCHITECTURE = "material_architecture"# 材料/结构路线(如永磁直驱vs双馈)
    DRIVETRAIN_ARCHITECTURE = "drivetrain_architecture"  # 传动/动力架构(如BEV/PHEV子类)
    OTHER = "other"


# ============================================================
# 树1：分类维度体系（型号 = 多维度的笛卡尔组合，而非单一枚举）
# ============================================================

class DimensionValue(BaseModel):
    value_id: str
    value_name: str
    axis_conformity_justification: str = Field(
        description="【关键字段】明确说明此取值为何符合所属维度的axis_of_variation定义，"
                    "而不是碰巧属于另一个分类角度。例如维度是'电池技术路线'，"
                    "取值'TOPCon'的justification应该是'一种N型电池片技术'，"
                    "而不能是'182mm尺寸的组件'这种物理规格描述——"
                    "若无法写出符合该轴定义的justification，说明这个取值本不该放在这里。"
    )
    prevalence_desc: Optional[str] = Field(
        default=None,
        description="定性描述市场地位，如'当前主流'/'新兴技术'/'区域性主流'，禁止编造具体数字市占率"
    )
    typical_spec_range: Optional[str] = None
    citations: list[Citation] = Field(default_factory=list)


class ClassificationDimension(BaseModel):
    dimension_id: str
    dimension_name: str
    dimension_category: DimensionCategory
    axis_of_variation: str = Field(
        description="【关键字段】用一句话精确定义这个维度下所有取值应该且只应该在"
                    "哪一个概念轴上产生差异，例如'不同的电池片技术路线'。"
                    "这句定义是后续校验'取值是否串轴'的唯一依据，必须写得足够精确、"
                    "可用来直接判断某个候选取值算不算数，而不能写成宽泛的产品名称。"
    )
    definition_rationale: str = Field(
        description="为什么这构成一个独立的分类角度——必须能回答'区分了什么、影响了什么'"
    )
    orthogonality_check: str = Field(
        description="该维度与本产品其它已识别维度的关系说明：完全独立/存在耦合(需说明耦合方式)"
    )
    values: list[DimensionValue]
    evidence: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_evidence_or_values_evidence(self):
        if not self.evidence and not any(v.citations for v in self.values):
            raise ValueError(
                f"维度 [{self.dimension_name}] 缺乏任何引用支持，禁止无来源新增维度"
            )
        return self


# 轻量级代码启发式：不能替代语义判断，但能给CriticAgent一个初筛提示，
# 降低"同轴一致性"这类判断完全依赖LLM主观判断的风险。
_PHYSICAL_SPEC_TOKEN = re.compile(
    r"\d+\s*(mm|cm|m²|m\b|kW|kWh|MW|MWh|W\b|V\b|kV|A\b|片|串|路|极|kg|吨|档位)",
    re.IGNORECASE,
)


def heuristic_axis_consistency_scan(dimension: "ClassificationDimension") -> list[str]:
    """对某个已产出的维度做代码层面的初筛，标记"取值名称看起来像另一个轴"的可疑项。
    仅产出警示文本供CriticAgent参考，不直接判定为错误（避免正则误伤），
    但如果CriticAgent对被标记的取值视而不见，人工复核时能一眼看出漏检。
    """
    warnings: list[str] = []
    for v in dimension.values:
        looks_like_spec = bool(_PHYSICAL_SPEC_TOKEN.search(v.value_name))
        category_claims_non_spec = dimension.dimension_category not in (
            DimensionCategory.PHYSICAL_SPEC, DimensionCategory.CAPACITY_POWER,
            DimensionCategory.VOLTAGE_CLASS,
        )
        if looks_like_spec and category_claims_non_spec:
            warnings.append(
                f"[系统预警-启发式] 维度「{dimension.dimension_name}」"
                f"(声称类别: {dimension.dimension_category.value}) 的取值「{v.value_name}」"
                f"包含物理规格/参数特征的字符模式，与该维度声称的分类角度不符，"
                f"请重点核查该取值是否为跨轴混入。"
            )
    return warnings


# ============================================================
# 树2：结构分解体系（产品的功能子系统/部件，与"型号维度"无关）
# ============================================================

class FunctionalSubsystem(BaseModel):
    subsystem_id: str
    subsystem_name: str
    parent_subsystem_id: Optional[str] = None  # 支持递归细分为多级BOM树
    decomposition_level: int = Field(
        description="0=顶层子系统, 1=子系统下的模块, 2=模块下的部件, 以此类推"
    )
    function_description: str
    varies_by_dimension_ids: list[str] = Field(
        default_factory=list,
        description="该子系统的具体构成/是否存在，是否随某些分类维度的取值而改变"
    )
    mass_share_desc: Optional[str] = Field(
        default=None,
        description="该子系统占整机质量的定性/定量描述，用于后续覆盖度校验"
    )
    evidence: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dimension_name_collision_hint(self):
        # 硬约束: 结构节点名不应与"技术路线/规格"这类措辞完全同构，
        # 真正的语义重叠交给 cross_validation_node 做，这里只做最基础的自查字段存在性
        if not self.function_description.strip():
            raise ValueError(f"子系统 [{self.subsystem_name}] 缺少功能描述，无法与分类维度区分")
        return self


class CopperComponent(BaseModel):
    """含铜部位必须挂在结构树的叶子节点上，不能挂在维度树上"""
    component_id: str
    parent_subsystem_id: str
    component_name: str
    copper_form: Literal[
        "pure_copper", "copper_alloy", "copper_clad_aluminum",
        "copper_wire", "copper_busbar", "copper_winding",
        "copper_foil", "copper_tape", "copper_sheet", "other",
    ]
    function_of_copper: str = Field(
        description="此处为何选用铜：导电/导热/耐蚀/屏蔽/成本工艺等，需具体到该部件"
    )
    unit_mass: Optional[QuantValue] = None
    mass_data_basis: Literal[
        "teardown_measurement", "bom_disclosure",
        "engineering_estimate", "literature_reported", "manufacturer_stated", "other",
    ]
    applies_to_dimension_values: dict[str, str] = Field(
        default_factory=dict,
        description="dimension_id -> value_id，标明此含铜量数据对应哪个具体型号取值组合，"
                    "若对全部取值通用可留空"
    )
    substitutable: Optional[bool] = Field(
        default=None,
        description="该铜部件是否存在已知可替代技术路线(为后续减量化措施预留标记)"
    )
    citations: list[Citation] = Field(default_factory=list)


# ============================================================
# 铜减量化措施 与 强度变化路径
# ============================================================

class EngineeringCase(BaseModel):
    """强制要求真实工程案例，杜绝空谈技术可能性"""
    project_or_product_name: str
    implementing_entity: str
    year: int
    outcome_description: str = Field(description="实际取得的减量效果或验证结果")
    scale: Literal["lab_prototype", "pilot_project", "commercial_deployment"]
    citations: list[Citation]


class CopperReductionMeasure(BaseModel):
    measure_id: str
    measure_name: str
    applicable_scope: Optional[str] = Field(
        default=None,
        description="子类别范围(如'陆上'/'海上')。同一措施在不同子类别下数值可能不同时，"
                    "应拆成多条记录、measure_id相同、applicable_scope不同，而不是把子类别"
                    "编码进measure_id或字段名——保持与Section3/6统一的'行不用列'原则。"
                    "无子类别的产品填None。"
    )
    target_component_ids: list[str]
    mechanism: str = Field(description="技术机理：材料替代/结构优化/拓扑改进/工艺改进等")
    mechanism_category: Literal[
        "material_substitution",   # 如铝代铜
        "structural_optimization", # 减少冗余/优化路径
        "design_topology_change",  # 系统级拓扑变化(如更高电压等级降低载流需求)
        "manufacturing_process",   # 工艺改进降低损耗/边角料
        "lifecycle_extension",     # 延长寿命摊薄单位强度(需谨慎论证是否算减量化)
        "other",
    ]
    expected_reduction: Optional[QuantValue] = Field(
        default=None, description="预期降低幅度，unit建议用'%'或'kg/functional_unit'"
    )
    trade_offs: Optional[str] = Field(
        default=None, description="已知的性能/成本/可靠性权衡，不能只报喜不报忧"
    )
    maturity: Literal["commercial", "pilot", "lab", "concept"]
    earliest_feasible_year: int
    engineering_case: EngineeringCase
    additional_citations: list[Citation] = Field(default_factory=list)


# ============================================================
# 情景实体：对应xlsx模板 Section 5「减量化情景定义」
# ============================================================

# 固定的情景代码体系：S0-S3，语义和名称在7种产品间强制统一，
# 不允许各产品自行定义情景数量或命名（例如不能有产品只出2个情景，
# 或把"加速减量"叫成别的名字）——这是与Section2/4/5"允许产品自由调研"
# 不同性质的约束：情景框架是本项目统一设定的分析口径，不是调研对象。
ScenarioCode = Literal["S0", "S1", "S2", "S3"]

SCENARIO_CODE_FIXED_NAMES: dict[str, str] = {
    "S0": "基准情景",
    "S1": "普通减量",
    "S2": "加速减量",
    "S3": "深度减量",
}
SCENARIO_SEVERITY_ORDER: list[str] = ["S0", "S1", "S2", "S3"]


class Scenario(BaseModel):
    """一个情景 = 一组措施的组合 + 达成节奏假设。

    情景代码固定为S0-S3四种，语义为：
      S0 基准情景 —— 不纳入任何减量化措施，Δmax恒为0，作为比较基准
      S1 普通减量 —— 力度最小的实质减量情景
      S2 加速减量 —— 力度中等
      S3 深度减量 —— 力度最大

    重要：S1/S2/S3彼此的included_measure_ids之间【不要求】构成父子集关系
    （即不强制S3的措施集合包含S2的全部措施），因为不同措施之间可能存在
    互斥关系（如两种材料替代方案不能同时采用），S3完全可以选用一套与S2
    不同的措施组合。唯一的硬约束是【减量力度的排序】：同一applicable_scope
    下必须满足 Δmax(S0)=0 < Δmax(S1) < Δmax(S2) < Δmax(S3)，这个约束在
    ProductReductionModel层面对整组情景做跨对象校验（见下方
    _scenario_severity_ordering），单个Scenario对象自己无法校验，
    因为需要同时看到同一子类别下的其它三个情景才能比较。

    若情景在不同子类别下的Δmax/达成节奏不同，应拆成多条记录
    （同一scenario_id，不同applicable_scope），而不是新增专属字段——
    与CopperReductionMeasure的applicable_scope用法一致。
    """
    scenario_id: ScenarioCode
    scenario_name: str
    applicable_scope: Optional[str] = Field(
        default=None, description="子类别范围(如'陆上'/'海上')，无子类别的产品填None"
    )
    scenario_definition: str = Field(description="情景的定性描述，如'高铜技术参考配置，不额外实施减铜措施'")
    included_measure_ids: list[str] = Field(
        default_factory=list,
        description="纳入本情景的CopperReductionMeasure.measure_id列表。"
                    "S1/S2/S3之间不要求存在包含关系，允许互斥措施分别归属不同情景。"
    )
    full_implementation_reduction_pct: QuantValue = Field(
        description="Δmax：本情景所有纳入措施全部100%实施后的综合降铜幅度。"
                    "S0固定为0；S1<S2<S3的严格递增关系在跨情景校验中强制检查。"
    )
    measure_start_year: int
    target_achievement_year: int = Field(description="达到target_achievement_rate_pct所在的年份")
    target_achievement_rate_pct: float = Field(
        description="target_achievement_year当年的情景实现率(0-100)，不一定是100，"
                    "可能受限于市场渗透率天花板等因素"
    )
    diffusion_method: Literal["linear", "s_curve", "step", "other"] = Field(
        description="措施从start_year到target_achievement_year之间的采纳率扩散方式假设"
    )
    scenario_rationale: str = Field(description="为什么这样设定Δmax/达成节奏，需要引用支撑")
    notes: Optional[str] = None
    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fixed_name_and_s0_constraints(self):
        expected_name = SCENARIO_CODE_FIXED_NAMES[self.scenario_id]
        if self.scenario_name != expected_name:
            raise ValueError(
                f"scenario_id={self.scenario_id} 的名称必须固定为「{expected_name}」，"
                f"不允许自定义为「{self.scenario_name}」——情景命名体系在7种产品间统一，不是调研对象。"
            )
        if self.scenario_id == "S0":
            if self.full_implementation_reduction_pct.value != 0:
                raise ValueError("S0(基准情景)的full_implementation_reduction_pct必须恒为0")
            if self.included_measure_ids:
                raise ValueError("S0(基准情景)不应纳入任何减量化措施，included_measure_ids必须为空")
        return self


class IntensityTrajectoryPoint(BaseModel):
    year: int
    scenario_id: str = Field(description="指向Scenario.scenario_id，取代此前的粗粒度枚举")
    applicable_scope: Optional[str] = Field(
        default=None, description="子类别范围，需与所引用Scenario的applicable_scope一致"
    )
    scenario_realization_rate_pct: float = Field(
        description="当年该情景的实现率(0-100)，由diffusion_method推算或人工设定"
    )
    unit_copper_intensity: QuantValue
    functional_unit: str
    delta_vs_baseline: Optional[QuantValue] = Field(
        default=None, description="较高铜技术参考基准的差值，通常为负值"
    )
    applied_measure_ids: list[str] = Field(default_factory=list)


# ============================================================
# 校验实体：把"概念混淆"变成可编程检测
# ============================================================

class ValidationFlag(BaseModel):
    flag_type: Literal[
        "dimension_overlap",           # 两个维度的取值集合语义重叠
        "concept_conflation",          # 结构节点名与维度取值名混淆
        "missing_component_coverage",  # 结构树未覆盖主要质量占比部件
        "unsupported_claim",           # 缺乏引用支撑的数值/结论
        "orphan_component",            # CopperComponent引用了不存在的subsystem_id
        "measure_without_case",        # 减量化措施缺少真实工程案例
        "scenario_severity_ordering_violation",  # S0<S1<S2<S3的减量力度排序被违反
        "incomplete_scenario_set",     # 某子类别下未凑齐S0-S3四个情景
        "duplicate_scenario_entry",    # 同一子类别下同一scenario_id出现了多次
        "other",
    ]
    description: str
    related_ids: list[str] = Field(default_factory=list)
    severity: Literal["blocking", "warning"]
    resolved: bool = False
    resolution_note: Optional[str] = None


# ============================================================
# 多角色协作专用：分歧/交锋记录
# ============================================================

AgentRole = Literal[
    "supervisor", "dimension_agent", "structure_agent",
    "copper_agent", "reduction_agent", "critic_agent", "quant_agent",
]


class Objection(BaseModel):
    """CriticAgent 对某个专业agent的产出提出的具体反对意见。
    与 ValidationFlag 的区别：Objection 是"过程中的交锋"，有明确的
    提出者/被质询者/轮次/回应状态，用于驱动revise循环；
    ValidationFlag 更偏"最终审计结论快照"。两者可以互相转化。
    """
    objection_id: str
    raised_by: AgentRole
    target_agent: AgentRole
    flag_type: Literal[
        "dimension_value_axis_mismatch",  # 【最高优先级】同一维度内混入了不属于该轴的取值
        "dimension_overlap", "concept_conflation", "missing_component_coverage",
        "unsupported_claim", "orphan_component", "measure_without_case",
        "scenario_severity_ordering_violation",  # S0<S1<S2<S3的减量力度排序被违反
        "incomplete_scenario_set",     # 某子类别下未凑齐S0-S3四个情景
        "duplicate_scenario_entry",    # 同一子类别下同一scenario_id出现了多次
        "insufficient_evidence_quality", "scope_creep", "other",
    ]
    detail: str = Field(description="具体到可执行的修改要求，而不是笼统的'有问题'。"
                        "若flag_type为dimension_value_axis_mismatch，必须点名具体是"
                        "哪个value_name串了轴、该维度声称的axis_of_variation是什么、"
                        "这个取值实际属于哪个轴。")
    severity: Literal["blocking", "warning"]
    round_raised: int
    target_field_refs: list[str] = Field(
        default_factory=list, description="被质询的具体条目ID，如某个dimension_id/subsystem_id"
    )
    addressed: bool = False
    response_note: Optional[str] = Field(
        default=None, description="被质询agent修订后对该objection的回应说明"
    )
    critic_accepts_response: Optional[bool] = Field(
        default=None, description="Critic复核修订后是否认可回应，None=尚未复核"
    )

    @model_validator(mode="after")
    def _axis_mismatch_always_blocking(self):
        # 用户明确要求：同一维度内取值不在同一分类轴上，是重大错误，
        # 不允许被critic降级为warning放行。
        if self.flag_type == "dimension_value_axis_mismatch" and self.severity != "blocking":
            raise ValueError(
                "dimension_value_axis_mismatch 类型的objection必须是blocking级别，"
                "不允许标记为warning——同轴不一致会使整个维度失去可计算意义。"
            )
        # 同理：情景减量力度排序/完整性/重复录入问题，也不允许降级为warning。
        if self.flag_type in (
            "scenario_severity_ordering_violation", "incomplete_scenario_set", "duplicate_scenario_entry",
        ) and self.severity != "blocking":
            raise ValueError(
                f"{self.flag_type} 类型的objection必须是blocking级别，不允许标记为warning。"
            )
        return self


def upsert_objections(existing: list[dict], new: list[dict]) -> list[dict]:
    """LangGraph自定义reducer：按objection_id做upsert，而不是简单追加。

    LangGraph在合并节点返回的state更新时会调用 reducer(当前累积值, 本次增量)。
    默认的operator.add会把new直接拼接到existing后面，导致同一个objection_id
    被"修订/复核"多次后，state里堆积大量重复/过期记录，读取时还需要额外的
    _latest_objections()辅助函数按id去重取最新版——这既浪费存储也容易在
    忘记调用去重辅助函数的地方读到过期数据。

    改用这个reducer后，state.objections字段本身在任何时刻都是"每个
    objection_id只有一条、且是最新版本"的干净列表，所有节点可以直接
    读取 state["objections"] 而不需要再调用任何去重辅助函数。
    """
    merged: dict[str, dict] = {o["objection_id"]: o for o in existing}
    for o in new:
        merged[o["objection_id"]] = o  # 新版本整体覆盖旧版本
    return list(merged.values())


class CriticReview(BaseModel):
    """CriticAgent单轮完整判断的结构化输出：既包含对本轮新发现问题的
    new_objections，也包含对历史已声称"被回应"的objection的复核结论。

    没有这个字段，critic的"跨轮记忆"就只是摆设——它能看到历史objection和
    对方的response_note，但如果没有一个显式的输出渠道让它说"这个回应我不
    认可，重新打回"，那么无论critic内心怎么判断，都不会对流程产生任何
    实际影响。reopened_objection_ids就是这个渠道。
    """
    new_objections: list[Objection] = Field(default_factory=list)
    reopened_objection_ids: list[str] = Field(
        default_factory=list,
        description="复核后认为对方的response_note并未真正解决问题、需要重新打开"
                    "(即重新标记为未解决)的历史objection_id列表。"
                    "只填之前已经addressed=True、但你复核后不认可其response_note的条目，"
                    "不要填从未被回应过的objection。"
    )
    reopen_reasoning: dict[str, str] = Field(
        default_factory=dict,
        description="key为reopened_objection_ids中的objection_id，value为具体说明"
                    "对方的回应哪里不成立——不能只填id不给理由，否则被质询方无法"
                    "知道还要再改什么。"
    )


class DebateLogEntry(BaseModel):
    """完整交锋记录，用于事后追溯'为什么最终结论是这样'，也便于人工审计"""
    round: int
    speaker: AgentRole
    message_type: Literal["proposal", "revision", "objection", "defense",
                           "approval", "routing_decision"]
    summary: str
    ref_ids: list[str] = Field(default_factory=list)


class SupervisorDecision(BaseModel):
    """Supervisor每次路由决策的结构化输出，强制给出理由，便于审计和调试"""
    next_agent: AgentRole
    reasoning: str
    unresolved_blocking_objections: list[str] = Field(
        default_factory=list, description="路由时仍未解决的blocking objection_id列表"
    )
    should_terminate: bool = False
    termination_reason: Optional[str] = None


# ============================================================
# 图A产出：调研阶段的锁定交付物（对应任务2的范围边界）
# ============================================================

class ProductResearchOutput(BaseModel):
    """图A(Research Graph)的唯一产出。一旦 research_status 变为 approved，
    应被视为只读事实资产——图B(Reduction Graph)只能读取，不能直接修改。
    """
    product_id: str
    product_name: str
    research_version: int = 1
    research_status: Literal[
        "draft", "pending_human_review", "approved", "amendment_in_progress",
    ] = "draft"
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    supersedes_version: Optional[int] = Field(
        default=None, description="若这是一次修订(amendment)的结果，指向被替代的旧版本号"
    )

    classification_dimensions: list[ClassificationDimension]
    functional_subsystems: list[FunctionalSubsystem]
    copper_components: list[CopperComponent]

    validation_flags: list[ValidationFlag] = Field(default_factory=list)
    objections: list[Objection] = Field(
        default_factory=list, description="调研阶段全部交锋记录(仅事实/概念类质询，不含措施证据类)"
    )
    debate_log: list[DebateLogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_reference_integrity(self):
        subsystem_ids = {s.subsystem_id for s in self.functional_subsystems}
        dimension_ids = {d.dimension_id for d in self.classification_dimensions}

        for comp in self.copper_components:
            if comp.parent_subsystem_id not in subsystem_ids:
                self.validation_flags.append(ValidationFlag(
                    flag_type="orphan_component",
                    description=f"部件 {comp.component_name} 挂载的子系统ID不存在: {comp.parent_subsystem_id}",
                    related_ids=[comp.component_id],
                    severity="blocking",
                ))
            for dim_id in comp.applies_to_dimension_values:
                if dim_id not in dimension_ids:
                    self.validation_flags.append(ValidationFlag(
                        flag_type="orphan_component",
                        description=f"部件 {comp.component_name} 引用了不存在的维度ID: {dim_id}",
                        related_ids=[comp.component_id],
                        severity="blocking",
                    ))

        # 同轴一致性的最终防线：即便critic在过程中失察，integration阶段也强制复扫一遍启发式规则
        for dim in self.classification_dimensions:
            for w in heuristic_axis_consistency_scan(dim):
                self.validation_flags.append(ValidationFlag(
                    flag_type="unsupported_claim",  # 启发式警告降级挂载，不冒充人工/LLM已确认的axis_mismatch
                    description=w,
                    related_ids=[dim.dimension_id],
                    severity="warning",
                ))

        if self.research_status == "approved" and any(
            f.severity == "blocking" and not f.resolved for f in self.validation_flags
        ):
            raise ValueError(
                "存在未解决的blocking级别validation_flag，不允许将research_status标记为approved"
            )
        return self


# ============================================================
# 图A→图B 的正规反馈通道：图B发现调研缺口时，不得直接改数据，
# 只能提交修订请求，触发图A的局部重跑
# ============================================================

class AmendmentRequest(BaseModel):
    requested_by: Literal["reduction_agent", "quant_agent", "critic_agent_reduction"]
    target_product_id: str
    target_research_version: int
    issue_description: str = Field(description="具体发现了什么调研缺口/疑似错误")
    suggested_rescan_scope: Literal[
        "specific_subsystem", "specific_dimension", "full_copper_identification", "full_research",
    ]
    suggested_rescan_target_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    status: Literal["pending", "accepted", "rejected"] = "pending"


# ============================================================
# 图B产出：减量化措施 + 路径建模（独立、可基于同一份研究成果多次重跑）
# ============================================================

class ProductReductionModel(BaseModel):
    product_id: str
    based_on_research_version: int = Field(
        description="本次减量化分析所依据的ProductResearchOutput版本号，用于追溯"
    )
    analysis_run_id: str

    functional_unit_candidates: list[str]
    functional_unit_rationale: str
    chosen_functional_unit: str

    baseline_year: int
    baseline_unit_intensity: QuantValue
    baseline_calc_method: str

    reduction_measures: list[CopperReductionMeasure]
    scenarios: list[Scenario]
    trajectory: list[IntensityTrajectoryPoint]

    amendment_requests: list[AmendmentRequest] = Field(default_factory=list)
    validation_flags: list[ValidationFlag] = Field(default_factory=list)
    objections: list[Objection] = Field(default_factory=list)
    debate_log: list[DebateLogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _trajectory_scenario_integrity(self):
        scenario_ids = {s.scenario_id for s in self.scenarios}
        for point in self.trajectory:
            if point.scenario_id not in scenario_ids:
                self.validation_flags.append(ValidationFlag(
                    flag_type="orphan_component",
                    description=f"路径点(year={point.year})引用了不存在的scenario_id: {point.scenario_id}",
                    related_ids=[point.scenario_id],
                    severity="blocking",
                ))
        return self

    @model_validator(mode="after")
    def _scenario_severity_ordering(self):
        """跨情景校验：同一applicable_scope下必须【恰好】各出现一次S0-S3，
        且 Δmax(S0)=0 < Δmax(S1) < Δmax(S2) < Δmax(S3) 严格递增。
        这个校验必须在ProductReductionModel层面做，因为单个Scenario对象
        看不到同一子类别下的其它三个情景，无法自行判断排序/重复是否正确。
        """
        from collections import Counter

        by_scope: dict[Optional[str], list[Scenario]] = {}
        for s in self.scenarios:
            by_scope.setdefault(s.applicable_scope, []).append(s)

        for scope, scen_list in by_scope.items():
            scope_label = scope or "(无子类别)"
            id_counts = Counter(s.scenario_id for s in scen_list)

            duplicated = [code for code, cnt in id_counts.items() if cnt > 1]
            if duplicated:
                self.validation_flags.append(ValidationFlag(
                    flag_type="duplicate_scenario_entry",
                    description=f"子类别「{scope_label}」下情景代码重复出现: {duplicated}，"
                                f"每个子类别下每个scenario_id只能出现一次，"
                                f"重复录入会导致取值歧义(取哪一条数据不确定)。",
                    related_ids=[f"{scope_label}:{c}" for c in duplicated],
                    severity="blocking",
                ))
                continue  # 存在重复时无法确定唯一取值，跳过下面的排序检查

            scen_map = {s.scenario_id: s for s in scen_list}
            missing = [c for c in SCENARIO_SEVERITY_ORDER if c not in scen_map]
            if missing:
                self.validation_flags.append(ValidationFlag(
                    flag_type="incomplete_scenario_set",
                    description=f"子类别「{scope_label}」缺少情景: {missing}，"
                                f"S0-S3四个情景必须在每个子类别下都齐全",
                    related_ids=[f"{scope_label}:{c}" for c in missing],
                    severity="blocking",
                ))
                continue  # 缺情景时无法继续比较排序，跳过下面的数值检查

            units = {scen_map[c].full_implementation_reduction_pct.unit for c in SCENARIO_SEVERITY_ORDER}
            if len(units) > 1:
                self.validation_flags.append(ValidationFlag(
                    flag_type="unsupported_claim",
                    description=f"子类别「{scope_label}」下S0-S3的Δmax单位不一致: {units}，无法比较排序",
                    related_ids=[scope_label],
                    severity="blocking",
                ))
                continue

            values = [scen_map[c].full_implementation_reduction_pct.value for c in SCENARIO_SEVERITY_ORDER]
            if not (values[0] < values[1] < values[2] < values[3]):
                self.validation_flags.append(ValidationFlag(
                    flag_type="scenario_severity_ordering_violation",
                    description=(
                        f"子类别「{scope_label}」的Δmax未满足严格递增要求: "
                        f"S0={values[0]} S1={values[1]} S2={values[2]} S3={values[3]}，"
                        f"深度减量(S3)的减量力度必须大于加速减量(S2)，大于普通减量(S1)，大于基准(S0)=0。"
                        f"注意：这不要求measure_id构成父子集关系，只要求最终的综合降幅数值严格递增。"
                    ),
                    related_ids=[f"{scope_label}:{c}" for c in SCENARIO_SEVERITY_ORDER],
                    severity="blocking",
                ))
        return self

    @model_validator(mode="after")
    def _measures_require_case(self):
        for measure in self.reduction_measures:
            if measure.engineering_case is None:
                self.validation_flags.append(ValidationFlag(
                    flag_type="measure_without_case",
                    description=f"措施 {measure.measure_name} 缺少工程案例",
                    related_ids=[measure.measure_id],
                    severity="blocking",
                ))
        return self


# ============================================================
# 7种产品的顶层注册表（只放"研究对象名"，不预置任何内部结构假设）
# ============================================================

TARGET_PRODUCTS: list[dict] = [
    {"product_id": "pv_station", "product_name": "光伏电站"},
    {"product_id": "building", "product_name": "建筑"},
    {"product_id": "hvac", "product_name": "空调"},
    {"product_id": "power_transformer", "product_name": "输配电变压器"},
    {"product_id": "underground_cable", "product_name": "配电地下铜缆"},
    {"product_id": "ev", "product_name": "电动汽车(BEV/PHEV)"},
    {"product_id": "wind_turbine", "product_name": "风力发电机(海上/陆上)"},
]
