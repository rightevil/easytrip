from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Version = Annotated[int, Field(strict=True, ge=1)]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TripRequest(Model):
    destination: Annotated[str, Field(max_length=200)] | None = None
    origin: Annotated[str, Field(max_length=200)] | None = None
    start_at: datetime | None = None
    duration_minutes: Annotated[int, Field(strict=True, ge=30, le=20160)] | None = None
    transport: Literal[
        "mixed", "walking", "transit", "bicycling", "electrobike", "driving"
    ] = "mixed"
    transport_source: Literal["default", "user"] = "default"
    budget_cny: (
        Annotated[float, Field(strict=True, ge=0, le=10000000, allow_inf_nan=False)]
        | None
    ) = None
    participants: Annotated[int, Field(strict=True, ge=1, le=100)] = 1
    preferences: Annotated[str, Field(max_length=4000)] = ""

    @model_validator(mode="after")
    def transport_provenance(self):
        if (
            "transport" in self.model_fields_set
            and "transport_source" not in self.model_fields_set
        ):
            self.transport_source = "user"
        return self

    @field_validator("start_at", mode="before")
    @classmethod
    def datetime_input(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("出发时间必须为带时区的日期时间")
        return value

    @field_validator("start_at")
    @classmethod
    def aware_datetime(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("出发时间必须包含时区")
        return value


class Workspace(Model):
    active_tab: Literal["overview", "map", "plan"] = "overview"
    category: Literal["all", "eat", "play", "stay"] = "all"
    map_center: (
        list[Annotated[float, Field(strict=True, allow_inf_nan=False)]] | None
    ) = None
    map_zoom: Annotated[float, Field(strict=True, ge=3, le=20, allow_inf_nan=False)] = (
        12
    )
    selected_ids: Annotated[list[Identifier], Field(max_length=100)] = Field(
        default_factory=list
    )
    map_visible_ids: Annotated[list[Identifier], Field(max_length=100)] = Field(
        default_factory=list
    )

    @field_validator("map_center")
    @classmethod
    def coordinates(cls, value):
        if value is not None and (
            len(value) != 2 or not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90
        ):
            raise ValueError("地图中心必须是有效的经度、纬度")
        return value


class CreateTrip(Model):
    operation_id: UUID
    name: Name = "未命名行程"
    request: TripRequest = Field(default_factory=TripRequest)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip():
            raise ValueError("行程名称不能为空白")
        return value


class TripPatch(Model):
    expected_version: Version
    name: Name | None = None
    request: TripRequest | None = None

    @model_validator(mode="after")
    def actual_change(self):
        fields = self.model_fields_set - {"expected_version"}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError("需要非空名称或行程条件")
        if self.name is not None and not self.name.strip():
            raise ValueError("行程名称不能为空白")
        return self


class WorkspacePatch(Model):
    expected_version: Version
    workspace: Workspace


class MessageCreate(Model):
    operation_id: UUID
    content: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空白")
        return value


class ArchiveTrip(Model):
    expected_version: Version
    archived: bool = Field(strict=True)
