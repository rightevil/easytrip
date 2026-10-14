"""Only normalized provider facts enter the route cache or plan snapshots."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .models import Model

Coordinates = Annotated[
    list[Annotated[float, Field(strict=True, allow_inf_nan=False)]],
    Field(min_length=2, max_length=2),
]
Quantity = Annotated[
    float, Field(strict=True, ge=0, le=100_000_000, allow_inf_nan=False)
]


class RouteCondition(Model):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)


class RouteLeg(Model):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["walking", "transit", "bicycling", "electrobike", "driving"]
    origin: Coordinates
    destination: Coordinates
    city_code: str | None = Field(default=None, pattern=r"^\d{3,4}$")
    departure_at: datetime
    read_at: datetime
    valid_until: datetime
    api_version: Literal["v5"] = "v5"
    status: Literal["available", "unavailable", "unknown"]
    reason: str = Field(max_length=2000)
    provider_infocode: str | None = Field(default=None, pattern=r"^\d{5}$")
    duration_seconds: Annotated[int, Field(strict=True, ge=0, le=31_536_000)] | None = (
        None
    )
    distance_meters: Quantity | None = None
    walking_distance_meters: Quantity | None = None
    geometry: list[list[Coordinates]] = Field(default_factory=list)
    geometry_complete: bool = Field(strict=True)
    cost_cny: Quantity | None = None
    cost_source: Literal["source", "unknown"] = "unknown"
    duration_source: Literal["amap"] = "amap"
    conditional: bool = Field(default=False, strict=True)
    conditions: list[RouteCondition] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def fact_consistency(self):
        if self.status == "available" and (
            self.duration_seconds is None
            or self.distance_meters is None
            or (self.mode == "transit" and self.city_code is None)
        ):
            raise ValueError("可用路线必须有真实耗时、距离及必要城市编码")
        if self.geometry_complete and not self.geometry:
            raise ValueError("完整线路必须包含真实几何")
        if (self.cost_source == "source") != (self.cost_cny is not None):
            raise ValueError("路线费用及其来源不一致")
        if self.valid_until <= self.read_at:
            raise ValueError("路线有效期必须晚于取得时间")
        return self

    @field_validator("origin", "destination")
    @classmethod
    def coordinate_range(cls, value):
        if not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90:
            raise ValueError("路线坐标超出经纬度范围")
        return value

    @field_validator("geometry")
    @classmethod
    def geometry_range(cls, lines):
        for line in lines:
            if len(line) < 2:
                raise ValueError("线路几何至少需要两个真实坐标")
            for point in line:
                cls.coordinate_range(point)
        return lines

    @field_validator("departure_at", "read_at", "valid_until")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("路线时间必须带时区")
        return value
