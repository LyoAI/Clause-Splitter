#!/usr/bin/env python3
"""Run one clause through ``ClauseAnalyzer`` and print the result as JSON.

Real LLM (``config/llm``) and real Zeus (``.env``) — no stubs.

    uv run python scripts/analyze_clause.py examples/clause_input.json
    uv run python scripts/analyze_clause.py examples/clause_input.json --raw
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clause_analysis import ClauseAnalyzer, ClauseInput  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="单条外规内化分析(真实 LLM + Zeus)")
    parser.add_argument("input", type=Path, help="ClauseInput 的 JSON 文件")
    parser.add_argument("--raw", action="store_true", help="输出完整 ClauseResult 而非 ESG data[] 元素")
    args = parser.parse_args()

    clause = ClauseInput.model_validate_json(args.input.read_text(encoding="utf-8"))
    analyzer = ClauseAnalyzer.from_config()
    result = asyncio.run(analyzer.analyze(clause))
    payload = result.model_dump(mode="json") if args.raw else result.to_esg()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
