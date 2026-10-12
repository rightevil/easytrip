from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .discovery_tasks import DiscoveryBudget
from .models import Model, TripRequest, Version, Workspace


class RunBudget(Model):
    max_steps: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    max_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 30


class CreateRun(Model):
    operation_id: UUID
    expected_version: Version
    kind: Literal["prepare_workspace", "discover"] = "prepare_workspace"
    budget: RunBudget | DiscoveryBudget = Field(default_factory=RunBudget)
    parent_run_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def task_budget(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            budget_type = DiscoveryBudget if value.get("kind") == "discover" else RunBudget
            budget = value.get("budget", {})
            value["budget"] = budget_type.model_validate(
                budget.model_dump() if isinstance(budget, (RunBudget, DiscoveryBudget)) else budget
            )
            if value.get("kind", "prepare_workspace") != "discover" and value.get("parent_run_id"):
                raise ValueError("本地准备任务不接受继续探索来源")
        return value


class RunControl(Model):
    trip_id: UUID
    generation: Annotated[int, Field(strict=True, ge=0)]


class RunSnapshot(Model):
    request: TripRequest
    workspace: Workspace
    workspace_version: Version
    messages_count: Annotated[int, Field(strict=True, ge=0)]
    operation: dict


class RequestReadiness(Model):
    conditions_complete: bool
    missing: list[Literal["origin", "start_at", "duration_minutes"]]
    destination: str | None
    transport: str


class WorkspaceSummary(Model):
    messages_count: Annotated[int, Field(strict=True, ge=0)]
    selected_count: Annotated[int, Field(strict=True, ge=0)]
    map_visible_count: Annotated[int, Field(strict=True, ge=0)]
    workspace_version: Version
    active_tab: str


class PreparedWorkspace(Model):
    conditions: RequestReadiness
    workspace: WorkspaceSummary
    summary: str
    not_connected: list[str]


STAGES = ("validate_request", "read_workspace", "summarize")
STAGE_MODELS = {
    "validate_request": RequestReadiness,
    "read_workspace": WorkspaceSummary,
    "summarize": PreparedWorkspace,
}


class RunUsage(Model):
    steps: Annotated[int, Field(strict=True, ge=0, le=3)] = 0
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0
    external_calls: Literal[0] = 0
    model_tokens: Literal[0] = 0
    images: Literal[0] = 0
    interrupted_stage_time_unknown: bool = False


class RunCheckpoint(Model):
    completed_stages: list[str] = Field(default_factory=list, max_length=3)
    validate_request: RequestReadiness | None = None
    read_workspace: WorkspaceSummary | None = None
    summarize: PreparedWorkspace | None = None

    @model_validator(mode="after")
    def ordered_stages(self):
        if self.completed_stages != list(STAGES[: len(self.completed_stages)]):
            raise ValueError("无效检查点顺序")
        if any(getattr(self, stage) is None for stage in self.completed_stages):
            raise ValueError("缺少检查点结果")
        return self
