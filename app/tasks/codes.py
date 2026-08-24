"""Stable, human-facing task codes derived from immutable database IDs."""

from __future__ import annotations

import re
import unicodedata


_BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CHECK_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TASK_CODE_PATTERN = re.compile(
    r"^(?:T-?)?(?P<payload>[0-9A-Z]+)(?P<check>[A-Z2-9])$"
)
_PREFIXED_MENTION_PATTERN = re.compile(
    r"(?<![0-9A-Z])T-?[0-9A-Z]{2,}(?![0-9A-Z])"
)
_BARE_MENTION_PATTERN = re.compile(
    r"(?<![0-9A-Z])[0-9][0-9A-Z]*[A-HJ-NP-Z](?![0-9A-Z])"
)


class TaskCodeError(ValueError):
    """Raised when a public task code is malformed or fails validation."""


def format_task_code(task_id: int) -> str:
    """Return the stable public code for one positive task primary key."""

    task_id = _validate_task_id(task_id)
    payload = _encode_base36(task_id)
    check = _CHECK_ALPHABET[_checksum_index(task_id)]
    return f"T-{payload}{check}"


def parse_task_code(value: str) -> int:
    """Parse a full or shorthand task code and verify its checksum.

    Accepted examples include ``T-1A``, ``T1A`` and ``1A``. Input is
    normalized with Unicode NFKC so codes copied from full-width text work too.
    """

    if not isinstance(value, str):
        raise TaskCodeError("task code must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    match = _TASK_CODE_PATTERN.fullmatch(normalized)
    if match is None:
        raise TaskCodeError("invalid task code format")
    try:
        task_id = int(match.group("payload"), 36)
    except ValueError as exc:
        raise TaskCodeError("invalid task code payload") from exc
    canonical = f"T-{match.group('payload')}{match.group('check')}"
    if task_id <= 0 or format_task_code(task_id) != canonical:
        raise TaskCodeError("invalid task code checksum")
    return task_id


def find_task_code_mentions(text: str) -> tuple[str, ...]:
    """Return unique canonical codes mentioned in conversational text.

    An explicitly prefixed but invalid code is treated as a user error. Invalid
    bare fragments are ignored because ordinary text such as ``Phase 4B`` can
    otherwise look like a shorthand code.
    """

    if not isinstance(text, str):
        raise TaskCodeError("task code text must be a string")
    normalized = unicodedata.normalize("NFKC", text).upper()
    mentions: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _PREFIXED_MENTION_PATTERN.finditer(normalized):
        task_id = parse_task_code(match.group())
        mentions.append((match.start(), match.end(), format_task_code(task_id)))
        occupied.append((match.start(), match.end()))
    for match in _BARE_MENTION_PATTERN.finditer(normalized):
        if any(
            match.start() < end and match.end() > start
            for start, end in occupied
        ):
            continue
        try:
            task_id = parse_task_code(match.group())
        except TaskCodeError:
            continue
        mentions.append((match.start(), match.end(), format_task_code(task_id)))
    mentions.sort(key=lambda item: item[0])
    result: list[str] = []
    for _, _, code in mentions:
        if code not in result:
            result.append(code)
    return tuple(result)


def _validate_task_id(task_id: int) -> int:
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise TaskCodeError("task_id must be a positive integer")
    return task_id


def _encode_base36(value: int) -> str:
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(digits))


def _checksum_index(task_id: int) -> int:
    # Multiplication spreads adjacent IDs across the alphabet; the offset keeps
    # the first persisted task's deliberately friendly code as ``T-1A``.
    return (task_id * 17 + 15) % len(_CHECK_ALPHABET)
