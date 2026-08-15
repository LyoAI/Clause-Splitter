#!/usr/bin/env python
"""入口④ —— FastAPI 薄壳启动.

用法:
    python run_api.py [--host 0.0.0.0] [--port 8000]
    CORS_ORIGINS="https://app.example.com" python run_api.py

薄壳只做 HTTP 封装,计算在 CLI 核心。端点见 /docs。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="Clause Splitter FastAPI 薄壳")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "clause_splitter.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
        sys.exit(130)
