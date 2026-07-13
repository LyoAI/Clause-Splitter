"""FastAPI routes — /api/v1 split/compare/summary/pipeline and task management."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from models.api import (
    SplitRequest,
    SplitResponse,
    CompareRequest,
    CompareResponse,
    SummaryRequest,
    SummaryResponse,
    PipelineResponse,
    TaskStatusResponse,
    TaskListResponse,
    ErrorResponse,
)
from models.task import TaskStatusEnum
from services.parser import DocumentParseService
from services.splitter import ClauseSplitService
from services.compare_service import CompareService
from services.summary_service import SummaryService
from services.task_manager import TaskManager
from api.dependencies import get_task_manager, get_llm

router = APIRouter(tags=["clause-splitter"])

UPLOAD_DIR = Path(tempfile.gettempdir()) / "clause_splitter_uploads"


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/v1/split  — 条款拆分 (existing)
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/split",
    response_model=SplitResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "文件格式不支持或文件为空"},
        413: {"model": ErrorResponse, "description": "文件过大"},
    },
)
async def submit_split_task(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="待拆分的法律文件（PDF/DOCX/DOC/TXT）"),
    document_name: str = Form(default="", description="可选：覆盖文件名作为文档名称"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> SplitResponse:
    """提交条款拆分任务.

    上传一个法律条文文件（PDF/Word），系统将异步进行条款结构分析
    和拆解，返回 task_id 用于后续查询进度和下载结果。
    """
    from config.settings import get_settings
    settings = get_settings()

    doc_name = document_name or file.filename or "未命名文档"

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {suffix}。支持的格式: {', '.join(sorted(settings.allowed_extensions))}",
        )

    content = await file.read()
    file_size = len(content)

    if file_size == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    max_size_mb = settings.max_file_size_bytes / (1024 * 1024)
    if file_size > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大: {file_size / 1024 / 1024:.1f} MB。最大允许 {max_size_mb:.0f} MB",
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    task = task_manager.create_task(document_name=doc_name, task_type="split")
    task_dir = UPLOAD_DIR / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    file_path = task_dir / file.filename
    file_path.write_bytes(content)

    background_tasks.add_task(
        _run_split_pipeline,
        task_id=task.task_id,
        file_path=str(file_path),
        document_name=doc_name,
    )

    return SplitResponse(
        task_id=task.task_id,
        status=TaskStatusEnum.PENDING,
        message=f"任务已提交 ({doc_name})，正在排队处理……",
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/v1/compare  — 内外规条款比对
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/compare",
    response_model=CompareResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "参数无效"},
        404: {"model": ErrorResponse, "description": "split 任务不存在或未完成"},
    },
)
async def submit_compare_task(
    background_tasks: BackgroundTasks,
    split_task_ids: str = Form(..., description="已完成拆分的 split 任务 ID，多个用逗号分隔（如 aaa111,bbb222）"),
    internal_files: list[UploadFile] = File(..., description="预拆分的内部规章制度 JSON 文件（支持多个，一个文件=一份内规）"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> CompareResponse:
    """提交内外规比对任务（支持多对多）.

    加载一个或多个已完成的外规拆分结果，与一个或多个上传的内规拆分结果
    进行语义级别匹配，判断每条外规义务是否被内规覆盖。

    多对多模式：多个外规 vs 多个内规 → 合并后统一匹配。
    """
    # Parse split task IDs
    ids = [tid.strip() for tid in split_task_ids.split(",") if tid.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="split_task_ids 不能为空")

    # Validate all split tasks
    ext_doc_names: list[str] = []
    for stid in ids:
        st = task_manager.get(stid)
        if st is None:
            raise HTTPException(status_code=404, detail=f"Split 任务不存在: {stid}")
        if st.status != TaskStatusEnum.COMPLETED:
            raise HTTPException(
                status_code=400,
                detail=f"Split 任务 {stid} 尚未完成，当前状态: {st.status.value}",
            )
        ext_doc_names.append(st.document_name or stid)

    # Read all internal obligation JSON files
    all_internal_obligations: list[dict] = []
    int_doc_names: list[str] = []
    for int_file in internal_files:
        try:
            int_content = await int_file.read()
            int_obls = json.loads(int_content)
            if isinstance(int_obls, dict):
                int_obls = int_obls.get("obligations", [int_obls])
            if not isinstance(int_obls, list):
                raise ValueError(f"文件 {int_file.filename} 不是数组格式")
            # Tag each obligation with its source document name
            doc_name = int_file.filename or "内规"
            for obl in int_obls:
                if isinstance(obl, dict) and not obl.get("document_name"):
                    obl["document_name"] = doc_name
            all_internal_obligations.extend(int_obls)
            int_doc_names.append(doc_name)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"内部条款 JSON 解析失败 ({int_file.filename}): {e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not all_internal_obligations:
        raise HTTPException(status_code=400, detail="内部条款为空")

    doc_label = ", ".join(ext_doc_names) + " ↔ " + ", ".join(int_doc_names)
    task = task_manager.create_task(document_name=doc_label[:80], task_type="compare")

    # Save merged internal obligations for the background task
    task_dir = UPLOAD_DIR / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    int_path = task_dir / "internal_obligations.json"
    int_path.write_text(json.dumps(all_internal_obligations, ensure_ascii=False), encoding="utf-8")
    # Save which split task IDs were used
    (task_dir / "split_task_ids.json").write_text(json.dumps(ids), encoding="utf-8")

    background_tasks.add_task(
        _run_compare_pipeline,
        task_id=task.task_id,
        split_task_ids=ids,
        internal_obligations=all_internal_obligations,
        external_document_names=ext_doc_names,
        internal_document_names=int_doc_names,
    )

    return CompareResponse(
        task_id=task.task_id,
        status=TaskStatusEnum.PENDING,
        message=f"比对任务已提交 ({len(ids)} 外规 ↔ {len(int_doc_names)} 内规)……",
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/v1/summary  — 差距分析报告生成
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/summary",
    response_model=SummaryResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "参数无效"},
        404: {"model": ErrorResponse, "description": "compare 任务不存在或未完成"},
    },
)
async def submit_summary_task(
    background_tasks: BackgroundTasks,
    compare_task_id: str = Form(..., description="已完成比对的 compare 任务 ID"),
    external_raw_text: str = Form(default="", description="外规条款原文（Markdown格式，用于报告引用）"),
    internal_raw_text: str = Form(default="", description="内规条款原文（Markdown格式，用于报告引用）"),
    document_name: str = Form(default="", description="文档名称（用于报告标题）"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> SummaryResponse:
    """提交差距分析报告生成任务.

    加载比对结果，结合内外规原文，生成专业的 Markdown 格式差距分析报告。
    """
    # Validate compare task
    compare_task = task_manager.get(compare_task_id)
    if compare_task is None:
        raise HTTPException(status_code=404, detail=f"Compare 任务不存在: {compare_task_id}")
    if compare_task.status != TaskStatusEnum.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"Compare 任务尚未完成，当前状态: {compare_task.status.value}",
        )

    doc_name = document_name or compare_task.document_name or "差距分析"
    task = task_manager.create_task(document_name=doc_name, task_type="summary")

    background_tasks.add_task(
        _run_summary_pipeline,
        task_id=task.task_id,
        compare_task_id=compare_task_id,
        external_raw_text=external_raw_text,
        internal_raw_text=internal_raw_text,
        document_name=doc_name,
    )

    return SummaryResponse(
        task_id=task.task_id,
        status=TaskStatusEnum.PENDING,
        message=f"报告生成任务已提交 ({doc_name})……",
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/v1/pipeline  — 完整流水线（拆分 → 比对 → 报告）
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/pipeline",
    response_model=PipelineResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "参数无效或文件格式不支持"},
        413: {"model": ErrorResponse, "description": "文件过大"},
    },
)
async def submit_pipeline_task(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="待拆分的外部法律文件（PDF/DOCX/DOC/TXT），支持多个"),
    internal_files: list[UploadFile] = File(..., description="预拆分的内部规章制度 JSON 文件，支持多个"),
    internal_raw_text: str = Form(default="", description="内规条款原文（Markdown格式，用于报告引用）"),
    document_name: str = Form(default="", description="可选：文档名称（合并多个外规时使用）"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> PipelineResponse:
    """提交完整流水线任务（支持多对多）.

    一次上传即可完成：条款拆分 → 内外规比对 → 差距分析报告 全流程。
    支持多个外部法规文件 + 多个内规 JSON 的合并比对。
    """
    from config.settings import get_settings
    settings = get_settings()

    # ── Validate & collect all external files ─────────────────────────────
    if not files:
        raise HTTPException(status_code=400, detail="至少需要一个外部法规文件")

    ext_file_data: list[tuple[str, bytes]] = []  # (filename, content)
    ext_doc_names: list[str] = []

    for ext_file in files:
        suffix = Path(ext_file.filename or "").suffix.lower()
        if suffix not in settings.allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: {suffix}（{ext_file.filename}）。支持的格式: {', '.join(sorted(settings.allowed_extensions))}",
            )

        content = await ext_file.read()
        file_size = len(content)

        if file_size == 0:
            raise HTTPException(status_code=400, detail=f"文件为空: {ext_file.filename}")

        max_size_mb = settings.max_file_size_bytes / (1024 * 1024)
        if file_size > settings.max_file_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大: {ext_file.filename} ({file_size / 1024 / 1024:.1f} MB)。最大允许 {max_size_mb:.0f} MB",
            )

        ext_file_data.append((ext_file.filename or "未命名", content))
        ext_doc_names.append(Path(ext_file.filename or "未命名").stem)

    # ── Read all internal obligation JSONs ────────────────────────────────
    all_internal_obligations: list[dict] = []
    int_doc_names: list[str] = []

    for int_file in internal_files:
        try:
            int_content = await int_file.read()
            int_obls = json.loads(int_content)
            if isinstance(int_obls, dict):
                int_obls = int_obls.get("obligations", [int_obls])
            if not isinstance(int_obls, list):
                raise ValueError(f"文件 {int_file.filename} 不是数组格式")
            doc_name = int_file.filename or "内规"
            for obl in int_obls:
                if isinstance(obl, dict) and not obl.get("document_name"):
                    obl["document_name"] = doc_name
            all_internal_obligations.extend(int_obls)
            int_doc_names.append(doc_name)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"内部条款 JSON 解析失败 ({int_file.filename}): {e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not all_internal_obligations:
        raise HTTPException(status_code=400, detail="内部条款为空")

    doc_name = document_name or ", ".join(ext_doc_names)

    # Create task
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    task = task_manager.create_task(document_name=doc_name[:80], task_type="pipeline")
    task_dir = UPLOAD_DIR / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # Save external files to disk
    ext_saved_paths: list[str] = []
    for fname, fcontent in ext_file_data:
        fpath = task_dir / fname
        fpath.write_bytes(fcontent)
        ext_saved_paths.append(str(fpath))

    # Save internal obligations
    int_path = task_dir / "internal_obligations.json"
    int_path.write_text(json.dumps(all_internal_obligations, ensure_ascii=False), encoding="utf-8")

    background_tasks.add_task(
        _run_full_pipeline,
        task_id=task.task_id,
        file_paths=ext_saved_paths,
        document_name=doc_name,
        external_document_names=ext_doc_names,
        internal_obligations=all_internal_obligations,
        internal_document_names=int_doc_names,
        internal_raw_text=internal_raw_text,
    )

    return PipelineResponse(
        task_id=task.task_id,
        status=TaskStatusEnum.PENDING,
        message=f"流水线任务已提交 ({len(files)} 外规 ↔ {len(int_doc_names)} 内规)，将依次执行拆分→比对→报告生成",
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/v1/task/{task_id}  — 任务状态查询
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/task/{task_id}",
    response_model=TaskStatusResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_task_status(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
) -> TaskStatusResponse:
    """查询任务状态和进度（支持 split/compare/summary/pipeline 所有任务类型）."""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    return TaskStatusResponse(
        task_id=task.task_id,
        task_type=task.task_type,
        status=task.status,
        document_name=task.document_name,
        progress=task.progress,
        current_step=task.current_step,
        total_articles=task.total_articles,
        processed_articles=task.processed_articles,
        total_obligations=task.total_obligations,
        error_message=task.error_message,
        pipeline_phase=task.pipeline_phase,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/v1/tasks  — 列出所有任务
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/tasks",
    response_model=TaskListResponse,
)
async def list_tasks(
    task_type: str = "",
    task_manager: TaskManager = Depends(get_task_manager),
) -> TaskListResponse:
    """列出所有已记录的任务（支持按类型筛选）.

    Args:
        task_type: 可选：筛选任务类型 (split | compare | summary | pipeline)
    """
    if task_type:
        tasks = task_manager.list_by_type(task_type)
    else:
        tasks = task_manager.get_all()

    return TaskListResponse(
        total=len(tasks),
        tasks=[
            TaskStatusResponse(
                task_id=t.task_id,
                task_type=t.task_type,
                status=t.status,
                document_name=t.document_name,
                progress=t.progress,
                current_step=t.current_step,
                total_articles=t.total_articles,
                processed_articles=t.processed_articles,
                total_obligations=t.total_obligations,
                error_message=t.error_message,
                pipeline_phase=t.pipeline_phase,
            )
            for t in tasks
        ],
    )


@router.delete("/tasks")
async def clear_tasks(
    task_manager: TaskManager = Depends(get_task_manager),
) -> dict:
    """清空所有任务记录（内存 + 磁盘）."""
    count = task_manager.clear_all()
    return {"deleted": count, "message": f"已清空 {count} 个任务记录"}


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/v1/result/{task_id}/json  — 结果 JSON
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/result/{task_id}/json",
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def get_result_json(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
) -> dict:
    """获取 JSON 格式的结果（支持所有任务类型）."""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    if task.status != TaskStatusEnum.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务尚未完成，当前状态: {task.status.value}",
        )

    json_path = task.result_json_path
    if json_path and Path(json_path).exists():
        return json.loads(Path(json_path).read_text(encoding="utf-8"))

    return {
        "task_id": task_id,
        "task_type": task.task_type,
        "document_name": task.document_name,
        "total_articles": task.total_articles,
        "total_obligations": task.total_obligations,
        "status": task.status.value,
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/v1/result/{task_id}/excel  — Excel 下载
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/result/{task_id}/excel",
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def download_excel(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
):
    """下载 Excel 格式的拆分结果."""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    if task.status != TaskStatusEnum.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务尚未完成，当前状态: {task.status.value}",
        )

    excel_path = task.result_excel_path
    if not excel_path or not Path(excel_path).exists():
        raise HTTPException(
            status_code=404,
            detail="Excel 文件尚未生成或已丢失，请重新提交任务",
        )

    filename = f"{Path(task.document_name).stem}_条款拆分结果.xlsx"
    return FileResponse(
        excel_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/v1/result/{task_id}/report  — 差距分析报告下载
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/result/{task_id}/report",
    responses={404: {"model": ErrorResponse}, 400: {"model": ErrorResponse}},
)
async def download_report(
    task_id: str,
    task_manager: TaskManager = Depends(get_task_manager),
):
    """下载 Markdown 格式的差距分析报告."""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    if task.status != TaskStatusEnum.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail=f"任务尚未完成，当前状态: {task.status.value}",
        )

    report_path = task.result_report_path
    if not report_path or not Path(report_path).exists():
        # Try to return report from JSON result
        json_path = task.result_json_path
        if json_path and Path(json_path).exists():
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            report = data.get("report") or data.get("summary_report", "")
            if report:
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse(report, media_type="text/markdown; charset=utf-8")

        raise HTTPException(
            status_code=404,
            detail="报告文件尚未生成或已丢失，请重新提交任务",
        )

    filename = f"{Path(task.document_name).stem}_差距分析报告.md"
    return FileResponse(
        report_path,
        media_type="text/markdown; charset=utf-8",
        filename=filename,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Background task: split
# ═══════════════════════════════════════════════════════════════════════════

async def _run_split_pipeline(
    task_id: str,
    file_path: str,
    document_name: str,
) -> None:
    """Run the split pipeline in the background."""
    import shutil
    task_manager = get_task_manager()
    llm = get_llm()

    try:
        task_manager.update(task_id, status=TaskStatusEnum.PARSING,
                            current_step="Parsing document...")

        # Progress callback: push LangGraph node state to TaskManager in real-time
        def _on_progress(progress: float, step: str, _state: dict) -> None:
            task_manager.update(
                task_id,
                progress=progress,
                current_step=step,
                total_articles=_state.get("total_articles", 0),
                processed_articles=_state.get("processed_articles", 0),
                total_obligations=len(_state.get("obligations", [])),
            )

        service = ClauseSplitService(llm=llm)
        result = await service.split(file_path, document_name,
                                     progress_callback=_on_progress)

        task_dir = UPLOAD_DIR / task_id
        json_path = task_dir / "result.json"
        json_path.write_text(
            result.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        excel_path = ""
        if result.excel_path and Path(result.excel_path).exists():
            excel_path = str(task_dir / f"{Path(document_name).stem}_条款拆分结果.xlsx")
            shutil.copy2(result.excel_path, excel_path)

        task_manager.mark_completed(
            task_id, result.total_obligations,
            json_path=str(json_path), excel_path=excel_path,
        )

    except Exception as e:
        import traceback
        task_manager.mark_failed(task_id, f"{e}\n{traceback.format_exc()}")


# ═══════════════════════════════════════════════════════════════════════════
# Background task: compare
# ═══════════════════════════════════════════════════════════════════════════

async def _run_compare_pipeline(
    task_id: str,
    split_task_ids: list[str],
    internal_obligations: list[dict],
    external_document_names: list[str] | None = None,
    internal_document_names: list[str] | None = None,
) -> None:
    """Run clause comparison in the background (supports many-to-many).

    Loads external obligations from multiple split tasks, merges them,
    and compares against all internal obligations.
    """
    task_manager = get_task_manager()
    llm = get_llm()

    try:
        task_manager.update(task_id, status=TaskStatusEnum.DECOMPOSING,
                            current_step="Loading split results...")

        # Load external obligations from ALL split tasks
        all_external_obligations: list[dict] = []
        ext_doc_names: list[str] = list(external_document_names or [])

        for stid in split_task_ids:
            split_task = task_manager.get(stid)
            if not split_task or not split_task.result_json_path:
                raise ValueError(f"Split task {stid} result not found")

            split_data = json.loads(Path(split_task.result_json_path).read_text(encoding="utf-8"))
            obls = split_data.get("obligations", [])
            ext_doc = split_data.get("document_name", "")

            # Tag each obligation with its source document if not already set
            for obl in obls:
                if isinstance(obl, dict) and not obl.get("document_name"):
                    obl["document_name"] = ext_doc

            all_external_obligations.extend(obls)
            if ext_doc not in ext_doc_names:
                ext_doc_names.append(ext_doc)

        task_manager.update(
            task_id,
            current_step=f"Comparing {len(all_external_obligations)} external ({len(split_task_ids)} docs) "
                         f"against {len(internal_obligations)} internal ({len(internal_document_names or [])} docs)...",
        )

        service = CompareService(llm=llm)
        result = await service.compare(
            external_obligations=all_external_obligations,
            internal_obligations=internal_obligations,
            external_document_names=ext_doc_names,
            internal_document_names=internal_document_names or [],
        )

        # Save result
        task_dir = UPLOAD_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        json_path = task_dir / "result.json"
        json_path.write_text(
            result.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        task_manager.mark_completed(
            task_id,
            total_obligations=len(result.matches),
            json_path=str(json_path),
        )

    except Exception as e:
        import traceback
        task_manager.mark_failed(task_id, f"{e}\n{traceback.format_exc()}")


# ═══════════════════════════════════════════════════════════════════════════
# Background task: summary
# ═══════════════════════════════════════════════════════════════════════════

async def _run_summary_pipeline(
    task_id: str,
    compare_task_id: str,
    external_raw_text: str,
    internal_raw_text: str,
    document_name: str,
) -> None:
    """Run gap analysis report generation in the background."""
    task_manager = get_task_manager()
    llm = get_llm()

    try:
        task_manager.update(task_id, status=TaskStatusEnum.DECOMPOSING,
                            current_step="Loading comparison result...")

        # Load compare result
        compare_task = task_manager.get(compare_task_id)
        if not compare_task or not compare_task.result_json_path:
            raise ValueError("Compare task result not found")

        compare_data = json.loads(Path(compare_task.result_json_path).read_text(encoding="utf-8"))
        from models.compare import CompareResult
        compare_result = CompareResult(**compare_data)

        task_manager.update(task_id, current_step="Generating gap analysis report...")

        task_dir = UPLOAD_DIR / task_id
        service = SummaryService(llm=llm)
        result = await service.generate(
            compare_result=compare_result,
            external_raw_text=external_raw_text,
            internal_raw_text=internal_raw_text,
            output_dir=str(task_dir),
        )

        # Save result JSON
        json_path = task_dir / "result.json"
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        task_manager.mark_completed(
            task_id,
            json_path=str(json_path),
            report_path=result.get("report_path", ""),
        )

    except Exception as e:
        import traceback
        task_manager.mark_failed(task_id, f"{e}\n{traceback.format_exc()}")


# ═══════════════════════════════════════════════════════════════════════════
# Background task: pipeline (split → compare → summary)
# ═══════════════════════════════════════════════════════════════════════════

async def _run_full_pipeline(
    task_id: str,
    file_paths: list[str],
    document_name: str,
    internal_obligations: list[dict],
    external_document_names: list[str] | None = None,
    internal_document_names: list[str] | None = None,
    internal_raw_text: str = "",
) -> None:
    """Run the full pipeline: split → compare → summary (supports many-to-many).

    Splits each external file, merges all obligations, then compares
    against all internal regulations and generates a combined report.
    """
    import shutil
    task_manager = get_task_manager()
    llm = get_llm()

    task_dir = UPLOAD_DIR / task_id
    ext_doc_list = list(external_document_names or [])
    int_doc_list = list(internal_document_names or [])

    try:
        # ── Phase 1: Split all external files ───────────────────────────
        task_manager.update(task_id, status=TaskStatusEnum.PARSING,
                            current_step=f"[1/3] Splitting {len(file_paths)} external regulation(s)...",
                            pipeline_phase="splitting")

        split_service = ClauseSplitService(llm=llm)
        all_split_results: list = []
        all_ext_obligations: list[dict] = []
        total_articles = 0
        total_obligations = 0

        for idx, file_path in enumerate(file_paths):
            doc_name = ext_doc_list[idx] if idx < len(ext_doc_list) else Path(file_path).stem

            # Per-file progress callback, scaled into pipeline's 0-33% split phase
            file_start_pct = 33.0 * idx / len(file_paths)
            file_end_pct = 33.0 * (idx + 1) / len(file_paths)

            def _make_split_callback(_file_start: float, _file_end: float):
                def _cb(progress: float, step: str, _state: dict) -> None:
                    # Scale node progress [0, 100] → pipeline range [_file_start, _file_end]
                    scaled = _file_start + (progress / 100.0) * (_file_end - _file_start)
                    task_manager.update(
                        task_id,
                        progress=round(scaled, 1),
                        current_step=f"[1/3] {step}",
                        total_articles=_state.get("total_articles", 0),
                        processed_articles=_state.get("processed_articles", 0),
                        total_obligations=len(_state.get("obligations", [])),
                    )
                return _cb

            result = await split_service.split(
                file_path, doc_name,
                progress_callback=_make_split_callback(file_start_pct, file_end_pct),
            )
            all_split_results.append(result)

            obls = [o.model_dump() for o in result.obligations]
            for obl in obls:
                if not obl.get("document_name"):
                    obl["document_name"] = doc_name
            all_ext_obligations.extend(obls)

            total_articles += result.total_articles
            total_obligations += result.total_obligations

            if len(file_paths) > 1:
                task_manager.update(
                    task_id,
                    progress=round(33.0 * (idx + 1) / len(file_paths), 1),
                    current_step=f"[1/3] Split {idx + 1}/{len(file_paths)}: {doc_name} "
                                 f"({result.total_obligations} obligations)",
                )

        # Save merged split result
        primary_result = all_split_results[0] if all_split_results else None
        split_json_path = task_dir / "split_result.json"
        # Use primary result but with merged obligations
        if primary_result:
            from models.clause import ClauseObligation
            merged_obls = [ClauseObligation(**o) for o in all_ext_obligations]
            primary_result.obligations = merged_obls
            primary_result.total_articles = total_articles
            primary_result.total_obligations = total_obligations
            primary_result.document_name = document_name

        if primary_result:
            split_json_path.write_text(
                primary_result.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        task_manager.update(task_id, progress=33.0,
                            total_articles=total_articles,
                            total_obligations=total_obligations)

        # ── Phase 2: Compare ────────────────────────────────────────────
        task_manager.update(task_id, status=TaskStatusEnum.DECOMPOSING,
                            current_step=f"[2/3] Comparing {len(all_ext_obligations)} external vs "
                                         f"{len(internal_obligations)} internal obligations...",
                            pipeline_phase="comparing")

        compare_service = CompareService(llm=llm)
        compare_result = await compare_service.compare(
            external_obligations=all_ext_obligations,
            internal_obligations=internal_obligations,
            external_document_names=ext_doc_list,
            internal_document_names=int_doc_list,
        )

        compare_json_path = task_dir / "compare_result.json"
        compare_json_path.write_text(
            compare_result.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        task_manager.update(task_id, progress=66.0)

        # ── Phase 3: Summary ────────────────────────────────────────────
        task_manager.update(task_id, current_step="[3/3] Generating gap analysis report...",
                            pipeline_phase="summarizing")

        # Get external raw text for report
        ext_raw = ""
        for fpath in file_paths:
            try:
                ext_raw += f"## {Path(fpath).stem}\n\n" + Path(fpath).read_text(encoding="utf-8")[:3000] + "\n\n"
            except Exception:
                pass

        summary_service = SummaryService(llm=llm)
        summary_result = await summary_service.generate(
            compare_result=compare_result,
            external_raw_text=ext_raw,
            internal_raw_text=internal_raw_text,
            output_dir=str(task_dir),
        )

        # ── Save combined pipeline result ───────────────────────────────
        from models.compare import PipelineResult
        pipeline = PipelineResult(
            document_name=document_name,
            split_result=primary_result,
            compare_result=compare_result,
            summary_report=summary_result["report"],
            summary_report_path=summary_result.get("report_path", ""),
        )

        final_json_path = task_dir / "result.json"
        final_json_path.write_text(
            pipeline.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Copy Excel if available
        excel_path = ""
        if primary_result and primary_result.excel_path and Path(primary_result.excel_path).exists():
            excel_path = str(task_dir / f"{Path(document_name).stem}_条款拆分结果.xlsx")
            shutil.copy2(primary_result.excel_path, excel_path)

        task_manager.mark_completed(
            task_id,
            total_obligations=total_obligations,
            json_path=str(final_json_path),
            excel_path=excel_path,
            report_path=summary_result.get("report_path", ""),
        )

    except Exception as e:
        import traceback
        task_manager.mark_failed(task_id, f"{e}\n{traceback.format_exc()}")
