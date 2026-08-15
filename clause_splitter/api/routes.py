"""FastAPI 薄壳路由 —— 上传 → 后台任务 → 轮询 → 下载.

计算全部落在 CLI 核心(``clause_splitter.split`` / ``clause_splitter.compare``),
薄壳只做 HTTP 封装 + 任务状态跟踪。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile

from clause_splitter.config import REPO_ROOT, get_settings
from clause_splitter.utils import get_logger
from .dependencies import get_llm, get_task_manager
from .tasks import TaskManager

logger = get_logger()
router = APIRouter(tags=["clause-splitter"])

#: 上传文件落盘根目录(outputs/uploads/)
_UPLOAD_ROOT = REPO_ROOT / "outputs" / "uploads"


def _save_upload(upload: UploadFile) -> Path:
    """把上传文件存到临时目录,返回路径。"""
    _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    safe = Path(upload.filename or "upload.bin").name
    path = _UPLOAD_ROOT / f"{__import__('uuid').uuid4().hex[:8]}_{safe}"
    with path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return path


def _task_or_404(manager: TaskManager, task_id: str):
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return task


# ═══════════════════════════════════════════════════════════════════════
# POST /split —— 上传文档,后台拆分
# ═══════════════════════════════════════════════════════════════════════

@router.post("/split")
async def submit_split_task(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    model: str = Form("qwen3-max"),
    provider: str = Form("openai"),
    manager: TaskManager = Depends(get_task_manager),
) -> dict:
    """上传法规文档(docx/pdf/txt/xlsx),后台执行条款拆分。"""
    llm = get_llm(model, provider)
    src = _save_upload(file)
    task = manager.create("split", document_name=file.filename or src.name)
    background_tasks.add_task(_run_split, task.task_id, src, llm, manager)
    return {"task_id": task.task_id}


async def _run_split(task_id: str, src: Path, llm, manager: TaskManager) -> None:
    from clause_splitter.split import SplitPipeline

    task = manager.get(task_id)
    if task is None:
        return
    out_dir = _UPLOAD_ROOT / task_id
    try:
        task.update(status="processing", current_step="开始拆分")

        def _progress(p: float, step: str, state: dict) -> None:
            t = manager.get(task_id)
            if t is not None:
                t.update(progress=min(p, 100.0), current_step=step,
                         total_articles=int(state.get("total_articles", 0)))

        pipeline = SplitPipeline(llm=llm)
        result = await pipeline.run(
            src, document_name=src.name, output_dir=str(out_dir), progress=_progress
        )
        task.update(
            status="completed", progress=100.0, current_step="拆分完成",
            total_articles=result.total_articles,
            total_obligations=result.total_obligations,
            result_excel_path=result.excel_path,
        )
        logger.info("任务 %s 拆分完成: %d 义务", task_id, result.total_obligations)
    except Exception as e:  # noqa: BLE001
        logger.exception("任务 %s 失败", task_id)
        task.update(status="failed", error_message=str(e))
    finally:
        shutil.rmtree(src, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
# POST /compare —— 上传外规,后台比对内规库
# ═══════════════════════════════════════════════════════════════════════

@router.post("/compare")
async def submit_compare_task(
    background_tasks: BackgroundTasks,
    external_file: UploadFile = File(...),
    internal_dir: str = Form("outputs/internal"),
    searcher: str = Form("hybrid"),
    model: str = Form("qwen3-max"),
    provider: str = Form("openai"),
    manager: TaskManager = Depends(get_task_manager),
) -> dict:
    """上传外规文件,与内规库比对,产出 compare_result.json + 差距分析报告。"""
    llm = get_llm(model, provider)
    src = _save_upload(external_file)
    task = manager.create("compare", document_name=external_file.filename or src.name)
    background_tasks.add_task(
        _run_compare, task.task_id, src, internal_dir, searcher, llm, manager
    )
    return {"task_id": task.task_id}


async def _run_compare(
    task_id: str, src: Path, internal_dir: str, searcher: str,
    llm, manager: TaskManager,
) -> None:
    from clause_splitter.compare.aggregate import run_compare
    from clause_splitter.compare.index import build_all_indexes, build_clause_store
    from clause_splitter.compare.loader import load_external_clauses
    from clause_splitter.compare.report import generate_gap_report

    task = manager.get(task_id)
    if task is None:
        return
    out_dir = _UPLOAD_ROOT / task_id
    try:
        task.update(status="processing", current_step="构建内规索引")

        store = build_clause_store(internal_dir, name="internal")
        build_all_indexes(store)

        clauses = load_external_clauses(str(src), str(out_dir), llm)
        task.update(current_step=f"比对 {len(clauses)} 条外规条款")

        result = await run_compare(
            clauses, store, llm, searcher=searcher,
            persist_dir=str(out_dir / "episodes"),
            model=model, provider=provider,
        )
        (out_dir / "compare_result.json").write_text(
            result.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
        )
        rep = generate_gap_report(result, store, llm, output_dir=str(out_dir))

        task.update(
            status="completed", progress=100.0, current_step="比对完成",
            total_obligations=result.total_external_obligations,
            result_json_path=str(out_dir / "compare_result.json"),
            result_report_path=rep["report_path"],
        )
        logger.info("任务 %s 比对完成: 覆盖 %.1f%%", task_id, result.coverage_rate * 100)
    except Exception as e:  # noqa: BLE001
        logger.exception("任务 %s 失败", task_id)
        task.update(status="failed", error_message=str(e))
    finally:
        shutil.rmtree(src, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
# 轮询 / 列表 / 清理 / 下载
# ═══════════════════════════════════════════════════════════════════════

@router.get("/task/{task_id}")
async def get_task_status(task_id: str, manager: TaskManager = Depends(get_task_manager)) -> dict:
    """查询任务状态与进度。"""
    return _task_or_404(manager, task_id).to_dict()


@router.get("/tasks")
async def list_tasks(manager: TaskManager = Depends(get_task_manager)) -> dict:
    """列出全部任务(按创建时间倒序)。"""
    return {"tasks": [t.to_dict() for t in manager.all()]}


@router.delete("/tasks")
async def clear_tasks(manager: TaskManager = Depends(get_task_manager)) -> dict:
    """清空全部任务。"""
    return {"cleared": manager.clear()}


@router.get("/result/{task_id}/excel")
async def download_excel(task_id: str, manager: TaskManager = Depends(get_task_manager)):
    """下载拆分结果 Excel。"""
    task = _task_or_404(manager, task_id)
    path = task.result_excel_path or ""
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="结果 Excel 不存在")
    return _file_response(path, "条款拆分结果.xlsx")


@router.get("/result/{task_id}/report")
async def download_report(task_id: str, manager: TaskManager = Depends(get_task_manager)):
    """下载差距分析报告(Markdown)。"""
    task = _task_or_404(manager, task_id)
    path = task.result_report_path or ""
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="差距分析报告不存在")
    return _file_response(path, "内外规合规差距分析报告.md")


def _file_response(path: str, filename: str):
    from fastapi.responses import FileResponse
    return FileResponse(path, filename=filename)
