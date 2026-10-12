"""Trip persistence API; planning and external calls belong to later stages."""

import asyncio
import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError

from ..domain.discovery_tasks import DiscoveryMessageCreate
from ..domain.models import (
    ArchiveTrip,
    CreateTrip,
    MessageCreate,
    TripPatch,
    WorkspacePatch,
)
from ..storage.database import NotFound, OperationConflict, VersionConflict


def _call(request: Request, method: str, *args, **kwargs):
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(
            503, detail={"code": "database_unavailable", "message": "行程存储暂不可用，请保留编辑后重试"}
        )
    try:
        return getattr(database, method)(*args, **kwargs)
    except NotFound:
        raise HTTPException(
            404, detail={"code": "not_found", "message": "行程不存在"}
        ) from None
    except VersionConflict as error:
        raise HTTPException(
            409,
            detail={
                "code": "version_conflict",
                "message": "版本已更新，请保留编辑并重新加载",
                "current_version": error.current_version,
            },
        ) from None
    except OperationConflict:
        raise HTTPException(
            409,
            detail={"code": "operation_conflict", "message": "操作 ID 已使用或行程当前状态不允许修改"},
        ) from None
    except (sqlite3.Error, OSError, json.JSONDecodeError, ValidationError):
        raise HTTPException(
            503, detail={"code": "database_unavailable", "message": "行程存储暂不可用，请保留编辑后重试"}
        ) from None


def make_router() -> APIRouter:
    router = APIRouter(prefix="/api/trips", tags=["trips"])

    @router.post("", status_code=201)
    def create_trip(body: CreateTrip, request: Request):
        return _call(request, "create_trip", body)

    @router.get("")
    def list_trips(
        request: Request,
        include_archived: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return _call(
            request, "list_trips", include_archived=include_archived, limit=limit, offset=offset
        )

    @router.get("/{trip_id}")
    def get_trip(trip_id: str, request: Request):
        return _call(request, "get_trip", trip_id)

    @router.patch("/{trip_id}")
    def update_trip(trip_id: str, body: TripPatch, request: Request):
        return _call(request, "update_trip", trip_id, body)

    @router.patch("/{trip_id}/workspace")
    def save_workspace(trip_id: str, body: WorkspacePatch, request: Request):
        return _call(request, "save_workspace", trip_id, body)

    @router.post("/{trip_id}/messages", status_code=201)
    async def add_message(trip_id: str, body: MessageCreate | DiscoveryMessageCreate, request: Request):
        settings = getattr(request.app.state, "settings", None)
        if settings is None:
            raise HTTPException(
                503, detail={"code": "configuration_unavailable", "message": "应用配置暂不可用，消息尚未保存"}
            )
        content = body.content
        for secret in sorted(settings.secrets, key=len, reverse=True):
            if secret:
                content = content.replace(secret, "[REDACTED]")
        method = "add_discovery_message" if isinstance(body, DiscoveryMessageCreate) and body.start_discovery else "add_message"
        if method == "add_discovery_message" and getattr(request.app.state, "executor", None) is None:
            raise HTTPException(503, detail={"code": "executor_unavailable", "message": "发现任务暂不可用，消息尚未保存"})
        result = await asyncio.to_thread(_call, request, method, trip_id, body.model_copy(update={"content": content}))
        if method == "add_discovery_message":
            request.app.state.executor.wake()
        return result

    @router.post("/{trip_id}/archive")
    def archive_trip(trip_id: str, body: ArchiveTrip, request: Request):
        return _call(request, "archive_trip", trip_id, body)

    @router.delete("/{trip_id}", status_code=204)
    def delete_trip(
        trip_id: str, request: Request, expected_version: Annotated[int, Query(ge=1)]
    ):
        _call(request, "delete_trip", trip_id, expected_version)
        return Response(status_code=204)

    return router
