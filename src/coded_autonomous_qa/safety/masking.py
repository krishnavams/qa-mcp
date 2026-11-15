"""Masking of sensitive customer data and secrets.

Applied to everything that is logged, audited, stored as evidence text or put
in a report. Masking is deliberately conservative: a false positive costs a
little readability, a false negative leaks customer data.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***"

SENSITIVE_KEYS = re.compile(
    r"(?:^|[_\-.])(pass(word|wd|code|phrase)?|pwd|secret|client[_-]?secret|token|access[_-]?token|api[_-]?key|"
    r"authorization|auth[_-]?token|credentials?|otp|pin|cvv|cvc|cookie|session[_-]?id|private[_-]?key|"
    r"account[_-]?(no|number)|card[_-]?(no|number)|aadhaar|pan|pan[_-]?(no|number)|ssn)(?:$|[_\-.])",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")  # Indian PAN
_AADHAAR = re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")  # Indian Aadhaar
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?<![\w])(?:\+?\d{1,3}[ -]?)?(?:\(?\d{3,5}\)?[ -]?)\d{3,4}[ -]?\d{3,4}(?![\w])")
_LONG_DIGITS = re.compile(r"\b\d{9,18}\b")  # bank account numbers
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDS = re.compile(r"(?i)(\w+://)[^/\s:@]+:[^/\s@]+@")
_KV_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|otp|pin)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return f"****-****-****-{digits[-4:]}"
    return raw


def mask_text(text: str) -> str:
    if not text:
        return text
    out = _URL_CREDS.sub(r"\1***:***@", text)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", out)
    out = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", out)
    out = _CARD.sub(_mask_card, out)
    out = _PAN.sub("*****####*", out)
    out = _AADHAAR.sub("XXXX-XXXX-####", out)
    out = _SSN.sub("***-**-####", out)
    out = _LONG_DIGITS.sub(lambda m: "*" * (len(m.group(0)) - 4) + m.group(0)[-4:], out)
    out = _PHONE.sub(lambda m: _mask_phone(m.group(0)), out)
    return out


def _mask_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        return raw
    return "+**-******" + digits[-4:]


def mask_value(value: Any, key: str | None = None) -> Any:
    """Recursively mask a JSON-like value; values under sensitive keys are replaced."""
    if (
        key is not None
        and SENSITIVE_KEYS.search(key)
        and not isinstance(value, (bool, dict, list, tuple))
        and value not in (None, "")
    ):
        return MASK
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(v) for v in value]
    return value
