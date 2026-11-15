from __future__ import annotations

import pytest
from pydantic import ValidationError

from coded_autonomous_qa.knowledge import SQLiteKnowledgeRepository
from coded_autonomous_qa.knowledge.models import (
    BusinessRule,
    Evidence,
    EvidenceKind,
    RuleStatus,
    RuleType,
    VerificationLevel,
)


def execution(level):
    return Evidence(kind=EvidenceKind.EXECUTION, summary="probe assertion evaluated", verification=level)


def rule(status=RuleStatus.OBSERVED, evidence=None, source="observation", **kw):
    return BusinessRule(
        app_id="app_1",
        rule_type=RuleType.MANDATORY,
        statement="Email is mandatory",
        status=status,
        confidence=0.7,
        source=source,
        key=kw.pop("key", "k1"),
        evidence=evidence or [Evidence(kind=EvidenceKind.OBSERVATION, summary="required attr")],
        **kw,
    )


class TestRuleTruthfulness:
    def test_verified_requires_execution_evidence(self):
        with pytest.raises(ValidationError, match="execution evidence"):
            rule(RuleStatus.VERIFIED)
        with pytest.raises(ValidationError, match="independently verified"):
            rule(RuleStatus.VERIFIED, evidence=[execution(VerificationLevel.HOST_REPORTED)])
        ok = rule(RuleStatus.VERIFIED, evidence=[execution(VerificationLevel.INDEPENDENTLY_VERIFIED)])
        assert ok.status is RuleStatus.VERIFIED

    def test_ai_rules_cannot_be_observed(self):
        with pytest.raises(ValidationError):
            rule(RuleStatus.OBSERVED, evidence=[Evidence(kind=EvidenceKind.AI_INFERENCE, summary="guess")], source="ai")

    def test_rule_needs_evidence(self):
        with pytest.raises(ValidationError):
            BusinessRule(
                app_id="a",
                rule_type=RuleType.CUSTOM,
                statement="xyz",
                confidence=0.1,
                source="human",
                key="k",
                evidence=[],
            )


class TestRepository:
    def test_upsert_merges_and_never_downgrades(self, tmp_path):
        repo = SQLiteKnowledgeRepository(tmp_path / "kb.db")
        verified = rule(RuleStatus.VERIFIED, evidence=[execution(VerificationLevel.INDEPENDENTLY_VERIFIED)])
        repo.upsert_rule(verified)
        merged, created = repo.upsert_rule(rule(RuleStatus.OBSERVED))
        assert not created
        assert merged.status is RuleStatus.VERIFIED
        assert {e.kind for e in merged.evidence} == {EvidenceKind.EXECUTION, EvidenceKind.OBSERVATION}
        assert len(repo.list_evidence("rule", merged.id)) == 2

    def test_audit_is_masked(self, tmp_path):
        repo = SQLiteKnowledgeRepository(tmp_path / "kb.db")
        repo.write_audit(
            tool="t",
            tier="discovery",
            arguments={"password": "pw", "note": "mail a@b.com"},
            outcome="ok",
            execution_state="analyzed",
            duration_ms=1,
            error=None,
        )
        entry = repo.list_audit(1)[0]
        assert 'pw"' not in entry["arguments"] and "a@b.com" not in entry["arguments"]

    def test_file_permissions(self, tmp_path):
        SQLiteKnowledgeRepository(tmp_path / "kb.db")
        assert oct((tmp_path / "kb.db").stat().st_mode)[-3:] == "600"
