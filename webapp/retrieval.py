"""Tavily 联网检索增强层（Retrieval-Augmented Structured LLM）
==================================================================
解决的核心问题：图A/图B 的 agent 原本是纯 LLM 推理，Citation 的来源
依赖模型参数记忆，容易编造 URL / 过时数据。

方案：不改动任何图代码与 persona，在 llm.with_structured_output(...) 的
invoke 路径上做"检索前置增强"：
  1. 根据系统提示词中的角色标记（「分类维度分析师」等）识别当前 agent；
  2. 按角色查询模板 + 产品名构造 Tavily 检索查询（每个 agent 2条查询）；
  3. 把真实检索结果(标题/URL/日期/摘要)作为证据块注入 system 消息；
  4. 强约束：Citation 必须优先引用证据块中的真实来源，禁止编造 URL。
supervisor/critic 等不需要外部事实的角色自动跳过检索。

用法：
    llm = RetrievalAugmentedLLM(base_llm, product_name="空调")
    structured = llm.with_structured_output(list[ClassificationDimension])
    dims = structured.invoke(messages)   # 内部自动先检索再结构化解码
"""

from __future__ import annotations
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import get_args, get_origin

import requests
from langchain_core.messages import SystemMessage
from pydantic import BaseModel, ValidationError

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from regions import CountryInfo, RegionInfo
from evidence_scoring import classify_source_tier, citation_region_hit

TAVILY_ENDPOINT = "https://api.tavily.com/search"

# 角色标记(取自各persona的「」称号) -> 基础查询模板（不含产品名/区域，运行时拼接）
# 双语模板：(中文, 英文)，按区域的 query_lang 选择，避免非中文区域检索被中文源占据
ROLE_QUERY_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "国家市场与标准研究员": [
        ("国家市场结构 标准 政策 材料选型", "national market standards policy material practice"),
        ("政府采购 行业规范 主流配置", "government procurement industry code mainstream configuration"),
    ],
    "分类维度分析师": [
        ("主流型号 分类维度 技术路线", "main product models classification technology routes"),
        ("型号分类 规格 标准 类型", "product models specifications standards types"),
    ],
    "产品结构分析师": [
        ("系统组成 主要部件 结构", "system components main parts structure"),
        ("整机构成 部件 BOM", "product BOM assembly parts list"),
    ],
    "铜部件识别专家": [
        ("含铜部件 铜用量 kg", "copper content parts kg per unit"),
        ("铜质量 拆解 用铜量", "teardown copper mass windings"),
    ],
    "铜减量化技术顾问": [
        ("铜减量化 材料替代 措施", "copper reduction material substitution"),
        ("减少铜用量 工程案例", "copper saving engineering case"),
    ],
    "国家措施适用性研究员": [
        ("减铜措施 工程应用 政策限制", "copper reduction measure deployment policy restriction"),
        ("材料替代 采购标准 示范项目", "material substitution procurement standard demonstration project"),
    ],
    "定量分析师": [
        ("单位铜强度 情景", "copper intensity per unit forecast"),
        ("铜强度 预测 2035", "copper intensity projection 2035"),
    ],
}

# 区域专项查询（每角色1条；拼接在基础查询之后，占用剩余证据额度）
REGION_ROLE_QUERY_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "国家市场与标准研究员": [
        ("市场份额 国家标准 铜铝材料", "market share national standard copper aluminum material"),
    ],
    "分类维度分析师": [
        ("市场占有率 主流型号", "market share leading models"),
    ],
    "产品结构分析师": [
        ("市场主流配置 型号", "market mainstream configuration"),
    ],
    "铜部件识别专家": [
        ("铝代铜 铝绕组 铜绕组 市场份额", "aluminum winding copper winding market share"),
    ],
    "铜减量化技术顾问": [
        ("铝代铜 政策 电网 变压器", "aluminum substitution policy transformer grid"),
    ],
    "国家措施适用性研究员": [
        ("本国实施案例 技术规范", "national implementation case technical code"),
    ],
    "定量分析师": [
        ("市场规模 铜 用量", "market size copper consumption"),
    ],
}

# 不需要联网事实的角色（路由/审查判断类）
SKIP_SEARCH_MARKERS = ("主管", "审查专家", "调研团队主管", "减量化团队主管")

