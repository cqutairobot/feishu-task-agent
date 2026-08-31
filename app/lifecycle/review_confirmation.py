"""Explicit confirmation marker for high-risk natural-language review writes."""

from __future__ import annotations

import re


_CONFIRMATION_PREFIX = re.compile(
    r"^(?:我\s*)?确认(?:执行|操作)\s*",
    re.IGNORECASE,
)


def has_explicit_review_confirmation(text: str) -> bool:
    """Return whether the message starts with an unambiguous write gate.

    The confirmation is intentionally local and conservative. A model cannot
    manufacture it from context, and questions/conditional sentences do not
    satisfy the prefix.
    """

    if not isinstance(text, str):
        return False
    normalized = " ".join(text.strip().split())
    if not normalized or "？" in normalized or "?" in normalized:
        return False
    return _CONFIRMATION_PREFIX.match(normalized) is not None
