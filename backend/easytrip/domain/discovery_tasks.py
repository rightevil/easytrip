"""Finite discovery budgets and resumable stage outputs, separate from P1 tasks."""

from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from .models import MessageCreate, Model, TripRequest, Version, Workspace

DISCOVERY_STAGES = (
    "understanding",
    "discovering",
    "fetching",
    "extracting",
    "evaluating",
    "candidates_ready",
)
Count = Annotated[int, Field(strict=True, ge=0)]


class DiscoveryBudget(Model):
    max_steps: Annotated[int, Field(strict=True, ge=1, le=400)] = 160
    max_seconds: Annotated[int, Field(strict=True, ge=1, le=1800)] = 600
    max_searches: Annotated[int, Field(strict=True, ge=1, le=16)] = 8
    target_notes: Annotated[int, Field(strict=True, ge=1, le=60)] = 30
    comment_limit: Annotated[int, Field(strict=True, ge=0, le=50)] = 20
    include_replies: bool = Field(default=False, strict=True)
    reply_limit: Annotated[int, Field(strict=True, ge=0, le=100)] = 20
    reserved_gap_searches: Annotated[int, Field(strict=True, ge=0, le=2)] = 2
    max_model_calls: Annotated[int, Field(strict=True, ge=1, le=128)] = 64
    max_images: Annotated[int, Field(strict=True, ge=0, le=180)] = 90
    max_tokens: Annotated[int, Field(strict=True, ge=1, le=2_000_000)] = 800_000

    @model_validator(mode="after")
    def reservation(self):
        if self.reserved_gap_searches >= self.max_searches:
            raise ValueError("补查预留必须少于总搜索预算")
        return self


class DiscoverySnapshot(Model):
    request: TripRequest
    workspace: Workspace
    workspace_version: Version
    messages_count: Count
    messages: list[dict] = Field(default_factory=list, max_length=100)
    operation: dict
    parent_run_id: str | None = None


class DiscoveryMessageCreate(MessageCreate):
    start_discovery: bool = Field(default=True, strict=True)
    expected_version: Version
    budget: DiscoveryBudget = Field(default_factory=DiscoveryBudget)


class AdoptDirection(Model):
    operation_id: UUID
    expected_version: Version
    run_id: UUID


class DiscoveryUsage(Model):
    steps: Count = 0
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0
    external_calls: Count = 0
    model_tokens: Count = 0
    reserved_tokens: Count = 0
    prompt_tokens: Count = 0
    completion_tokens: Count = 0
    images: Count = 0
    images_analyzed_unique: Count = 0
    images_unanalyzed: Count = 0
    searches: Count = 0
    detail_calls: Count = 0
    model_calls: Count = 0
    unknown_cost_calls: Count = 0
    unknown_result_calls: Count = 0
    notes_obtained: Count = 0
    notes_failed: Count = 0
    comments_obtained: Count = 0
    comments_selected: Count = 0
    comments_analyzed: Count = 0
    comments_judged: Count = 0
    notes_analyzed: Count = 0
    images_obtained: Count = 0
    evidence_count: Count = 0
    judgement_rows: Count = 0
    interrupted_stage_time_unknown: bool = False


class DiscoveryCheckpoint(Model):
    completed_stages: list[str] = Field(default_factory=list, max_length=6)
    understanding: dict | None = None
    discovering: dict | None = None
    fetching: dict | None = None
    extracting: dict | None = None
    evaluating: dict | None = None
    candidates_ready: dict | None = None

    @model_validator(mode="after")
    def ordered_stages(self):
        if self.completed_stages != list(
            DISCOVERY_STAGES[: len(self.completed_stages)]
        ):
            raise ValueError("无效探索检查点顺序")
        if any(getattr(self, stage) is None for stage in self.completed_stages):
            raise ValueError("缺少探索阶段结果")
        return self