MAX_RESULTS_PER_QUERY = 4
MAX_EVIDENCE_AFTER_DEDUPE = 10  # URL去重后的证据上限（区域查询占用额度）
CONTENT_SNIPPET_CHARS = 600
SEARCH_TIMEOUT = 25
# 校验失败后的自愈重试次数（把Pydantic错误回喂给LLM修正）
MAX_SELF_HEAL_RETRIES = 2

SELF_HEAL_PROMPT = (
    "你上一次的输出未能通过Pydantic数据校验，错误信息：\n{err}\n\n"
    "请严格按原Schema重新输出完整JSON，务必纠正上述错误："
    "必填字段缺失的必须补上；类型不符（如字符串字段填了null）的必须替换为"
    "符合类型的合法值。所有ID类字段（如parent_subsystem_id）必须引用"
    "输入上下文中真实存在的标识符，禁止填null、空串或无意义占位符；"
    "其它已正确的字段保持不变。不要输出任何解释文字。")


def tavily_search(api_key: str, query: str, max_results: int = MAX_RESULTS_PER_QUERY) -> list[dict]:
    """调用 Tavily Search API，返回 [{title,url,content,published_date}]"""
    resp = requests.post(
        TAVILY_ENDPOINT,
        json={
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
        },
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "content": (r.get("content") or "")[:CONTENT_SNIPPET_CHARS],
            "published_date": r.get("published_date") or "",
        }
        for r in data.get("results", [])
    ]


def _extract_json_array(text: str):
    """从LLM返回的字符串中鲁棒地提取JSON数组/对象"""
    text = text.strip()
    # 直接整体解析
    try:
        data = json.loads(text)
        if isinstance(data, (list, dict)):
            return data
    except Exception:
        pass
    # markdown代码块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            if isinstance(data, (list, dict)):
                return data
        except Exception:
            pass
    # 首个[ ... 最后一个]
    i, j = text.find("["), text.rfind("]")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            pass
    # 首个{ ... 最后一个}
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            pass
    return None


def _list_elem(schema):
    """若schema为list[BaseModel]则返回元素类型，否则返回None"""
    if get_origin(schema) in (list,):  # py3.9兼容：不使用 typing.List
        args = get_args(schema)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def coerce_structured(result, schema):
    """兼容性纠偏：部分OpenAI兼容端点(如mimo-v2.5)在 list[Model] 类型的
    结构化输出下可能返回原始JSON字符串/单个对象/未解码条目，这里统一
    纠偏为schema期望的形态，失败则原样返回让上层报错。"""
    elem = _list_elem(schema)

    # 期望list但拿到字符串 -> 解析JSON
    if elem is not None and isinstance(result, str):
        data = _extract_json_array(result)
        if data is None:
            return result
        if isinstance(data, dict):
            # 包装对象如 {"items": [...]} / {"dimensions": [...]}
            for v in data.values():
                if isinstance(v, list):
                    data = v
                    break
        if not isinstance(data, list):
            data = [data]
        result = data

    # 期望list但拿到单个BaseModel(elem实例) -> 包一层
    if elem is not None and isinstance(result, BaseModel):
        result = [result]

    # list内条目未解码(dict/str) -> 逐条验证为elem
    if elem is not None and isinstance(result, list):
        fixed = []
        for item in result:
            if isinstance(item, elem):
                fixed.append(item)
            elif isinstance(item, str):
                data = _extract_json_array(item)
                if isinstance(data, dict):
                    item = data
            if isinstance(item, dict):
                fixed.append(elem.model_validate(item))
            elif isinstance(item, str):
                fixed.append(elem.model_validate_json(item))
            else:
                fixed.append(item)
        return fixed

    # 期望单模型但拿到字符串 -> 解析JSON后验证
    if (isinstance(schema, type) and issubclass(schema, BaseModel)
            and isinstance(result, str)):
        data = _extract_json_array(result)
        if isinstance(data, dict):
            try:
                return schema.model_validate(data)
            except ValidationError:
                return result
    return result


def detect_role(system_text: str) -> str | None:
    for role in ROLE_QUERY_TEMPLATES:
        if role in system_text:
            return role
    return None


def should_skip_search(system_text: str) -> bool:
    return any(m in system_text for m in SKIP_SEARCH_MARKERS)


