"""Provider facts and explicit P3 actions; P2 candidate text remains immutable."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .models import ConfirmedOrigin, Identifier, Model, Version

Coordinates = Annotated[
    list[Annotated[float, Field(strict=True, allow_inf_nan=False)]],
    Field(min_length=2, max_length=2),
]


class Poi(Model):
    id: Identifier | None = None
    name: Annotated[str, Field(max_length=1000)] | None = None
    location: Coordinates | None = None
    address: Annotated[str, Field(max_length=2000)] | None = None
    city: Annotated[str, Field(max_length=200)] | None = None
    adcode: Annotated[str, Field(max_length=100)] | None = None
    type: Annotated[str, Field(max_length=1000)] | None = None
    entrance_location: Coordinates | None = None
    read_at: datetime
    api_version: Annotated[str, Field(min_length=1, max_length=100)] = "v5"
    missing_fields: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("location", "entrance_location")
    @classmethod
    def coordinates(cls, value):
        if value is not None and (
            not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90
        ):
            raise ValueError("POI必须是有效的经度、纬度")
        return value

    @field_validator("read_at")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("读取时间必须带时区")
        return value


class PoiMatch(Model):
    status: Literal["unique", "ambiguous", "unmatched"]
    poi: Poi | None = None
    options: list[Poi] = Field(default_factory=list, max_length=50)
    reason: Annotated[str, Field(max_length=1000)] = ""

    @model_validator(mode="after")
    def unique_coordinate(self):
        if self.status == "unique":
            if (
                self.poi is None
                or not self.poi.id
                or not self.poi.name
                or self.poi.location is None
            ):
                raise ValueError("唯一匹配必须有有效POI和坐标")
        elif self.poi is not None:
            raise ValueError("未唯一匹配不能提供落点")
        if any(not option.id or option.location is None for option in self.options):
            raise ValueError("备选POI必须有标识和坐标")
        return self


class MatchPlaces(Model):
    expected_version: Version
    expected_workspace_version: Version
    run_id: UUID | None = None
    candidate_ids: list[Identifier] | None = Field(
        default=None, min_length=1, max_length=20
    )
    refresh_map: bool = Field(default=False, strict=True)


class ConfirmPoi(Model):
    expected_version: Version
    expected_workspace_version: Version
    poi_id: Identifier


class MapAction(Model):
    expected_version: Version
    expected_workspace_version: Version
    action: Literal["initialize", "refresh", "add", "remove", "select", "unselect"]
    candidate_id: Identifier | None = None
    category: Literal["eat", "play", "stay"] | None = None
    city: Annotated[str, Field(max_length=200)] | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.action in {"add", "remove", "select", "unselect"}) != (
            self.candidate_id is not None
        ):
            raise ValueError("单个地点操作必须提供候选标识")
        return self


class ConfirmOrigin(Model):
    expected_version: Version
    expected_workspace_version: Version
    source: Literal["amap", "map"]
    search_id: UUID | None = None
    poi_id: Identifier | None = None
    name: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    location: Coordinates | None = None

    @model_validator(mode="after")
    def confirmation_source(self):
        if self.source == "amap":
            if (
                self.search_id is None
                or self.poi_id is None
                or self.location is not None
                or self.name is not None
            ):
                raise ValueError("搜索起点只能确认已保存搜索中的POI")
        else:
            if (
                self.search_id is not None
                or self.poi_id is not None
                or self.name is None
                or self.location is None
            ):
                raise ValueError("地图选点必须提供名称和坐标")
            ConfirmedOrigin(name=self.name, location=self.location, source="map")
        return self
