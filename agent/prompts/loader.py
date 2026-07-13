"""Prompt loader — centralized loading + variable interpolation.

Supports:
1. Loading core splitting rules from SKILL.md
2. Loading scene-specific prompts from Python modules
3. Consistent variable interpolation ({document_name}, etc.)

All prompt files use single-brace {variable} placeholders. The loader
handles interpolation in one place — no more .replace() calls in nodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils import get_logger

logger = get_logger()

_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


class PromptLoader:
    """Loads and interpolates system prompts for each pipeline stage.

    Supports loading from skills/{name}/SKILL.md subdirectories.

    Usage:
        loader = PromptLoader()
        prompt = loader.get_decompose_simple_prompt(document_name="法规.docx")
        compare_prompt = loader.load_skill_content("compare")
    """

    def __init__(self, skills_dir: Path | str | None = None) -> None:
        self._skills_dir = Path(skills_dir) if skills_dir else _SKILLS_DIR
        self._cache: dict[str, str] = {}

    # ── SKILL.md loading ──────────────────────────────────────────────────

    @property
    def skill_content(self) -> str:
        """Lazy-load the 'split' SKILL.md (backward-compat)."""
        return self.load_skill_content("split")

    def load_skill_content(self, name: str) -> str:
        """Load a SKILL.md from skills/{name}/SKILL.md, stripping YAML frontmatter.

        Results are cached per name.

        Args:
            name: Skill subdirectory name (e.g. 'split', 'compare', 'summary').

        Returns:
            Skill content as string, or "" on failure.
        """
        if name in self._cache:
            return self._cache[name]

        skill_path = self._skills_dir / name / "SKILL.md"
        try:
            text = skill_path.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3:].lstrip()
            self._cache[name] = text
            logger.info("SKILL.md [%s] loaded, %d chars", name, len(text))
            return text
        except Exception as e:
            logger.error("Failed to load SKILL.md [%s]: %s", name, e)
            return ""

    # ── Scene prompts ────────────────────────────────────────────────────

    def get_structure_analysis_prompt(self) -> str:
        from agent.prompts.structure_analysis import SYSTEM_PROMPT
        return SYSTEM_PROMPT

    def get_complexity_judge_prompt(self) -> str:
        from agent.prompts.complexity_judge import SYSTEM_PROMPT
        return SYSTEM_PROMPT

    def get_decompose_simple_prompt(self, document_name: str = "") -> str:
        """One-shot decomposition prompt with SKILL.md rules injected."""
        from agent.prompts.decompose_simple import SYSTEM_PROMPT_TEMPLATE
        return self.format_prompt(
            SYSTEM_PROMPT_TEMPLATE,
            skill_content=self.skill_content,
            document_name=document_name,
        )

    def get_decompose_complex_prompt(self, document_name: str = "") -> str:
        """ReAct decomposition prompt with SKILL.md rules injected."""
        from agent.prompts.decompose_complex import SYSTEM_PROMPT_TEMPLATE
        return self.format_prompt(
            SYSTEM_PROMPT_TEMPLATE,
            skill_content=self.skill_content,
            document_name=document_name,
        )

    def get_validation_prompt(self) -> str:
        from agent.prompts.validation import SYSTEM_PROMPT
        return SYSTEM_PROMPT

    # ── Variable interpolation ───────────────────────────────────────────

    @staticmethod
    def format_prompt(template: str, **kwargs: Any) -> str:
        """Safely interpolate {variable} placeholders in a template.

        Unknown placeholders are left as-is instead of raising KeyError.
        """
        result = template
        for key, value in kwargs.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result
