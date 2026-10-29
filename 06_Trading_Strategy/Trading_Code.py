"""Canonical CREON codes shared by quote, order and daily-bar requests."""

import re


def normalize_code(code):
    normalized = str(code).strip()
    if normalized.startswith('A'):
        normalized = normalized[1:]
    return normalized.zfill(6) if normalized.isdigit() else normalized


def creon_code(code):
    normalized = normalize_code(code)
    if re.fullmatch(r'[0-9A-Z]{6}', normalized):
        return f'A{normalized}'
    if re.fullmatch(r'Q[0-9A-Z]{6}', normalized):
        return normalized
    raise ValueError(f'올바르지 않은 CREON 종목코드: {code!r}')
