"""拆分流水线 —— 普通顺序编排(替代旧 LangGraph graph).

文本路径 ``run()``:
    parse_document → analyze_structure → plan_batches →
    逐批 decompose_batch → validate_with_retry → export_excel + write_structure_json

Excel 多文档路径 ``run_excel()``:
    parse_excel 一次 → 按 document_name 分组 → asyncio 并行跑每个文档的
    (plan → decompose → validate → export)。

progress 回调签名: ``fn(progress: float, step: str, state: dict)``,
里程碑沿用 Settings.progress_milestones。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clause_splitter.config import REPO_ROOT, get_settings
from clause_splitter.models import ArticleInfo, ClauseObligation, ClauseSplitResult, ComplexityLabel, DocumentStructure
from clause_splitter.split import export as split_export
from clause_splitter.split.decompose import decompose_batch
from clause_splitter.split.parse import parse_document, parse_excel
from clause_splitter.split.plan import plan_batches
from clause_splitter.split.structure import analyze_structure, estimate_complexity
from clause_splitter.split.validate import validate_with_retry
from clause_splitter.utils import get_logger

logger = get_logger()

ProgressCallback = Callable[[float, str, dict[str, Any]], None]


class SplitPipeline:
    """条款拆分流水线 —— 显式阶段列表顺序编排."""

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm

    # ── 文本路径 ─────────────────────────────────────────────────────

    async def run(
        self,
        file_path: str | Path,
        document_name: str = "",
        progress: ProgressCallback | None = None,
        output_dir: str = "",
    ) -> ClauseSplitResult:
        """执行完整拆分流水线(文本文档;Excel 自动转 run_excel)。"""
        start_time = time.time()
        document_name = document_name or Path(file_path).name
        settings = get_settings()
        milestones = settings.progress_milestones
        model_used = getattr(self.llm, "model", "") or getattr(self.llm, "model_name", "")

        logger.info("开始拆分: %s", document_name)

        # ── 解析 ──────────────────────────────────────────────────────
        parsed = parse_document(file_path, document_name)
        if parsed["file_type"] == "excel":
            logger.info("Excel 文件，转多文档路径: %s", document_name)
            return await self._run_excel_wrapped(
                file_path, document_name, output_dir, progress, start_time, model_used,
            )
        if parsed["status"] == "failed":
            return ClauseSplitResult(
                document_name=document_name,
                total_articles=0, total_obligations=0, obligations=[],
                processing_notes=[parsed.get("error_message", "文档解析失败")],
                processing_time_seconds=round(time.time() - start_time, 1),
                model_used=model_used,
            )

        if progress:
            progress(parsed.get("progress", milestones["parse_complete"]), parsed.get("current_step", "文档解析完成"), parsed)

        # ── 结构分析 ─────────────────────────────────────────────────
        try:
            doc_structure = analyze_structure(parsed["raw_text"], llm=self.llm)
        except Exception as e:
            return ClauseSplitResult(
                document_name=document_name,
                total_articles=0, total_obligations=0, obligations=[],
                processing_notes=[f"结构分析失败: {e}"],
                processing_time_seconds=round(time.time() - start_time, 1),
                model_used=model_used,
            )

        if not doc_structure.articles:
            return ClauseSplitResult(
                document_name=document_name,
                total_articles=0, total_obligations=0, obligations=[],
                processing_notes=["未找到任何条款"],
                processing_time_seconds=round(time.time() - start_time, 1),
                model_used=model_used,
            )

        if progress:
            progress(milestones["structure_complete"], f"结构分析完成 —— {len(doc_structure.articles)} 条条款", {"total_articles": len(doc_structure.articles)})

        # ── 批次规划 + 逐批拆分 ──────────────────────────────────────
        batches = plan_batches([a.model_dump() for a in doc_structure.articles])
        if progress:
            progress(milestones["batches_planned"], f"批次规划完成 —— {len(batches)} 组", {})

        total_articles = len(doc_structure.articles)
        all_obligations: list[dict[str, Any]] = []
        processed = 0
        for idx, batch in enumerate(batches):
            obls = await decompose_batch(batch, document_name, llm=self.llm)
            all_obligations.extend(obls)
            processed = min(processed + len(batch), total_articles)
            if progress:
                prog = milestones["decompose_start"] + (processed / max(total_articles, 1)) * (milestones["decompose_end"] - milestones["decompose_start"])
                progress(prog, f"拆分中…… {processed}/{total_articles} 条款 (batch {idx + 1}/{len(batches)})", {"obligations": len(all_obligations)})

        # ── 验证 + self-correct ──────────────────────────────────────
        obligations, _errors = validate_with_retry(all_obligations)
        if progress:
            progress(milestones["validate_complete"], f"验证通过 —— {len(obligations)} 条有效义务记录", {"obligations": len(obligations)})

        # ── 导出 ─────────────────────────────────────────────────────
        try:
            excel_path = split_export.export_excel(obligations, document_name, output_dir, settings.template_path)
        except Exception as e:
            return ClauseSplitResult(
                document_name=document_name,
                total_articles=total_articles, total_obligations=len(obligations),
                obligations=[ClauseObligation(**o) for o in obligations],
                processing_notes=[f"Excel 导出失败: {e}"],
                processing_time_seconds=round(time.time() - start_time, 1),
                model_used=model_used,
            )
        split_export.write_structure_json(doc_structure, document_name, output_dir)
        if progress:
            progress(milestones["export_complete"], "拆分完成", {"excel_path": excel_path})

        result = ClauseSplitResult(
            document_name=document_name,
            total_articles=total_articles,
            total_obligations=len(obligations),
            obligations=[ClauseObligation(**o) for o in obligations],
            processing_notes=[],
            processing_time_seconds=round(time.time() - start_time, 1),
            model_used=model_used,
            excel_path=excel_path,
        )
        logger.info("拆分完成: %d 条义务, 耗时 %.1fs", result.total_obligations, result.processing_time_seconds)
        return result

    # ── Excel 多文档路径 ─────────────────────────────────────────────

    async def run_excel(
        self,
        file_path: str | Path,
        document_name: str = "",
        output_base_dir: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """从单个 Excel 拆分多个制度文档,并行处理,分文档输出。"""
        settings = get_settings()
        start_time = time.time()
        excel_name = document_name or Path(file_path).stem

        if progress:
            progress(0.0, f"解析 Excel: {excel_name} ……", {})

        parsed = parse_excel(file_path, document_name, llm=self.llm)
        if parsed["status"] == "failed":
            return {
                "excel_file": str(file_path), "excel_name": excel_name,
                "total_documents": 0, "completed_documents": 0,
                "total_articles": 0, "total_obligations": 0,
                "documents": [], "errors": [parsed["error_message"]],
                "total_processing_time_seconds": round(time.time() - start_time, 1),
            }

        doc_groups: dict[str, list[dict[str, Any]]] = parsed["document_groups"]
        if not doc_groups:
            return {
                "excel_file": str(file_path), "excel_name": excel_name,
                "total_documents": 0, "completed_documents": 0,
                "total_articles": 0, "total_obligations": 0,
                "documents": [], "errors": ["Excel 中未提取到任何条款"],
                "total_processing_time_seconds": round(time.time() - start_time, 1),
            }

        logger.info("[MultiDoc] 发现 %d 个制度文档, %d 条条款", len(doc_groups), parsed["total_articles"])
        if progress:
            progress(10.0, f"发现 {len(doc_groups)} 个制度文档，准备并行拆分……", {"total_articles": parsed["total_articles"]})

        base_dir = output_base_dir or str(Path(REPO_ROOT) / "outputs" / split_export._safe_filename(excel_name))
        sem = asyncio.Semaphore(settings.llm_max_workers)

        async def _process_one(doc_key: str, articles_data: list[dict], idx: int, total: int) -> dict[str, Any]:
            async with sem:
                return await self._split_doc_group(doc_key, articles_data, base_dir, idx, total, start_time)

        tasks = [
            _process_one(doc_key, doc_articles, idx, len(doc_groups))
            for idx, (doc_key, doc_articles) in enumerate(doc_groups.items())
        ]
        logger.info("[MultiDoc] 并行拆分 %d 个文档 (concurrency=%d)……", len(tasks), settings.llm_max_workers)
        if progress:
            progress(15.0, f"并行拆分 {len(tasks)} 个文档……", {})

        doc_results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_results: list[dict[str, Any]] = []
        errors: list[str] = []
        for i, result in enumerate(doc_results):
            doc_keys = list(doc_groups.keys())
            doc_key = doc_keys[i] if i < len(doc_keys) else f"doc_{i}"
            if isinstance(result, Exception):
                errors.append(f"{doc_key}: {result}")
                processed_results.append({
                    "document_name": doc_key,
                    "article_count": len(doc_groups.get(doc_key, [])),
                    "obligation_count": 0, "json_path": "", "excel_path": "",
                    "processing_time_seconds": 0, "status": "failed", "error": str(result),
                })
            else:
                processed_results.append(result)
                if result.get("error"):
                    errors.append(f"{result['document_name']}: {result['error']}")

        total_articles = sum(r.get("article_count", 0) for r in processed_results)
        total_obligations = sum(r.get("obligation_count", 0) for r in processed_results)
        completed = sum(1 for r in processed_results if r.get("status") == "completed")
        elapsed = round(time.time() - start_time, 1)

        logger.info("[MultiDoc] 全部完成: %d/%d 成功, %d 条款 → %d 义务, 总耗时 %.1fs",
                    completed, len(processed_results), total_articles, total_obligations, elapsed)

        if progress:
            progress(100.0, f"全部完成: {completed}/{len(processed_results)} 文档, {total_obligations} 义务, {elapsed}s",
                     {"total_articles": total_articles, "total_obligations": total_obligations})

        return {
            "excel_file": str(file_path),
            "excel_name": excel_name,
            "output_base_dir": base_dir,
            "total_documents": len(processed_results),
            "completed_documents": completed,
            "total_articles": total_articles,
            "total_obligations": total_obligations,
            "documents": processed_results,
            "errors": errors,
            "total_processing_time_seconds": elapsed,
        }

    # ── 内部:单文档组子流水线 / Excel 包装 ───────────────────────────

    async def _split_doc_group(
        self,
        doc_key: str,
        articles_data: list[dict[str, Any]],
        base_dir: str,
        idx: int,
        total: int,
        start_time: float,
    ) -> dict[str, Any]:
        """Excel 中一个制度文档的完整子流水线(plan → decompose → validate → export)。"""
        doc_start = time.time()
        settings = get_settings()
        try:
            articles: list[ArticleInfo] = []
            for ad in articles_data:
                text = ad.get("full_text", "")
                art_id = ad.get("article_id", "")
                section = ad.get("section", "")

                complexity = estimate_complexity(text)
                parent_parts = [p for p in [doc_key, section] if p]
                parent_section = " › ".join(parent_parts) if parent_parts else None

                articles.append(ArticleInfo(
                    article_id=art_id,
                    full_text=text,
                    parent_section=parent_section,
                    complexity_score=complexity["complexity_score"],
                    complexity_label=(
                        ComplexityLabel.COMPLEX
                        if complexity["complexity_score"] >= settings.complex_solo_threshold
                        else ComplexityLabel.SIMPLE
                    ),
                    estimated_obligation_count=max(1, complexity["metrics"]["sentence_count"]),
                ))

            doc_structure = DocumentStructure(
                document_title=doc_key,
                articles=articles,
                total_character_count=sum(len(a.full_text) for a in articles),
                structure_type="excel_structured",
            )

            doc_output_dir = str(Path(base_dir) / split_export._safe_filename(doc_key))
            batches = plan_batches([a.model_dump() for a in articles])
            all_obligations: list[dict[str, Any]] = []
            for batch in batches:
                all_obligations.extend(await decompose_batch(batch, doc_key, llm=self.llm))
            obligations, _ = validate_with_retry(all_obligations)

            excel_path = split_export.export_excel(obligations, doc_key, doc_output_dir, settings.template_path)
            split_export.write_structure_json(doc_structure, doc_key, doc_output_dir)

            split_result = ClauseSplitResult(
                document_name=doc_key,
                total_articles=len(articles),
                total_obligations=len(obligations),
                obligations=[ClauseObligation(**o) for o in obligations],
                processing_time_seconds=round(time.time() - doc_start, 1),
                excel_path=excel_path,
            )
            json_path = Path(doc_output_dir) / "条款拆分结果.json"
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(split_result.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")

            logger.info("[MultiDoc] '%s' 完成: %d 义务, %.1fs [%d/%d]",
                        doc_key, len(obligations), time.time() - doc_start, idx + 1, total)

            return {
                "document_name": doc_key,
                "article_count": len(articles),
                "obligation_count": len(obligations),
                "json_path": str(json_path),
                "excel_path": excel_path,
                "processing_time_seconds": round(time.time() - doc_start, 1),
                "status": "completed",
                "error": None,
            }
        except Exception as e:
            logger.error("[MultiDoc] 文档 '%s' 拆分子管线失败: %s", doc_key, e)
            return {
                "document_name": doc_key,
                "article_count": len(articles_data),
                "obligation_count": 0, "json_path": "", "excel_path": "",
                "processing_time_seconds": round(time.time() - doc_start, 1),
                "status": "failed", "error": str(e),
            }

    async def _run_excel_wrapped(
        self,
        file_path: str | Path,
        document_name: str,
        output_dir: str,
        progress: ProgressCallback | None,
        start_time: float,
        model_used: str,
    ) -> ClauseSplitResult:
        """run() 里检测到 Excel 时转 run_excel,并把结果包装成 ClauseSplitResult。"""
        result = await self.run_excel(
            file_path, document_name=document_name,
            output_base_dir=output_dir or None, progress=progress,
        )
        first_doc = (result.get("documents") or [{}])[0]
        return ClauseSplitResult(
            document_name=document_name,
            total_articles=result.get("total_articles", 0),
            total_obligations=result.get("total_obligations", 0),
            obligations=[],
            processing_notes=[
                f"Excel 多文档模式: {result.get('total_documents', 0)} 个文档",
                *(f"错误: {e}" for e in result.get("errors", [])),
            ],
            processing_time_seconds=round(time.time() - start_time, 1),
            model_used=model_used,
            excel_path=first_doc.get("excel_path", ""),
        )
