"""Immutable plan snapshots and explicit adoption; external facts are not client input."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_serializer, model_validator

from .discovery_tasks import DiscoveryBudget, DiscoveryUsage
from .models import (
    ConfirmedOrigin,
    DayWindow,
    Identifier,
    LocalDate,
    Model,
    TripRequest,
    Version,
)

PLAN_STAGES = ("planning_input", "routing", "scheduled")


class SearchRailTimetable(Model):
    expected_version: Version
    date: LocalDate
    departure_station_id: Identifier
    arrival_station_id: Identifier

    @model_validator(mode="after")
    def distinct_stations(self):
        if self.departure_station_id == self.arrival_station_id:
            raise ValueError("铁路出发站和到达站不能相同")
        return self


class PlanningBudget(DiscoveryBudget):
    max_steps: int = Field(default=100, strict=True, ge=1, le=400)
    max_routes: int = Field(default=40, strict=True, ge=1, le=40)
    max_combinations: int = Field(default=3, strict=True, ge=1, le=3)
    max_searches: int = Field(default=2, strict=True, ge=1, le=4)
    reserved_gap_searches: int = Field(default=0, strict=True, ge=0, le=2)
    target_notes: int = Field(default=2, strict=True, ge=1, le=5)
    comment_limit: int = Field(default=5, strict=True, ge=0, le=20)
    max_model_calls: int = Field(default=4, strict=True, ge=1, le=8)
    max_images: int = Field(default=0, strict=True, ge=0, le=3)
    max_tokens: int = Field(default=131072, strict=True, ge=1, le=262144)


class PlanningUsage(DiscoveryUsage):
    amap_calls: int = Field(default=0, strict=True, ge=0)
    route_calls: int = Field(default=0, strict=True, ge=0)
    city_calls: int = Field(default=0, strict=True, ge=0)
    poi_calls: int = Field(default=0, strict=True, ge=0)
    cache_hits: int = Field(default=0, strict=True, ge=0)


class PlanningCheckpoint(Model):
    completed_stages: list[str] = Field(default_factory=list, max_length=3)
    planning_input: dict | None = None
    routing: list[dict] | None = Field(default=None, max_length=3)
    scheduled: dict | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.completed_stages != list(
            PLAN_STAGES[: len(self.completed_stages)]
        ) or any(getattr(self, stage) is None for stage in self.completed_stages):
            raise ValueError("规划检查点顺序或结果不完整")
        return self


class DayPlan(DayWindow):
    overnight_rail_id: Identifier | None = None
    continuing_rail_id: Identifier | None = None
    origin: ConfirmedOrigin | None = None
    endpoint: ConfirmedOrigin | None = None
    lodging_candidate_id: Identifier | None = None
    itinerary: list[dict] = Field(default_factory=list, max_length=300)
    unknowns: list[dict] = Field(default_factory=list, max_length=300)
    conflicts: list[dict] = Field(default_factory=list, max_length=300)

    @model_serializer(mode="wrap")
    def omit_legacy_rail_defaults(self, handler):
        result = handler(self)
        for field in ("overnight_rail_id", "continuing_rail_id"):
            if getattr(self, field) is None:
                result.pop(field, None)
        return result


class PlanPayload(Model):
    input: dict
    days: list[DayPlan] = Field(default_factory=list, max_length=15)
    supplements: list[dict] = Field(default_factory=list, max_length=4)
    itinerary: list[dict] = Field(default_factory=list, max_length=300)
    unknowns: list[dict] = Field(default_factory=list, max_length=300)
    conflicts: list[dict] = Field(default_factory=list, max_length=300)
    usage: dict = Field(default_factory=dict)
    diff: list[dict] = Field(default_factory=list, max_length=300)
    valid_until: datetime | None = None

    @model_serializer(mode="wrap")
    def omit_legacy_days(self, handler):
        result = handler(self)
        if not self.days:
            result.pop("days", None)
        return result

    @model_validator(mode="after")
    def validity_time(self):
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("路线有效期限必须包含时区")
        if self.days and (
            self.itinerary != [step for day in self.days for step in day.itinerary]
            or any(
                step.get("date") != day.date.isoformat()
                for day in self.days
                for step in day.itinerary
            )
        ):
            raise ValueError("按日安排必须与完整行程逐项一致")
        windows = TripRequest.model_validate(self.input["request"]).planning.day_windows
        if windows and [day.date for day in self.days] != [
            window.date for window in windows
        ]:
            raise ValueError("按日安排必须覆盖全部明确的日期窗口")
        return self


class SavePlan(Model):
    operation_id: UUID
    request_version: Version
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    parent_plan_id: UUID | None = None
    run_id: UUID | None = None
    status: Literal["draft", "conditional", "partial", "conflict", "validated"]
    payload: PlanPayload


class AdoptPlan(Model):
    operation_id: UUID
    expected_version: Version
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class ReplacePlanningCandidate(Model):
    operation_id: UUID
    expected_version: Version
    expected_workspace_version: Version
    input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")
    candidate_id: Identifier
    replacement_id: Identifier
