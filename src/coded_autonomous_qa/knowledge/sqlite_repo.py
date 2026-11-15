"""SQLite implementation of the knowledge repository.

Each table keeps the full pydantic record as JSON in ``data`` plus the columns
needed for lookups and uniqueness. Replacing the storage means implementing
:class:`coded_autonomous_qa.knowledge.repository.KnowledgeRepository` again;
no engine touches SQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from coded_autonomous_qa.knowledge.models import (
    Application,
    Approval,
    ApprovalStatus,
    BusinessRule,
    ChangeSet,
    CodeDependency,
    Component,
    Defect,
    Evidence,
    NavigationEdge,
    PomReference,
    Screen,
    StepResult,
    TestCase,
    TestHistoryEntry,
    TestPlan,
    TestRun,
    Workflow,
    utcnow,
)
from coded_autonomous_qa.safety.masking import mask_value

M = TypeVar("M", bound=BaseModel)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS screens (
  id TEXT PRIMARY KEY, app_id TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL,
  UNIQUE(app_id, fingerprint));
CREATE TABLE IF NOT EXISTS screen_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT, screen_id TEXT NOT NULL, label TEXT NOT NULL, data TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS components (
  id TEXT PRIMARY KEY, app_id TEXT NOT NULL, screen_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  data TEXT NOT NULL, UNIQUE(screen_id, kind, name));
CREATE TABLE IF NOT EXISTS navigation_edges (
  id TEXT PRIMARY KEY, app_id TEXT NOT NULL, from_screen TEXT NOT NULL, to_screen TEXT NOT NULL,
  action TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(from_screen, to_screen, action));
CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, app_id TEXT NOT NULL, name TEXT NOT NULL,
  data TEXT NOT NULL, UNIQUE(app_id, name));
CREATE TABLE IF NOT EXISTS exploration_runs (id TEXT PRIMARY KEY, app_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pom_references (
  id TEXT PRIMARY KEY, class_name TEXT NOT NULL, file_path TEXT NOT NULL, screen_id TEXT, data TEXT NOT NULL,
  UNIQUE(file_path, class_name));
CREATE TABLE IF NOT EXISTS business_rules (
  id TEXT PRIMARY KEY, app_id TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
  UNIQUE(app_id, key));
CREATE TABLE IF NOT EXISTS test_cases (id TEXT PRIMARY KEY, app_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_plans (id TEXT PRIMARY KEY, app_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_runs (id TEXT PRIMARY KEY, app_id TEXT, state TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS step_results (
  run_id TEXT NOT NULL, test_case_id TEXT NOT NULL, step_id TEXT NOT NULL, status TEXT NOT NULL,
  data TEXT NOT NULL, PRIMARY KEY(run_id, test_case_id, step_id));
CREATE TABLE IF NOT EXISTS defects (
  id TEXT PRIMARY KEY, app_id TEXT, signature TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_sets (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS code_dependencies (
  repo TEXT NOT NULL, source TEXT NOT NULL, source_type TEXT NOT NULL, target TEXT NOT NULL,
  target_type TEXT NOT NULL, relation TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(repo, source, target, relation));
CREATE TABLE IF NOT EXISTS test_history (test_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failure_associations (
  file_path TEXT NOT NULL, test_id TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
  runs INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(file_path, test_id));
CREATE TABLE IF NOT EXISTS evidence_index (
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  ref TEXT, sha256 TEXT, summary TEXT NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT NOT NULL, tier TEXT NOT NULL,
  arguments TEXT NOT NULL, outcome TEXT NOT NULL, execution_state TEXT, duration_ms INTEGER NOT NULL,
  error TEXT);
CREATE INDEX IF NOT EXISTS ix_components_app ON components(app_id);
CREATE INDEX IF NOT EXISTS ix_edges_app ON navigation_edges(app_id);
CREATE INDEX IF NOT EXISTS ix_rules_app ON business_rules(app_id, status);
CREATE INDEX IF NOT EXISTS ix_evidence_owner ON evidence_index(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS ix_deps_target ON code_dependencies(repo, target);
"""


class NotFound(KeyError):
    pass


def _dump(model: BaseModel) -> str:
    return model.model_dump_json()