def format_evidence_block(results: list[dict], queries: list[str]) -> str:
    lines = [
        "【联网检索证据】以下为刚刚通过 Tavily 检索到的真实网络资料"
        f"（检索时间 {datetime.now():%Y-%m-%d %H:%M}，查询词：{' / '.join(queries)}）。"
        "你的产出必须优先基于这些证据；Citation 请引用其中真实存在的来源"
        "（title/publisher/url 照抄，不要改动），来源类型按实际情况选择。"
        "若你用自身知识补充证据之外的结论，url 必须留空，【严禁编造任何URL或文献】；"
        "证据不足以支撑的字段宁可留空/标注不确定性，也不允许臆断。"
        "标注了[区域命中]的结果明确覆盖研究区域，引用时应填"
        "Citation.evidence_country 为该结果实际覆盖的国家；"
        "未命中的结果一般是全球/其他区域口径。",
        "",
    ]
    for i, r in enumerate(results, start=1):
        date = f"，日期 {r['published_date']}" if r["published_date"] else ""
        hit = "[区域命中] " if r.get("region_hit") else ""
        lines.append(f"[{i}] {hit}{r['title']}（{r['url']}{date}）")
        if r["content"]:
            lines.append(f"    摘要：{r['content']}")
        lines.append("")
    return "\n".join(lines)


class RetrievalAugmentedLLM:
    """结构化输出统一入口（含Tavily检索增强 + 端点兼容性处理）。

    inner 必须是裸聊天模型(ChatOpenAI等)：
      - 单模型schema：走 function calling（主流端点支持良好）；
      - list[Model] schema：部分端点(如mimo-v2.5)会忽略嵌套JSON Schema、
        自造字段名，因此改走"schema内联提示 + json解析 + Pydantic校验"
        的可靠通道。
    """

    def __init__(self, inner, product_name: str, context_text: str = "",
                 api_key: str | None = None, region=None):
        self.inner = inner
        self.product_name = product_name
        self.context_text = context_text
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY")
        # 区域：RegionInfo 或 None（自由文本区域）；区域检索词/命中词由此驱动
        self.region = region if isinstance(region, (RegionInfo, CountryInfo)) else None
        # 可观测性：记录本LLM实例的全部检索行为，供上层写入执行日志
        self.search_log: list[dict] = []

    def with_structured_output(self, schema):
        elem = _list_elem(schema)
        if elem is not None:
            return _ListStructured(self, elem)
        return _SingleStructured(self, self.inner.with_structured_output(schema), schema)

    # 内部：组装带证据块与统一口径的完整消息
    def _compose(self, messages, extra_tail=None):
        msgs = []
        if self.context_text:
            msgs.append(SystemMessage(content=self.context_text))
        msgs.extend(messages)
        if extra_tail:
            msgs.append(SystemMessage(content=extra_tail))
        return msgs

    def _maybe_retrieve(self, messages):
        """返回(追加的证据SystemMessage或None)；不满足检索条件时返回None"""
        sys_text = ""
        if messages and hasattr(messages[0], "content"):
            sys_text = messages[0].content if isinstance(messages[0].content, str) else ""
        if (not self.api_key or not sys_text or should_skip_search(sys_text)):
            return None
        role = detect_role(sys_text)
        if role is None:
            return None
        evidence_block, results = self._search_for_role(role)
        print(f"[retrieval] {self.product_name}/{role}: 获得{len(results)}条证据", flush=True)
        return SystemMessage(content=evidence_block)

    def _self_heal(self, base_msgs, first_error, redo):
        """校验失败自愈：把Pydantic错误信息回喂给模型重试（最多2次）。
        redo(m) 接收追加修复提示后的消息列表，返回已验证结果。"""
        last = first_error
        for attempt in range(1, MAX_SELF_HEAL_RETRIES + 1):
            print(f"[self-heal] {self.product_name}: 第{attempt}次修正重试 "
                  f"({type(last).__name__}: {str(last)[:160]})", flush=True)
            fix = SystemMessage(content=SELF_HEAL_PROMPT.format(err=str(last)[:1500]))
            try:
                return redo([*base_msgs, fix])
            except (ValidationError, ValueError) as exc:
                last = exc
        raise last

    def _search_for_role(self, role: str) -> tuple[str, list[dict]]:
        # 按区域查询语言选择基础模板（双语对），并追加区域专项查询
        lang = self.region.query_lang if self.region else "both"

        def pick(pair):
            zh, en = pair
            if lang == "zh":
                return [zh]
            if lang == "en":
                return [en]
            return [zh, en]

        terms: list[str] = []
        for pair in ROLE_QUERY_TEMPLATES[role]:
            terms.extend(pick(pair))
        region_terms: list[str] = []
        for pair in REGION_ROLE_QUERY_TEMPLATES.get(role, []):
            region_terms.extend(pick(pair))

        region_alias = (self.region.all_hit_terms[0] if self.region
                        else None)
        queries = [f"{self.product_name} {q}" for q in terms]
        if self.region and region_alias:
            queries += [f"{region_alias} {self.product_name} {q}" for q in region_terms]
        elif region_terms:
            queries += [f"{self.product_name} {q}" for q in region_terms]

        all_results: list[dict] = []
        errors = []
        for q in queries:
            try:
                all_results.extend(tavily_search(self.api_key, q))
                self.search_log.append({"query": q, "ok": True, "n": len(all_results)})
            except Exception as exc:
                errors.append(f"{q}: {type(exc).__name__}: {exc}")
                self.search_log.append({"query": q, "ok": False, "error": str(exc)[:120]})
        if errors:
            print(f"[retrieval] 检索部分失败({self.product_name}/{role}): {errors}", flush=True)
        # 按URL去重，避免多条查询结果重叠；逐条标注区域命中与来源分层
        seen, uniq = set(), []
        for r in all_results:
            if not r["url"] or r["url"] in seen:
                continue
            seen.add(r["url"])
            r["region_hit"] = citation_region_hit(r, self.region)
            r["source_tier"] = classify_source_tier(r.get("title", ""), r["url"])
            uniq.append(r)
        uniq.sort(key=lambda x: 0 if x.get("region_hit") else 1)  # 区域命中优先展示
        return format_evidence_block(uniq[:MAX_EVIDENCE_AFTER_DEDUPE], queries), uniq[:MAX_EVIDENCE_AFTER_DEDUPE]


