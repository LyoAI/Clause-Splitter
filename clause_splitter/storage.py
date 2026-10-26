"""DATA_DIR layout — one regulation, one directory tree.

data/regulations/{id}/
    source/{file}               original upload
    split/structure.json        article segmentation (split-artifact format)
    episodes/                   per-clause agent trajectories
    compare_result.json         raw comparison result (pre relevance filter)
    内外规合规差距分析报告.md     gap-analysis report (engine filename)
    内规检索复核.html            self-contained review page (engine filename)
data/internal/docs/{file}       internal library uploads (mirrored into the RAG service)
"""

from __future__ import annotations

import re
from pathlib import Path

from config.config_app import CONFIG_APP

_UNSAFE = re.compile(r"[\\/:*?\"<>|\s]+")

#: Artifact names exposed via GET /regulations/{id}/artifacts (fixed allowlist).
ARTIFACTS = ("structure.json", "compare_result.json", "gap_report.md", "review.html")


def safe_filename(name: str) -> str:
    """Strip path separators / filesystem-hostile chars from an upload name."""
    cleaned = _UNSAFE.sub("_", (name or "").strip()).strip("._")
    return cleaned[:120] or "unnamed"


def regulations_root() -> Path:
    return CONFIG_APP.data_dir / "regulations"


def regulation_dir(reg_id: str) -> Path:
    return regulations_root() / reg_id


def source_dir(reg_id: str) -> Path:
    return regulation_dir(reg_id) / "source"


def split_dir(reg_id: str) -> Path:
    return regulation_dir(reg_id) / "split"


def episodes_dir(reg_id: str) -> Path:
    return regulation_dir(reg_id) / "episodes"


def internal_docs_dir() -> Path:
    return CONFIG_APP.data_dir / "internal" / "docs"


def artifact_path(reg_id: str, name: str) -> Path | None:
    """Resolve an allowlisted artifact name to its file (no traversal)."""
    table = {
        "structure.json": split_dir(reg_id) / "structure.json",
        "compare_result.json": regulation_dir(reg_id) / "compare_result.json",
        "gap_report.md": regulation_dir(reg_id) / "内外规合规差距分析报告.md",
        "review.html": regulation_dir(reg_id) / "内规检索复核.html",
    }
    return table.get(name)
