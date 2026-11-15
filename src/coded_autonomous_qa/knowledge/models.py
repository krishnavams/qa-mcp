"""Domain models (pydantic). These are both the MCP tool schemas and the stored records.

Truthfulness is encoded in the types:

* :class:`ExecutionState` says exactly how far a request got. Only a report
  from the host (``record_*`` / ``ingest_*`` tools) moves anything past
  ``awaiting_host_execution``.
* :class:`BusinessRule` cannot be ``verified`` without execution evidence, and
  an AI-only rule is always ``inferred``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class ExecutionState(StrEnum):
    PLANNED = "planned"  # only a plan exists
    AWAITING_HOST_EXECUTION = "awaiting_host_execution"  # action requests handed to the host
    IN_PROGRESS = "in_progress"  # some host results recorded
    RECORDED = "recorded"  # host-reported data stored (not re-verified)
    EXECUTED = "executed"  # all steps reported by the host
    VERIFIED = "verified"  # confirmed by executed evidence
    ANALYZED = "analyzed"  # pure analysis over stored data
    REQUIRES_APPROVAL = "requires_approval"
    BLOCKED = "blocked"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
class EvidenceKind(StrEnum):
    OBSERVATION = "observation"  # host tool reported what it saw
    SCREENSHOT = "screenshot"
    EXECUTION = "execution"  # an executed step/test produced this
    LOG = "log"
    SOURCE_CODE = "source_code"
    TEST_RESULT = "test_result"
    HUMAN = "human"
    AI_INFERENCE = "ai_inference"  # reasoning only — never sufficient for verification


class Evidence(Strict):
    kind: EvidenceKind
    summary: str = Field(max_length=4000)
    ref: str | None = Field(default=None, description="File path, URI or record id the evidence points at.")
    sha256: str | None = None
    source_tool: str | None = Field(default=None, description="Host tool that produced it, e.g. 'browser'.")
    run_id: str | None = None
    captured_at: str = Field(default_factory=utcnow)
    file_verified: bool = Field(default=False, description="True when the server hashed the referenced file.")


# ---------------------------------------------------------------------------
# Applications, screens, components
# ---------------------------------------------------------------------------
class Application(Strict):
    id: str = Field(default_factory=lambda: new_id("app"))
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    environment: str
    description: str = ""
    created_at: str = Field(default_factory=utcnow)


class FieldObservation(Strict):
    """A field as reported by a host vision/DOM tool."""

    name: str = Field(min_length=1, max_length=200, description="Stable name, e.g. the input name or label.")
    label: str | None = None
    type: str = Field(
        default="text",
        description="text, email, number, date, select, checkbox, radio, password, textarea, file, tel, url, ...",
    )
    required: bool | None = Field(default=None, description="Required attribute or explicit required marker.")
    required_marker: bool = Field(default=False, description="A visible '*' or 'required' marker was seen.")
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    options: list[str] = Field(default_factory=list)
    placeholder: str | None = None
    default_value: str | None = None
    enabled: bool = True
    visible: bool = True
    selector: str | None = Field(default=None, description="CSS/Playwright selector if the host knows one.")
    vision_description: str | None = Field(default=None, description="Natural-language locator for vision tools.")
    validation_messages: list[str] = Field(default_factory=list)


class ControlObservation(Strict):
    label: str = Field(min_length=1, max_length=300)
    kind: str = Field(default="button", description="button, link, tab, menu_item, icon, ...")
    selector: str | None = None
    href: str | None = None
    vision_description: str | None = None
    enabled: bool = True


class ScreenObservation(Strict):
    """What a host tool saw on one screen. Fill it from Coded's browser/vision tools."""

    url: str
    title: str | None = None
    screen_name: str | None = Field(default=None, description="Human name, e.g. 'Login'.")
    headings: list[str] = Field(default_factory=list)
    fields: list[FieldObservation] = Field(default_factory=list)
    controls: list[ControlObservation] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list, description="Visible error/info/success messages.")
    visible_text_excerpt: str | None = Field(default=None, max_length=8000)
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    source_tool: str = Field(description="Host tool(s) that produced this, e.g. 'browser+view_image'.")


