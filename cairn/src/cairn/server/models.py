from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_serializer, model_validator


class AuditBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_seconds: int = Field(gt=0, default=1800)
    drain_seconds: int = Field(gt=0, default=120)
    final_review_seconds: int = Field(gt=0, default=360)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, gt=0)
    end_line: int | None = Field(default=None, gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("evidence path must be non-empty and contain no NUL")
        return value

    @model_validator(mode="after")
    def validate_lines(self) -> "EvidenceReference":
        if self.end_line is not None and (self.start_line is None or self.end_line < self.start_line):
            raise ValueError("end_line requires start_line and must not precede it")
        return self


class AuditFactData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: list[EvidenceReference] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    premise_ids: list[str] = Field(default_factory=list)
    review_status: Literal["unverified", "verified", "refuted"] = "unverified"
    validation_level: str | None = None
    review_notes: str | None = None

    @field_validator("premise_ids", "conditions")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        items = [item.strip() for item in value]
        if any(not item for item in items) or len(items) != len(set(items)):
            raise ValueError("items must be non-empty and unique")
        return items


class AuditLease(BaseModel):
    id: str
    worker: str


class ClosingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["candidate", "no_candidate"]
    from_: list[str] = Field(default_factory=list, alias="from")
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate(self):
        if not self.description.strip():
            raise ValueError("closing requires a non-empty explanation")
        if self.kind == "candidate":
            if not self.from_ or len(self.from_) != len(set(self.from_)) or set(self.from_) & {"origin", "goal"}:
                raise ValueError("candidate requires unique ordinary proof facts")
        elif self.from_:
            raise ValueError("no_candidate cannot contain proof facts")
        return self


class AuditClosing(BaseModel):
    status: Literal["pending", "running", "success", "error", "stale"] = "pending"
    input_digest: str | None = None
    attempts: int = 0
    max_attempts: int = 2
    round_deadline: str | None = None
    attempt_deadline: str | None = None
    round_deadline_seconds: float | None = None
    attempt_deadline_seconds: float | None = None
    token: str | None = None
    session: str | None = None
    result: ClosingResult | None = None
    error: str | None = None
    request_id: str | None = None


class AuditMetadata(BaseModel):
    project_id: str
    repo_path: str
    workspace_root: str
    workspace_path: str | None = None
    baseline: dict[str, JsonValue]
    user_goal: str
    policy: dict[str, JsonValue]
    policy_digest: str
    budget: AuditBudget
    seed_ids: dict[str, str]
    phase: Literal["preparing", "researching", "draining", "finalizing", "closed"] = "preparing"
    completion_pending: bool = False
    audit_revision: int = 1
    reason_revision: int = 1
    started_at: str | None = None
    research_deadline: str | None = None
    drain_deadline: str | None = None
    final_deadline: str | None = None
    leases: dict[str, AuditLease] = Field(default_factory=dict)
    draining_lease_ids: list[str] = Field(default_factory=list)
    closing: AuditClosing = Field(default_factory=AuditClosing)
    termination_reason: str | None = None


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    description: str
    audit: AuditFactData | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        if self.audit is None:
            data.pop("audit", None)
        return data


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    lease_id: str | None = None

    model_config = {"populate_by_name": True}

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        if self.lease_id is None:
            data.pop("lease_id", None)
        return data


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str
    lease_id: str | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        if self.lease_id is None:
            data.pop("lease_id", None)
        return data


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed", "draining", "budget_exhausted"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None
    mode: Literal["generic", "audit"] = "generic"
    audit: AuditMetadata | None = None
    runtime_generation: int | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        if self.runtime_generation is None:
            data.pop("runtime_generation", None)
        if self.mode == "generic":
            data.pop("mode", None)
            data.pop("audit", None)
            data.pop("runtime_generation", None)
        return data


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str
    worker: str | None = Field(default=None, min_length=1)
    harness: str | None = Field(default=None, min_length=1)

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    harness: str | None = Field(default=None, min_length=1)
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str
    harness: str | None = Field(default=None, min_length=1)
    lease_id: str | None = None

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    harness: str | None = Field(default=None, min_length=1)
    description: str
    audit: AuditFactData | None = None
    lease_id: str | None = None

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    harness: str | None = Field(default=None, min_length=1)
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    source_session: str | None = None
    lease_id: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


class AuditSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    description: str = Field(min_length=1)
    audit: AuditFactData = Field(default_factory=AuditFactData)

    @field_validator("key", "description")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class AuditInitialIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: list[str] = Field(alias="from", min_length=1)
    description: str = Field(min_length=1)

    @field_validator("from_")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        return AuditFactData.validate_items(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class CreateAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)
    creator: str = Field(min_length=1, default="audit-controller")
    repo_path: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    baseline: dict[str, JsonValue] = Field(min_length=1)
    policy: dict[str, JsonValue] = Field(min_length=1)
    budget: AuditBudget = Field(default_factory=AuditBudget)
    seeds: list[AuditSeed] = Field(default_factory=list)
    intents: list[AuditInitialIntent] = Field(default_factory=list)
    hints: list[CreateHintInline] = Field(default_factory=list)

    @field_validator("title", "origin", "goal", "user_goal", "creator")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class AuditRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: str | None = Field(default=None, min_length=1)
    harness: str | None = Field(default=None, min_length=1)

    record_key: str = Field(min_length=1)
    object_type: Literal["project", "fact", "intent"]
    object_id: str = Field(min_length=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: str = Field(min_length=1)
    session: str | None = None
    attempt_status: Literal["success", "error"]
    decision: Literal["approved", "rejected", "needs_evidence"] | None = None
    error: str | None = None
    result: dict[str, JsonValue] | None = None
    artifacts: list[str] = Field(default_factory=list)
    usage: dict[str, JsonValue] | None = None
    fact_audit: AuditFactData | None = None
    hint: str | None = Field(default=None, min_length=1)

    @field_validator("record_key", "object_id", "phase")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_attempt(self) -> "AuditRecordRequest":
        if self.attempt_status == "error":
            if not self.error or not self.error.strip() or self.decision is not None or self.fact_audit is not None or self.hint is not None:
                raise ValueError("an error attempt requires an error and cannot contain a decision")
        elif self.error is not None:
            raise ValueError("a successful attempt cannot contain an error")
        return self


class AuditRecord(AuditRecordRequest):
    id: str
    created_at: str
    output_digest: str | None = None


class AuditDetail(AuditMetadata):
    records: list[AuditRecord] = Field(default_factory=list)


class PolicyChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: bool
    deployment: bool
    attacker_capabilities: bool
    scope: bool
    cvss: bool
    evidence: bool


class CVSSVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    AV: Literal["N", "A", "L", "P"]
    AC: Literal["L", "H"]
    PR: Literal["N", "L", "H"]
    UI: Literal["N", "R"]
    S: Literal["U", "C"]
    C: Literal["N", "L", "H"]
    I: Literal["N", "L", "H"]
    A: Literal["N", "L", "H"]


class FinalAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected", "needs_evidence"]
    reason: str = Field(min_length=1)
    proof_ids: list[str] = Field(min_length=1)
    checks: PolicyChecks
    cvss_vector: CVSSVector | None = None

    @model_validator(mode="after")
    def validate_approval(self):
        if not self.reason.strip() or len(self.proof_ids) != len(set(self.proof_ids)):
            raise ValueError("review reason and unique proof_ids are required")
        if self.decision == "approved" and not all(self.checks.model_dump().values()):
            raise ValueError("approval requires every fixed policy check")
        return self


class AuditCompletion(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    project_id: str
    from_: list[str] = Field(alias="from")
    description: str
    worker: str
    source_session: str | None = None
    proof_digest: str
    policy_digest: str
    baseline_digest: str
    audit_revision: int
    status: Literal["pending", "stale", "review_error", "approved", "rejected", "needs_evidence"] = "pending"
    created_at: str
    updated_at: str
    attempts: int = 0
    max_attempts: int = 2
    round_deadline: str | None = None
    attempt_deadline: str | None = None
    round_deadline_seconds: float | None = None
    attempt_deadline_seconds: float | None = None
    review_token: str | None = None
    review_owner: str | None = None
    review_session: str | None = None
    decision: Literal["approved", "rejected", "needs_evidence"] | None = None
    error: str | None = None


class CompletionClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1)
    max_attempts: int = Field(default=2, gt=0)
    timeout_seconds: int = Field(default=60, gt=0)
    retry: bool = False


class CompletionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    expected_audit_revision: int
    policy_digest: str
    proof_digest: str
    record: AuditRecordRequest
    assessment: FinalAssessment | None = None

    @model_validator(mode="after")
    def validate_result(self):
        if self.record.phase != "final_review" or self.record.object_type != "project":
            raise ValueError("completion review must be a final_review project record")
        if self.record.attempt_status == "error":
            if self.assessment is not None:
                raise ValueError("execution error cannot contain a verdict")
        elif self.assessment is None or self.record.decision != self.assessment.decision:
            raise ValueError("successful review requires a matching assessment and decision")
        return self


class AuditBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finalize: bool = False


class ClosingReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    input_digest: str
    record: AuditRecordRequest
    result: ClosingResult | None = None

    @model_validator(mode="after")
    def validate_result(self):
        if self.record.phase != "closing" or self.record.object_type != "project" or self.record.decision is not None:
            raise ValueError("closing is a project observation, not an approval")
        if (self.record.attempt_status == "success") != (self.result is not None):
            raise ValueError("only a successful closing can contain a result")
        return self
