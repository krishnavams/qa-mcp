"""Existing-framework adapters: skills, test results, read-only application databases."""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Skills (Automation Script Generator, POM generator)
# ---------------------------------------------------------------------------
SKILL_DIR_NAMES = (".idfc/skills", ".coded/skills", ".claude/skills", "skills")


@dataclass
class SkillInfo:
    name: str
    path: str | None
    found: bool
    description: str | None = None


def locate_skill(name: str, roots: list[Path]) -> SkillInfo:
    """Find ``<root>/<skills dir>/<name>/SKILL.md`` under the allowed roots (and the Coded user dir)."""
    candidates = [r / d / name / "SKILL.md" for r in roots for d in SKILL_DIR_NAMES]
    candidates.append(Path.home() / ".config" / "coded" / "skills" / name / "SKILL.md")
    for path in candidates:
        if path.is_file():
            desc = None
            text = path.read_text(encoding="utf-8", errors="replace")[:4000]
            m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
            if m:
                desc = m.group(1).strip()
            return SkillInfo(name=name, path=str(path), found=True, description=desc)
    return SkillInfo(name=name, path=None, found=False)


# ---------------------------------------------------------------------------
# Test results (JUnit XML — emitted by pytest --junitxml and most runners)
# ---------------------------------------------------------------------------
MAX_RESULT_BYTES = 50_000_000


@dataclass
class TestOutcome:
    test_id: str
    file: str | None
    status: str  # passed | failed | error | skipped
    duration_s: float
    message: str = ""
    details: str = ""


@dataclass
class ResultSummary:
    outcomes: list[TestOutcome] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


class ResultParseError(Exception):
    pass


def parse_junit_xml(path: Path) -> ResultSummary:
    if path.stat().st_size > MAX_RESULT_BYTES:
        raise ResultParseError("Result file too large.")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if "<!DOCTYPE" in raw or "<!ENTITY" in raw:
        raise ResultParseError("DTD/entity declarations are not accepted in result files.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ResultParseError(f"Not valid JUnit XML: {exc}") from exc
    summary = ResultSummary()
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "?")
        file = case.get("file")
        if not file and classname:
            file = classname.replace(".", "/") + ".py"
        test_id = f"{file}::{name}" if file else f"{classname}::{name}"
        status, message, details = "passed", "", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                status = "failed" if tag == "failure" else tag
                message = el.get("message", "") or ""
                details = (el.text or "")[:8000]
                break
        try:
            duration = float(case.get("time", "0") or 0)
        except ValueError:
            duration = 0.0
        summary.outcomes.append(
            TestOutcome(
                test_id=test_id, file=file, status=status, duration_s=duration, message=message[:2000], details=details
            )
        )
    return summary


# ---------------------------------------------------------------------------
# Read-only application database (SQLite)
# ---------------------------------------------------------------------------
class ReadOnlyQueryError(Exception):
    pass


_ALLOWED_START = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


def run_readonly_query(db_path: Path, sql: str, params: list | None = None, max_rows: int = 200) -> dict:
    """Execute a single SELECT against a SQLite file opened with ``mode=ro`` plus an authorizer."""
    if not _ALLOWED_START.match(sql) or ";" in sql.strip().rstrip(";"):
        raise ReadOnlyQueryError("Only a single SELECT/WITH statement is allowed.")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

        def authorizer(action: int, *_: object) -> int:
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        cur = conn.execute(sql, params or [])
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
        return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}
    except sqlite3.DatabaseError as exc:
        raise ReadOnlyQueryError(str(exc)) from exc
    finally:
        conn.close()
