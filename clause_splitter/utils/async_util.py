"""在任意环境(普通终端 / Jupyter / IPython / IDE 控制台)运行协程的工具。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def run_coro(coro: Any) -> Any:
    """从同步代码运行一个协程,无论当前线程是否已有运行中的事件循环。

    ``asyncio.run()`` 要求当前线程**没有**运行中的 loop;Jupyter / IPython /
    IDE 控制台自带一个运行中的 loop,直接调用会抛
    "asyncio.run() cannot be called from a running event loop"。

    这里先探测:干净环境走常规 ``asyncio.run``;已有 loop 时,把协程放进一个
    独立的 daemon 线程里用 ``asyncio.run`` 执行(线程内没有 loop,允许),
    并把异常/结果抛回主线程。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 干净环境(普通终端)→ 常规路径。
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 协程内的异常要抛回主线程
            box["error"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")
