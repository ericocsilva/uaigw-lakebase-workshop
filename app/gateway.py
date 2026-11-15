"""Thin client for Unity Gateway (formerly Unity AI Gateway).

Every model call in this app goes through the gateway's provider-agnostic
Responses API:  POST {workspace}/ai-gateway/mlflow/v1/responses
with `model` set to the three-part Unity Catalog name of a *model service*
(e.g. workspace.uaigw_workshop.ab_split).

The app authenticates as its own service principal (app authorization), so the
service principal needs USE CATALOG, USE SCHEMA and EXECUTE on each service.
"""
from __future__ import annotations

import os
import re
import time
from functools import lru_cache

import httpx
from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

CATALOG = os.environ.get("WORKSHOP_CATALOG", "workspace")
SCHEMA = os.environ.get("WORKSHOP_SCHEMA", "uaigw_workshop")

SERVICES = {
    # Traffic split between two Databricks-hosted models (configured in the UI).
    "split": os.environ.get("SPLIT_SERVICE", f"{CATALOG}.{SCHEMA}.ab_split"),
    # Same model, but with guardrails (service policies) attached in the UI.
    "guarded": os.environ.get("GUARDED_SERVICE", f"{CATALOG}.{SCHEMA}.guarded_chat"),
    # Built-in system model service with no custom policies -> the comparison baseline.
    "unguarded": os.environ.get("UNGUARDED_SERVICE", "system.ai.llama-4-maverick"),
}

# Dated model ids returned by the gateway, e.g. "gemma-3-12b-it-060225".
_DATE_SUFFIX = re.compile(r"-\d{6}$")


@lru_cache(maxsize=1)
def wc() -> WorkspaceClient:
    if IS_DATABRICKS_APP:
        return WorkspaceClient()  # app service principal, injected by Databricks Apps
    return WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE"))


def host() -> str:
    h = wc().config.host
    return h.rstrip("/") if h.startswith("http") else f"https://{h}"


def auth_header() -> dict:
    # The SDK caches and refreshes the OAuth token for us.
    return wc().config.authenticate()


def backend_label(model: str) -> str:
    """Turn the model id reported by the gateway into a short backend label."""
    return _DATE_SUFFIX.sub("", model or "") or "unknown"


_NOISE = {"meta", "databricks", "it", "instruct", "models", "system", "ai"}


def _tokens(s: str) -> set[str]:
    s = _DATE_SUFFIX.sub("", (s or "").lower().split(".")[-1])
    return {t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _NOISE}


def destination_role(label: str, cfg: dict) -> str | None:
    """Was this response served by a primary or a fallback destination?
    The gateway reports the backing model id (e.g. 'qwen3-next-instruct-091725'),
    so we match it against the model names in the service's live routing config."""
    if not cfg or not cfg.get("ok"):
        return None
    lt = _tokens(label)
    best, best_score = None, 0
    for role, dests in (("primary", cfg.get("destinations", [])), ("fallback", cfg.get("fallback", []))):
        for d in dests:
            score = len(lt & (_tokens(d.get("model", "")) | _tokens(d.get("name", ""))))
            if score > best_score:
                best, best_score = role, score
    return best


def extract_text(body: dict) -> str:
    parts = []
    for item in body.get("output") or []:
        if isinstance(item, dict):
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts).strip()


def policy_info(body: dict) -> dict | None:
    """Service policies (guardrails) that DENY a call return HTTP 200 plus a
    top-level `databricks_service_policy` object describing the block."""
    p = body.get("databricks_service_policy") if isinstance(body, dict) else None
    return p if p else None


async def call_responses(client: httpx.AsyncClient, service: str, prompt: str,
                         max_output_tokens: int = 256) -> dict:
    """One request through Unity Gateway. Never raises: errors become results."""
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{host()}/ai-gateway/mlflow/v1/responses",
            headers=auth_header(),
            json={"model": service, "input": [{"role": "user", "content": prompt}],
                  "max_output_tokens": max_output_tokens},
        )
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        model = body.get("model", "") if isinstance(body, dict) else ""
        msg = ""
        if r.status_code != 200 and isinstance(body, dict):
            err = body.get("error")
            msg = body.get("message") or (err.get("message") if isinstance(err, dict) else str(err or ""))
            if not msg:
                msg = r.text[:300]
        pol = policy_info(body) if r.status_code == 200 else None
        if r.status_code == 200:
            served_by = "blocked by policy" if pol else backend_label(model)
        elif r.status_code == 429:
            # Two different 429s: the service's own rate limit (what you configure)
            # vs. Free Edition's per-model workspace QPS cap on pay-per-token models.
            served_by = "model QPS cap" if "QPS" in msg else "rate limited"
        elif r.status_code == 403 and "rate limit set to 0" in msg:
            served_by = "rate limited"
        elif r.status_code in (401, 403):
            served_by = "denied"
        elif r.status_code == 404:
            served_by = "not found"
        else:
            served_by = "error"
        return {
            "http_status": r.status_code,
            "model": model,
            "served_by": served_by,
            "text": (extract_text(body) if r.status_code == 200 else "")[:2000],
            "error_message": (msg or "")[:500],
            "policy": pol,
            "latency_ms": ms,
            "request_id": r.headers.get("x-request-id", ""),
        }
    except Exception as e:  # noqa: BLE001
        return {"http_status": 0, "model": "", "served_by": "error", "text": "", "error_message": str(e)[:500],
                "policy": None, "latency_ms": int((time.perf_counter() - t0) * 1000), "request_id": ""}


def get_service_config(full_name: str) -> dict:
    """Read a model service's live routing / rate-limit config from Unity Catalog.
    Shown in the UI so attendees see their UI changes reflected in the app."""
    try:
        r = wc().api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{full_name}")
        cfg = r.get("config", {}) or {}
        routing = cfg.get("routing", {}) or {}
        return {
            "ok": True,
            "destinations": [
                {"name": d.get("name"), "traffic_percentage": d.get("traffic_percentage"),
                 "model": (d.get("pay_per_token_config") or {}).get("model", "")}
                for d in routing.get("destinations", []) if not d.get("is_deleted")
            ],
            "fallback": [
                {"name": d.get("name"), "model": (d.get("pay_per_token_config") or {}).get("model", "")}
                for d in (routing.get("fallback") or {}).get("destinations", []) if not d.get("is_deleted")
            ],
            "rate_limits": cfg.get("rate_limits", []) or [],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e).split(" Config: host=")[0][:300]}
