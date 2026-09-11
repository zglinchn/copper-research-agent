"""运行管理器（RunManager）
==================================================================
职责：
  1. 每个 run 在独立后台线程中执行图A(调研)或图B(减量化)；
  2. 通过 graph.stream(stream_mode="updates") 逐节点捕获进度事件，
     供前端工作流面板实时展示；
  3. 捕获图A human_review_gate 的动态中断(interrupt)，挂起线程等待
     人工在网页上"通过/打回"，再用 Command(resume=...) 精确恢复；
  4. 支持"停止运行"：协作式取消（当前LLM调用无法强断，在下一个
     节点边界生效）。

LLM 双模式：
  - 配置 OPENAI_API_KEY（可选 OPENAI_BASE_URL / COPPER_MODEL）→ 在线调研；
  - 未配置 → DemoLLM 演示模式（占位数据，页面明确标注）。
  两种模式都会注入"研究区域/基准年/补充约束"的统一口径前言。
"""

from __future__ import annotations
import json
import hashlib
import os
import queue
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import get_args, get_origin

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command as ResumeCommand
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, SystemMessage
from pydantic import BaseModel

import research_graph
import reduction_graph
from schemas import TARGET_PRODUCTS
from demo_llm import DemoLLM
from retrieval import RetrievalAugmentedLLM, coerce_structured

PRODUCT_ICONS = {
    "pv_station": "☀️",
    "building": "🏢",
    "hvac": "❄️",
    "power_transformer": "🔌",
    "underground_cable": "⚡",
    "ev": "🚗",
    "wind_turbine": "💨",
}

# 工作流面板节点顺序与中文标签（图A/图B）
PIPELINES = {
    "research": [
        ("start", "开始"),
        ("supervisor", "主管调度"),
        ("country_agent", "国家市场与标准调研"),
        ("dimension_agent", "调研维度规划"),
        ("structure_agent", "结构分解"),
        ("copper_agent", "含铜部位识别"),
        ("critic_agent", "审查质询"),
        ("region_evidence_gate", "区域证据评估"),
        ("human_review_gate", "人工审核"),
        ("done", "完成"),
    ],
    "reduction": [
        ("start", "开始"),
        ("loader", "加载调研成果"),
        ("supervisor", "主管调度"),
        ("baseline_agent", "基线归一化参数"),
        ("baseline_calc_node", "基线确定性计算"),
        ("reduction_agent", "减量措施调研"),
        ("country_policy_agent", "国家措施适用性核查"),
        ("critic_agent", "审查质询"),
        ("quant_agent", "情景参数定义"),
        ("calculation_node", "情景路径确定性计算"),
        ("integration", "整合输出"),
        ("done", "完成"),
    ],
}

MAX_EVENTS = 300
# 运行历史配额：成功/失败终态各最多保留的归档条数，超出删除最旧的
MAX_ARCHIVED_PER_OUTCOME = 10


def run_outcome(run: dict) -> str | None:
    """run 的终态分类：'success' / 'failure'；未到达终态返回 None。"""
    status = run.get("status")
    if status == "completed":
        return "success"
    if status in ("failed", "cancelled", "interrupted"):
        return "failure"
    return None


class ContextAugmentedLLM:
    """把"研究区域/基准年/补充约束"作为统一口径前言注入每次LLM调用，
    不侵入图代码与persona提示词。"""

    def __init__(self, inner, context_text: str):
        self.inner = inner
        self.context_text = context_text

    def with_structured_output(self, schema):
        inner_so = self.inner.with_structured_output(schema)

        class _Augmented:
            def invoke(_self, messages):
                msgs = [SystemMessage(content=self.context_text), *messages]
                return inner_so.invoke(msgs)

        return _Augmented()


