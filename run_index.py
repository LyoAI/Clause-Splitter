#!/usr/bin/env python
"""入口② —— 内规库索引 / embedding 构建 CLI(离线步骤).

用法:
    python run_index.py [--internal-dir outputs/internal/] [--name internal] \\
           [--embed-model ~/Models/Qwen3-Embedding-0.6B] [--rebuild]

流程(模仿 ``run_prepare_docs.py``):
    扫描 ``--internal-dir`` 下的内规原始文档(pdf / docx / pptx / xlsx / 图片)
    → MinerU 本地解析 → 层级感知分块 → DocumentStore → faiss 向量索引
    → BM25 Lucene 索引。粒度按章节/条款标题,不做逐条义务拆分。

索引幂等:内容不变即复用缓存,重跑不重复解析/embedding;``--rebuild`` 强制重建。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证从任意 cwd 运行都能导入 clause_splitter
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="内规库索引/embedding 构建")
    parser.add_argument("--internal-dir", default="upload_materials/internal/",
                        help="内规库原始文档目录(pdf/docx/pptx/xlsx/图片)")
    parser.add_argument("--name", default="internal",
                        help="store 名,决定缓存目录 .cache/<name>/")
    parser.add_argument("--embed-model", default="",
                        help="embedding 模型路径(默认 config.EMBED_MODEL)")
    parser.add_argument("--rebuild", action="store_true",
                        help="强制重建全部索引")
    parser.add_argument("--list", action="store_true",
                        help="仅列出待索引的文档,不构建")
    args = parser.parse_args()

    from clause_splitter.compare.index import prepare_docs
    from clause_splitter.tools.parser.ingest import scan_supported_docs

    if args.list:
        try:
            files = scan_supported_docs(Path(args.internal_dir))
        except FileNotFoundError as e:
            print(f"✗ {e}")
            sys.exit(1)
        print(f"发现 {len(files)} 份内规文档:")
        for f in files:
            print(f"  - {f}")
        return

    store = prepare_docs(
        args.internal_dir,
        name=args.name,
        embed_model=args.embed_model or None,
        rebuild=args.rebuild,
    )
    print(f"\n═══ 索引构建完成 ═══")
    print(f"{len(store.docs)} 份文档 → {len(store.all_chunks())} 个检索单元 ({args.name})")
    print(f"缓存目录: .cache/{args.name}/{{store, chunks, bm25, faiss, md}}")


if __name__ == "__main__":
    main()
