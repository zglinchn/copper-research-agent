"""铜产品减量化模型智能体 — Web 服务
==================================================================
FastAPI 后端：静态页面 + REST API，包装图A(调研)/图B(减量化)两个
LangGraph 工作流。

启动方式（在工作区根目录）：
    python3 -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000

在线调研模式（可选）：先设置环境变量
    export OPENAI_API_KEY=sk-...          # 必填才启用在线模式
    export OPENAI_BASE_URL=...            # 可选，兼容OpenAI协议的网关
    export COPPER_MODEL=gpt-4.1           # 可选，默认gpt-4.1
未设置时自动进入演示模式（占位数据跑通全流程，页面明确标注）。
"""

from __future__ import annotations
import json
import os
import queue
import sys

# 保证以 `python3 webapp/server.py` 或 uvicorn 任意cwd启动时都能找到模块：
# 根目录(schemas/research_graph等) 与 webapp目录(runner/export_excel等)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEBAPP = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, _WEBAPP):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_env_file():
    """启动时自动加载 webapp/.env（已设置的环境变量优先，不被覆盖）。
    使 mimo/Tavily 密钥持久化配置：无论谁、在何目录、如何启动服务，
    都无需手动 export。已有真实环境变量时以环境变量为准。"""
    env_path = os.path.join(_WEBAPP, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_env_file()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from schemas import TARGET_PRODUCTS
from runner import RunManager, PRODUCT_ICONS, PIPELINES, llm_mode
from export_excel import export_run_to_xlsx

app = FastAPI(title="铜产品减量化模型智能体", version="1.0.0")
manager = RunManager()


@app.get("/api/regions")
def list_regions():
    """GCAM 区域注册表（前端区域 datalist/下拉）"""
    try:
        from regions import region_options
        return {"regions": region_options()}
    except Exception as exc:
        raise HTTPException(500, f"区域注册表加载失败: {exc}")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ============================================================
# 元信息
# ============================================================

@app.get("/api/meta")
def get_meta():
    return {
        "products": [
            {**p, "icon": PRODUCT_ICONS.get(p["product_id"], "📦")}
            for p in TARGET_PRODUCTS
        ],
        "llm_mode": llm_mode(),
        "pipelines": {k: [{"node": n, "label": l} for n, l in v]
                      for k, v in PIPELINES.items()},
    }


# ============================================================
# 创建 / 查询运行
# ============================================================

class RunRequest(BaseModel):
    product_ids: list[str]
    region: str = "中国"
    baseline_year: int = 2025
    constraints: str = ""


@app.post("/api/runs")
def create_runs(req: RunRequest):
    if not req.product_ids:
        raise HTTPException(400, "product_ids不能为空")
    run_ids = manager.create_runs(
        product_ids=req.product_ids, region=req.region.strip() or "中国",
        baseline_year=int(req.baseline_year), constraints=req.constraints.strip(),
    )
    if not run_ids:
        raise HTTPException(400, "未创建任何运行：product_id无效")
    return {"run_ids": run_ids, "llm_mode": llm_mode()}


@app.get("/api/runs")
def list_runs():
    return {"runs": manager.list_runs()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    return manager.detail(run)


# ============================================================
# 人工审核（图A human_review_gate 的 interrupt 恢复）
# ============================================================

class ReviewRequest(BaseModel):
    approved: bool
    approved_by: str = "网页审核"
    rejection_target_agent: str = "dimension_agent"
    rejection_note: str = ""


@app.post("/api/runs/{run_id}/review")
def review_run(run_id: str, req: ReviewRequest):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    decision = {"approved": req.approved}
    if req.approved:
        decision.update({
            "approved_by": req.approved_by.strip() or "网页审核",
            "approved_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        })
    else:
        decision.update({
            "rejection_target_agent": req.rejection_target_agent,
            "rejection_target_stage": "dimension_structure_debate",
            "rejection_note": req.rejection_note.strip() or "人工审核未通过，请修订",
        })
    if not manager.submit_review(run_id, decision):
        raise HTTPException(409, f"当前状态({run['status']})不接受审核操作")
    return {"ok": True}


# ============================================================
# 图B：基于已批准调研成果启动减量化分析
# ============================================================

class ReductionRequest(BaseModel):
    analysis_run_id: str = ""


@app.post("/api/runs/{run_id}/start_reduction")
def start_reduction(run_id: str, req: ReductionRequest):
    new_id = manager.create_reduction_run(run_id, req.analysis_run_id.strip() or None)
    if new_id is None:
        raise HTTPException(409, "仅当调研run已完成且为图A成果时才能启动减量化分析")
    return {"run_id": new_id}


# ============================================================
# 停止 / 导出
# ============================================================

@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    if not manager.cancel(run_id):
        raise HTTPException(404, "运行不存在")
    return {"ok": True,
            "note": "已发出停止信号；当前LLM调用无法强制中断，将在下一个节点边界生效"}


@app.get("/api/runs/{run_id}/export.xlsx")
def export_xlsx(run_id: str):
    run = manager.get(run_id)
    if run is None or run.get("result") is None:
        raise HTTPException(409, "运行尚未完成，无可导出结果")
    content = export_run_to_xlsx(run)
    filename = f"{run['product_id']}-{run['graph_type']}-{run['run_id']}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/runs/{run_id}/result.json")
def export_json(run_id: str):
    run = manager.get(run_id)
    if run is None or run.get("result") is None:
        raise HTTPException(409, "运行尚未完成，无可导出结果")
    return JSONResponse(content=run["result"],
                        headers={"Content-Disposition":
                                 f'attachment; filename="{run["product_id"]}-{run["run_id"]}.json"'})


# LLM token增量的SSE推送：前端EventSource订阅，实现逐字打字机观感
_TERMINAL_STATUS = {"completed", "failed", "cancelled", "interrupted"}


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str):
    if manager.get(run_id) is None:
        raise HTTPException(404, "运行不存在")
    q = manager.subscribe(run_id)

    def gen():
        try:
            # 先推一次当前已有缓冲（前端直接填充，不回放打字机）
            run = manager.get(run_id)
            init = {"type": "init", "buf": (run or {}).get("stream_buf") or {}}
            yield f"data: {json.dumps(init, ensure_ascii=False)}\n\n"
            while True:
                run = manager.get(run_id)
                if run is None or (run.get("status") in _TERMINAL_STATUS and q.empty()):
                    yield "data: {\"type\":\"end\"}\n\n"
                    return
                try:
                    node, text = q.get(timeout=10)
                    payload = json.dumps({"type": "token", "node": node, "text": text},
                                         ensure_ascii=False)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"  # 心跳保活
        finally:
            manager.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ============================================================
# 静态页面
# ============================================================

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
