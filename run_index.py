#!/usr/bin/env python
"""入口② —— 内规库条款索引构建 CLI(离线步骤).

用法:
    python run_index.py [--internal-dir outputs/internal/] [--name internal] \\
           [--embed-model ~/Models/Qwen3-Embedding-0.6B] [--rebuild]

流程:
    扫描 ``--internal-dir`` 下每份制度的 structure.json / 拆分结果 JSON →
    ``build_clause_store``(每条条款一个 Chunk)→ 构建三种索引
    (grep/bm25/faiss)→ 持久化 store 到 ``.cache/<name>/``。

索引按 ``corpus_signature``(内容 hash)缓存 —— 内容不变即复用,重跑幂等;
``--rebuild`` 强制全部重建。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证从任意 cwd 运行都能导入 clause_splitter
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="内规库条款索引构建")
    parser.add_argument("--internal-dir", default="outputs/internal/",
                        help="内规库目录(含 *_structure.json / 拆分结果 JSON)")
    parser.add_argument("--name", default="internal",
                        help="store 名,决定缓存目录 .cache/<name>/")
    parser.add_argument("--embed-model", default="",
                        help="embedding 模型路径(默认 config.EMBED_MODEL)")
    parser.add_argument("--rebuild", action="store_true",
                        help="强制重建全部索引")
    parser.add_argument("--list", action="store_true",
                        help="仅列出待索引的条款来源文件,不构建")
    args = parser.parse_args()

    from clause_splitter.compare.index import build_clause_store, build_all_indexes
    from clause_splitter.compare._sources import scan_clause_source_files

    if args.list:
        files = scan_clause_source_files(Path(args.internal_dir))
        print(f"发现 {len(files)} 份条款来源文件:")
        for f in files:
            print(f"  - {f}")
        return

    store = build_clause_store(internal_dir=args.internal_dir, name=args.name)
    if len(store.all_chunks()) == 0:
        print(f"✗ 内规库为空: {args.internal_dir} 下未找到条款来源。")
        sys.exit(1)

    print(f"条款级 store: {len(store.docs)} 制度 / {len(store.all_chunks())} 条款")

    summary = build_all_indexes(
        store, embed_model=args.embed_model or None, rebuild=args.rebuild,
    )
    print("\n═══ 索引构建完成 ═══")
    print(f"cache_key : {summary['cache_key']}")
    print(f"grep 目录 : {summary['chunks']}")
    print(f"bm25 目录 : {summary['bm25']}")
    print(f"faiss 目录: {summary['faiss'] or '(跳过 — embedding 不可用)'}")
    print(f"store 目录: {summary['store']}")


if __name__ == "__main__":
    main()
