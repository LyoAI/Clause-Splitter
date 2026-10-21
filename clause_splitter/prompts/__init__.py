"""All LLM prompts as editable markdown files in this folder.

``render(name, **vars)`` loads ``<name>.md`` and substitutes ``{{var}}`` slots
via plain replacement, so literal braces in prompt text (JSON/YAML examples)
are never touched. Tune prompts by editing the .md files — no Python changes.
"""

from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the raw text of ``prompts/<name>.md``."""
    return resources.files("clause_splitter.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def render(name: str, **variables: object) -> str:
    """Load a prompt and substitute ``{{var}}`` slots (unknown slots stay as-is)."""
    text = load(name)
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
