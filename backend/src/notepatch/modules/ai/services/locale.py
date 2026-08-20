from __future__ import annotations

import re


_BCP47_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def normalize_client_locale(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or not _BCP47_PATTERN.fullmatch(candidate):
        return None
    parts = candidate.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        else:
            normalized.append(part)
    return "-".join(normalized)


def preferred_accept_language(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidates: list[tuple[float, int, str]] = []
    for index, item in enumerate(value.split(",")):
        pieces = [piece.strip() for piece in item.split(";") if piece.strip()]
        if not pieces or pieces[0] == "*":
            continue
        quality = 1.0
        for parameter in pieces[1:]:
            if not parameter.lower().startswith("q="):
                continue
            try:
                quality = float(parameter[2:])
            except ValueError:
                quality = 0.0
        locale = normalize_client_locale(pieces[0])
        if locale is not None and quality > 0:
            candidates.append((quality, -index, locale))
    if not candidates:
        return None
    return max(candidates)[2]


def resolve_client_locale(
    explicit_locale: str | None,
    accept_language: str | None,
    fallback_locale: str,
) -> str:
    return (
        normalize_client_locale(explicit_locale)
        or preferred_accept_language(accept_language)
        or normalize_client_locale(fallback_locale)
        or "zh-CN"
    )
