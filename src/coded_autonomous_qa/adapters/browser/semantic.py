"""Semantic element targeting shared by every engine.

A :class:`ElementTarget` is derived once from a test step (and what the knowledge base knows), then
rendered per engine:

* :func:`to_playwright_selector` — a selector string for Coded's ``browser`` tool (Playwright syntax);
* the Playwright adapter turns it into ``get_by_role`` / ``get_by_label`` / … locators;
* the Selenium adapter turns it into XPath/CSS;
* :func:`to_description` — natural language for vision engines (PyMidsense).
"""

from __future__ import annotations

import re

from coded_autonomous_qa.knowledge.models import (
    Assertion,
    AssertionKind,
    ElementTarget,
    StepAction,
    TestStep,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][\w.\-]*$")
CLICK_ROLES = ("button", "link", "tab", "menuitem")


def target_for_step(step: TestStep) -> ElementTarget | None:
    """The semantic target of a step (explicit ``element`` wins, then known selector, then the step text)."""
    if step.element is not None and not step.element.is_empty():
        return step.element
    text = (step.target or "").strip()
    if not text and not step.selector:
        return None
    t = ElementTarget(css=step.selector or None, description=text or None)
    if step.action in (StepAction.FILL, StepAction.SELECT):
        if _IDENTIFIER.match(text):
            t.field_name = text
        t.label = text or None
        t.role = "combobox" if step.action is StepAction.SELECT else None
    elif step.action is StepAction.CHECK:
        t.label = text or None
        t.role = "checkbox"
    elif step.action in (StepAction.CLICK, StepAction.AI_ACTION):
        t.name = text or None
    else:  # assert / observe targets: a field or any element
        t.label = text or None
        if _IDENTIFIER.match(text):
            t.field_name = text
    return t


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def to_playwright_selector(target: ElementTarget | None, action: StepAction) -> tuple[str | None, str]:
    """A Playwright selector string and where it came from. Never a bare ``text=`` match: that also hits
    headings and paragraphs (an ``<h1>Sign in</h1>`` above the "Sign in" button)."""
    if target is None:
        return None, "none"
    if target.css:
        return target.css, "known"
    if target.test_id:
        return f'[data-testid="{_q(target.test_id)}"]', "test_id"
    if action in (StepAction.FILL, StepAction.SELECT):
        if target.field_name:
            n = _q(target.field_name)
            return f'[name="{n}"], [id="{n}"]', "inferred:name_or_id"
        if target.label:
            return f'internal:label="{_q(target.label)}"i', "inferred:label"
        if target.placeholder:
            return f'[placeholder="{_q(target.placeholder)}"]', "inferred:placeholder"
    if action is StepAction.CHECK and target.label:
        return f'internal:label="{_q(target.label)}"i', "inferred:label"
    name = target.name or target.text or target.label
    if not name:
        return None, "none"
    n = _q(name)
    roles = [target.role] if target.role else list(CLICK_ROLES)
    parts = []
    for role in roles:
        if role == "button":
            parts.append(f'button:has-text("{n}")')
        elif role == "link":
            parts.append(f'a:has-text("{n}")')
        parts.append(f'[role={role}]:has-text("{n}")')
    parts += [f'input[type=submit][value="{n}"]', f'input[type=button][value="{n}"]']
    return ", ".join(parts), "inferred:clickable_text"


def to_description(target: ElementTarget | None, action: StepAction) -> str:
    """Natural-language description for vision engines."""
    if target is None:
        return ""
    if target.description:
        base = target.description
    else:
        base = target.label or target.name or target.text or target.placeholder or target.field_name or target.css or ""
    kind = {StepAction.FILL: "input field", StepAction.SELECT: "dropdown", StepAction.CHECK: "checkbox"}.get(action)
    if kind and kind not in base.lower():
        return f"the '{base}' {kind}"
    return base


# ---------------------------------------------------------------------------
# Natural-language expectation -> structured assertion (heuristic, flagged as such)
# ---------------------------------------------------------------------------
_QUOTED = re.compile("[\"'\u201c\u2018](.+?)[\"'\u201d\u2019]")
_URL = re.compile(r"\b(?:url|address|page)\b.*\b(?:contains?|includes?|is|ends with)\s+(\S+)", re.I)
_TITLE = re.compile(r"\btitle\b.*\b(?:contains?|is)\s+(.+)$", re.I)
_SHOWN = re.compile(
    r"^(?:the\s+)?(?P<x>.+?)\s+(?:is|are)\s+(?:shown|displayed|visible|present)\b|"
    r"^(?:see|shows?|displays?)\s+(?:the\s+)?(?P<y>.+)$",
    re.I,
)
_ABSENT = re.compile(
    r"^(?:no\s+)(?P<x>.+?)(?:\s+(?:is|are)\s+(?:shown|displayed))?$|"
    r"^(?:the\s+)?(?P<y>.+?)\s+(?:is|are)\s+not\s+(?:shown|displayed|visible)",
    re.I,
)


def parse_assertion(expected: str | None) -> Assertion | None:
    """Best-effort structured form of a plain-English expectation. Returns None when unsure:
    an expectation without a structured form can only be judged by the host, never independently."""
    text = (expected or "").strip().rstrip(".")
    if not text:
        return None
    text = re.sub(r"^(?:verify|assert|ensure|confirm|expect|check)\s+(?:that\s+)?", "", text, flags=re.I)
    if m := _URL.search(text):
        return Assertion(kind=AssertionKind.URL_CONTAINS, value=m.group(1).strip("'\""), inferred_from_text=True)
    if m := _TITLE.search(text):
        return Assertion(kind=AssertionKind.TITLE_CONTAINS, value=m.group(1).strip(" '\""), inferred_from_text=True)
    if m := _ABSENT.match(text):
        val = (m.group("x") or m.group("y") or "").strip()
        q = _QUOTED.search(val)
        if val:
            return Assertion(kind=AssertionKind.TEXT_ABSENT, value=q.group(1) if q else val, inferred_from_text=True)
    if q := _QUOTED.search(text):
        return Assertion(kind=AssertionKind.TEXT_VISIBLE, value=q.group(1), inferred_from_text=True)
    if m := _SHOWN.match(text):
        val = (m.group("x") or m.group("y") or "").strip()
        val = re.sub(r"^(?:a|an|the)\s+", "", val, flags=re.I)
        if val and len(val) <= 200:
            return Assertion(kind=AssertionKind.TEXT_VISIBLE, value=val, inferred_from_text=True)
    return None


def assertion_for_step(step: TestStep) -> Assertion | None:
    """The structured assertion a step carries, or one parsed from its expectation."""
    if step.assertion is not None:
        return step.assertion
    if step.action in (StepAction.ASSERT, StepAction.OBSERVE, StepAction.WAIT) and step.expected:
        return parse_assertion(step.expected)
    return None
