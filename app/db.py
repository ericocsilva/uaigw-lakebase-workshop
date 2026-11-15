"""Lakebase (managed Postgres) access for the app.

When you add a *Lakebase* resource to a Databricks App, the platform injects
the standard Postgres env vars (PGHOST, PGDATABASE, PGUSER, PGPORT, PGSSLMODE)
for the app's service principal. There is no static password: we mint a
short-lived OAuth credential (valid 1h) with the Databricks SDK every time the
pool opens a new connection, and recycle connections well before expiry.

Everything the app stores lives in one schema (APP_SCHEMA, default `uaigw_app`)
that the app's service principal creates on startup -- so it OWNS it:
  * test_runs / test_results  -> every gateway test you run, and each request
  * chat_threads              -> one row per chat conversation (for the sidebar)
  * checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations
                              -> LangGraph's own tables (conversation state)
"""
from __future__ import annotations

import json
import os
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gateway import wc

APP_SCHEMA = os.environ.get("APP_SCHEMA", "uaigw_app")

_pool: ConnectionPool | None = None
_lock = threading.Lock()
_status = {"ok": False, "error": "not initialised yet"}
_endpoint_cache: dict[str, str] = {}

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {APP_SCHEMA};

CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.test_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    test_type       TEXT NOT NULL,          -- split | guardrail | ratelimit
    service         TEXT NOT NULL,
    prompt          TEXT,
    n_requests      INT NOT NULL,
    counts          JSONB NOT NULL,         -- {{"llama-4-maverick": 11, "gemma-3-12b-it": 9}}
    service_config  JSONB,                  -- routing / rate limits at the time of the run
    elapsed_ms      INT,
    triggered_by    TEXT
);

CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.test_results (
    result_id       BIGSERIAL PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES {APP_SCHEMA}.test_runs(run_id) ON DELETE CASCADE,
    seq             INT NOT NULL,
    service         TEXT NOT NULL,
    http_status     INT,
    served_by       TEXT,                   -- backend label, "rate limited", "blocked by policy", ...
    model           TEXT,                   -- exact model id reported by the gateway
    latency_ms      INT,
    response_text   TEXT,
    error_message   TEXT,
    policy          JSONB,                  -- databricks_service_policy object when a guardrail fired
    request_id      TEXT
);
CREATE INDEX IF NOT EXISTS test_results_run_idx ON {APP_SCHEMA}.test_results(run_id);

