"""Safe arithmetic evaluation via an isolated subprocess."""

from __future__ import annotations

from clause_splitter.utils import get_logger

logger = get_logger()


def calculate(expression: str) -> str:
    """Evaluate ``expression`` in ``python -I``; return a number or ``[错误] ...``.

    Commas inside numbers are stripped; the run is isolated from the parent
    process and the output must be a plain number.
    """
    import os
    import re as _re
    import subprocess
    import sys
    import tempfile

    expr = _re.sub(r"(?<=\d),(?=\d)", "", expression)

    if not _re.match(r"^[\d\s+\-*/()\.\%\^a-zA-Z,]+$", expr):
        logger.warning("rejected expression (disallowed characters): %s", expression[:120])
        return f"[错误] 表达式包含不允许的字符: {expression[:120]}"

    script = (
        "from math import sqrt, exp, log, log2, log10, floor, ceil\n"
        f"print({expr})"
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        tmp = f.name

    try:
        r = subprocess.run(
            [sys.executable, "-I", tmp],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        if out and _re.match(r"^-?[\d.]+(?:[eE][+-]?\d+)?$", out):
            return out
        logger.warning("expression produced a non-numeric result: %s", expression[:120])
        return f"[错误] {r.stderr.strip() or f'非数值结果: {out[:80]}'}"
    except subprocess.TimeoutExpired:
        logger.warning("expression timed out: %s", expression[:120])
        return f"[错误] 计算超时: {expression[:120]}"
    except Exception as e:  # noqa: BLE001
        logger.warning("expression failed (%s): %s", e, expression[:120])
        return f"[错误] 计算失败: {e}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
