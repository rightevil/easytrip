"""Immutable plan snapshots and explicit adoption; external facts are not client input."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import Model, Version


class PlanPayload(Model):
    input: dict
    itinerary: list[dict] = Field(default_factory=list, max_length=300)
    unknowns: list[dict] = Field(default_factory=list, max_length=300)
    conflicts: list[dict] = Field(default_factory=list, max_length=300)
    usage: dict = Field(default_factory=dict)
    diff: list[dict] = Field(default_factory=list, max_length=300)
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validity_time(self):
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("路线有效期限必须包含时区")
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
