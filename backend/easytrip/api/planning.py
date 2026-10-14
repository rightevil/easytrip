"""Read frozen planning inputs and stored versions; never accept client route facts."""

from uuid import UUID

from fastapi import APIRouter, Request

from ..domain.planning import AdoptPlan
from .trips import _call


def make_router():
    router = APIRouter(prefix="/api/trips", tags=["planning"])

    @router.get("/{trip_id}/planning-input")
    def inputs(trip_id: str, request: Request):
        return _call(request, "planning_input", trip_id)

    @router.get("/{trip_id}/plans")
    def plans(trip_id: str, request: Request):
        return _call(request, "list_plans", trip_id)

    @router.get("/{trip_id}/plans/{plan_id}")
    def plan(trip_id: str, plan_id: UUID, request: Request):
        return _call(request, "get_plan", trip_id, str(plan_id))

    @router.post("/{trip_id}/plans/{plan_id}/adopt")
    def adopt(trip_id: str, plan_id: UUID, body: AdoptPlan, request: Request):
        return _call(request, "adopt_plan", trip_id, str(plan_id), body)

    return router
