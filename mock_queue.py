"""FlowPilot queue contract — in-memory reference implementation.

FOR LOCAL TESTING ONLY. Implements QUEUE_CONTRACT.md exactly:
5 endpoints + Bearer auth + status lifecycle + idempotency rules.

Run:   FLOWPILOT_QUEUE_TOKEN=<secret> uvicorn mock_queue:app --port 8899
"""
from __future__ import annotations

import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Token handling: env only, never logged.
# ---------------------------------------------------------------------------
DEFAULT_TOKEN = "dev-token-change-me"
TOKEN = os.environ.get("FLOWPILOT_QUEUE_TOKEN", DEFAULT_TOKEN)

log = logging.getLogger("mock_queue")

app = FastAPI(title="FlowPilot mock queue", version="1.0")

if TOKEN == DEFAULT_TOKEN:
    log.warning(
        "FLOWPILOT_QUEUE_TOKEN is unset: using default dev token "
        "'dev-token-change-me'. DO NOT use this outside localhost."
    )

# ---------------------------------------------------------------------------
# Storage: one dict, keyed by task_id.
# ---------------------------------------------------------------------------
TERMINAL = {"done", "failed"}

tasks: Dict[str, Dict[str, Any]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def err(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


async def require_auth(authorization: Optional[str] = Header(default=None)):
    """Bearer check on every call. Constant-time compare. 401 either way."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    provided = authorization[len("Bearer "):]
    if not provided or not hmac.compare_digest(provided, TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    # Normalize all auth failures (and other HTTPExceptions) to the envelope.
    code = "unauthorized" if exc.status_code == 401 else "bad_request"
    return err(code, exc.detail if isinstance(exc.detail, str) else "request failed", exc.status_code)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class TaskIn(BaseModel):
    task_id: str = Field(..., max_length=128)
    goal: str = Field(..., max_length=2000)
    snapshot: Dict[str, Any]
    site_pack: str = Field(..., max_length=128)
    created_at: str


class ResponseIn(BaseModel):
    plan: Dict[str, Any]


class ResultIn(BaseModel):
    status: str
    result: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/tasks", status_code=201, dependencies=[Depends(require_auth)])
async def post_task(body: TaskIn, response: Response):
    tid = body.task_id
    if tid in tasks:
        # Idempotent re-POST: return existing record, never overwrite.
        response.status_code = 200
        log.info("re-post task_id=%s status=%s", tid, tasks[tid]["status"])
        return {"task_id": tid, "status": tasks[tid]["status"]}
    record = {
        "task_id": tid,
        "goal": body.goal,
        "snapshot": body.snapshot,
        "site_pack": body.site_pack,
        "created_at": body.created_at,
        "updated_at": now_iso(),
        "status": "pending",
    }
    tasks[tid] = record
    log.info("new task_id=%s site_pack=%s", tid, body.site_pack)
    return {"task_id": tid, "status": "pending"}


@app.get("/tasks", dependencies=[Depends(require_auth)])
async def list_tasks(status: Optional[str] = None):
    # Default filter is pending per contract; unknown values -> 400.
    wanted = status or "pending"
    valid = {"pending", "responded", "executing", "done", "failed"}
    if wanted not in valid:
        return err("bad_request", f"unknown status filter: {wanted}", 400)
    out: List[Dict[str, Any]] = []
    for t in tasks.values():
        if t["status"] != wanted:
            continue
        # Contract permits truncation/omission of snapshot in listings.
        # Here we include it verbatim (reference impl); operators MAY strip it.
        out.append(
            {
                "task_id": t["task_id"],
                "goal": t["goal"],
                "snapshot": t["snapshot"],
                "site_pack": t["site_pack"],
                "created_at": t["created_at"],
                "status": t["status"],
            }
        )
    return {"tasks": out}


@app.post("/tasks/{task_id}/response", dependencies=[Depends(require_auth)])
async def post_response(task_id: str, body: ResponseIn):
    if task_id not in tasks:
        return err("not_found", f"unknown task_id: {task_id}", 404)
    plan = body.plan
    if not isinstance(plan.get("steps"), list):
        return err("bad_request", "plan must be an object with steps as an array", 400)
    t = tasks[task_id]
    if t["status"] != "pending":
        # Race guard: first claim wins; responders treat 409 as "already claimed".
        log.info("response race on task_id=%s current=%s", task_id, t["status"])
        return err("conflict", f"task is already {t['status']}", 409)
    t["plan"] = plan
    t["status"] = "responded"
    t["updated_at"] = now_iso()
    log.info("plan accepted task_id=%s", task_id)
    return {"task_id": task_id, "status": "responded"}


@app.get("/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def get_task(task_id: str):
    if task_id not in tasks:
        return err("not_found", f"unknown task_id: {task_id}", 404)
    t = tasks[task_id]
    # Omit plan/result when absent (no null placeholders).
    record = {
        "task_id": t["task_id"],
        "status": t["status"],
        "goal": t["goal"],
        "snapshot": t["snapshot"],
        "site_pack": t["site_pack"],
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
    }
    if "plan" in t:
        record["plan"] = t["plan"]
    if "result" in t:
        record["result"] = t["result"]
    return record


@app.post("/tasks/{task_id}/result", dependencies=[Depends(require_auth)])
async def post_result(task_id: str, body: ResultIn):
    if task_id not in tasks:
        return err("not_found", f"unknown task_id: {task_id}", 404)
    if body.status not in {"executing", "done", "failed"}:
        return err("bad_request", "status must be executing, done or failed", 400)
    t = tasks[task_id]
    current = t["status"]
    if current in TERMINAL:
        # Terminal states are final: acknowledge, never overwrite.
        log.info("result post to terminal task_id=%s status=%s", task_id, current)
        return {"task_id": task_id, "status": current}
    legal = {
        "responded": {"executing", "done", "failed"},
        "executing": {"done", "failed"},
    }.get(current, set())
    if body.status not in legal:
        return err(
            "conflict",
            f"illegal transition {current} -> {body.status}",
            409,
        )
    if body.result is not None:
        t["result"] = body.result
    t["status"] = body.status
    t["updated_at"] = now_iso()
    log.info("task_id=%s -> %s", task_id, body.status)
    return {"task_id": task_id, "status": body.status}


# ---------------------------------------------------------------------------
# Dev helper (not part of the contract): clear all state between test runs.
# ---------------------------------------------------------------------------
@app.post("/_reset", dependencies=[Depends(require_auth)], include_in_schema=False)
async def reset():
    tasks.clear()
    return {"ok": True}