class WatchdogLLM:
    """LLM 调用级看门狗。

    线上实测：mimo 网关偶发长尾挂起（经代理的长连接有心跳，httpx
    read-timeout 永不触发，单个结构化调用可挂 40 分钟+）。本包装给每次
    调用加总时长上限：超时即放弃当前请求并重发一个全新请求（旧线程
    留在后台不再等待，服务进程长驻期间自然回收）。"""

    DEADLINE_SECONDS = 420
    RETRIES = 2

    def __init__(self, inner):
        self.inner = inner

    def with_structured_output(self, schema):
        outer = self

        class _WatchdogSO:
            def invoke(_self, messages):
                return outer._guarded(
                    lambda _messages: outer._invoke_structured(messages, schema), messages
                )

        return _WatchdogSO()

    def invoke(self, messages):
        return self._guarded(self._invoke_raw, messages)

    @staticmethod
    def _schema_instruction(schema) -> str:
        elem = get_args(schema)[0] if get_origin(schema) is list and get_args(schema) else None
        schema_type = elem if elem is not None else schema
        if isinstance(schema_type, type) and issubclass(schema_type, BaseModel):
            schema_json = schema_type.model_json_schema()
            if elem is not None:
                schema_json = {"type": "array", "items": schema_json}
            return (
                "【结构化输出格式】你的可见回复必须是且只能是符合以下 JSON Schema 的 JSON，"
                "不要输出解释文字或 Markdown 代码块：\n" +
                json.dumps(schema_json, ensure_ascii=False)
            )
        return ""

    @staticmethod
    def _chunk_text(chunk) -> str:
        message = getattr(chunk, "message", chunk)
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict)
            )
        tool_chunks = getattr(message, "tool_call_chunks", None) or []
        return "".join(str(item.get("args") or "") for item in tool_chunks)

    def _invoke_raw(self, messages):
        """将原始模型调用改为stream，保留完整可见文本后再交给结构化解析。"""
        if hasattr(self.inner, "stream"):
            parts = [self._chunk_text(chunk) for chunk in self.inner.stream(messages)]
            return AIMessage(content="".join(parts))
        return self.inner.invoke(messages)

    def _invoke_structured(self, messages, schema):
        instruction = self._schema_instruction(schema)
        prompt = [*messages, SystemMessage(content=instruction)] if instruction else messages
        raw = self._invoke_raw(prompt)
        content = raw.content if isinstance(raw, AIMessage) else raw
        return coerce_structured(content, schema)

    def _guarded(self, fn, messages):
        import concurrent.futures as cf
        last = None
        for attempt in range(self.RETRIES + 1):
            ex = cf.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex.submit(fn, messages)
                return fut.result(timeout=self.DEADLINE_SECONDS)
            except cf.TimeoutError as exc:
                last = exc
                print(f"[watchdog] LLM调用超过{self.DEADLINE_SECONDS}s无响应，"
                      f"放弃并重发（第{attempt + 1}/{self.RETRIES + 1}次）", flush=True)
            finally:
                ex.shutdown(wait=False)
        raise last


def _infer_stream_node(messages) -> str:
    """从当前模型请求的系统提示词识别工作流节点，供实时流日志归档。"""
    text = "\n".join(
        m if isinstance(m, str) else str(getattr(m, "content", ""))
        for m in (messages or [])
    )
    markers = (
        ("country_agent", ("国家市场与标准研究员", "国家市场与标准调研")),
        ("dimension_agent", ("分类维度分析师", "调研维度规划")),
        ("structure_agent", ("产品结构分析师", "结构分解")),
        ("copper_agent", ("铜部件识别专家", "含铜部位识别")),
        ("country_policy_agent", ("国家措施适用性研究员", "措施适用性")),
        ("reduction_agent", ("铜减量化技术顾问", "减量措施调研")),
        ("quant_agent", ("定量分析师", "情景参数定义")),
        ("critic_agent", ("审查专家", "CriticAgent")),
        ("supervisor", ("团队主管", "主管调度")),
    )
    for node, names in markers:
        if any(name in text for name in names):
            return node
    return "model"


class _LiveTokenHandler(BaseCallbackHandler):
    """接收聊天模型真实返回的可见 token，不生成或改写模型内容。"""

    def __init__(self, emit):
        self.emit = emit
        self.node = "model"

    def on_chat_model_start(self, serialized, messages, **kwargs):
        batch = messages[0] if messages and isinstance(messages[0], (list, tuple)) else messages
        self.node = _infer_stream_node(batch or [])

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.node = _infer_stream_node(prompts or [])

    def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        message = getattr(chunk, "message", chunk)
        text = RunManager._llm_chunk_text(message) if message is not None else ""
        if not text:
            if isinstance(token, str):
                text = token
            elif isinstance(token, list):
                text = "".join(str(x) for x in token)
        if text:
            self.emit(self.node, text)


