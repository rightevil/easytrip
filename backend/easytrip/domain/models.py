from datetime import date as LocalDate
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

Version = Annotated[int, Field(strict=True, ge=1)]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmedOrigin(Model):
    name: Annotated[str, Field(min_length=1, max_length=300)]
    location: list[Annotated[float, Field(strict=True, allow_inf_nan=False)]]
    poi_id: Identifier | None = None
    source: Literal["amap", "map"]
    request_origin: Annotated[str, Field(max_length=200)] | None = None
    city: Annotated[str, Field(max_length=200)] | None = None

    @model_validator(mode="after")
    def provenance(self):
        if not self.name.strip() or (self.source == "amap") != (
            self.poi_id is not None
        ):
            raise ValueError("起点名称和POI来源标识无效")
        return self

    @field_validator("location")
    @classmethod
    def valid_location(cls, value):
        if len(value) != 2 or not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90:
            raise ValueError("起点必须是有效的经度、纬度")
        return value


class ActivityConstraint(Model):
    candidate_id: Identifier
    date: LocalDate | None = None
    duration_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] | None = None
    duration_source: Literal["user", "source", "estimated", "unknown"] = "unknown"
    cost_cny: (
        Annotated[float, Field(strict=True, ge=0, le=10000000, allow_inf_nan=False)]
        | None
    ) = None
    cost_source: Literal["user", "source", "estimated", "unknown"] = "unknown"
    required: bool = Field(default=False, strict=True)
    excluded: bool = Field(default=False, strict=True)
    locked: bool = Field(default=False, strict=True)
    state: Literal["pending", "completed", "skipped"] = "pending"
    fixed_start_at: datetime | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None

    @model_serializer(mode="wrap")
    def omit_unassigned_date(self, handler):
        result = handler(self)
        if self.date is None:
            result.pop("date", None)
        return result

    @field_validator("fixed_start_at", "not_before", "not_after")
    @classmethod
    def aware_times(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("活动时间必须包含时区")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.required and (self.excluded or self.state == "skipped"):
            raise ValueError("必去活动不能同时排除或跳过")
        if self.locked and (self.excluded or self.state == "skipped"):
            raise ValueError("锁定活动不能同时排除或跳过")
        if self.fixed_start_at and (self.excluded or self.state == "skipped"):
            raise ValueError("请先明确清除固定预约，再排除或跳过活动")
        if self.not_before and self.not_after and self.not_before >= self.not_after:
            raise ValueError("活动时间窗必须有正时长")
        for value, source in (
            (self.duration_minutes, self.duration_source),
            (self.cost_cny, self.cost_source),
        ):
            if (value is None) != (source == "unknown"):
                raise ValueError("时长及费用必须同时记录数值与来源；未知不可补数")
        return self


class DayWindow(Model):
    date: LocalDate
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("每日时间窗口必须带时区")
        return value

    @model_validator(mode="after")
    def positive_window(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("每日结束时间必须晚于开始时间")
        return self


class OvernightStay(Model):
    date: LocalDate
    candidate_id: Identifier


class RailSegment(Model):
    id: Identifier
    date: LocalDate
    departure_station_id: Identifier | None = None
    arrival_station_id: Identifier | None = None
    train_number: Name | None = None
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    confirmed: bool = Field(default=False, strict=True)
    source: Literal["user"] = "user"
    boarding_buffer_minutes: int = Field(default=30, strict=True, ge=0, le=240)
    arrival_buffer_minutes: int = Field(default=20, strict=True, ge=0, le=240)
    cost_cny: (
        Annotated[float, Field(strict=True, ge=0, le=10000000, allow_inf_nan=False)]
        | None
    ) = None

    @field_validator("departure_at", "arrival_at")
    @classmethod
    def aware_times(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("铁路时刻必须带时区")
        return value

    @model_validator(mode="after")
    def confirmed_timetable(self):
        if (
            self.departure_at
            and self.arrival_at
            and self.arrival_at <= self.departure_at
        ):
            raise ValueError("铁路到达时刻必须晚于出发时刻，跨日需填写实际日期")
        if (
            self.departure_station_id
            and self.departure_station_id == self.arrival_station_id
        ):
            raise ValueError("铁路出发站和到达站不能相同")
        if self.confirmed and not all(
            (
                self.departure_station_id,
                self.arrival_station_id,
                self.train_number.strip() if self.train_number else None,
                self.departure_at,
                self.arrival_at,
            )
        ):
            raise ValueError("确认铁路前必须明确两站、车次与完整时刻")
        return self


class PlanningConstraints(Model):
    rail_segments: list[RailSegment] = Field(default_factory=list, max_length=28)
    day_windows: list[DayWindow] = Field(default_factory=list, max_length=14)
    overnight_stays: list[OvernightStay] = Field(default_factory=list, max_length=13)
    progress_at: datetime | None = None
    progress_origin: ConfirmedOrigin | None = None
    dietary_restrictions: list[Annotated[str, Field(min_length=1, max_length=200)]] = (
        Field(default_factory=list, max_length=20)
    )
    origin_city: Annotated[str, Field(max_length=200)] | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=100)] = "Asia/Shanghai"
    return_to_origin: bool = Field(default=False, strict=True)
    end_candidate_id: Identifier | None = None
    end_at: datetime | None = None
    max_walk_meters: Annotated[int, Field(strict=True, ge=0, le=100000)] | None = None
    disabled_modes: list[
        Literal["walking", "transit", "bicycling", "electrobike", "driving"]
    ] = Field(default_factory=list, max_length=5)
    transfer_buffer_minutes: Annotated[int, Field(strict=True, ge=0, le=120)] = 10
    activity_constraints: list[ActivityConstraint] = Field(
        default_factory=list, max_length=100
    )

    @model_serializer(mode="wrap")
    def omit_single_day_defaults(self, handler):
        result = handler(self)
        for field in ("day_windows", "overnight_stays", "rail_segments"):
            if not getattr(self, field):
                result.pop(field, None)
        return result

    @field_validator("timezone")
    @classmethod
    def timezone_name(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("规划时区必须是有效的 IANA 时区") from None
        return value

    @field_validator("end_at", "progress_at")
    @classmethod
    def aware_end(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("返程期限必须包含时区")
        return value

    @model_validator(mode="after")
    def distinct_constraints(self):
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self.timezone)
        dates = [window.date for window in self.day_windows]
        rail_ids = [rail.id for rail in self.rail_segments]
        if len(set(rail_ids)) != len(rail_ids):
            raise ValueError("铁路段标识不能重复")
        for rail in self.rail_segments:
            if dates and (
                rail.date not in dates
                or (
                    rail.arrival_at
                    and rail.arrival_at.astimezone(zone).date() not in dates
                )
            ):
                raise ValueError("铁路出发和到达必须由每日窗口覆盖")
            if (
                rail.departure_at
                and rail.departure_at.astimezone(zone).date() != rail.date
            ):
                raise ValueError("铁路出发时刻必须属于所填本地日期")
        if any(
            right != left + timedelta(days=1) for left, right in zip(dates, dates[1:])
        ):
            raise ValueError("每日窗口必须按连续且唯一的本地日期排列")
        for window in self.day_windows:
            if any(
                value and value.astimezone(zone).date() != window.date
                for value in (window.start_at, window.end_at)
            ):
                raise ValueError("每日时间必须属于所填本地日期")
        nights = [stay.date for stay in self.overnight_stays]
        if len(nights) != len(set(nights)) or not set(nights).issubset(dates[:-1]):
            raise ValueError("每晚酒店只能绑定一次，且必须属于非末日窗口")
        for item in self.activity_constraints:
            if item.date and item.date not in dates:
                raise ValueError("活动日期必须对应明确的每日窗口")
            if item.date and any(
                value and value.astimezone(zone).date() != item.date
                for value in (item.fixed_start_at, item.not_before, item.not_after)
            ):
                raise ValueError("活动预约和时间窗必须属于分配日期")
        if (self.progress_at is None) != (self.progress_origin is None):
            raise ValueError("当前位置与当前时间必须成对明确提供")
        if self.progress_origin and self.progress_origin.source != "map":
            raise ValueError("当前位置只能使用用户明确提供的地图坐标")
        ids = [item.candidate_id for item in self.activity_constraints]
        if len(ids) != len(set(ids)) or len(self.disabled_modes) != len(
            set(self.disabled_modes)
        ):
            raise ValueError("活动约束与禁用交通不能重复")
        if self.return_to_origin and self.end_candidate_id:
            raise ValueError("终点必须选择返回起点或指定地点之一")
        return self


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
    planning: PlanningConstraints = Field(default_factory=PlanningConstraints)

    @model_validator(mode="after")
    def transport_provenance(self):
        if (
            "transport" in self.model_fields_set
            and "transport_source" not in self.model_fields_set
        ):
            self.transport_source = "user"
        if (
            self.start_at
            and self.planning.end_at
            and self.planning.end_at <= self.start_at
        ):
            raise ValueError("结束期限必须晚于出发时间")
        deadline = self.planning.end_at
        if self.start_at and self.duration_minutes is not None:
            duration_end = self.start_at + timedelta(minutes=self.duration_minutes)
            deadline = min(deadline, duration_end) if deadline else duration_end
        for window in self.planning.day_windows:
            if self.start_at and window.start_at and window.start_at < self.start_at:
                raise ValueError("每日开始不能早于行程出发时间")
            if deadline and window.end_at and window.end_at > deadline:
                raise ValueError("每日结束不能晚于行程总期限")
        for rail in self.planning.rail_segments:
            if (
                self.start_at
                and rail.departure_at
                and rail.departure_at < self.start_at
            ):
                raise ValueError("铁路出发不能早于行程开始")
            if deadline and rail.arrival_at and rail.arrival_at > deadline:
                raise ValueError("铁路到达不能晚于行程总期限")
        return self

    def planning_windows(self):
        """Only a same-local-day trip has an unambiguous implicit window."""
        from zoneinfo import ZoneInfo

        if self.planning.day_windows:
            return self.planning.day_windows
        deadline = self.planning.end_at
        if self.start_at and self.duration_minutes is not None:
            end = self.start_at + timedelta(minutes=self.duration_minutes)
            deadline = min(end, deadline) if deadline else end
        zone = ZoneInfo(self.planning.timezone)
        if (
            not self.start_at
            or not deadline
            or self.start_at.astimezone(zone).date() != deadline.astimezone(zone).date()
        ):
            return []
        return [
            DayWindow(
                date=self.start_at.astimezone(zone).date(),
                start_at=self.start_at,
                end_at=deadline,
            )
        ]

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
    map_initialized: bool = Field(default=False, strict=True)
    map_city: Annotated[str, Field(max_length=200)] | None = None
    confirmed_origin: ConfirmedOrigin | None = None

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
