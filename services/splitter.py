"""条款拆分服务 —— 编排 Agent 流水线.

ClauseSplitService 是外部调用的主入口：
1. 接收文件路径和配置
2. 编排 Agent graph 执行
3. 返回 ClauseSplitResult
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.state import create_initial_state
from agent.graph import build_split_graph
from models.clause import ClauseSplitResult, ClauseObligation
from utils import get_logger

logger = get_logger()


class ClauseSplitService:
    """条款拆分服务 —— 编排整个拆分流水线."""

    def __init__(self, llm: Any = None) -> None:
        """初始化服务.

        Args:
            llm: LLM 实例（ChatOpenAI / PingAnLLM 等）。如果为 None，使用默认配置。
        """
        self.llm = llm

    async def split(
        self,
        file_path: str,
        document_name: str,
        progress_callback: Callable[[float, str, dict[str, Any]], None] | None = None,
        output_dir: str = "",
    ) -> ClauseSplitResult:
        """执行完整的条款拆分流水线.

        Args:
            file_path: 上传文件的路径。
            document_name: 原始文件名。
            progress_callback: 可选的进度回调 fn(progress: float, step: str, state: dict)。
                               每次 LangGraph 节点完成后调用。
            output_dir: 导出目录。空字符串表示使用默认 outputs/{doc_name}/。

        Returns:
            ClauseSplitResult 包含所有提取的义务记录。
        """
        start_time = time.time()

        logger.info("开始拆分: %s", document_name)

        # 构建并运行 graph
        graph = build_split_graph()

        initial_state = create_initial_state(
            file_path=file_path,
            document_name=document_name,
            llm=self.llm,
            output_dir=output_dir or None,
        )

        config = {"configurable": {"thread_id": f"split-{document_name}"}}
        try:
            if progress_callback is not None:
                # Streaming mode: report progress after each node step
                final_state = initial_state.copy()
                async for chunk in graph.astream(initial_state, config=config):
                    # chunk is {node_name: partial_state_dict}
                    for _node_name, node_output in chunk.items():
                        if isinstance(node_output, dict):
                            final_state.update(node_output)
                            progress = float(node_output.get("progress", 0))
                            step = str(node_output.get("current_step", ""))
                            progress_callback(progress, step, final_state)
            else:
                final_state = await graph.ainvoke(initial_state, config=config)
        except Exception as e:
            logger.error("流水线执行失败: %s", e)
            return ClauseSplitResult(
                document_name=document_name,
                total_articles=0,
                total_obligations=0,
                obligations=[],
                processing_notes=[f"流水线执行失败: {e}"],
                processing_time_seconds=time.time() - start_time,
            )

        # 构建结果
        obligations_data = final_state.get("obligations", [])
        obligations: list[ClauseObligation] = []
        validation_errors: list[str] = []

        for obl_dict in obligations_data:
            try:
                obligations.append(ClauseObligation(**obl_dict))
            except Exception as e:
                validation_errors.append(str(e))

        processing_notes: list[str] = []
        if validation_errors:
            processing_notes.append(
                f"{len(validation_errors)} 条记录 Pydantic 验证失败"
            )
        error_msg = final_state.get("error_message")
        if error_msg:
            processing_notes.append(f"错误: {error_msg}")

        excel_path = final_state.get("excel_path", "")

        result = ClauseSplitResult(
            document_name=document_name,
            total_articles=final_state.get("total_articles", 0),
            total_obligations=len(obligations),
            obligations=obligations,
            processing_notes=processing_notes,
            processing_time_seconds=round(time.time() - start_time, 1),
            model_used=getattr(self.llm, "model_name", "") if self.llm else "",
            excel_path=final_state.get("excel_path", ""),
        )

        logger.info(
            "拆分完成: %d 条义务, 耗时 %.1fs",
            result.total_obligations,
            result.processing_time_seconds,
        )

        return result

    async def split_multi_document_excel(
        self,
        file_path: str,
        document_name: str = "",
        output_base_dir: str | None = None,
        progress_callback: Callable[[float, str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """从单个 Excel 文件中拆分多个制度文档，并行处理，分文档输出。

        流水线：
        1. 一次性解析 Excel（LLM schema 检测 + 列映射 + 条款提取）
        2. 按 document_name 分组
        3. 并行运行每个文档的拆分子管线（plan_batches → decompose → export）
        4. 每个文档的结果保存到独立的子目录

        Args:
            file_path: Excel 文件路径（.xlsx/.xls）。
            document_name: Excel 文件的显示名称（用于输出根目录命名）。
            output_base_dir: 输出根目录。如果为 None，默认 outputs/{excel_name}/。
            progress_callback: 可选的进度回调。

        Returns:
            {
                "excel_file": str,
                "total_documents": int,
                "total_articles": int,
                "total_obligations": int,
                "documents": [
                    {
                        "document_name": "制度A",
                        "article_count": 10,
                        "obligation_count": 35,
                        "json_path": "outputs/.../制度A/条款拆分结果.json",
                        "excel_path": "outputs/.../制度A/条款拆分结果.xlsx",
                        "processing_time_seconds": 12.3,
                        "status": "completed",
                    },
                    ...
                ],
                "errors": [],
                "total_processing_time_seconds": float,
            }
        """
        import asyncio
        import json
        import time
        from pathlib import Path

        from config.settings import get_settings
        from agent.state import create_initial_state
        from agent.graph import build_split_subgraph
        from agent.tools.excel_parse_tools import (
            read_excel_preview,
            extract_excel_articles,
            _infer_article_id_from_text,
        )
        from agent.tools.utility_tools import estimate_complexity
        from prompts.split.excel_parse import (
            SYSTEM_PROMPT,
            format_user_prompt,
            format_retry_feedback,
            format_samples_for_llm,
        )
        from agent.nodes.parse_excel import (
            _parse_llm_excel_output,
            _heuristic_column_detection,
        )
        from models.clause import (
            DocumentStructure,
            ArticleInfo,
            ComplexityLabel,
            ClauseObligation,
        )

        settings = get_settings()
        start_time = time.time()
        excel_name = document_name or Path(file_path).stem

        # ═══════════════════════════════════════════════════════════════
        # Phase 1: Parse Excel ONCE (shared across all documents)
        # ═══════════════════════════════════════════════════════════════
        logger.info("[MultiDoc] Phase 1: 解析 Excel %s", excel_name)
        if progress_callback:
            progress_callback(0.0, f"解析 Excel: {excel_name} ……", {})

        preview = read_excel_preview(
            file_path, max_rows=settings.excel_max_sample_rows,
        )
        if preview.get("error"):
            return {
                "excel_file": file_path,
                "total_documents": 0,
                "total_articles": 0,
                "total_obligations": 0,
                "documents": [],
                "errors": [f"Excel 预览失败: {preview['error']}"],
                "total_processing_time_seconds": time.time() - start_time,
            }

        # ── LLM schema detection ─────────────────────────────────────
        column_mapping = None
        if self.llm is not None:
            sample_text = format_samples_for_llm(preview)
            user_prompt = format_user_prompt(
                sample_rows_text=sample_text,
                sheet_name=preview.get("active_sheet", ""),
                total_rows=preview.get("total_rows", 0),
                file_name=Path(file_path).name,
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            for iteration in range(settings.excel_max_react_iterations):
                try:
                    response = self.llm.invoke(messages)
                    content = response.content if hasattr(response, "content") else str(response)
                    parsed = _parse_llm_excel_output(content)
                    if parsed and "column_mapping" in parsed and parsed["column_mapping"].get("article_text"):
                        column_mapping = parsed["column_mapping"]
                        logger.info(
                            "[MultiDoc] Schema 检测成功: %s, multi_doc=%s",
                            parsed.get("format_type", "?"),
                            parsed.get("is_multi_document", False),
                        )
                        break
                    messages.append({"role": "assistant", "content": content[:1000]})
                    messages.append({
                        "role": "user",
                        "content": format_retry_feedback("输出解析失败"),
                    })
                except Exception as e:
                    logger.warning("[MultiDoc] Schema 检测 iter %d 异常: %s", iteration + 1, e)

        # ── Heuristic fallback ───────────────────────────────────────
        if column_mapping is None:
            column_mapping = _heuristic_column_detection(preview)

        if column_mapping is None or not column_mapping.get("article_text"):
            return {
                "excel_file": file_path,
                "total_documents": 0,
                "total_articles": 0,
                "total_obligations": 0,
                "documents": [],
                "errors": ["无法识别 Excel 的列结构，请检查表头"],
                "total_processing_time_seconds": time.time() - start_time,
            }

        # ── Extract all articles ─────────────────────────────────────
        extraction = extract_excel_articles(
            file_path=file_path,
            column_mapping=column_mapping,
            sheet_name=preview.get("active_sheet"),
            max_rows=settings.max_excel_rows_per_document,
        )
        if extraction.get("error"):
            return {
                "excel_file": file_path,
                "total_documents": 0,
                "total_articles": 0,
                "total_obligations": 0,
                "documents": [],
                "errors": [f"条款提取失败: {extraction['error']}"],
                "total_processing_time_seconds": time.time() - start_time,
            }

        doc_groups = extraction.get("document_groups", {})
        all_articles_data = extraction.get("articles", [])

        if not doc_groups:
            return {
                "excel_file": file_path,
                "total_documents": 0,
                "total_articles": 0,
                "total_obligations": 0,
                "documents": [],
                "errors": ["Excel 中未提取到任何条款"],
                "total_processing_time_seconds": time.time() - start_time,
            }

        logger.info(
            "[MultiDoc] Phase 1 完成: %d 个文档, %d 条条款, schema=%.1fs",
            len(doc_groups), len(all_articles_data), time.time() - start_time,
        )

        if progress_callback:
            progress_callback(
                10.0,
                f"发现 {len(doc_groups)} 个制度文档，准备并行拆分……",
                {"total_articles": len(all_articles_data)},
            )

        # ═══════════════════════════════════════════════════════════════
        # Phase 2: Parallel per-document split
        # ═══════════════════════════════════════════════════════════════
        base_dir = output_base_dir or str(
            Path(settings.project_root) / "outputs" / excel_name
        )
        subgraph = build_split_subgraph()

        async def _process_one_document(
            doc_key: str,
            articles_data: list[dict],
            doc_index: int,
            total_docs: int,
        ) -> dict[str, Any]:
            """Run the sub-pipeline for one document group."""
            doc_start = time.time()

            # ── Build ArticleInfo objects ────────────────────────────
            articles: list[ArticleInfo] = []
            for ad in articles_data:
                text = ad.get("full_text", "")
                art_id = ad.get("article_id", "")
                section = ad.get("section", "")

                if not art_id:
                    art_id = _infer_article_id_from_text(text)

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

            # ── Build DocumentStructure ──────────────────────────────
            raw_text_parts = [f"{a.article_id} {a.full_text}" for a in articles]
            synthetic_raw_text = f"## {doc_key}\n\n" + "\n\n".join(raw_text_parts)

            doc_structure = DocumentStructure(
                document_title=doc_key,
                articles=articles,
                total_character_count=sum(len(a.full_text) for a in articles),
                structure_type="excel_structured",
            )

            # ── Output directory ─────────────────────────────────────
            safe_doc_name = _safe_filename(doc_key)
            doc_output_dir = str(Path(base_dir) / safe_doc_name)

            # ── Build initial state for sub-pipeline ─────────────────
            initial_state = create_initial_state(
                file_path=file_path,
                document_name=doc_key,
                llm=self.llm,
                raw_text=synthetic_raw_text,
                document_structure=doc_structure.model_dump(),
                total_articles=len(articles),
                output_dir=doc_output_dir,
            )

            # ── Run sub-pipeline ─────────────────────────────────────
            config = {"configurable": {"thread_id": f"sub-{doc_key}"}}
            try:
                final_state = await subgraph.ainvoke(initial_state, config=config)
            except Exception as e:
                logger.error("[MultiDoc] 文档 '%s' 拆分子管线失败: %s", doc_key, e)
                return {
                    "document_name": doc_key,
                    "article_count": len(articles),
                    "obligation_count": 0,
                    "json_path": "",
                    "excel_path": "",
                    "processing_time_seconds": time.time() - doc_start,
                    "status": "failed",
                    "error": str(e),
                }

            # ── Build ClauseObligation list from final state ─────────
            obligations_data = final_state.get("obligations", [])
            obligations: list[ClauseObligation] = []
            for obl_dict in obligations_data:
                try:
                    obligations.append(ClauseObligation(**obl_dict))
                except Exception:
                    pass

            # ── Save JSON result ─────────────────────────────────────
            from models.clause import ClauseSplitResult

            split_result = ClauseSplitResult(
                document_name=doc_key,
                total_articles=len(articles),
                total_obligations=len(obligations),
                obligations=obligations,
                processing_time_seconds=round(time.time() - doc_start, 1),
                excel_path=final_state.get("excel_path", ""),
            )

            json_path = Path(doc_output_dir) / "条款拆分结果.json"
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                split_result.model_dump_json(indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            logger.info(
                "[MultiDoc] '%s' 完成: %d 义务, %.1fs [%d/%d]",
                doc_key, len(obligations), time.time() - doc_start,
                doc_index + 1, total_docs,
            )

            return {
                "document_name": doc_key,
                "article_count": len(articles),
                "obligation_count": len(obligations),
                "json_path": str(json_path),
                "excel_path": final_state.get("excel_path", ""),
                "processing_time_seconds": round(time.time() - doc_start, 1),
                "status": "completed",
                "error": None,
            }

        # ── Run all document groups in parallel ───────────────────────
        tasks = []
        for idx, (doc_key, doc_articles) in enumerate(doc_groups.items()):
            tasks.append(_process_one_document(
                doc_key=doc_key,
                articles_data=doc_articles,
                doc_index=idx,
                total_docs=len(doc_groups),
            ))

        logger.info(
            "[MultiDoc] Phase 2: 并行拆分 %d 个文档 (%d workers)……",
            len(tasks), min(len(tasks), settings.llm_max_workers),
        )
        if progress_callback:
            progress_callback(15.0, f"并行拆分 {len(tasks)} 个文档……", {})

        doc_results = await asyncio.gather(*tasks, return_exceptions=True)

        # ── Handle exceptions from gather ────────────────────────────
        processed_results: list[dict[str, Any]] = []
        errors: list[str] = []
        for i, result in enumerate(doc_results):
            if isinstance(result, Exception):
                doc_keys = list(doc_groups.keys())
                doc_key = doc_keys[i] if i < len(doc_keys) else f"doc_{i}"
                errors.append(f"{doc_key}: {result}")
                processed_results.append({
                    "document_name": doc_key,
                    "article_count": len(list(doc_groups.values())[i]) if i < len(doc_groups) else 0,
                    "obligation_count": 0,
                    "json_path": "",
                    "excel_path": "",
                    "processing_time_seconds": 0,
                    "status": "failed",
                    "error": str(result),
                })
            elif isinstance(result, dict):
                processed_results.append(result)
                if result.get("error"):
                    errors.append(f"{result['document_name']}: {result['error']}")

        # ═══════════════════════════════════════════════════════════════
        # Aggregate results
        # ═══════════════════════════════════════════════════════════════
        total_articles = sum(r.get("article_count", 0) for r in processed_results)
        total_obligations = sum(r.get("obligation_count", 0) for r in processed_results)
        completed = sum(1 for r in processed_results if r.get("status") == "completed")

        elapsed = round(time.time() - start_time, 1)
        logger.info(
            "[MultiDoc] 全部完成: %d/%d 成功, %d 条款 → %d 义务, 总耗时 %.1fs, "
            "并行 %d 文档",
            completed, len(processed_results),
            total_articles, total_obligations, elapsed,
            len(processed_results),
        )

        if progress_callback:
            progress_callback(
                100.0,
                f"全部完成: {completed}/{len(processed_results)} 文档, {total_obligations} 义务, {elapsed}s",
                {"total_articles": total_articles, "total_obligations": total_obligations},
            )

        return {
            "excel_file": file_path,
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


def _safe_filename(name: str) -> str:
    """Convert a document name to a safe filesystem directory name.

    Replaces characters that are problematic in file paths.
    """
    import re
    # Strip leading/trailing whitespace first
    safe = name.strip()
    # Replace path separators and special chars
    safe = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", safe)
    # Collapse multiple underscores/spaces
    safe = re.sub(r"[_\s]+", "_", safe)
    # Strip leading/trailing dots, spaces, underscores (Windows issue)
    safe = safe.strip("._ ")
    return safe or "未命名文档"
