"""Persisted local preparation tasks, polled by their owning trip."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from ..domain.tasks import CreateRun, RunControl
from .trips import _call


def _executor(request):
    executor = getattr(request.app.state, "executor", None)
    if executor is None:
        raise HTTPException(
            503,
            detail={
                "code": "executor_unavailable",
                "message": "准备任务暂不可用，请稍后重试",
            },
        )
    return executor


def make_router():
    router = APIRouter(prefix="/api", tags=["runs"])

    @router.post("/trips/{trip_id}/runs", status_code=201)
    async def create_run(trip_id: str, body: CreateRun, request: Request):
        executor = _executor(request)
        run = await asyncio.to_thread(_call, request, "enqueue_run", trip_id, body)
        executor.wake()
        return run

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, trip_id: UUID, request: Request):
        run = _call(request, "get_run", run_id, str(trip_id))
        if run["kind"] == "discover":
            run["trip_usage"] = _call(request, "trip_usage", str(trip_id))
            if (run.get("stop_reason") or "").startswith("model_"):
                for action in reversed(_call(request, "list_actions", run_id)):
                    if action["kind"] != "model":
                        continue
                    code = (action.get("usage") or {}).get("http_status")
                    if type(code) is int and 400 <= code <= 599:
                        run["model_http_status"] = code
                    break
        return run

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, body: RunControl, request: Request):
        run = await asyncio.to_thread(
            _call, request, "cancel_run", run_id, str(body.trip_id), body.generation
        )
        executor = getattr(request.app.state, "executor", None)
        if executor is not None and run["status"] == "cancelled":
            executor.interrupt(run_id, body.generation)
        return run

    @router.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, body: RunControl, request: Request):
        executor = _executor(request)
        run = await asyncio.to_thread(
            _call, request, "resume_run", run_id, str(body.trip_id), body.generation
        )
        executor.wake()
        return run

    return router
