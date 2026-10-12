"""Read persisted discovery results and their original source snapshots."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request

from .trips import _call


def make_router():
    router = APIRouter(prefix="/api/trips", tags=["discovery"])

    @router.get("/{trip_id}/candidates")
    def candidates(
        trip_id: str,
        request: Request,
        run_id: UUID | None = None,
        category: Literal["eat", "play", "stay"] | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return _call(
            request, "list_candidates", trip_id,
            run_id=str(run_id) if run_id else None,
            category=category, limit=limit, offset=offset,
        )

    @router.get("/{trip_id}/candidates/{candidate_id}/evidence")
    def evidence(trip_id: str, candidate_id: str, request: Request):
        return {"items": _call(request, "candidate_evidence", trip_id, candidate_id)}

    @router.get("/{trip_id}/sources/{source_id}")
    def source(trip_id: str, source_id: str, run_id: UUID, request: Request):
        return _call(request, "get_source", trip_id, source_id, run_id=str(run_id))

    @router.get("/{trip_id}/sources")
    def sources(trip_id: str, run_id: UUID, request: Request):
        return {"items": _call(request, "list_run_sources", trip_id, str(run_id))}

    @router.get("/{trip_id}/gaps")
    def gaps(trip_id: str, request: Request, run_id: UUID | None = None):
        return {"items": _call(request, "list_gaps", trip_id, run_id=str(run_id) if run_id else None)}

    return router