class _SingleStructured:
    """单模型schema：function calling + 检索增强 + 兼容性纠偏"""

    def __init__(self, parent, inner_so, schema):
        self.parent = parent
        self.inner_so = inner_so
        self.schema = schema

    def invoke(self, messages):
        evidence = self.parent._maybe_retrieve(messages)
        msgs = self.parent._compose(messages)
        if evidence is not None:
            msgs = [evidence, *msgs]

        def redo(m):
            return coerce_structured(self.inner_so.invoke(m), self.schema)

        try:
            return redo(msgs)
        except (ValidationError, ValueError) as exc:
            # 自愈：校验失败时把错误回喂给模型，让它修正自己的输出
            return self.parent._self_heal(msgs, exc, redo)


class _ListStructured:
    """list[Model] schema：schema内联提示 + json解析 + 逐条Pydantic校验"""

    def __init__(self, parent, elem):
        self.parent = parent
        self.elem = elem
        self._schema_json = json.dumps(elem.model_json_schema(), ensure_ascii=False)

    def invoke(self, messages):
        evidence = self.parent._maybe_retrieve(messages)
        tail = (
            "【输出格式硬约束】你的可见回复必须是且只能是一个JSON数组"
            "（以[开头，以]结尾），不要输出任何解释文字或markdown代码块标记。"
            "数组每个元素必须严格符合以下JSON Schema，字段名必须与Schema完全一致，"
            "必填字段必须提供，不确定的可选字段可省略；"
            "嵌套对象(如Citation)的来源信息必须来自上面给出的真实检索证据，禁止编造：\n"
            + self._schema_json
        )
        msgs = self.parent._compose(messages, extra_tail=tail)
        if evidence is not None:
            msgs = [evidence, *msgs]

        def redo(m):
            return self._parse_and_validate(self.parent.inner.invoke(m))

        try:
            return redo(msgs)
        except (ValidationError, ValueError) as exc:
            # 自愈：解析失败或字段校验失败时回喂错误重试
            return self.parent._self_heal(msgs, exc, redo)

    def _parse_and_validate(self, resp):
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        data = _extract_json_array(content)
        if data is None:
            raise ValueError(
                f"list结构化输出解析失败：模型未返回可解析的JSON数组。"
                f"原始返回前500字符：{content[:500]}")
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    data = v
                    break
        if not isinstance(data, list):
            data = [data]
        return [self.elem.model_validate(item) for item in data]
