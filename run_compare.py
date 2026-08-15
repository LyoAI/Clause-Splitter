#!/usr/bin/env python
"""入口③ —— 内外规比对 CLI(核心功能).

用法:
    python run_compare.py --external <外规> --internal-dir upload_materials/internal/ \\
           [--searcher hybrid] [--output-dir outputs/external/网络数据安全风险评估办法/] \\
           [--index-dir /mnt/shared/idx]

- ``--external``:外规来源,三种皆可:
  - 目录:扫描其中全部 ``*_structure.json`` / 拆分结果 JSON,逐制度逐条款比对;
  - ``*_structure.json`` / ``*_split_result.json`` / ``*_条款拆分结果.json``:直接读取;
  - docx/pdf/txt:自动先跑拆分(产出 structure.json),再比对。
- ``--internal-dir``:内规库目录(含各制度的 structure.json / 拆分结果)。
- ``--searcher``:grep / bm25 / faiss / hybrid(默认 hybrid,三检索工具全绑)。
- ``--limit N``:只比对前 N 条外规条款(联调用)。

产物:
- ``{output_dir}/compare_result.json``(CompareResult,含全部 MatchResult 与检索召回候选)
- ``{output_dir}/内外规合规差距分析报告.md``(Task #6 报告)
- ``{output_dir}/内规检索复核.html``(自包含复核页:每条外规条款的 top-k 内规召回)
- ``{output_dir}/episodes/``(每条 episode 轨迹,防幻觉审计用)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证从任意 cwd 运行都能导入 clause_splitter
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _build_llm(args: argparse.Namespace):
    from clause_splitter.llm import get_llm
    return get_llm(args.model, args.provider)


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


async def _run(args: argparse.Namespace) -> None:
    from clause_splitter.compare.aggregate import run_compare
    from clause_splitter.compare.index import prepare_docs
    from clause_splitter.compare.loader import load_external_clauses
    from clause_splitter.models import CompareResult

    llm = _build_llm(args)

    # 1) 内规库:优先装载既有索引(run_index 构建,含自定义位置);
    #    没有才回退到原始文档自动构建(增量,幂等)。
    from clause_splitter.compare.index import load_store

    index_root = args.index_dir or None
    store = load_store(name=args.name, index_root=index_root)
    if store is None:
        store = prepare_docs(
            args.internal_dir, name=args.name, index_root=index_root,
        )
    print(f"内规库: {len(store.docs)} 份文档 / {len(store.all_chunks())} 个检索单元")

    # 2) 装载外规条款。
    clauses = await load_external_clauses(args.external, args.output_dir or "", llm)
    if args.limit:
        clauses = clauses[: args.limit]
    if not clauses:
        raise SystemExit("外规条款为空,无可比对内容。")
    print(f"外规条款: {len(clauses)} 条(来自 {len({c.document_name for c in clauses})} 个制度)")

    # 3) 跑比对(batch 并行,每条条款一个 episode)。
    persist_dir = Path(args.output_dir or "outputs") / "episodes"
    result: CompareResult = await run_compare(
        clauses,
        store,
        llm,
        searcher=args.searcher,
        top_k=args.top_k,
        max_tool_calls=args.max_tool_calls,
        concurrency=args.concurrency,
        episode_timeout=args.episode_timeout,
        persist_dir=str(persist_dir),
        model=args.model,
        provider=args.provider,
        review_top_k=args.review_top_k,
    )

    # 4) 落盘 CompareResult。
    out_dir = Path(args.output_dir) if args.output_dir else Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "compare_result.json"
    result_path.write_text(result.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")

    # 5) 生成差距分析报告。
    from clause_splitter.compare.report import generate_gap_report

    rep = generate_gap_report(result, store, llm, output_dir=str(out_dir))

    # 6) 生成内规检索复核页(每条外规条款的 top-k 内规召回,自包含 HTML)。
    from clause_splitter.compare.review import render_review_html

    review_path = render_review_html(result, out_dir / "内规检索复核.html", store_name=args.name)

    # 7) 打印统计。
    print("\n═══ 比对完成 ═══")
    print(f"外规条款: {result.total_external_obligations} | 内规条款: {result.total_internal_obligations}")
    print(f"覆盖率: {result.coverage_rate:.1%}")
    print(f"报告: {rep['report_path'] or '(未写盘)'} [模式: {rep['mode']}]")
    print(f"结果: {result_path}")
    print(f"复核页: {review_path} (浏览器打开)")
    print(f"episode 轨迹: {persist_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="内外规比对 CLI(条款级 + 可插拔检索)")
    parser.add_argument("--external", required=True, help="外规来源:目录 / *_structure.json / 拆分结果 JSON / docx/pdf/txt")
    parser.add_argument("--internal-dir", default="upload_materials/internal",
                        help="内规库原始文档目录(仅当 --index-dir 下没有既有索引时自动构建,默认 upload_materials/internal)")
    parser.add_argument("--name", default="internal", help="内规库 store 名(与 run_index --name 一致,默认 internal)")
    parser.add_argument("--index-dir", default="",
                        help="内规索引存储根目录(与 run_index --index-dir 一致;不传则用 .cache/ 或 $INDEX_CACHE_DIR)")
    parser.add_argument("--searcher", default="hybrid", choices=["grep", "bm25", "faiss", "hybrid"])
    parser.add_argument("--model", default="qwen3-max", help="LLM 模型名(对应 config/llm/<model>.yaml)")
    parser.add_argument("--provider", default="openai", choices=["openai", "pingan"], help="LLM provider 类型")
    parser.add_argument("--output-dir", default="", help="结果输出目录(默认 outputs/ 下)")
    parser.add_argument("--top-k", type=int, default=None, help="每条检索返回条款数(默认 config.SEARCH_TOP_K=10)")
    parser.add_argument("--review-top-k", type=int, default=None,
                        help="复核页每条外规条款展示的 top-k 召回数(默认 config.REVIEW_TOP_K=10)")
    parser.add_argument("--max-tool-calls", type=int, default=None, help="单条 episode 工具调用上限(默认 15)")
    parser.add_argument("--concurrency", type=int, default=None, help="并行 episode 数(默认 4)")
    parser.add_argument("--episode-timeout", type=float, default=None, help="单条 episode 超时秒数(默认 600)")
    parser.add_argument("--limit", type=int, default=0, help="只比对前 N 条外规条款(联调用)")
    args = parser.parse_args()

    # 缺省值从 config 读,避免 CLI 与 config 双源漂移。
    from clause_splitter.config import (
        COMPARE_CONCURRENCY,
        EPISODE_TIMEOUT_S,
        MAX_TOOL_CALLS,
        REVIEW_TOP_K,
        SEARCH_TOP_K,
    )
    args.top_k = args.top_k or SEARCH_TOP_K
    args.review_top_k = args.review_top_k or REVIEW_TOP_K
    args.max_tool_calls = args.max_tool_calls or MAX_TOOL_CALLS
    args.concurrency = args.concurrency or COMPARE_CONCURRENCY
    args.episode_timeout = args.episode_timeout or EPISODE_TIMEOUT_S

    from clause_splitter.utils.async_util import run_coro

    try:
        run_coro(_run(args))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)


if __name__ == "__main__":
    main()
