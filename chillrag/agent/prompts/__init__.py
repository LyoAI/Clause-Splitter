"""Prompt templates for the agent loop (centralized, decoupled from logic)."""

from .qa_prompts import (
    DEFAULT_LANG,
    build_user_prompt,
    get_force_answer_msg,
    get_system_prompt,
    prompt_lang,
)

__all__ = [
    "DEFAULT_LANG",
    "get_system_prompt",
    "get_force_answer_msg",
    "prompt_lang",
    "build_user_prompt",
]
