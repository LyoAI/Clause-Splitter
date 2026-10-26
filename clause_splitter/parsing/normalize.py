"""Text normalization shared by the remote and local parse paths.

Kept deliberately conservative: legal text is evidence, so nothing beyond
line endings and invisible characters is rewritten. Heading markers and
punctuation must survive untouched for the split ladder.
"""

from __future__ import annotations

import re

_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Normalize line endings and invisible characters; collapse blank runs."""
    if not text:
        return ""
    text = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUN_RE.sub("\n\n", text).strip()
