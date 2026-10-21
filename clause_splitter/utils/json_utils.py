"""从 LLM 输出中健壮地提取 JSON"""
from __future__ import annotations
import json
import re


def parse_json_from_text(text: str) -> dict:
    """从文本中提取第一个 JSON 对象。支持被 markdown 代码块包裹的情况。"""
    # 先尝试去掉 ```json ... ``` 包裹
    code_block = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if code_block:
        text = code_block.group(1)

    # 直接尝试解析整体
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 提取第一个 {...} 块
    match = re.search(r"\{[\s\S]+\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}


def safe_get(d: dict, key: str, default=None):
    return d.get(key, default)