CREATE TABLE IF NOT EXISTS {APP_SCHEMA}.chat_threads (
    thread_id   TEXT PRIMARY KEY,
    title       TEXT,
    service     TEXT,
    user_email  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _endpoint_name() -> str:
    """Lakebase endpoint resource path, needed to mint a database credential."""
    ep = os.environ.get("LAKEBASE_ENDPOINT")
    if ep:
        return ep
    host = os.environ.get("PGHOST", "")
    if host in _endpoint_cache:
        return _endpoint_cache[host]
    # Fallback: find the endpoint whose host matches PGHOST.
    w = wc()
    for proj in w.postgres.list_projects():
        for br in w.postgres.list_branches(parent=proj.name):
            for e in w.postgres.list_endpoints(parent=br.name):
                hosts = getattr(getattr(e, "status", None), "hosts", None)
                if hosts and getattr(hosts, "host", None) == host:
                    _endpoint_cache[host] = e.name
                    return e.name
    raise RuntimeError(f"could not find the Lakebase endpoint for PGHOST={host}")


def _token() -> str:
    return wc().postgres.generate_database_credential(endpoint=_endpoint_name()).token


class OAuthConnection(psycopg.Connection):
    """psycopg connection that fetches a fresh Lakebase OAuth token on connect."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
        kwargs["password"] = _token()
        return super().connect(conninfo, **kwargs)


def configured() -> bool:
    return bool(os.environ.get("PGHOST"))


def _conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "databricks_postgres"),
        user=os.environ.get("PGUSER") or wc().config.client_id or wc().current_user.me().user_name,
        sslmode=os.environ.get("PGSSLMODE", "require"),
        options=f"-c search_path={APP_SCHEMA},public",
    )


def pool() -> ConnectionPool:
    """Lazily open the pool and create the schema/tables (idempotent)."""
    global _pool
    if _pool is not None:
        return _pool
    with _lock:
        if _pool is not None:
            return _pool
        if not configured():
            raise RuntimeError("Lakebase is not attached to this app (no PGHOST). "
                               "Add a Lakebase resource to the app and redeploy.")
        p = ConnectionPool(
            conninfo=_conninfo(),
            connection_class=OAuthConnection,
            # LangGraph's PostgresSaver requires autocommit + dict rows + no prepared statements.
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
            min_size=1, max_size=8,
            max_lifetime=45 * 60,   # recycle before the 1h OAuth token expires
            max_idle=10 * 60,
            check=ConnectionPool.check_connection,  # survives Lakebase scale-to-zero
            open=False,
        )
        p.open(wait=True, timeout=60)
        with p.connection() as conn:
            for stmt in (s.strip() for s in DDL.split(";")):
                if not stmt:
                    continue
                try:
                    conn.execute(stmt)
                except psycopg.errors.InsufficientPrivilege as e:
                    # Objects already exist but are owned by another role (e.g. the
                    # Lakebase resource was removed and re-added, which reassigns
                    # ownership). Usable as long as we were GRANTed access.
                    print(f"[db] skipped DDL (not owner): {stmt.splitlines()[0][:70]} -- {e}")
        # LangGraph creates/migrates its checkpoint tables (in APP_SCHEMA via search_path).
        from langgraph.checkpoint.postgres import PostgresSaver
        PostgresSaver(p).setup()
        _pool = p
        return _pool


def status() -> dict:
    global _status
    if not configured():
        _status = {"ok": False, "error": "No Lakebase resource attached to the app (PGHOST not set)."}
        return _status
    try:
        with pool().connection() as conn:
            row = conn.execute("SELECT current_user AS usr, current_database() AS db, version() AS v").fetchone()
        _status = {"ok": True, "user": row["usr"], "database": row["db"], "schema": APP_SCHEMA,
                   "host": os.environ.get("PGHOST"), "version": row["v"].split(" on ")[0]}
    except Exception as e:  # noqa: BLE001
        _status = {"ok": False, "error": str(e)[:400]}
    return _status


# ------------------------------------------------------------------ test runs
def save_run(test_type: str, service: str, prompt: str, results: list[dict], counts: dict,
             service_config: dict | None, elapsed_ms: int, user: str | None) -> int | None:
    if not configured():
        return None
    with pool().connection() as conn, conn.transaction():
        run_id = conn.execute(
            f"""INSERT INTO {APP_SCHEMA}.test_runs
                (test_type, service, prompt, n_requests, counts, service_config, elapsed_ms, triggered_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_id""",
            (test_type, service, prompt, len(results), json.dumps(counts),
             json.dumps(service_config) if service_config else None, elapsed_ms, user),
        ).fetchone()["run_id"]
        with conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {APP_SCHEMA}.test_results
                    (run_id, seq, service, http_status, served_by, model, latency_ms,
                     response_text, error_message, policy, request_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(run_id, i + 1, r.get("service", service), r["http_status"], r["served_by"], r["model"],
                  r["latency_ms"], r["text"], r["error_message"],
                  json.dumps(r["policy"]) if r.get("policy") else None, r["request_id"])
                 for i, r in enumerate(results)],
            )
    return run_id


def list_runs(limit: int = 50) -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(
            f"""SELECT run_id, created_at, test_type, service, prompt, n_requests, counts,
                       elapsed_ms, triggered_by
                FROM {APP_SCHEMA}.test_runs ORDER BY run_id DESC LIMIT %s""", (limit,)).fetchall()


def get_run(run_id: int) -> dict:
    with pool().connection() as conn:
        run = conn.execute(f"SELECT * FROM {APP_SCHEMA}.test_runs WHERE run_id=%s", (run_id,)).fetchone()
        res = conn.execute(f"SELECT * FROM {APP_SCHEMA}.test_results WHERE run_id=%s ORDER BY seq",
                           (run_id,)).fetchall()
    return {"run": run, "results": res}


# --------------------------------------------------------------- chat threads
def upsert_thread(thread_id: str, title: str, service: str, user: str | None) -> None:
    with pool().connection() as conn:
        conn.execute(
            f"""INSERT INTO {APP_SCHEMA}.chat_threads (thread_id, title, service, user_email)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (thread_id) DO UPDATE SET updated_at = now(), service = EXCLUDED.service""",
            (thread_id, title[:80], service, user))


def list_threads(user: str | None, limit: int = 30) -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(
            f"""SELECT t.thread_id, t.title, t.service, t.created_at, t.updated_at,
                       (SELECT count(*) FROM {APP_SCHEMA}.checkpoints c WHERE c.thread_id = t.thread_id)
                         AS checkpoints
                FROM {APP_SCHEMA}.chat_threads t
                WHERE %s::text IS NULL OR t.user_email = %s
                ORDER BY t.updated_at DESC LIMIT %s""", (user, user, limit)).fetchall()


def delete_thread(thread_id: str) -> None:
    with pool().connection() as conn:
        conn.execute(f"DELETE FROM {APP_SCHEMA}.chat_threads WHERE thread_id=%s", (thread_id,))
