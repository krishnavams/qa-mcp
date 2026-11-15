"""Engine 20 — Autonomous Regression Brain.

Change analysis uses read-only git; dependencies come from a static import
graph of the repository plus the POM↔screen links in the knowledge base;
history comes from ingested results. Selection is a recommendation — tests
are run only through ``run_existing_tests`` / ``execute_test_plan``.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from coded_autonomous_qa.adapters.code_index import ImportGraph, build_import_graph, is_test_file, name_similarity
from coded_autonomous_qa.adapters.git_adapter import GitAdapter, GitError
from coded_autonomous_qa.engines.context import EngineContext, result
from coded_autonomous_qa.engines.execution import ExecutionEngine
from coded_autonomous_qa.knowledge.models import ChangeSet, CodeDependency, ExecutionState, utcnow
from coded_autonomous_qa.safety.guards import SafetyError, data_path, resolve_readable_path

CRITICAL_PATHS = re.compile(
    r"(auth|login|session|payment|pay|transfer|loan|account|kyc|security|permission|"
    r"settings|config|migration|schema|checkout|pricing|interest)",
    re.I,
)


class RegressionEngine:
    def __init__(self, ctx: EngineContext, execution: ExecutionEngine) -> None:
        self.ctx = ctx
        self.execution = execution

    def _repo(self, repo_path: str) -> tuple[GitAdapter, Path]:
        root = resolve_readable_path(repo_path, self.ctx.settings)
        try:
            git = GitAdapter(root)
            top = git.toplevel()
        except GitError as exc:
            raise SafetyError(f"Not a usable git repository: {exc}", code="git_error") from exc
        resolve_readable_path(top, self.ctx.settings)
        return GitAdapter(top), top

    # ------------------------------------------------------------------
    def analyze_git_changes(self, repo_path: str, base: str = "HEAD~1", head: str | None = "HEAD") -> dict[str, Any]:
        git, top = self._repo(repo_path)
        try:
            files = git.changed_files(base, head)
            commits = git.commits(base, head) if head else []
        except GitError as exc:
            return result(ExecutionState.FAILED, f"git failed: {exc}")
        cs = self.ctx.repo.save_change_set(
            ChangeSet(repo_path=str(top), base=base, head=head or "WORKTREE", files=files, commits=commits)
        )
        areas: dict[str, int] = {}
        for f in files:
            area = f["path"].split("/")[0] if "/" in f["path"] else "(root)"
            areas[area] = areas.get(area, 0) + f["added"] + f["deleted"]
        return result(
            ExecutionState.ANALYZED,
            f"{len(files)} file(s) changed between {base} and {head or 'working tree'}.",
            change_id=cs.id,
            files=files,
            commits=commits[:50],
            churn_by_area=areas,
            tests_changed=[f["path"] for f in files if is_test_file(f["path"])],
        )

    def map_test_dependencies(self, repo_path: str) -> dict[str, Any]:
        _, top = self._repo(repo_path)
        graph = build_import_graph(top)
        repo_key = str(top)
        deps: list[CodeDependency] = []
        for src, targets in graph.imports.items():
            for t in targets:
                deps.append(
                    CodeDependency(
                        source=src,
                        source_type="test" if is_test_file(src) else "module",
                        target=t,
                        target_type="module",
                        relation="imports",
                    )
                )
        poms = self.ctx.repo.list_poms()
        pom_by_file: dict[str, list] = {}
        for p in poms:
            path = Path(p.file_path)
            if path.is_relative_to(top):
                pom_by_file.setdefault(str(path.relative_to(top)), []).append(p)
        tests = [f for f in graph.imports if is_test_file(f)]
        for t in tests:
            reach = graph.transitive_imports(t)
            for mod, depth in reach.items():
                if depth > 1:
                    deps.append(
                        CodeDependency(
                            source=t,
                            source_type="test",
                            target=mod,
                            target_type="module",
                            relation="transitively_imports",
                            evidence=f"depth {depth}",
                        )
                    )
                for p in pom_by_file.get(mod, []):
                    if p.class_name in graph.names.get(t, set()) or depth <= 2:
                        deps.append(
                            CodeDependency(
                                source=t,
                                source_type="test",
                                target=p.class_name,
                                target_type="pom",
                                relation="uses_pom",
                                evidence=mod,
                            )
                        )
        for p in poms:
            if p.screen_id:
                deps.append(
                    CodeDependency(
                        source=p.class_name,
                        source_type="pom",
                        target=p.screen_id,
                        target_type="screen",
                        relation="covers_screen",
                        evidence=f"name match {p.match_confidence}",
                    )
                )
        n = self.ctx.repo.replace_dependencies(repo_key, deps)
        return result(
            ExecutionState.ANALYZED,
            f"Mapped {len(graph.imports)} Python file(s), {len(tests)} test file(s);"
            f" {n} dependency record(s) stored." + (" Graph truncated (file limit)." if graph.truncated else ""),
            repo=repo_key,
            test_files=len(tests),
            relations={
                r: sum(d.relation == r for d in deps)
                for r in ("imports", "transitively_imports", "uses_pom", "covers_screen")
            },
        )

    def _graph(self, repo: str) -> ImportGraph:
        g = ImportGraph()
        for d in self.ctx.repo.list_dependencies(repo, relation="imports"):
            g.imports.setdefault(d.source, set()).add(d.target)
        return g

    # ------------------------------------------------------------------
    def identify_impacted_components(self, change_id: str) -> dict[str, Any]:
        cs = self.ctx.repo.get_change_set(change_id)
        g = self._graph(cs.repo_path)
        if not g.imports:
            return result(
                ExecutionState.NOT_CONFIGURED,
                "No dependency map for this repository; run map_test_dependencies first.",
                change_id=change_id,
            )
        changed = [f["path"] for f in cs.files]
        old_paths = [f["old_path"] for f in cs.files if f.get("old_path")]
        impact = g.impact([*changed, *old_paths])
        modules = {p: d for p, d in impact.items() if not is_test_file(p)}
        tests = {p: d for p, d in impact.items() if is_test_file(p)}
        pom_deps = self.ctx.repo.list_dependencies(cs.repo_path, relation="uses_pom")
        poms_hit: dict[str, str] = {}
        for p in self.ctx.repo.list_poms():
            path = Path(p.file_path)
            rel = str(path.relative_to(cs.repo_path)) if path.is_relative_to(cs.repo_path) else None
            if rel and rel in impact:
                poms_hit[p.class_name] = (
                    f"{rel} {'changed' if impact[rel] == 0 else f'depends on change (depth {impact[rel]})'}"
                )
        for d in pom_deps:
            if d.target in poms_hit and d.source not in tests:
                tests[d.source] = 1
        screens: dict[str, dict[str, Any]] = {}
        for p in self.ctx.repo.list_poms():
            if p.class_name in poms_hit and p.screen_id:
                s = self.ctx.repo.get_screen(p.screen_id)
                screens[s.id] = {"screen": s.name, "via": f"POM {p.class_name}", "basis": "pom link"}
        all_screens = [s for a in self.ctx.repo.list_applications() for s in self.ctx.repo.list_screens(a.id)]
        for f in changed:
            if f.endswith((".py",)) and f in modules:
                continue
            for s in all_screens:
                if s.id not in screens and name_similarity(Path(f).stem, s.name) >= 0.5:
                    screens[s.id] = {"screen": s.name, "via": f, "basis": "inferred from file name"}
        ui_cases = []
        for a in self.ctx.repo.list_applications():
            for tc in self.ctx.repo.list_test_cases(a.id):
                if any(s_id for s_id in screens if (tc.start_url or "") == self.ctx.repo.get_screen(s_id).url):
                    ui_cases.append({"test_case_id": tc.id, "title": tc.title})
        return result(
            ExecutionState.ANALYZED,
            f"{len(modules)} module(s), {len(poms_hit)} POM(s), {len(screens)} screen(s) and "
            f"{len(tests)} test file(s) impacted.",
            change_id=change_id,
            modules=modules,
            poms=poms_hit,
            screens=screens,
            tests=tests,
            ui_test_cases=ui_cases[:100],
        )

    def calculate_regression_risk(self, change_id: str) -> dict[str, Any]:
        cs = self.ctx.repo.get_change_set(change_id)
        g = self._graph(cs.repo_path)
        dependents = g.dependents
        assoc = self.ctx.repo.failure_associations([f["path"] for f in cs.files])
        file_fail: dict[str, tuple[int, int]] = {}
        for per_file in assoc.values():
            for path, (fails, runs) in per_file.items():
                f0, r0 = file_fail.get(path, (0, 0))
                file_fail[path] = (f0 + fails, r0 + runs)
        scored = []
        for f in cs.files:
            path = f["path"]
            churn = f.get("added", 0) + f.get("deleted", 0)
            fails, runs = file_fail.get(path, (0, 0))
            factors = {
                "churn": round(min(25.0, 5 * math.log1p(churn)), 1),
                "fan_in": round(min(25.0, 5 * math.log1p(len(g.impact([path])) - 1)), 1),
                "critical_area": 20.0 if CRITICAL_PATHS.search(path) else 0.0,
                "failure_history": round(20.0 * fails / runs, 1) if runs else 0.0,
                "deleted_or_renamed": 10.0 if f["status"] in ("deleted", "renamed") else 0.0,
            }
            if is_test_file(path):
                factors = {k: v * 0.3 for k, v in factors.items()}
            score = round(min(100.0, sum(factors.values())), 1)
            scored.append(
                {"path": path, "score": score, "factors": factors, "direct_dependents": len(dependents.get(path, ()))}
            )
        scored.sort(key=lambda x: -x["score"])
        top = [s["score"] for s in scored[:5]]
        overall = round(max(top or [0]) * 0.6 + (sum(top) / len(top) if top else 0) * 0.4, 1)
        level = "critical" if overall >= 70 else "high" if overall >= 50 else "medium" if overall >= 25 else "low"
        return result(
            ExecutionState.ANALYZED,
            f"Regression risk {level} ({overall}/100).",
            change_id=change_id,
            overall=overall,
            level=level,
            files=scored,
            basis="churn, fan-in from the import graph, critical paths, recorded failure history",
        )

    def recommend_regression_tests(
        self, change_id: str, max_tests: int | None = None, time_budget_s: float | None = None
    ) -> dict[str, Any]:
        impacted = self.identify_impacted_components(change_id)
        if impacted["execution_state"] == ExecutionState.NOT_CONFIGURED.value:
            return impacted
        cs = self.ctx.repo.get_change_set(change_id)
        changed = [f["path"] for f in cs.files]
        assoc = self.ctx.repo.failure_associations(changed)
        history = {h.test_id: h for h in self.ctx.repo.list_test_history()}
        candidates: dict[str, dict[str, Any]] = {}

        def add(test: str, score: float, reason: str) -> None:
            c = candidates.setdefault(test, {"test": test, "score": 0.0, "reasons": []})
            c["score"] += score
            c["reasons"].append(reason)

        for test, depth in impacted["tests"].items():
            add(test, 50.0 / (1 + depth), "changed" if depth == 0 else f"depends on changed code (depth {depth})")
        for test_id, per_file in assoc.items():
            fails = sum(f for f, _ in per_file.values())
            runs = sum(r for _, r in per_file.values())
            if fails:
                add(
                    test_id.split("::")[0],
                    30.0 * fails / max(runs, 1),
                    f"failed {fails}/{runs} time(s) when these files changed",
                )
        for test in list(candidates):
            hs = [h for tid, h in history.items() if tid == test or tid.startswith(test + "::")]
            if hs:
                runs = sum(h.runs for h in hs)
                fails = sum(h.failures for h in hs)
                if fails:
                    candidates[test]["score"] += 15.0 * fails / max(runs, 1)
                    candidates[test]["reasons"].append(f"historical failure rate {fails}/{runs}")
                if any(h.last_status == "failed" for h in hs):
                    candidates[test]["score"] += 10.0
                    candidates[test]["reasons"].append("failed on its last run")
                if any(h.flips >= 2 for h in hs):
                    candidates[test]["reasons"].append("flaky (pass/fail flips)")
                candidates[test]["est_duration_s"] = round(sum(h.avg_duration_s for h in hs), 2)
            if CRITICAL_PATHS.search(test):
                candidates[test]["score"] += 5.0
                candidates[test]["reasons"].append("covers a critical area")
        ranked = sorted(candidates.values(), key=lambda c: (-c["score"], c.get("est_duration_s", 0), c["test"]))
        selected, budget = [], 0.0
        for c in ranked:
            if max_tests and len(selected) >= max_tests:
                break
            if time_budget_s is not None and budget + c.get("est_duration_s", 0) > time_budget_s and selected:
                continue
            budget += c.get("est_duration_s", 0)
            selected.append({**c, "score": round(c["score"], 1), "priority": len(selected) + 1})
        repo = cs.repo_path
        test_ids = [s["test"] for s in selected]
        return result(
            ExecutionState.PLANNED,
            f"{len(selected)} test(s) recommended in priority order (of {len(ranked)} candidate(s)); none executed. "
            "Run them with run_existing_tests(test_ids, change_id) so results feed the history.",
            change_id=change_id,
            repo=repo,
            recommended=selected,
            ui_test_cases=impacted["ui_test_cases"],
            run_with={"tool": "run_existing_tests", "arguments": {"test_ids": test_ids, "change_id": change_id}},
            estimated_duration_s=round(budget, 1),
        )

    def generate_regression_report(self, change_id: str) -> dict[str, Any]:
        cs = self.ctx.repo.get_change_set(change_id)
        risk = self.calculate_regression_risk(change_id)
        rec = self.recommend_regression_tests(change_id)
        runs = [r for r in self.ctx.repo.list_runs(limit=200) if r.change_id == change_id]
        lines = [
            f"# Regression report — {cs.base}..{cs.head}",
            "",
            f"- Repository: `{cs.repo_path}`",
            f"- Generated: {utcnow()}",
            f"- Files changed: {len(cs.files)}",
            f"- Risk: **{risk['level']}** ({risk['overall']}/100)",
            "",
            "## Highest-risk changes",
            "",
            "| File | Score | Factors |",
            "|---|---|---|",
        ]
        lines += [
            f"| `{f['path']}` | {f['score']} | {', '.join(f'{k}={v}' for k, v in f['factors'].items() if v)} |"
            for f in risk["files"][:20]
        ]
        lines += ["", "## Recommended tests", ""]
        if rec["execution_state"] == ExecutionState.NOT_CONFIGURED.value:
            lines.append(f"_{rec['message']}_")
        else:
            lines += [f"{t['priority']}. `{t['test']}` — {'; '.join(t['reasons'])}" for t in rec["recommended"]]
        lines += ["", "## Executions linked to this change", ""]
        if not runs:
            lines.append("_No test run has been executed for this change yet._")
        for r in runs:
            lines.append(
                f"- {r.id}: state **{r.state.value}**, outcome **{r.outcome or 'n/a'}** "
                f"{json.dumps(r.summary.get('counts', r.summary.get('step_counts', {})))}"
            )
        md = data_path(self.ctx.settings, "reports", f"regression_{change_id}.md")
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return result(
            ExecutionState.ANALYZED,
            "Regression report written.",
            report_path=str(md),
            markdown="\n".join(lines)[:20000],
            executed_runs=len(runs),
        )
