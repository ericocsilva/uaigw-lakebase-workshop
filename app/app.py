"""Unity Gateway + Lakebase workshop app.

Tabs in the UI (static/index.html) map to these API routes:
  1. Traffic split   POST /api/run {test_type: "split"}      -> ab_split service
  2. Guardrails      POST /api/guardrail                     -> guarded vs unguarded
  3. Rate limits     POST /api/run {test_type: "ratelimit"}  -> ab_split service
  4. Chat            POST /api/chat, GET /api/threads...     -> LangGraph + Lakebase checkpointer
  5. History         GET  /api/runs, /api/runs/{id}          -> rows stored in Lakebase
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import gateway as gw

app = FastAPI(title="Unity Gateway + Lakebase workshop")
log = logging.getLogger("uvicorn.error")


def _self_check() -> None:
    """Runs once at startup and prints a readiness report to the app logs
    (Compute > Apps > your app > Logs). Handy when something is misconfigured."""
    lake = db.status()
    log.info("SELF-CHECK lakebase: %s", "OK " + str(lake) if lake.get("ok") else "FAILED " + lake.get("error", ""))

    async def ping(full: str) -> dict:
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await gw.call_responses(client, full, "Reply with only: ok", 16)

    for key, full in gw.SERVICES.items():
        c = gw.get_service_config(full)
        if not c.get("ok"):
            log.info("SELF-CHECK service %-9s %s -> FAILED %s", key, full, c.get("error"))
            continue
        r = asyncio.run(ping(full))
        log.info("SELF-CHECK service %-9s %s -> config OK, inference HTTP %s %s", key, full,
                 r["http_status"], r["served_by"] if r["http_status"] == 200 else r["error_message"][:200])

    if lake.get("ok"):
        try:  # LangGraph round trip: write checkpoints to Lakebase, read them back, clean up.
            import agent
            tid = f"self-check-{uuid.uuid4()}"
            out = agent.chat(tid, "Reply with only: ok", gw.SERVICES["unguarded"])
            n = agent.checkpoint_count(tid)
            agent.delete(tid)
            log.info("SELF-CHECK langgraph: OK reply=%r checkpoints=%s", out["reply"]["content"][:40], n)
        except Exception as e:  # noqa: BLE001
            log.info("SELF-CHECK langgraph: FAILED %s", str(e)[:300])


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_self_check, daemon=True).start()


def _user(request: Request) -> str | None:
    # Databricks Apps forwards the signed-in user's identity in these headers.
    return request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-preferred-username")


def _counts(results: list[dict]) -> dict:
    c: dict[str, int] = {}
    for r in results:
        c[r["served_by"]] = c.get(r["served_by"], 0) + 1
    return c


# ---------------------------------------------------------------- config/health
@app.get("/api/config")
async def config():
    h = gw.host()

    def svc_url(full: str) -> str:
        cat, sch, svc = full.split(".")
        return f"{h}/explore/model-services/{cat}/{sch}/{svc}"

    return {
        "services": gw.SERVICES,
        "links": {k: svc_url(v) for k, v in gw.SERVICES.items()},
        "usage_table": f"{h}/explore/data/system/ai_gateway/usage",
        "app_sp": os.environ.get("DATABRICKS_CLIENT_ID", "(local run: your own identity)"),
        "app_name": os.environ.get("DATABRICKS_APP_NAME", "local"),
        "schema": f"{gw.CATALOG}.{gw.SCHEMA}",
    }


@app.get("/api/status")
async def status():
    lake = await run_in_threadpool(db.status)
    svcs = {}
    for key, full in gw.SERVICES.items():
        svcs[key] = await run_in_threadpool(gw.get_service_config, full)
        svcs[key]["name"] = full
    return {"lakebase": lake, "services": svcs}


@app.get("/api/service/{key}")
async def service_config(key: str):
    if key not in gw.SERVICES:
        return JSONResponse({"error": f"unknown service '{key}'"}, status_code=400)
    return await run_in_threadpool(gw.get_service_config, gw.SERVICES[key])


# ----------------------------------------------------------- split / ratelimit
class RunReq(BaseModel):
    test_type: str = "split"         # split | ratelimit
    n: int = 20
    prompt: str = "What is Databricks? Answer in one sentence."
    sequential: bool = False


@app.post("/api/run")
async def run(req: RunReq, request: Request):
    n = max(1, min(req.n, 100))
    svc = gw.SERVICES["split"]
    cfg = await run_in_threadpool(gw.get_service_config, svc)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=120.0) as client:
        if req.sequential:
            results = [await gw.call_responses(client, svc, req.prompt, 128) for _ in range(n)]
        else:
            results = await asyncio.gather(*[gw.call_responses(client, svc, req.prompt, 128) for _ in range(n)])
    elapsed = int((time.perf_counter() - t0) * 1000)
    # Mark answers that came from a *fallback* destination: on Free Edition the
    # per-model QPS limit makes primaries return 429 under bursts, and the
    # gateway transparently retries on the fallback model.
    for r in results:
        if r["http_status"] == 200 and r["served_by"] != "blocked by policy" \
                and gw.destination_role(r["served_by"], cfg) == "fallback":
            r["served_by"] += " (fallback)"
    counts = _counts(results)
    run_id, save_err = None, None
    try:
        run_id = await run_in_threadpool(db.save_run, req.test_type, svc, req.prompt, list(results), counts,
                                         cfg if cfg.get("ok") else None, elapsed, _user(request))
    except Exception as e:  # noqa: BLE001
        save_err = str(e)[:300]
    return {"run_id": run_id, "save_error": save_err, "service": svc, "total": n, "counts": counts,
            "elapsed_ms": elapsed, "config": cfg, "results": results}


# ------------------------------------------------------------------ guardrails
class GuardReq(BaseModel):
    prompt: str


@app.post("/api/guardrail")
async def guardrail(req: GuardReq, request: Request):
    t0 = time.perf_counter()
    # Sequential on purpose: both services use the same backing model, and Free
    # Edition enforces a per-model workspace QPS limit (parallel calls -> 429).
    async with httpx.AsyncClient(timeout=120.0) as client:
        g = await gw.call_responses(client, gw.SERVICES["guarded"], req.prompt, 400)
        u = await gw.call_responses(client, gw.SERVICES["unguarded"], req.prompt, 400)
    g["service"], u["service"] = gw.SERVICES["guarded"], gw.SERVICES["unguarded"]
    elapsed = int((time.perf_counter() - t0) * 1000)
    run_id, save_err = None, None
    try:
        run_id = await run_in_threadpool(db.save_run, "guardrail", gw.SERVICES["guarded"], req.prompt, [g, u],
                                         {"guarded": g["served_by"], "unguarded": u["served_by"]},
                                         None, elapsed, _user(request))
    except Exception as e:  # noqa: BLE001
        save_err = str(e)[:300]
    return {"run_id": run_id, "save_error": save_err, "guarded": g, "unguarded": u}


# --------------------------------------------------------------------- history
@app.get("/api/runs")
async def runs():
    try:
        return {"runs": await run_in_threadpool(db.list_runs)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:400]}, status_code=503)


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: int):
    try:
        return await run_in_threadpool(db.get_run, run_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:400]}, status_code=503)


# ------------------------------------------------------------------------ chat
class ChatReq(BaseModel):
    message: str
    thread_id: str | None = None
    service: str = "guarded"   # key in gw.SERVICES


@app.post("/api/chat")
async def chat(req: ChatReq, request: Request):
    import agent  # imported lazily so the app still starts if Lakebase is missing
    if req.service not in gw.SERVICES:
        return JSONResponse({"error": f"unknown service '{req.service}'"}, status_code=400)
    thread_id = req.thread_id or str(uuid.uuid4())
    svc = gw.SERVICES[req.service]
    try:
        out = await run_in_threadpool(agent.chat, thread_id, req.message, svc)
        await run_in_threadpool(db.upsert_thread, thread_id, req.message, svc, _user(request))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:600], "thread_id": thread_id}, status_code=502)
    return {"thread_id": thread_id, "service": svc, **out}


@app.get("/api/threads")
async def threads(request: Request):
    try:
        return {"threads": await run_in_threadpool(db.list_threads, _user(request))}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:400]}, status_code=503)


@app.get("/api/threads/{thread_id}")
async def thread(thread_id: str):
    import agent
    try:
        msgs = await run_in_threadpool(agent.history, thread_id)
        cps = await run_in_threadpool(agent.checkpoint_count, thread_id)
        return {"thread_id": thread_id, "messages": msgs, "checkpoints": cps}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:400]}, status_code=503)


@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: str):
    import agent
    try:
        await run_in_threadpool(agent.delete, thread_id)
        await run_in_threadpool(db.delete_thread, thread_id)
        return {"deleted": thread_id}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:400]}, status_code=503)


# ---------------------------------------------------------------------- static
_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_static, "index.html"))
