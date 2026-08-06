"""Compare prompts — the retrieve-then-judge flow.

Exposes the judge system prompt and user-prompt builder used by
:mod:`operators.compare.judge`. The judge does NOT search: it receives exactly
the candidates BM25 retrieved and emits a candidate row number.
"""

from __future__ import annotations

from prompts.compare.judge import JUDGE_SYSTEM_PROMPT, build_judge_user_prompt

__all__ = ["JUDGE_SYSTEM_PROMPT", "build_judge_user_prompt"]