def _build_base_llm(product_name: str, token_callback=None):
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        from langchain_openai import ChatOpenAI
        handler = _LiveTokenHandler(token_callback) if token_callback else None
        return WatchdogLLM(ChatOpenAI(
            model=os.environ.get("COPPER_MODEL", "gpt-4.1"),
            temperature=0,
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=300,
            max_retries=2,
            streaming=True,
            callbacks=[handler] if handler else None,
        )), "online"
    return DemoLLM(product_name=product_name), "demo"


def llm_mode() -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        return "demo"
    return "online+retrieval" if os.environ.get("TAVILY_API_KEY") else "online"


# run 快照的磁盘持久化目录：服务重启后已完成的成果仍可查看/导出
DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "runs"
# 活跃run中不可序列化（Event/Thread）也不需要持久化的键
_VOLATILE_KEYS = ("_review_event", "_cancel", "_thread", "_review_decision",
                  "review", "review_preview", "stream_buf", "stream_current",
                  "_subscribers")


class RunManager:
    def __init__(self):
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load_archived()
        self._prune_archived()

    # --------------------------------------------------------
    # 创建 run
    # --------------------------------------------------------
    def create_runs(self, product_ids: list[str], region: str, baseline_year: int,
                    constraints: str, custom_product_name: str = "") -> list[str]:
        run_ids = []
        for pid in product_ids:
            product = next((p for p in TARGET_PRODUCTS if p["product_id"] == pid), None)
            if product is None:
                continue
            run_id = uuid.uuid4().hex[:12]
            run = self._new_run(run_id, "research", product["product_id"],
                                product["product_name"], region, baseline_year, constraints)
            with self._lock:
                self._runs[run_id] = run
            self._persist(run)
            threading.Thread(target=self._execute, args=(run,), daemon=True).start()
            run_ids.append(run_id)
        custom_name = custom_product_name.strip()
        if custom_name:
            custom_id = "custom_" + hashlib.sha1(custom_name.encode("utf-8")).hexdigest()[:12]
            run_id = uuid.uuid4().hex[:12]
            run = self._new_run(run_id, "research", custom_id, custom_name,
                                region, baseline_year, constraints)
            with self._lock:
                self._runs[run_id] = run
            self._persist(run)
            threading.Thread(target=self._execute, args=(run,), daemon=True).start()
            run_ids.append(run_id)
        return run_ids

    def create_reduction_run(self, parent_run_id: str, analysis_run_id: str | None) -> str | None:
        parent = self._runs.get(parent_run_id)
        if parent is None or parent.get("result") is None:
            return None
        result = parent["result"]
        if "classification_dimensions" not in result:  # 必须是图A的调研成果
            return None
        if not result.get("geography") or not result.get("country_profile"):
            return None
        if (result.get("evidence_assessment") or {}).get("tier") == "insufficient":
            return None
        run_id = uuid.uuid4().hex[:12]
        run = self._new_run(run_id, "reduction", parent["product_id"], parent["product_name"],
                            parent["region"], parent["baseline_year"], parent["constraints"])
        run["parent_run_id"] = parent_run_id
        run["research_output"] = result
        run["analysis_run_id"] = analysis_run_id or f"run-{datetime.now():%Y%m%d-%H%M%S}"
        with self._lock:
            self._runs[run_id] = run
        self._persist(run)
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        return run_id

    def _new_run(self, run_id, graph_type, product_id, product_name,
                 region, baseline_year, constraints) -> dict:
        return {
            "run_id": run_id,
            "graph_type": graph_type,
            "product_id": product_id,
            "product_name": product_name,
            "region": region,
            "baseline_year": baseline_year,
            "constraints": constraints,
            "status": "running",
            "current_node": None,
            "node_counts": {},
            "events": [],
            "stream_order": [],
            "partial_result": {
                "product_id": product_id,
                "product_name": product_name,
                "classification_dimensions": [],
                "functional_subsystems": [],
                "copper_components": [],
                "objections": [],
                "validation_flags": [],
                "debate_log": [],
            },
            "review": None,          # 人工审核中断payload摘要
            "review_preview": None,  # 人工审核完整预览（ProductResearchOutput dump）
            "error": None,
            "result": None,
            "stream_buf": {},      # 节点级LLM流式输出缓冲（stream_mode="messages"）
            "stream_current": None,  # 当前正在产出token的节点
            "llm_mode": llm_mode(),
            "parent_run_id": None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": None,
            "finished_at": None,
            "_cancel": threading.Event(),
            "_review_event": threading.Event(),
            "_review_decision": None,
        }

    # --------------------------------------------------------
    # 查询 / 审核 / 取消
    # --------------------------------------------------------
    def get(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    # --------------------------------------------------------
    # 磁盘持久化：重启后已完成成果仍可查看/导出
    # --------------------------------------------------------
    def _persist(self, run: dict):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            snap = {k: v for k, v in run.items()
                    if not k.startswith("_") and k not in _VOLATILE_KEYS}
            (DATA_DIR / f"{run['run_id']}.json").write_text(
                json.dumps(snap, ensure_ascii=False, default=str))
        except Exception:
            traceback.print_exc()
        if run_outcome(run) is not None:
            self._prune_archived()

    def _prune_archived(self):
        """成功/失败终态各最多保留 MAX_ARCHIVED_PER_OUTCOME 条，
        超出时删除最旧的（内存 + 磁盘归档）；进行中的 run 不参与清理。"""
        groups: dict[str, list[dict]] = {"success": [], "failure": []}
        drop_ids: list[str] = []
        with self._lock:
            for run in self._runs.values():
                outcome = run_outcome(run)
                if outcome:
                    groups[outcome].append(run)
            for runs in groups.values():
                runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
                drop_ids.extend(r["run_id"] for r in runs[MAX_ARCHIVED_PER_OUTCOME:])
            for run_id in drop_ids:
                self._runs.pop(run_id, None)
        for run_id in drop_ids:
            try:
                (DATA_DIR / f"{run_id}.json").unlink(missing_ok=True)
            except OSError:
                traceback.print_exc()

    def _load_archived(self):
        if not DATA_DIR.exists():
            return
        for f in sorted(DATA_DIR.glob("*.json")):
            try:
                run = json.loads(f.read_text())
                # 重启时刻仍在进行中的run：图执行已不可恢复，标记为中断归档
                if run.get("status") in ("queued", "running", "cancelling",
                                          "waiting_review"):
                    run["status"] = "interrupted"
                    run["error"] = "服务重启，本次运行已中断（此前已产出的结果仍可导出）"
                    if not run.get("finished_at"):
                        run["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self._runs[run["run_id"]] = run
            except Exception:
                traceback.print_exc()

    def list_runs(self) -> list[dict]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: r["created_at"], reverse=True)
        return [self.summary(r) for r in runs]

    def clear_finished_runs(self) -> list[str]:
        """清理已结束的历史与磁盘快照，保留正在执行及其图A父任务。"""
        active_statuses = {"queued", "running", "cancelling", "waiting_review"}
        with self._lock:
            protected_parents = {
                run.get("parent_run_id") for run in self._runs.values()
                if run.get("status") in active_statuses and run.get("parent_run_id")
            }
            removed_ids = [
                run_id for run_id, run in self._runs.items()
                if run.get("status") not in active_statuses and run_id not in protected_parents
            ]
            for run_id in removed_ids:
                self._runs.pop(run_id, None)
        for run_id in removed_ids:
            try:
                (DATA_DIR / f"{run_id}.json").unlink(missing_ok=True)
            except OSError:
                traceback.print_exc()
        return removed_ids

    def delete_run_group(self, run_id: str) -> tuple[list[str] | None, str | None]:
        """删除选中案例及其关联图A/图B记录；活动案例不可删除。"""
        active_statuses = {"queued", "running", "cancelling", "waiting_review"}
        with self._lock:
            selected = self._runs.get(run_id)
            if selected is None:
                return None, "运行记录不存在或已被删除"
            root_id = selected.get("parent_run_id") or run_id
            member_ids = [
                rid for rid, run in self._runs.items()
                if rid == root_id or run.get("parent_run_id") == root_id
            ]
            if any(self._runs[rid].get("status") in active_statuses for rid in member_ids):
                return None, "进行中、停止中或待审核的案例不能删除"
            for rid in member_ids:
                self._runs.pop(rid, None)
        for rid in member_ids:
            try:
                (DATA_DIR / f"{rid}.json").unlink(missing_ok=True)
            except OSError:
                traceback.print_exc()
        return member_ids, None

    def summary(self, run: dict) -> dict:
        keys = ("run_id", "graph_type", "product_id", "product_name", "region",
                "baseline_year", "status", "current_node", "node_counts", "error",
                "llm_mode", "parent_run_id", "created_at", "finished_at")
        return {k: run.get(k) for k in keys}

    def submit_review(self, run_id: str, decision: dict) -> bool:
        run = self._runs.get(run_id)
        if run is None or run["status"] != "waiting_review":
            return False
        run["_review_decision"] = decision
        run["_review_event"].set()
        return True
    def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None:
            return False
        run["_cancel"].set()
        if run["status"] in ("queued", "running"):
            run["status"] = "cancelling"
            self._persist(run)
        return True

    # --------------------------------------------------------
    # 执行主体
    # --------------------------------------------------------
    def _execute(self, run: dict):
        run["started_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            def emit_token(node, text):
                run["_callback_stream_seen"] = True
                self._append_stream(run, node, text)

            base_llm, mode = _build_base_llm(run["product_name"], emit_token)
            run["_callback_streaming"] = mode == "online"
            from regions import resolve_country
            country = resolve_country(run["region"])
            if country is None:
                raise ValueError(f"无法识别国家：{run['region']}；请选择国家注册表中的国家")
            ctx = (f"本次调研统一口径——研究国家：{country.country_name}"
                   f"（country_id={country.country_id}，GCAM区域={country.gcam_region_id}）；"
                   f"调研基准年：{run['baseline_year']}；补充约束：{run['constraints'] or '无'}。"
                   f"国家事实必须提供evidence_country、evidence_year和statistic_caliber。")
            # 在线模式且配置了TAVILY_API_KEY时，使用检索增强层（内含统一口径
            # 注入 + 各专业agent区域化检索前置 + 端点兼容性处理）；否则仅注入口径
            _region = country
            if mode == "online" and os.environ.get("TAVILY_API_KEY"):
                llm = RetrievalAugmentedLLM(base_llm, product_name=run["product_name"],
                                             context_text=ctx, region=_region)
            else:
                llm = ContextAugmentedLLM(base_llm, ctx)

            if run["graph_type"] == "research":
                graph = research_graph.build_research_graph(llm, checkpointer=MemorySaver())
                init_state = {
                    "product_id": run["product_id"],
                    "product_name": run["product_name"],
                    "region_id": country.gcam_region_id,
                    "region_name": country.country_name,
                    "geography": {"geography_level": "country",
                                  "country_id": country.country_id,
                                  "country_name": country.country_name,
                                  "gcam_region_id": country.gcam_region_id},
                    "country_profile": None, "country_retry_count": 0,
                    "stage": "dimension_structure_debate",
                    "dimension_proposal": [], "dimension_version": 0,
                    "structure_proposal": [], "structure_version": 0,
                    "copper_components": [],
                    "objections": [], "debate_log": [],
                    "evidence_assessment": None,
                    "evidence_policy": "region_specific",
                    "evidence_gate_done": False,
                    "critic_coverage_score": None,
                    "validation_flags": [],
                    "round_in_stage": 0, "total_round": 0,
                    "research_version": 1, "final_output": None,
                }
            else:
                graph = reduction_graph.build_reduction_graph(llm, checkpointer=MemorySaver())
                init_state = {
                    "product_id": run["product_id"],
                    "region_id": (run["research_output"].get("geography") or {}).get(
                        "gcam_region_id", country.gcam_region_id),
                    "region_name": (run["research_output"].get("geography") or {}).get(
                        "country_name", country.country_name),
                    "evidence_policy": (run.get("research_output") or {}).get(
                        "evidence_assessment", {}).get("evidence_policy", "region_specific")
                        if isinstance(run.get("research_output"), dict) else "region_specific",
                    "research_output": run["research_output"],
                    "analysis_run_id": run["analysis_run_id"],
                    "baseline_year": run["baseline_year"],
                    "stage": "baseline_quantification",
                    "baseline_params": None, "model_baselines": [],
                    "baseline_calc_method": "",
                    "reduction_measures": [], "country_measure_review": None,
                    "scenario_params": [],
                    "scenarios": [], "trajectory": [],
                    "functional_unit_candidates": [], "chosen_functional_unit": "",
                    "baseline_unit_intensity": None,
                    "calculation_params": None, "calculation_violations": [],
                    "baseline_retry_used": False,
                    "amendment_requests": [], "objections": [], "debate_log": [],
                    "round_in_stage": 0, "total_round": 0, "final_output": None,
                }

            config = {"configurable": {"thread_id": run["run_id"]}}
            payload = init_state

            while True:
                # 双流模式：updates 负责节点级进度，messages 负责LLM token级增量
                for mode, chunk in graph.stream(payload, config,
                                                 stream_mode=["updates", "messages"]):
                    if run["_cancel"].is_set():
                        run["status"] = "cancelled"
                        run["finished_at"] = datetime.now().isoformat(timespec="seconds")
                        self._add_event(run, "done", "已停止", "用户手动停止了本次运行")
                        self._persist(run)
                        return
                    if mode == "messages" and not run.get("_callback_streaming"):
                        # 兼容没有callback能力的模型；在线模型由真实token回调负责，避免重复追加
                        msg, meta = chunk
                        node = str((meta or {}).get("langgraph_node") or "?")
                        text = self._llm_chunk_text(msg)
                        if text:
                            self._append_stream(run, node, text)
                        continue
                    # ---- mode == "updates" ----
                    if "__interrupt__" in chunk:
                        intr = chunk["__interrupt__"][0]
                        run["review"] = self._summarize_review(intr.value)
                        # 完整预览也暴露给前端，供人工在页面上实际审阅内容
                        run["review_preview"] = (intr.value or {}).get("preview")
                        run["status"] = "waiting_review"
                        run["current_node"] = "human_review_gate"
                        self._add_event(run, "human_review_gate", "等待人工审核",
                                        "图已暂停，请在页面上审核调研成果")
                        self._persist(run)
                        # 挂起等待网页端审核，唤醒后继续resume循环
                        # 注意：唤醒后必须先把event复位，否则打回重做后
                        # 二次进入审核关卡时wait()会立即返回、取不到新决策
                        run["_review_event"].wait()
                        run["_review_event"].clear()
                        decision = run.pop("_review_decision", None) or {"approved": True}
                        run["review"] = None
                        run["review_preview"] = None
                        run["status"] = "running"
                        self._persist(run)
                        payload = ResumeCommand(resume=decision)
                        break
                    node, update = next(iter(chunk.items()))
                    self._record_node(run, node, update)
                else:
                    # 流正常结束（无中断）：检查最终产出
                    values = graph.get_state(config).values or {}
                    if values.get("final_output"):
                        run["result"] = values["final_output"]
                        run["status"] = "completed"
                        self._add_event(run, "done", "运行完成",
                                        "最终产出已生成，可在结果面板查看/导出")
                    else:
                        run["status"] = "failed"
                        run["error"] = "图在未产出final_output的情况下结束"
                    run["finished_at"] = datetime.now().isoformat(timespec="seconds")
                    self._persist(run)
                    return

        except Exception as exc:  # 任何异常都落到run上，前端可见
            traceback.print_exc()
            run["status"] = "failed"
            run["error"] = f"{type(exc).__name__}: {exc}"
            run["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._persist(run)

    # --------------------------------------------------------
    # 节点进度记录
    # --------------------------------------------------------
    # --------------------------------------------------------
    # LLM 流式输出采集（stream_mode="messages"）
    # --------------------------------------------------------
    @staticmethod
    def _llm_chunk_text(msg) -> str:
        """从AIMessageChunk提取可展示文本增量。
        结构化输出(function calling)的实际生成内容在 tool_call_chunks 的
        args 里逐字符流出，与普通content一并展示。"""
        c = getattr(msg, "content", "")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = "".join(x.get("text", "") for x in c if isinstance(x, dict))
        else:
            text = ""
        tc = getattr(msg, "tool_call_chunks", None)
        if tc:
            text += "".join((a.get("args") or "") for a in tc)
        return text

    def _append_stream(self, run: dict, node: str, text: str):
        """全量累积token增量（不人为截断，数据完整性优先）；
        可视区域的滚动丢弃由前端overflow滚动自然完成。
        同时把增量推给所有SSE订阅者，保持模型原始输出顺序。"""
        buf = run.setdefault("stream_buf", {})
        buf[node] = (buf.get(node) or "") + text
        order = run.setdefault("stream_order", [])
        if node not in order:
            order.append(node)
        run["stream_current"] = node
        for q in list(run.get("_subscribers") or []):
            try:
                q.put_nowait((node, text))  # 慢消费者队列满时丢弃，不阻塞图执行
            except queue.Full:
                pass

    def subscribe(self, run_id: str):
        """注册SSE订阅者，返回接收(node,text)增量的队列；run不存在返回None"""
        run = self._runs.get(run_id)
        if run is None:
            return None
        q = queue.Queue(maxsize=2000)
        run.setdefault("_subscribers", []).append(q)
        return q

    def unsubscribe(self, run_id: str, q) -> None:
        run = self._runs.get(run_id)
        if run is not None:
            subs = run.get("_subscribers") or []
            if q in subs:
                subs.remove(q)

    def _record_node(self, run: dict, node: str, update: dict):
        counts = run["node_counts"]
        counts[node] = counts.get(node, 0) + 1
        run["current_node"] = node
        self._persist(run)

        if not isinstance(update, dict):
            update = {}  # 部分节点无 state 变更时 updates 流可能给 None
        summary = ""
        log = update.get("debate_log") if isinstance(update, dict) else None
        if log:
            summary = log[-1].get("summary", "")
        if update.get("total_round") is not None:
            run["total_round"] = update["total_round"]

        # 将图节点已经产出的结构化字段持续汇总，供“调研成果”在运行中实时预览。
        partial = run.setdefault("partial_result", {
            "product_id": run["product_id"], "product_name": run["product_name"],
            "classification_dimensions": [], "functional_subsystems": [],
            "copper_components": [], "objections": [], "validation_flags": [],
            "debate_log": [],
        })
        field_map = {
            "dimension_proposal": "classification_dimensions",
            "structure_proposal": "functional_subsystems",
            "copper_components": "copper_components",
            "country_profile": "country_profile",
            "geography": "geography",
            "evidence_assessment": "evidence_assessment",
            "validation_flags": "validation_flags",
            "objections": "objections",
            "debate_log": "debate_log",
            "baseline_params": "baseline_params",
            "model_baselines": "model_baselines",
            "reduction_measures": "reduction_measures",
            "country_measure_review": "country_measure_review",
            "scenario_params": "scenario_params",
            "scenarios": "scenarios",
            "trajectory": "trajectory",
            "chosen_functional_unit": "chosen_functional_unit",
            "baseline_unit_intensity": "baseline_unit_intensity",
        }
        for source, target in field_map.items():
            if source in update and update[source] is not None:
                partial[target] = update[source]

        labels = dict(PIPELINES[run["graph_type"]])
        self._add_event(run, node, labels.get(node, node), summary or "节点执行完成")

    def _add_event(self, run: dict, node: str, label: str, detail: str):
        run["events"].append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "node": node, "label": label, "detail": detail,
        })
        if len(run["events"]) > MAX_EVENTS:
            del run["events"][:len(run["events"]) - MAX_EVENTS]

    # --------------------------------------------------------
    # 人工审核payload摘要（避免把数百KB的preview直接塞给列表接口）
    # --------------------------------------------------------
    def _summarize_review(self, payload: dict) -> dict:
        preview = payload.get("preview", {})
        dims = preview.get("classification_dimensions", [])
        return {
            "action": payload.get("action"),
            "product_id": payload.get("product_id"),
            "research_version": payload.get("research_version"),
            "n_dimensions": len(dims),
            "n_subsystems": len(preview.get("functional_subsystems", [])),
            "n_components": len(preview.get("copper_components", [])),
            "n_objections": len(preview.get("objections", [])),
            "n_flags": len(preview.get("validation_flags", [])),
            "dimension_names": [d.get("dimension_name") for d in dims],
            "instructions": payload.get("instructions"),
        }

    def detail(self, run: dict) -> dict:
        """完整详情：摘要 + 事件流 + （若完成）最终产出 + （若待审核）完整预览"""
        d = self.summary(run)
        d["total_round"] = run.get("total_round")
        d["events"] = run["events"][-120:]
        d["review"] = run.get("review")
        # LLM流式输出（按节点缓存的token累积文本），供前端实时展示可见模型输出
        d["stream_buf"] = run.get("stream_buf") or {}
        d["stream_current"] = run.get("stream_current")
        d["stream_order"] = run.get("stream_order") or []
        d["partial_result"] = run.get("partial_result") or {}
        # 待审核时附完整预览内容（审核卡片内直接展示，而不是只给统计数字）
        if run["status"] == "waiting_review":
            d["review_preview"] = run.get("review_preview")
        d["result"] = run.get("result")
        return d
