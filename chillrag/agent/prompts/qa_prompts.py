"""Document-QA prompt templates — domain-neutral, in Chinese and English.

The agent answers a question from whatever corpus it was given. Nothing here
assumes a subject matter, a document genre, or an output shape: the corpus could
be filings, contracts, manuals or papers, and the question could ask for prose,
a list, or strict JSON.

Two knobs carry everything caller-specific:

- ``instructions`` — appended to the system prompt. This is where a caller puts
  its domain vocabulary, its house style, or a required output contract.
- ``output_instruction`` — appended to the user message, so an output format
  travels with the question it belongs to and is not memorized per corpus.

Both default to empty, so the unconfigured prompt is reusable as-is.
"""

from __future__ import annotations

from chillrag.utils import get_logger

logger = get_logger()

QA_SYSTEM_PROMPT_ZH = """你是文档问答助手。依据检索到的文档内容回答用户的问题。

可用工具：
{tools}

要求：
- 每一轮新问题都要先检索再作答，即使上一轮已答过类似内容；对话历史里的回答不能当作本轮的依据。
- 问题涉及多个方面时，可分多轮检索不同关键词。
- 只依据检索到的内容作答，不要编造。材料中没有依据时，明确说明无法从材料中确认。
- 引用依据时给出文件名（检索结果中的文件名 / doc 字段）。
- 检索到足以回答本轮问题的材料后，直接输出最终回答，不要再调用工具。"""

QA_SYSTEM_PROMPT_EN = """You are a document question-answering assistant. Answer the user's question from the documents you retrieve.

Available tools:
{tools}

Requirements:
- Search before answering, and search again for every new question — even one similar to a question already answered. An answer in the conversation history is not evidence for the current question.
- When a question has several parts, run more rounds with different keywords.
- Answer only from what you retrieve — never fabricate. If the materials do not support an answer, say so plainly.
- When citing evidence, name the source file (the file name / doc field in the search results).
- Once you have retrieved enough to answer the current question, output the final answer directly and stop calling tools."""

FORCE_ANSWER_ZH = (
    "工具调用次数已达上限。请基于已检索到的信息直接给出最终回答，不要再调用任何工具。"
)

FORCE_ANSWER_EN = (
    "Tool-call budget exhausted. Give your final answer now based on the "
    "information gathered so far — do not call any more tools."
)

_SYSTEM_PROMPTS = {"zh": QA_SYSTEM_PROMPT_ZH, "en": QA_SYSTEM_PROMPT_EN}
_FORCE_ANSWER_MSGS = {"zh": FORCE_ANSWER_ZH, "en": FORCE_ANSWER_EN}

#: Language used by ``get_system_prompt`` / ``get_force_answer_msg`` when the
#: caller does not choose one. Chinese matches the corpora these prompts were
#: written for; a caller answering English documents passes ``lang="en"``.
DEFAULT_LANG = "zh"


def prompt_lang(lang: str = "") -> str:
    """Normalize ``lang`` to a valid key, defaulting to ``DEFAULT_LANG``."""
    key = (lang or DEFAULT_LANG).lower()
    if key not in _SYSTEM_PROMPTS:
        logger.error("unknown prompt language %r (have: %s)", key, ", ".join(_SYSTEM_PROMPTS))
        raise ValueError(
            f"unknown prompt language {key!r} (have: {', '.join(_SYSTEM_PROMPTS)})"
        )
    return key


def get_system_prompt(lang: str = "", instructions: str | None = None) -> str:
    """The system prompt, with the ``{tools}`` placeholder still in place.

    ``instructions`` is appended verbatim under a heading, so a caller can add
    its own requirements without restating the base prompt.
    """
    prompt = _SYSTEM_PROMPTS[prompt_lang(lang)]
    if instructions:
        prompt = f"{prompt}\n\n{instructions.strip()}"
    return prompt


def get_force_answer_msg(lang: str = "") -> str:
    """The budget-exhaustion hint in the active prompt language."""
    return _FORCE_ANSWER_MSGS[prompt_lang(lang)]


def build_user_prompt(question: str, output_instruction: str | None = None) -> str:
    """The user message: the question, plus an optional output requirement.

    Kept as a function (not a constant) so a caller can shape the request per
    question — a strict-JSON ask and a "answer in one sentence" ask are the same
    mechanism.
    """
    if not output_instruction:
        return question
    return f"{question}\n\n{output_instruction.strip()}"