class Screen(Strict):
    id: str = Field(default_factory=lambda: new_id("scr"))
    app_id: str
    name: str
    url: str
    fingerprint: str
    title: str | None = None
    observation_count: int = 1
    last_observation: ScreenObservation
    first_seen: str = Field(default_factory=utcnow)
    last_seen: str = Field(default_factory=utcnow)


class Component(Strict):
    id: str = Field(default_factory=lambda: new_id("cmp"))
    app_id: str
    screen_id: str
    kind: str  # field | control
    name: str
    data: dict[str, Any]  # FieldObservation / ControlObservation dump


class NavigationEdge(Strict):
    id: str = Field(default_factory=lambda: new_id("nav"))
    app_id: str
    from_screen_id: str
    to_screen_id: str
    action: str = Field(description="What was done, e.g. \"click 'Sign in'\".")
    evidence: list[Evidence] = Field(default_factory=list)


class Workflow(Strict):
    id: str = Field(default_factory=lambda: new_id("wf"))
    app_id: str
    name: str
    screen_ids: list[str]
    actions: list[str]
    status: str = "inferred"  # inferred from the navigation graph until executed end-to-end
    evidence: list[Evidence] = Field(default_factory=list)


class PomReference(Strict):
    id: str = Field(default_factory=lambda: new_id("pom"))
    class_name: str
    file_path: str
    methods: list[str] = Field(default_factory=list)
    locators: dict[str, str] = Field(default_factory=dict)
    screen_id: str | None = None
    match_confidence: float | None = None


# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------
class RuleType(StrEnum):
    MANDATORY = "mandatory"
    LENGTH = "length"
    FORMAT = "format"
    RANGE = "range"
    ALLOWED_VALUES = "allowed_values"
    DEPENDENCY = "dependency"
    CONDITIONAL_VALIDATION = "conditional_validation"
    WORKFLOW_TRANSITION = "workflow_transition"
    INPUT_COMBINATION = "input_combination"
    CUSTOM = "custom"


class RuleStatus(StrEnum):
    INFERRED = "inferred"  # heuristic or AI reasoning only
    OBSERVED = "observed"  # backed by observed UI evidence (attribute, marker, message, state change)
    VERIFIED = "verified"  # confirmed by an executed probe with recorded evidence
    REFUTED = "refuted"  # an executed probe contradicted it
    DEPRECATED = "deprecated"


class BusinessRule(Strict):
    id: str = Field(default_factory=lambda: new_id("rule"))
    app_id: str
    screen_id: str | None = None
    rule_type: RuleType
    statement: str = Field(min_length=3, max_length=2000)
    fields: list[str] = Field(default_factory=list)
    condition: dict[str, Any] = Field(default_factory=dict, description="Machine-readable rule parameters.")
    status: RuleStatus = RuleStatus.INFERRED
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(description="heuristic | observation | ai | human | execution")
    evidence: list[Evidence] = Field(default_factory=list)
    key: str = Field(description="Dedup key: same rule discovered twice updates one record.")
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _truthful_status(self) -> BusinessRule:
        kinds = {e.kind for e in self.evidence}
        if self.status in (RuleStatus.VERIFIED, RuleStatus.REFUTED) and EvidenceKind.EXECUTION not in kinds:
            raise ValueError(f"A rule can only be {self.status.value} with execution evidence.")
        if self.status is RuleStatus.OBSERVED and not (kinds - {EvidenceKind.AI_INFERENCE}):
            raise ValueError("An observed rule needs non-AI evidence.")
        if self.source == "ai" and self.status not in (
            RuleStatus.INFERRED,
            RuleStatus.VERIFIED,
            RuleStatus.REFUTED,
            RuleStatus.DEPRECATED,
        ):
            raise ValueError("AI-proposed rules start as inferred.")
        if not self.evidence:
            raise ValueError("Every rule needs at least one evidence item.")
        return self


