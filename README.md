# Unity Gateway + Lakebase workshop

A hands-on workshop for **Databricks Free Edition**: deploy a Databricks App that

- tests **Unity Gateway** model services: traffic split, fallback, rate limits, guardrails (service policies);
- chats through a **LangGraph** agent whose state is checkpointed in **Lakebase** (managed Postgres);
- stores **every test run and every request** in Lakebase, so you can analyze them with SQL.

👉 **Start here: [WORKSHOP.md](WORKSHOP.md)**, a step-by-step guide that starts from a brand-new Free Edition workspace.

## Repository layout

| Path | What it is |
|---|---|
| `app/app.py` | FastAPI backend: test endpoints, chat, history |
| `app/gateway.py` | Calls Unity Gateway (`/ai-gateway/mlflow/v1/responses`) and reads live service config |
| `app/agent.py` | LangGraph graph + `PostgresSaver` checkpointer on Lakebase |
| `app/db.py` | Lakebase connection pool with OAuth token rotation; schema + tables created on startup |
| `app/static/index.html` | Single-page UI (5 tabs) |
| `app/app.yaml`, `app/requirements.txt` | Databricks Apps runtime config |
| `setup/01_create_gateway_services.py` | 🛟 Rescue notebook: creates the two model services |
| `setup/02_check_and_fix.py` | 🛟 Diagnoses the whole setup, and can fix grants and the app's Lakebase resource |
| `sql/lakebase_queries.sql` | Queries for the Lakebase SQL editor |

## Data model (Lakebase schema `uaigw_app`, owned by the app)

```
test_runs(run_id, created_at, test_type, service, prompt, n_requests, counts, service_config, elapsed_ms, triggered_by)
test_results(result_id, run_id → test_runs, seq, service, http_status, served_by, model, latency_ms, response_text, error_message, policy, request_id)
chat_threads(thread_id, title, service, user_email, created_at, updated_at)
checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations   ← LangGraph
```

Derived from the `uaigw-split-demo` app (Azure Foundry vs Databricks split), adapted to models available in Free Edition.
