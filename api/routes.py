"""FastAPI routes — /api/v1 split/summary_comparison/pipeline and task management."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from models.api import (
    SplitRequest,
    SplitResponse,
    SummaryComparisonResponse,
    PipelineResponse,
    TaskStatusResponse,
    TaskListResponse,
    ErrorResponse,
)
from models.task import TaskStatusEnum
from services.parser import DocumentParseService
from services.splitter import ClauseSplitService
from services.summary_service import SummaryService
from services.task_manager import TaskManager
from api.dependencies import get_task_manager, get_llm

router = APIRouter(tags=["clause-splitter"])

UPLOAD_DIR = Path(tempfile.gettempdir()) / "clause_splitter_uploads"


# ═══════════════════════════════════════════════════════════════════════════
# POST /api/v1/split  — 条款拆分
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
    file: UploadFile = File(..., description="待拆分的法律文件（PDF/DOCX/DOC/TXT/XLSX/XLS）"),
    document_name: str = Form(default="", description="可选：覆盖文件名作为文档名称"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> SplitResponse:
    """提交条款拆分任务."""
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
# POST /api/v1/summary_comparison  — 比对 + 报告 一体化
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/summary_comparison",
    response_model=SummaryComparisonResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "参数无效"},
    },
)
async def submit_summary_comparison(
    background_tasks: BackgroundTasks,
    external_files: list[UploadFile] = File(..., description="外规拆分 JSON 文件（支持多个）"),
    internal_files: list[UploadFile] = File(..., description="内规拆分 JSON 文件（支持多个）"),
    external_raw_text: str = Form(default="", description="外规原文（Markdown，用于报告引用）"),
    internal_raw_text: str = Form(default="", description="内规原文（Markdown，用于报告引用）"),
    document_name: str = Form(default="", description="文档名称（用于报告标题）"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> SummaryComparisonResponse:
    """提交比对+报告生成一体化任务.

    一次调用完成：外规 vs 内规条款比对 → 差距分析报告 Markdown。
    """
    # ── Load external obligations ───────────────────────────────────────
    all_external: list[dict] = []
    ext_doc_names: list[str] = []
    for ext_file in external_files:
        try:
            content = await ext_file.read()
            obls = json.loads(content)
            if isinstance(obls, dict):
                obls = obls.get("obligations", [obls])
            if not isinstance(obls, list):
                raise ValueError(f"文件 {ext_file.filename} 不是数组格式")
            doc_name = ext_file.filename or "外规"
            for obl in obls:
                if isinstance(obl, dict) and not obl.get("document_name"):
                    obl["document_name"] = doc_name
            all_external.extend(obls)
            ext_doc_names.append(doc_name)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"外规 JSON 解析失败 ({ext_file.filename}): {e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Load internal obligations ───────────────────────────────────────
    all_internal: list[dict] = []
    int_doc_names: list[str] = []
    for int_file in internal_files:
        try:
            content = await int_file.read()
            obls = json.loads(content)
            if isinstance(obls, dict):
                obls = obls.get("obligations", [obls])
            if not isinstance(obls, list):
                raise ValueError(f"文件 {int_file.filename} 不是数组格式")
            doc_name = int_file.filename or "内规"
            for obl in obls:
                if isinstance(obl, dict) and not obl.get("document_name"):
                    obl["document_name"] = doc_name
            all_internal.extend(obls)
            int_doc_names.append(doc_name)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"内规 JSON 解析失败 ({int_file.filename}): {e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not all_external or not all_internal:
        raise HTTPException(status_code=400, detail="外规或内规条款为空")

    doc_label = document_name or (", ".join(ext_doc_names) + " vs " + ", ".join(int_doc_names))
    task = task_manager.create_task(document_name=doc_label[:80], task_type="summary_comparison")

    task_dir = UPLOAD_DIR / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "external_obligations.json").write_text(
        json.dumps(all_external, ensure_ascii=False), encoding="utf-8")
    (task_dir / "internal_obligations.json").write_text(
        json.dumps(all_internal, ensure_ascii=False), encoding="utf-8")

    background_tasks.add_task(
        _run_summary_comparison,
        task_id=task.task_id,
        external_obligations=all_external,
        internal_obligations=all_internal,
        external_document_names=ext_doc_names,
        internal_document_names=int_doc_names,
        external_raw_text=external_raw_text,
        internal_raw_text=internal_raw_text,
        document_name=doc_label,
    )

    return SummaryComparisonResponse(
        task_id=task.task_id,
        status=TaskStatusEnum.PENDING,
        message=f"比对+报告任务已提交 ({len(ext_doc_names)} 外规 ↔ {len(int_doc_names)} 内规)……",
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
    files: list[UploadFile] = File(..., description="待拆分的外部法律文件（PDF/DOCX/DOC/TXT/XLSX/XLS），支持多个"),
    internal_files: list[UploadFile] = File(..., description="预拆分的内部规章制度 JSON 文件，支持多个"),
    internal_raw_text: str = Form(default="", description="内规条款原文（Markdown格式，用于报告引用）"),
    document_name: str = Form(default="", description="可选：文档名称（合并多个外规时使用）"),
    task_manager: TaskManager = Depends(get_task_manager),
) -> PipelineResponse:
    """提交完整流水线任务（支持多对多）."""
    from config.settings import get_settings
    settings = get_settings()

    if not files:
        raise HTTPException(status_code=400, detail="至少需要一个外部法规文件")

    ext_file_data: list[tuple[str, bytes]] = []
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

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    task = task_manager.create_task(document_name=doc_name[:80], task_type="pipeline")
    task_dir = UPLOAD_DIR / task.task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    ext_saved_paths: list[str] = []
    for fname, fcontent in ext_file_data:
        fpath = task_dir / fname
        fpath.write_bytes(fcontent)
        ext_saved_paths.append(str(fpath))

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
    """查询任务状态和进度."""
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

@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    task_type: str = "",
    task_manager: TaskManager = Depends(get_task_manager),
) -> TaskListResponse:
    """列出所有已记录的任务（支持按类型筛选）."""
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
    """获取 JSON 格式的结果."""
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
    """Run the split pipeline in the background.

    For Excel files (.xlsx/.xls): uses the multi-document pipeline which
    auto-detects column structure, groups by document name, and runs
    per-document parallel splitting with per-document output directories.

    For PDF/DOCX/DOC/TXT: uses the standard text-based pipeline.
    """
    import json
    import shutil
    from pathlib import Path

    task_manager = get_task_manager()
    llm = get_llm()
    suffix = Path(file_path).suffix.lower()

    try:
        # ── Excel path: multi-document pipeline ──────────────────────
        if suffix in (".xlsx", ".xls"):
            task_manager.update(task_id, status=TaskStatusEnum.PARSING,
                                current_step="检测到 Excel 文件，启动多文档解析……")

            async def _on_excel_progress(progress: float, step: str, _state: dict) -> None:
                task_manager.update(
                    task_id,
                    progress=progress,
                    current_step=step,
                    total_articles=_state.get("total_articles", 0),
                    total_obligations=_state.get("total_obligations", 0),
                )

            service = ClauseSplitService(llm=llm)
            result = await service.split_multi_document_excel(
                file_path, document_name,
                progress_callback=_on_excel_progress,
            )

            # Save aggregated result JSON
            task_dir = UPLOAD_DIR / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            json_path = task_dir / "result.json"
            json_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Collect first available Excel path
            excel_path = ""
            for doc in result.get("documents", []):
                if doc.get("excel_path") and Path(doc["excel_path"]).exists():
                    excel_path = doc["excel_path"]
                    break

            task_manager.mark_completed(
                task_id,
                total_obligations=result.get("total_obligations", 0),
                total_articles=result.get("total_articles", 0),
                json_path=str(json_path),
                excel_path=excel_path,
            )

        else:
            # ── Text-based path: standard pipeline ───────────────────
            task_manager.update(task_id, status=TaskStatusEnum.PARSING,
                                current_step="Parsing document...")

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
# Background task: summary_comparison (compare + summary in one call)
# ═══════════════════════════════════════════════════════════════════════════

async def _run_summary_comparison(
    task_id: str,
    external_obligations: list[dict],
    internal_obligations: list[dict],
    external_document_names: list[str] | None = None,
    internal_document_names: list[str] | None = None,
    external_raw_text: str = "",
    internal_raw_text: str = "",
    document_name: str = "",
) -> None:
    """Run compare + summary in one background task."""
    task_manager = get_task_manager()
    llm = get_llm()

    task_dir = UPLOAD_DIR / task_id
    ext_doc_list = list(external_document_names or [])
    int_doc_list = list(internal_document_names or [])

    try:
        # ── Phase 1: Compare (BM25 + ReAct Agent) ──────────────────────
        task_manager.update(
            task_id,
            status=TaskStatusEnum.DECOMPOSING,
            current_step=f"[1/2] Comparing {len(external_obligations)} external ({len(ext_doc_list)} docs) "
                         f"against {len(internal_obligations)} internal ({len(int_doc_list)} docs)...",
            pipeline_phase="comparing",
        )

        # Use new BM25 + ReAct ComparePipeline for scalable many-to-many
        compare_pipeline = ComparePipeline(llm=llm)
        compare_result = await compare_pipeline.run(
            external_obligations=external_obligations,
            internal_obligations=internal_obligations,
            external_document_names=ext_doc_list,
            internal_document_names=int_doc_list,
            output_dir=str(task_dir),
        )

        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "compare_result.json").write_text(
            compare_result.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        task_manager.update(task_id, progress=50.0,
                            total_obligations=compare_result.total_external_obligations)

        # ── Phase 2: Summary ────────────────────────────────────────────
        task_manager.update(
            task_id,
            current_step="[2/2] Generating gap analysis report...",
            pipeline_phase="summarizing",
        )

        summary_service = SummaryService(llm=llm)
        summary_result = await summary_service.generate(
            compare_result=compare_result,
            external_raw_text=external_raw_text,
            internal_raw_text=internal_raw_text,
            output_dir=str(task_dir),
        )

        # ── Save combined result ───────────────────────────────────────
        combined = {
            "document_name": document_name,
            "external_documents": ext_doc_list,
            "internal_documents": int_doc_list,
            "compare_result": compare_result.model_dump(),
            "report": summary_result["report"],
            "report_path": summary_result.get("report_path", ""),
        }

        json_path = task_dir / "result.json"
        json_path.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        task_manager.mark_completed(
            task_id,
            total_obligations=compare_result.total_external_obligations,
            json_path=str(json_path),
            report_path=summary_result.get("report_path", ""),
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
    """Run the full pipeline: split → compare → summary (supports many-to-many)."""
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

            file_start_pct = 33.0 * idx / len(file_paths)
            file_end_pct = 33.0 * (idx + 1) / len(file_paths)

            def _make_split_callback(_file_start: float, _file_end: float):
                def _cb(progress: float, step: str, _state: dict) -> None:
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

        primary_result = all_split_results[0] if all_split_results else None
        split_json_path = task_dir / "split_result.json"
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

        compare_pipeline = ComparePipeline(llm=llm)
        compare_result = await compare_pipeline.run(
            external_obligations=all_ext_obligations,
            internal_obligations=internal_obligations,
            external_document_names=ext_doc_list,
            internal_document_names=int_doc_list,
            output_dir=str(task_dir),
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
