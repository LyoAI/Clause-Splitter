#!/usr/bin/env python
"""入口① —— 条款拆分 CLI(独立功能).

用法:
    python run_split.py <file> [--model qwen3-max] [--provider openai] \\
           [--output-dir outputs/xxx/] [--no-llm]

- 文本文档(docx/pdf/doc/txt):单文档流水线,输出 ``{stem}_条款拆分结果.xlsx``
  及 ``{stem}_structure.json``。
- Excel(xlsx/xls):多文档并行,每个制度一个子目录。
- ``--no-llm``:不调 LLM(仅验证管线;拆分必须依赖 LLM,将得到空义务表)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 保证从任意 cwd 运行都能导入 clause_splitter
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _build_llm(args: argparse.Namespace):
    if args.no_llm:
        return None
    from clause_splitter.llm import get_llm
    return get_llm(args.model, args.provider)


async def _run(args: argparse.Namespace) -> None:
    from clause_splitter.split import SplitPipeline

    llm = _build_llm(args)
    file_path = Path(args.file)
    document_name = file_path.name

    def progress(p: float, step: str, _state: dict) -> None:
        print(f"[{p:5.1f}%] {step}")

    pipeline = SplitPipeline(llm=llm)
    suffix = file_path.suffix.lower()

    if suffix in (".xlsx", ".xls"):
        result = await pipeline.run_excel(
            file_path, document_name=document_name,
            output_base_dir=args.output_dir or None, progress=progress,
        )
        print("\n═══ Excel 多文档拆分完成 ═══")
        print(f"文件: {result['excel_file']}")
        print(f"文档数: {result['completed_documents']}/{result['total_documents']}")
        print(f"条款: {result['total_articles']} → 义务: {result['total_obligations']}")
        print(f"输出目录: {result['output_base_dir']}")
        for d in result["documents"]:
            status = "✓" if d["status"] == "completed" else "✗"
            print(f"  {status} {d['document_name']}: {d['obligation_count']} 义务 → {d['excel_path'] or d.get('error', '')}")
        if result["errors"]:
            print("\n⚠ 错误:")
            for e in result["errors"]:
                print(f"  - {e}")
    else:
        result = await pipeline.run(
            file_path, document_name=document_name,
            output_dir=args.output_dir or "", progress=progress,
        )
        print("\n═══ 拆分完成 ═══")
        print(f"文档: {result.document_name}")
        print(f"条款: {result.total_articles} → 义务: {result.total_obligations}")
        print(f"耗时: {result.processing_time_seconds}s")
        print(f"Excel: {result.excel_path}")
        for note in result.processing_notes:
            print(f"⚠ {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="条款拆分 CLI")
    parser.add_argument("file", help="输入文档(.docx/.pdf/.doc/.txt/.xlsx/.xls)")
    parser.add_argument("--model", default="qwen3-max", help="LLM 模型名(对应 config/llm/<model>.yaml)")
    parser.add_argument("--provider", default="openai", choices=["openai", "pingan"], help="LLM provider 类型")
    parser.add_argument("--output-dir", default="", help="输出目录(默认 outputs/<doc名>/ 或 outputs/<excel名>/)")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM(仅验证管线)")
    args = parser.parse_args()

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)


if __name__ == "__main__":
    main()
