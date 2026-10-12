"""P2 source snapshots and text candidates; model claims are not POI facts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .models import Identifier, Model

Category = Literal["eat", "play", "stay"]
Purpose = Literal["discovery", "main", "meal_gap", "lodging_gap"]
Text = Annotated[str, Field(min_length=1, max_length=30000)]


class Source(Model):
    platform: Annotated[str, Field(min_length=1, max_length=80)] = "xiaohongshu"
    kind: Literal["note", "comment"]
    external_id: Identifier | None = None
    parent_source_id: Identifier | None = None
    reply_to_source_id: Identifier | None = None
    title: Annotated[str, Field(max_length=500)] = ""
    text: Annotated[str, Field(max_length=30000)] = ""
    images: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list, max_length=100
    )
    published_at: datetime | None = None
    author_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    completeness: Literal["complete", "partial", "unknown"] = "partial"
    read_status: Literal["success", "failed"] = "success"
    error_code: Annotated[str, Field(max_length=100)] | None = None

    @field_validator(
        "external_id", "parent_source_id", "reply_to_source_id", "platform"
    )
    @classmethod
    def nonblank_identifier(cls, value):
        if value is not None and not value.strip():
            raise ValueError("来源标识不能为空白")
        return value

    @field_validator("published_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("来源时间必须带时区")
        return value

    @model_validator(mode="after")
    def parent_relationship(self):
        if (self.kind == "comment") != (self.parent_source_id is not None):
            raise ValueError("评论必须关联父笔记，笔记不可关联父来源")
        if self.kind == "note" and self.reply_to_source_id is not None:
            raise ValueError("笔记不能关联被回复评论")
        return self


class SourceNote(Source):
    kind: Literal["note"] = "note"


class SourceComment(Source):
    kind: Literal["comment"] = "comment"


class Citation(Model):
    citation_kind: Literal["text", "image"] = "text"
    image_index: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def image_citation(self):
        if (self.citation_kind == "image") != (self.image_index is not None):
            raise ValueError("图片引用必须指定原图片序号，文本引用不能指定图片")
        return self


class SubmittedImage(Model):
    source_id: Identifier
    image_index: Annotated[int, Field(strict=True, ge=1)]


class ImageObservation(SubmittedImage):
    text: Text
    uncertain: Literal[True] = True


class PlaceMention(Citation):
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    source_id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=300)]
    city: Annotated[str, Field(max_length=200)] | None = None
    branch_hint: Annotated[str, Field(max_length=400)] | None = None
    kind: Literal["place", "area", "route", "generic"] = "place"
    category: Literal["eat", "play", "stay", "transport"]
    quote: Text


class Activity(Citation):
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    mention_id: Identifier
    source_id: Identifier
    quote: Text
    description: Annotated[str, Field(min_length=1, max_length=1000)]


class Evidence(Citation):
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    source_id: Identifier
    mention_id: Identifier | None = None
    object_text: Annotated[str, Field(max_length=400)] = ""
    dimension: Annotated[str, Field(min_length=1, max_length=200)]
    stance: Literal["positive", "negative", "mixed", "question", "unknown"]
    nature: Literal[
        "experience",
        "question",
        "hearsay",
        "wish",
        "suggestion",
        "content_reaction",
        "unknown",
    ]
    quote: Text
    link_status: Literal["linked", "unlinked", "uncertain"] = "unlinked"
    model_id: Annotated[str, Field(max_length=300)] | None = None
    schema_version: Annotated[str, Field(min_length=1, max_length=100)] = "p2-v1"

    @model_validator(mode="after")
    def linked_mention(self):
        if self.link_status == "linked" and self.mention_id is None:
            raise ValueError("已关联证据必须指定地点提及")
        if self.link_status == "unlinked" and self.mention_id is not None:
            raise ValueError("未关联证据不能指定地点提及")
        if self.citation_kind == "image" and (
            self.nature != "unknown" or self.link_status == "linked"
        ):
            raise ValueError("图片观察只能保留为待核实证据")
        return self


class CandidateStats(Model):
    author_count: Annotated[int, Field(strict=True, ge=0)] = 0
    positive_count: Annotated[int, Field(strict=True, ge=0)] = 0
    negative_count: Annotated[int, Field(strict=True, ge=0)] = 0
    uncertain_count: Annotated[int, Field(strict=True, ge=0)] = 0


class CandidateDraft(Model):
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    name: Annotated[str, Field(min_length=1, max_length=300)]
    category: Category
    kind: Literal["place", "direction"] = "place"
    city: Annotated[str, Field(max_length=200)] | None = None
    direction: Annotated[str, Field(max_length=300)] | None = None
    mention_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    match_status: Literal["unmatched"] = "unmatched"
    summary: Annotated[str, Field(max_length=2000)] = ""
    uncertainty: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=100
    )
    stats: CandidateStats = Field(default_factory=CandidateStats)

    @model_validator(mode="after")
    def direction_city(self):
        if self.kind == "direction" and (self.city is None or not self.city.strip()):
            raise ValueError("方向候选必须指定城市")
        return self


class Gap(Model):
    id: Identifier = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["meal", "lodging"]
    status: Literal["open", "undetermined", "filled", "unfilled"] = "undetermined"
    period: Literal["lunch", "dinner", "overnight"] | None = None
    area: Annotated[str, Field(max_length=300)] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    conditions: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=100
    )
    source_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    candidate_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
