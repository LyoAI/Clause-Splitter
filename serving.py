"""Service entry point (PingAn deployment convention: port 18998, detector-managed)."""

import os

import uvicorn

if __name__ == "__main__":
    from common.loguru import logger

    uvicorn.run(app="api.base_fastapi:app", host="0.0.0.0",
                port=int(os.environ.get("SERVICE_PORT", "18998")), workers=1, timeout_keep_alive=60)
