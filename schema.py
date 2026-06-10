from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ISODateTime = str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


class TaskCreate(BaseModel):
    industry: str = Field(min_length=1, max_length=80)
    competitors: list[str] = Field(min_length=1, max_length=8)
    websites: list[str] = Field(default_factory=list, max_length=8)
    focus_areas: list[str] = Field(default_factory=list, max_length=12)
    source_mode: Literal["缓存样例", "实时采集", "上传资料", "实时采集+上传资料"] = "缓存样例"
    notes: str = Field(default="", max_length=1000)
    defer_workflow: bool = False

    @field_validator("industry", "notes")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("competitors", "websites", "focus_areas")
    @classmethod
    def trim_list(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class CompetitorRecord(BaseModel):
    id: str
    task_id: str
    name: str
    website: str = ""
    industry: str
    target_users: list[str] = Field(default_factory=list)
    collected_at: ISODateTime


class SourceRecord(BaseModel):
    id: str
    task_id: str
    source_type: str
    title: str
    url_or_path: str
    author_site: str
    published_at: str = ""
    collected_at: ISODateTime
    credibility: str
    excerpt: str
    related_claim_ids: list[str] = Field(default_factory=list)
    fallback_reason: str = ""
    provider: str = ""
    search_log_id: str = ""
    search_query: str = ""
    auth_info: str = ""
    auth_level: int = 0
    time_cost_ms: int = 0


class EvidenceChunkRecord(BaseModel):
    id: str
    task_id: str
    source_id: str
    chunk_index: int = Field(ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    summary: str
    excerpt: str
    collected_at: ISODateTime


class ClaimRecord(BaseModel):
    id: str
    task_id: str
    section: str
    content: str
    confidence: float = Field(ge=0, le=1)
    source_ids: list[str] = Field(default_factory=list)
    counter_evidence: str = ""
    uncertainty: str = ""
    generated_agent: str
    needs_review: bool = False
    status: Literal["draft", "needs_review", "confirmed", "reportable"] = "draft"
    claim_type: Literal["fact", "inference", "recommendation", "assumption"] = "fact"
    created_at: ISODateTime


class LLMClaimDraft(BaseModel):
    section: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=1200)
    confidence: float = Field(ge=0, le=1)
    source_ids: list[str] = Field(min_length=1, max_length=8)
    counter_evidence: str = Field(default="", max_length=500)
    uncertainty: str = Field(default="", max_length=500)
    needs_review: bool = False
    status: Literal["draft", "needs_review", "confirmed", "reportable"] = "draft"
    claim_type: Literal["fact", "inference", "recommendation", "assumption"] = "fact"

    @field_validator("source_ids")
    @classmethod
    def must_have_source(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("LLM claims must include source_ids")
        return cleaned


class ReportableClaim(ClaimRecord):
    @field_validator("source_ids")
    @classmethod
    def must_have_source(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("reportable claims must include at least one source_id")
        return value


class QAFindingRecord(BaseModel):
    id: str
    task_id: str
    claim_id: str
    severity: Literal["low", "medium", "high", "critical"]
    reason: str
    target_agent: str
    finding_type: str = "general"
    action_hint: str = ""
    meta_json: str = "{}"
    fix_status: Literal["open", "fixed", "waived"] = "open"
    recheck_result: str = ""
    created_at: ISODateTime
    fixed_at: str = ""


class AgentRunRecord(BaseModel):
    id: str
    task_id: str
    agent_name: str
    input_summary: str
    output_summary: str
    status: str
    started_at: ISODateTime
    ended_at: ISODateTime
    duration_ms: int = Field(ge=0)
    error: str = ""
    retry_count: int = Field(default=0, ge=0)
    token_input: int = Field(default=0, ge=0)
    token_output: int = Field(default=0, ge=0)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ReportRecord(BaseModel):
    id: str
    task_id: str
    version: int = Field(ge=1)
    title: str
    content: dict[str, Any]
    generated_at: ISODateTime
    citation_map: dict[str, list[str]]
    qa_status: str
    confidence_score: float = Field(ge=0, le=1)


class ManualActionRecord(BaseModel):
    id: str
    task_id: str
    user_text: str
    selected_text: str = ""
    interpreted_intent: str
    target_agent: str
    status: str
    result_summary: str
    created_at: ISODateTime


class QuestionnaireDesignRecord(BaseModel):
    id: str
    task_id: str
    title: str
    research_objective: str
    target_users: str = ""
    content_json: str
    focus_dimensions_json: str = "[]"
    status: Literal["draft", "finalized", "deployed"] = "draft"
    estimated_time_minutes: int = 5
    created_at: ISODateTime


class SurveyAnalysisRecord(BaseModel):
    id: str
    task_id: str
    source_id: str
    questionnaire_design_id: str = ""
    title: str
    respondent_count: int = Field(ge=0)
    summary: str
    segments_json: str = "[]"
    findings_json: str = "[]"
    statistics_json: str = "[]"
    claims_generated_json: str = "[]"
    confidence_score: float = Field(ge=0, le=1, default=0.7)
    created_at: ISODateTime


class InterviewAnalysisRecord(BaseModel):
    id: str
    task_id: str
    source_id: str
    interview_guide_id: str = ""
    title: str
    interviewee_profile_json: str = "{}"
    summary: str
    key_quotes_json: str = "[]"
    scenarios_json: str = "[]"
    pain_points_json: str = "[]"
    needs_json: str = "[]"
    claims_generated_json: str = "[]"
    confidence_score: float = Field(ge=0, le=1, default=0.7)
    created_at: ISODateTime


class DAGNode(BaseModel):
    id: str
    label: str
    status: str
    duration_ms: int = 0
    detail: str = ""


class DAGEdge(BaseModel):
    source: str
    target: str
    label: str = ""
    edge_type: Literal["normal", "rollback"] = "normal"


class DAGResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[DAGNode]
    edges: list[DAGEdge]
    timeline: list[dict[str, Any]]