# ---------------------------------------------------------------------------
# Tests, runs, defects
# ---------------------------------------------------------------------------
class StepAction(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    PRESS = "press"
    WAIT = "wait"
    ASSERT = "assert"
    OBSERVE = "observe"
    AI_ACTION = "ai_action"  # free-form step for a vision agent (e.g. PyMidsense ai_act)


class TestStep(Strict):
    __test__ = False  # not a pytest test class

    id: str = Field(default_factory=lambda: new_id("step"))
    action: StepAction
    target: str | None = Field(default=None, max_length=500, description="Field/control name or NL description.")
    value: str | None = Field(default=None, max_length=2000)
    expected: str | None = Field(default=None, max_length=2000)
    selector: str | None = None
    pom_ref: str | None = Field(default=None, description="'ClassName.member' of an existing POM element.")
    destructive: bool = False


class TestCase(Strict):
    __test__ = False  # not a pytest test class

    id: str = Field(default_factory=lambda: new_id("tc"))
    app_id: str
    title: str = Field(min_length=3, max_length=300)
    kind: str = Field(default="positive", description="positive | negative | boundary | exploratory | security")
    origin: str = Field(default="nl", description="nl | bug_hunter | business_rule | imported")
    priority: int = Field(default=3, ge=1, le=5)
    start_url: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    steps: list[TestStep]
    rule_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TestPlan(Strict):
    __test__ = False  # not a pytest test class

    id: str = Field(default_factory=lambda: new_id("plan"))
    app_id: str
    instruction: str
    test_case_ids: list[str]
    destructive: bool = False
    destructive_terms: list[str] = Field(default_factory=list)
    content_hash: str
    created_at: str = Field(default_factory=utcnow)


class StepStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class StepResult(Strict):
    run_id: str
    test_case_id: str
    step_id: str
    status: StepStatus = StepStatus.PENDING
    actual: str | None = None
    error: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    recorded_at: str | None = None


class TestRun(Strict):
    __test__ = False  # not a pytest test class

    id: str = Field(default_factory=lambda: new_id("run"))
    app_id: str | None = None
    plan_id: str | None = None
    kind: str = Field(default="plan", description="plan | existing_tests")
    state: ExecutionState = ExecutionState.AWAITING_HOST_EXECUTION
    outcome: str | None = Field(default=None, description="passed | failed | partial (only once executed)")
    approval_id: str | None = None
    change_id: str | None = None
    created_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class DefectStatus(StrEnum):
    SUSPECTED = "suspected"  # seen once / derived from an anomaly
    REPRODUCED = "reproduced"  # failed again in a separate executed run
    NOT_REPRODUCED = "not_reproduced"
    CLOSED = "closed"


class Defect(Strict):
    id: str = Field(default_factory=lambda: new_id("bug"))
    app_id: str | None = None
    title: str
    severity: str = "medium"
    status: DefectStatus = DefectStatus.SUSPECTED
    category: str = "functional"
    environment: str
    steps_to_reproduce: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    signature: str
    run_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    report_path: str | None = None
    created_at: str = Field(default_factory=utcnow)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Approval(Strict):
    id: str = Field(default_factory=lambda: new_id("apr"))
    subject_type: str  # plan | existing_tests
    subject_id: str
    content_hash: str  # approval binds to exactly this content
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: str = Field(default_factory=utcnow)
    decided_at: str | None = None
    decided_by: str | None = None
    expires_at: str | None = None


class ChangeSet(Strict):
    id: str = Field(default_factory=lambda: new_id("chg"))
    repo_path: str
    base: str
    head: str
    files: list[dict[str, Any]]  # {path, status, added, deleted, old_path}
    commits: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow)


class CodeDependency(Strict):
    source: str  # e.g. test file / POM class / module
    source_type: str  # test | pom | module | screen
    target: str
    target_type: str
    relation: str  # imports | uses_pom | covers_screen | transitively_imports
    evidence: str = ""


class TestHistoryEntry(Strict):
    __test__ = False  # not a pytest test class

    test_id: str
    runs: int = 0
    failures: int = 0
    last_status: str | None = None
    last_failed_at: str | None = None
    avg_duration_s: float = 0.0
    flips: int = Field(default=0, description="pass<->fail transitions (flakiness signal)")
