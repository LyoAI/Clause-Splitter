"""Safe arithmetic evaluation.

Models do arithmetic unreliably, and a wrong number is indistinguishable from a
right one once it is in prose: any computation the agent needs is delegated to
this tool instead. The expression is executed as ``print(<expr>)`` in a
short-lived child process under ``-I`` (isolated mode), so even a hostile
payload cannot affect the parent or read its state.

Public API:
- ``calculate(expression) -> str`` — run the expression and return the numeric
  result, or an ``[错误] ...`` message on failure.
"""

from __future__ import annotations

from chillrag.utils import get_logger

logger = get_logger()


def calculate(expression: str) -> str:
    """Execute an arithmetic expression via an isolated subprocess.

    The LLM writes a Python expression; we run ``print(expression)`` in a
    short-lived child process with ``-I`` (isolated mode) so even malicious
    payloads cannot affect the parent or read its state.

    Commas inside numbers are stripped (``1,234.5`` -> ``1234.5``); supported
    operators are ``+ - * / () **`` plus ``abs``/``round``/``min``/``max``/
    ``sum``/``len``/``pow``/``int``/``float``/``sqrt``/``exp``/``log``/``log2``/
    ``log10``/``floor``/``ceil``. Returns the result string, or an error
    prefixed with ``[错误]``.
    """
    import os
    import re as _re
    import subprocess
    import sys
    import tempfile

    # Remove commas from numbers (e.g. "174,755,235" -> "174755235").
    expr = _re.sub(r"(?<=\d),(?=\d)", "", expression)

    # Coarse arithmetic filter: digits, whitespace, operators, parentheses, dot,
    # percent, caret, commas, and ASCII letters (for the math builtins below).
    # The subprocess isolation (-I) + numeric-output validation is the real
    # defense — underscore and quotes are deliberately excluded, so dunder /
    # string-based attacks fail the regex regardless of letters allowed.
    if not _re.match(r"^[\d\s+\-*/()\.\%\^a-zA-Z,]+$", expr):
        logger.warning("rejected expression (disallowed characters): %s", expression[:120])
        return f"[错误] 表达式包含不允许的字符: {expression[:120]}"

    # sqrt/exp/log/... are math-module functions, not builtins; pre-import them
    # so bare names work in the isolated subprocess.
    script = "from math import sqrt, exp, log, log2, log10, floor, ceil\n" + f"print({expr})"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        tmp = f.name

    try:
        r = subprocess.run(
            [sys.executable, "-I", tmp],  # -I = isolated mode
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip()
        # Only return the result if it looks like a plain number.
        if out and _re.match(r"^-?[\d.]+(?:[eE][+-]?\d+)?$", out):
            return out
        logger.warning("expression produced a non-numeric result: %s", expression[:120])
        return f"[错误] {r.stderr.strip() or f'非数值结果: {out[:80]}'}"
    except subprocess.TimeoutExpired:
        logger.warning("expression timed out: %s", expression[:120])
        return f"[错误] 计算超时: {expression[:120]}"
    except Exception as e:
        logger.warning("expression failed (%s): %s", e, expression[:120])
        return f"[错误] 计算失败: {e}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
