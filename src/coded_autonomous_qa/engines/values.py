"""Test-data generation from observed field constraints (valid, invalid, boundary)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

INJECTION_PROBES = [
    ("<script>alert(1)</script>", "html/script injection"),
    ("' OR '1'='1", "SQL meta-characters"),
    ("{{7*7}}", "template expression"),
]


@dataclass
class Probe:
    value: str
    category: str  # empty | boundary_valid | boundary_invalid | format_invalid | range_invalid | ...
    expect_accept: bool
    reason: str


def valid_value(field: dict[str, Any]) -> str:
    ftype = (field.get("type") or "text").lower()
    if field.get("options"):
        return str(field["options"][0])
    if ftype == "email":
        return "qa.user@example.test"
    if ftype in ("number", "range"):
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and hi is not None:
            return _num((lo + hi) / 2)
        return _num(lo if lo is not None else (hi if hi is not None else 1))
    if ftype == "tel":
        return "9876543210"
    if ftype == "url":
        return "https://example.test"
    if ftype == "date":
        return "2025-01-15"
    if ftype in ("checkbox", "radio"):
        return "true"
    if ftype == "password":
        return "${secret:TEST_PASSWORD}"
    lo, hi = field.get("min_length"), field.get("max_length")
    n = max(lo or 0, min(hi or 8, 8))
    return ("QaValue" * 10)[: max(n, 1)]


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def probes_for(field: dict[str, Any], include_security: bool = True) -> list[Probe]:
    """Negative and boundary probes derived only from constraints that were observed."""
    ftype = (field.get("type") or "text").lower()
    out: list[Probe] = []
    if field.get("required"):
        out.append(Probe("", "empty_required", False, "required field left empty"))
        if ftype not in ("select", "checkbox", "radio", "date", "number"):
            out.append(Probe("   ", "whitespace_only", False, "required field with whitespace only"))
    lo, hi = field.get("min_length"), field.get("max_length")
    if hi:
        out.append(Probe("A" * hi, "boundary_valid", True, f"exactly max length {hi}"))
        out.append(Probe("A" * (hi + 1), "boundary_invalid", False, f"max length + 1 ({hi + 1})"))
    if lo and lo > 0:
        out.append(Probe("A" * lo, "boundary_valid", True, f"exactly min length {lo}"))
        if lo > 1:
            out.append(Probe("A" * (lo - 1), "boundary_invalid", False, f"min length - 1 ({lo - 1})"))
    vmin, vmax = field.get("min"), field.get("max")
    if vmin is not None:
        out.append(Probe(_num(vmin), "boundary_valid", True, f"minimum {vmin}"))
        out.append(Probe(_num(vmin - 1), "range_invalid", False, f"below minimum {vmin}"))
    if vmax is not None:
        out.append(Probe(_num(vmax), "boundary_valid", True, f"maximum {vmax}"))
        out.append(Probe(_num(vmax + 1), "range_invalid", False, f"above maximum {vmax}"))
    if ftype == "email":
        out += [
            Probe("not-an-email", "format_invalid", False, "missing @ and domain"),
            Probe("a@b", "format_invalid", False, "missing top-level domain"),
        ]
    elif ftype in ("number", "range"):
        out.append(Probe("abc", "format_invalid", False, "non-numeric text in a number field"))
    elif ftype == "tel":
        out.append(Probe("12ab", "format_invalid", False, "letters in a phone number"))
    elif ftype == "date":
        out.append(Probe("2025-02-30", "format_invalid", False, "impossible date"))
    if field.get("pattern"):
        out.append(Probe("@@invalid@@", "format_invalid", False, f"does not match pattern {field['pattern']}"))
    if field.get("options"):
        out.append(Probe("__not_an_option__", "allowed_values_invalid", False, "value outside the option list"))
    if include_security and ftype in ("text", "textarea", "search", "email", "url"):
        for value, reason in INJECTION_PROBES:
            out.append(Probe(value, "security_probe", False, f"{reason} must be rejected or rendered inert"))
    return out
