from __future__ import annotations

from pydantic import BaseModel, Field


class Clause(BaseModel):
    id: str
    title: str
    text: str
    status: str = "ok"


class ExtractedField(BaseModel):
    key: str
    label: str
    value: str
    status: str = "present"
    evidence_clause_ids: list[str] = Field(default_factory=list)


class RiskFinding(BaseModel):
    rule_id: str
    title: str
    level: str
    message: str
    reason: str
    evidence_clause_ids: list[str] = Field(default_factory=list)
    policy_reference_ids: list[str] = Field(default_factory=list)
    action: str


class WorkflowStep(BaseModel):
    key: str
    label: str
    status: str
    updated_at: str | None = None


class AgentTraceEvent(BaseModel):
    at: str
    type: str
    message: str
    payload: dict = Field(default_factory=dict)


class StoredFile(BaseModel):
    original_filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int
    sha256: str
    storage_backend: str
    object_key: str
    local_path: str | None = None
    bucket: str | None = None
    saved_at: str


class ReportSnapshot(BaseModel):
    title: str
    summary: str
    recommendation: str
    generated_at: str


class TaskRecord(BaseModel):
    id: str
    name: str
    contract_type: str
    contract_type_label: str
    source_filename: str
    status: str
    status_label: str
    overall_risk: str
    overall_risk_label: str
    decision: str
    decision_label: str
    summary: str
    created_at: str
    contract_text: str
    stored_file: StoredFile | None = None
    clauses: list[Clause] = Field(default_factory=list)
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    risks: list[RiskFinding] = Field(default_factory=list)
    workflow_steps: list[WorkflowStep] = Field(default_factory=list)
    agent_trace: list[AgentTraceEvent] = Field(default_factory=list)
    report_snapshot: ReportSnapshot | None = None


class RagflowProbe(BaseModel):
    base_url: str
    status: str
    detail: str
    healthy: bool
    datasets: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class DatabaseProbe(BaseModel):
    backend: str
    status: str
    detail: str
    healthy: bool
    dsn: str | None = None
    task_count: int | None = None


class ObjectStorageProbe(BaseModel):
    backend: str
    status: str
    detail: str
    healthy: bool
    endpoint_url: str | None = None
    bucket: str | None = None


class LLMProbe(BaseModel):
    base_url: str
    chat_completions_url: str
    model: str
    status: str
    detail: str
    title: str = "模型状态"
    configured: bool
    verified: bool
    api_key_present: bool
    env_file_path: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    checked_at: str | None = None
    latency_ms: float | None = None
    response_preview: str | None = None
    error_detail: str | None = None