class SQLiteKnowledgeRepository:
    """Thread-safe (one connection, one lock) SQLite store."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)  # runs in its own implicit transaction
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        if self.path != ":memory:":
            try:
                Path(self.path).chmod(0o600)
            except OSError:  # pragma: no cover
                pass

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def _one(self, model: type[M], sql: str, params: tuple) -> M:
        rows = self._rows(sql, params)
        if not rows:
            raise NotFound(f"{model.__name__} not found: {params[0] if params else ''}")
        return model.model_validate_json(rows[0]["data"])

    def _many(self, model: type[M], sql: str, params: tuple = ()) -> list[M]:
        return [model.model_validate_json(r["data"]) for r in self._rows(sql, params)]

    def _index_evidence(self, c: sqlite3.Connection, owner_type: str, owner_id: str, items: list[Evidence]) -> None:
        c.execute("DELETE FROM evidence_index WHERE owner_type=? AND owner_id=?", (owner_type, owner_id))
        for e in items:
            c.execute(
                "INSERT INTO evidence_index(owner_type, owner_id, kind, ref, sha256, summary, captured_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (owner_type, owner_id, e.kind.value, e.ref, e.sha256, e.summary, e.captured_at),
            )

    # --- applications ----------------------------------------------------------
    def save_application(self, app: Application) -> Application:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO applications(id, name, data) VALUES(?,?,?)", (app.id, app.name, _dump(app))
            )
        return app

    def get_application(self, app_id: str) -> Application:
        return self._one(Application, "SELECT data FROM applications WHERE id=? OR name=?", (app_id, app_id))

    def list_applications(self) -> list[Application]:
        return self._many(Application, "SELECT data FROM applications ORDER BY name")

    # --- screens / components / navigation ----------------------------------------
    def find_screen_by_fingerprint(self, app_id: str, fingerprint: str) -> Screen | None:
        rows = self._rows("SELECT data FROM screens WHERE app_id=? AND fingerprint=?", (app_id, fingerprint))
        return Screen.model_validate_json(rows[0]["data"]) if rows else None

    def save_screen(self, screen: Screen, components: list[Component] | None = None) -> Screen:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO screens(id, app_id, fingerprint, data) VALUES(?,?,?,?)",
                (screen.id, screen.app_id, screen.fingerprint, _dump(screen)),
            )
            if components is not None:
                c.execute("DELETE FROM components WHERE screen_id=?", (screen.id,))
                for comp in components:
                    c.execute(
                        "INSERT OR REPLACE INTO components(id, app_id, screen_id, kind, name, data) "
                        "VALUES(?,?,?,?,?,?)",
                        (comp.id, comp.app_id, comp.screen_id, comp.kind, comp.name, _dump(comp)),
                    )
        return screen

    def get_screen(self, screen_id: str) -> Screen:
        return self._one(Screen, "SELECT data FROM screens WHERE id=?", (screen_id,))

    def list_screens(self, app_id: str) -> list[Screen]:
        return self._many(Screen, "SELECT data FROM screens WHERE app_id=? ORDER BY rowid", (app_id,))

    def list_components(self, app_id: str, screen_id: str | None = None) -> list[Component]:
        if screen_id:
            return self._many(Component, "SELECT data FROM components WHERE screen_id=? ORDER BY rowid", (screen_id,))
        return self._many(Component, "SELECT data FROM components WHERE app_id=? ORDER BY rowid", (app_id,))

    def save_screen_state(self, screen_id: str, label: str, data: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO screen_states(screen_id, label, data, created_at) VALUES(?,?,?,?)",
                (screen_id, label, json.dumps(data, default=str), utcnow()),
            )

    def list_screen_states(self, screen_id: str) -> list[dict[str, Any]]:
        return [
            {"label": r["label"], "created_at": r["created_at"], **json.loads(r["data"])}
            for r in self._rows("SELECT * FROM screen_states WHERE screen_id=? ORDER BY id", (screen_id,))
        ]

    def save_edge(self, edge: NavigationEdge) -> NavigationEdge:
        with self._tx() as c:
            existing = list(
                c.execute(
                    "SELECT data FROM navigation_edges WHERE from_screen=? AND to_screen=? AND action=?",
                    (edge.from_screen_id, edge.to_screen_id, edge.action),
                )
            )
            if existing:
                old = NavigationEdge.model_validate_json(existing[0]["data"])
                edge = old.model_copy(update={"evidence": [*old.evidence, *edge.evidence][-10:]})
            c.execute(
                "INSERT OR REPLACE INTO navigation_edges(id, app_id, from_screen, to_screen, action, data) "
                "VALUES(?,?,?,?,?,?)",
                (edge.id, edge.app_id, edge.from_screen_id, edge.to_screen_id, edge.action, _dump(edge)),
            )
        return edge

    def list_edges(self, app_id: str) -> list[NavigationEdge]:
        return self._many(NavigationEdge, "SELECT data FROM navigation_edges WHERE app_id=? ORDER BY rowid", (app_id,))

    def save_workflow(self, wf: Workflow) -> Workflow:
        with self._tx() as c:
            c.execute("DELETE FROM workflows WHERE app_id=? AND name=?", (wf.app_id, wf.name))
            c.execute(
                "INSERT INTO workflows(id, app_id, name, data) VALUES(?,?,?,?)", (wf.id, wf.app_id, wf.name, _dump(wf))
            )
        return wf

    def list_workflows(self, app_id: str) -> list[Workflow]:
        return self._many(Workflow, "SELECT data FROM workflows WHERE app_id=? ORDER BY name", (app_id,))

    # --- exploration runs (free-form progress state) ------------------------------
    def save_exploration_run(self, run_id: str, app_id: str, data: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO exploration_runs(id, app_id, data) VALUES(?,?,?)",
                (run_id, app_id, json.dumps(data, default=str)),
            )

    def get_exploration_run(self, run_id: str) -> dict[str, Any]:
        rows = self._rows("SELECT data FROM exploration_runs WHERE id=?", (run_id,))
        if not rows:
            raise NotFound(f"exploration run not found: {run_id}")
        return json.loads(rows[0]["data"])

    # --- POM references ---------------------------------------------------------
    def save_pom(self, pom: PomReference) -> PomReference:
        with self._tx() as c:
            rows = list(
                c.execute(
                    "SELECT id FROM pom_references WHERE file_path=? AND class_name=?", (pom.file_path, pom.class_name)
                )
            )
            if rows:
                pom = pom.model_copy(update={"id": rows[0]["id"]})
            c.execute(
                "INSERT OR REPLACE INTO pom_references(id, class_name, file_path, screen_id, data) VALUES(?,?,?,?,?)",
                (pom.id, pom.class_name, pom.file_path, pom.screen_id, _dump(pom)),
            )
        return pom

    def list_poms(self) -> list[PomReference]:
        return self._many(PomReference, "SELECT data FROM pom_references ORDER BY class_name")

    # --- business rules ----------------------------------------------------------
    def upsert_rule(self, rule: BusinessRule) -> tuple[BusinessRule, bool]:
        """Insert, or merge into the existing rule with the same key. Returns (rule, created).

        Merging never downgrades: a verified/refuted rule keeps its status; evidence accumulates.
        """
        with self._tx() as c:
            rows = list(c.execute("SELECT data FROM business_rules WHERE app_id=? AND key=?", (rule.app_id, rule.key)))
            created = not rows
            if rows:
                old = BusinessRule.model_validate_json(rows[0]["data"])
                rank = {"inferred": 0, "observed": 1, "verified": 2, "refuted": 2, "deprecated": 3}
                status = old.status if rank[old.status.value] >= rank[rule.status.value] else rule.status
                seen = {(e.kind, e.summary, e.ref) for e in old.evidence}
                evidence = [*old.evidence, *(e for e in rule.evidence if (e.kind, e.summary, e.ref) not in seen)]
                rule = old.model_copy(
                    update={
                        "status": status,
                        "confidence": max(old.confidence, rule.confidence),
                        "evidence": evidence[-25:],
                        "condition": {**old.condition, **rule.condition},
                        "updated_at": utcnow(),
                    }
                )
                BusinessRule.model_validate(rule.model_dump())
            c.execute(
                "INSERT OR REPLACE INTO business_rules(id, app_id, key, status, data) VALUES(?,?,?,?,?)",
                (rule.id, rule.app_id, rule.key, rule.status.value, _dump(rule)),
            )
            self._index_evidence(c, "rule", rule.id, rule.evidence)
        return rule, created

    def save_rule(self, rule: BusinessRule) -> BusinessRule:
        BusinessRule.model_validate(rule.model_dump())
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO business_rules(id, app_id, key, status, data) VALUES(?,?,?,?,?)",
                (rule.id, rule.app_id, rule.key, rule.status.value, _dump(rule)),
            )
            self._index_evidence(c, "rule", rule.id, rule.evidence)
        return rule

    def get_rule(self, rule_id: str) -> BusinessRule:
        return self._one(BusinessRule, "SELECT data FROM business_rules WHERE id=?", (rule_id,))

    def list_rules(self, app_id: str, status: str | None = None) -> list[BusinessRule]:
        if status:
            return self._many(
                BusinessRule,
                "SELECT data FROM business_rules WHERE app_id=? AND status=? ORDER BY rowid",
                (app_id, status),
            )
        return self._many(BusinessRule, "SELECT data FROM business_rules WHERE app_id=? ORDER BY rowid", (app_id,))

    # --- tests / plans / runs ----------------------------------------------------
    def save_test_case(self, tc: TestCase) -> TestCase:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO test_cases(id, app_id, data) VALUES(?,?,?)", (tc.id, tc.app_id, _dump(tc))
            )
        return tc

    def get_test_case(self, tc_id: str) -> TestCase:
        return self._one(TestCase, "SELECT data FROM test_cases WHERE id=?", (tc_id,))

    def list_test_cases(self, app_id: str) -> list[TestCase]:
        return self._many(TestCase, "SELECT data FROM test_cases WHERE app_id=? ORDER BY rowid", (app_id,))

    def save_plan(self, plan: TestPlan) -> TestPlan:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO test_plans(id, app_id, data) VALUES(?,?,?)", (plan.id, plan.app_id, _dump(plan))
            )
        return plan

    def get_plan(self, plan_id: str) -> TestPlan:
        return self._one(TestPlan, "SELECT data FROM test_plans WHERE id=?", (plan_id,))

    def save_run(self, run: TestRun) -> TestRun:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO test_runs(id, app_id, state, data) VALUES(?,?,?,?)",
                (run.id, run.app_id, run.state.value, _dump(run)),
            )
        return run

    def get_run(self, run_id: str) -> TestRun:
        return self._one(TestRun, "SELECT data FROM test_runs WHERE id=?", (run_id,))

    def list_runs(self, app_id: str | None = None, limit: int = 50) -> list[TestRun]:
        if app_id:
            return self._many(
                TestRun, "SELECT data FROM test_runs WHERE app_id=? ORDER BY rowid DESC LIMIT ?", (app_id, limit)
            )
        return self._many(TestRun, "SELECT data FROM test_runs ORDER BY rowid DESC LIMIT ?", (limit,))

    def save_step_result(self, result: StepResult) -> StepResult:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO step_results(run_id, test_case_id, step_id, status, data) VALUES(?,?,?,?,?)",
                (result.run_id, result.test_case_id, result.step_id, result.status.value, _dump(result)),
            )
            if result.evidence:
                self._index_evidence(c, "step", f"{result.run_id}:{result.step_id}", result.evidence)
        return result

    def list_step_results(self, run_id: str) -> list[StepResult]:
        return self._many(StepResult, "SELECT data FROM step_results WHERE run_id=? ORDER BY rowid", (run_id,))

    # --- defects ------------------------------------------------------------------
    def save_defect(self, defect: Defect) -> Defect:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO defects(id, app_id, signature, status, data) VALUES(?,?,?,?,?)",
                (defect.id, defect.app_id, defect.signature, defect.status.value, _dump(defect)),
            )
            self._index_evidence(c, "defect", defect.id, defect.evidence)
        return defect

    def get_defect(self, defect_id: str) -> Defect:
        return self._one(Defect, "SELECT data FROM defects WHERE id=?", (defect_id,))

    def find_defects_by_signature(self, signature: str) -> list[Defect]:
        return self._many(Defect, "SELECT data FROM defects WHERE signature=? ORDER BY rowid", (signature,))

    def list_defects(self, app_id: str | None = None) -> list[Defect]:
        if app_id:
            return self._many(Defect, "SELECT data FROM defects WHERE app_id=? ORDER BY rowid DESC", (app_id,))
        return self._many(Defect, "SELECT data FROM defects ORDER BY rowid DESC")

    # --- approvals ----------------------------------------------------------------
    def save_approval(self, approval: Approval) -> Approval:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO approvals(id, subject_id, status, data) VALUES(?,?,?,?)",
                (approval.id, approval.subject_id, approval.status.value, _dump(approval)),
            )
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        return self._one(Approval, "SELECT data FROM approvals WHERE id=?", (approval_id,))

    def list_approvals(self, status: ApprovalStatus | None = None) -> list[Approval]:
        if status:
            return self._many(
                Approval, "SELECT data FROM approvals WHERE status=? ORDER BY rowid DESC", (status.value,)
            )
        return self._many(Approval, "SELECT data FROM approvals ORDER BY rowid DESC")

    # --- regression ---------------------------------------------------------------
    def save_change_set(self, cs: ChangeSet) -> ChangeSet:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO change_sets(id, data) VALUES(?,?)", (cs.id, _dump(cs)))
        return cs

    def get_change_set(self, change_id: str) -> ChangeSet:
        return self._one(ChangeSet, "SELECT data FROM change_sets WHERE id=?", (change_id,))

    def replace_dependencies(self, repo: str, deps: list[CodeDependency], relations: list[str] | None = None) -> int:
        with self._tx() as c:
            if relations:
                c.executemany(
                    "DELETE FROM code_dependencies WHERE repo=? AND relation=?", [(repo, r) for r in relations]
                )
            else:
                c.execute("DELETE FROM code_dependencies WHERE repo=?", (repo,))
            c.executemany(
                "INSERT OR REPLACE INTO code_dependencies(repo, source, source_type, target, target_type, relation, "
                "evidence) VALUES(?,?,?,?,?,?,?)",
                [(repo, d.source, d.source_type, d.target, d.target_type, d.relation, d.evidence) for d in deps],
            )
        return len(deps)

    def list_dependencies(
        self,
        repo: str | None = None,
        *,
        target: str | None = None,
        source: str | None = None,
        relation: str | None = None,
    ) -> list[CodeDependency]:
        sql, params = "SELECT * FROM code_dependencies WHERE 1=1", []
        for col, val in (("repo", repo), ("target", target), ("source", source), ("relation", relation)):
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        return [
            CodeDependency(
                source=r["source"],
                source_type=r["source_type"],
                target=r["target"],
                target_type=r["target_type"],
                relation=r["relation"],
                evidence=r["evidence"],
            )
            for r in self._rows(sql, tuple(params))
        ]

    def get_test_history(self, test_id: str) -> TestHistoryEntry:
        rows = self._rows("SELECT data FROM test_history WHERE test_id=?", (test_id,))
        return TestHistoryEntry.model_validate_json(rows[0]["data"]) if rows else TestHistoryEntry(test_id=test_id)

    def save_test_history(self, entry: TestHistoryEntry) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO test_history(test_id, data) VALUES(?,?)", (entry.test_id, _dump(entry)))

    def list_test_history(self) -> list[TestHistoryEntry]:
        return self._many(TestHistoryEntry, "SELECT data FROM test_history ORDER BY test_id")

    def record_failure_association(self, file_path: str, test_id: str, failed: bool) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO failure_associations(file_path, test_id, failures, runs) VALUES(?,?,?,1) "
                "ON CONFLICT(file_path, test_id) DO UPDATE SET failures=failures+excluded.failures, runs=runs+1",
                (file_path, test_id, 1 if failed else 0),
            )

    def failure_associations(self, file_paths: list[str]) -> dict[str, dict[str, tuple[int, int]]]:
        """{test_id: {file_path: (failures, runs)}} for the given changed files."""
        out: dict[str, dict[str, tuple[int, int]]] = {}
        if not file_paths:
            return out
        marks = ",".join("?" * len(file_paths))
        for r in self._rows(f"SELECT * FROM failure_associations WHERE file_path IN ({marks})", tuple(file_paths)):
            out.setdefault(r["test_id"], {})[r["file_path"]] = (r["failures"], r["runs"])
        return out

    # --- evidence / audit ---------------------------------------------------------
    def list_evidence(self, owner_type: str, owner_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._rows(
                "SELECT kind, ref, sha256, summary, captured_at FROM evidence_index WHERE owner_type=? AND owner_id=?",
                (owner_type, owner_id),
            )
        ]

    def write_audit(
        self,
        *,
        tool: str,
        tier: str,
        arguments: dict[str, Any],
        outcome: str,
        execution_state: str | None,
        duration_ms: int,
        error: str | None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO audit_log(ts, tool, tier, arguments, outcome, execution_state, duration_ms, error) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    utcnow(),
                    tool,
                    tier,
                    json.dumps(mask_value(arguments), default=str)[:20000],
                    outcome,
                    execution_state,
                    duration_ms,
                    error,
                ),
            )

    def list_audit(self, limit: int = 100, tool: str | None = None) -> list[dict[str, Any]]:
        if tool:
            rows = self._rows("SELECT * FROM audit_log WHERE tool=? ORDER BY id DESC LIMIT ?", (tool, limit))
        else:
            rows = self._rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        tables = [
            "applications",
            "screens",
            "components",
            "navigation_edges",
            "workflows",
            "pom_references",
            "business_rules",
            "test_cases",
            "test_plans",
            "test_runs",
            "defects",
            "approvals",
            "change_sets",
            "code_dependencies",
            "test_history",
            "evidence_index",
            "audit_log",
        ]
        return {t: self._rows(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in tables}
