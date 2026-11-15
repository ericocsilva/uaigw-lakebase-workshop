# Databricks notebook source
# MAGIC %md
# MAGIC # 🛟 Rescue: create the Unity Gateway model services
# MAGIC
# MAGIC **Only use this if you got stuck in Part 2 of the workshop** (creating the model services in the UI).
# MAGIC It creates exactly what the UI steps create, and it is safe to run more than once:
# MAGIC
# MAGIC | Service | Routing | Used by |
# MAGIC |---|---|---|
# MAGIC | `workspace.uaigw_workshop.ab_split` | 50 % `llama-4-maverick` / 50 % `gemma-3-12b-it`, fallback `qwen3-next-80b-a3b-instruct` | Traffic split, Rate limits, Chat |
# MAGIC | `workspace.uaigw_workshop.guarded_chat` | 100 % `llama-4-maverick` | Guardrails, Chat |
# MAGIC
# MAGIC Guardrails (service policies) are **not** created here — attaching them in the UI is the point of Part 6.
# MAGIC
# MAGIC ▶️ Attach the notebook to **Serverless** and click **Run all**.

# COMMAND ----------

dbutils.widgets.text("catalog", "workspace", "Catalog")
dbutils.widgets.text("schema", "uaigw_workshop", "Schema")
dbutils.widgets.dropdown("reset_routing", "no", ["no", "yes"], "Reset existing routing to the defaults?")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
RESET = dbutils.widgets.get("reset_routing") == "yes"

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()  # runs as you, the notebook user
print(f"Running as {w.current_user.me().user_name} on {w.config.host}")

# COMMAND ----------

# MAGIC %md ### 1 · Schema
# MAGIC A model service is a Unity Catalog securable, so it lives in a schema — just like a table.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}` COMMENT 'Unity Gateway + Lakebase workshop'")
print(f"✅ schema {CATALOG}.{SCHEMA} ready")

# COMMAND ----------

# MAGIC %md ### 2 · Model services

# COMMAND ----------

def dest(name, model, pct=None):
    d = {"name": name,
         "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
         "pay_per_token_config": {"model": f"models/system.ai.{model}"}}
    if pct is not None:
        d["traffic_percentage"] = pct
    return d


SERVICES = {
    "ab_split": {
        "comment": "Workshop: traffic split between two Databricks-hosted models",
        "config": {"routing": {
            "destinations": [dest("llama-4-maverick", "llama-4-maverick", 50),
                             dest("gemma-3-12b", "gemma-3-12b-it", 50)],
            "fallback": {"destinations": [dest("qwen3-next-80b", "qwen3-next-80b-a3b-instruct")]},
        }},
    },
    "guarded_chat": {
        "comment": "Workshop: model service with guardrails (service policies)",
        "config": {"routing": {"destinations": [dest("llama-4-maverick", "llama-4-maverick", 100)]}},
    },
}

BASE = "/api/2.1/unity-catalog/model-services"

for svc_id, body in SERVICES.items():
    full = f"{CATALOG}.{SCHEMA}.{svc_id}"
    try:
        current = w.api_client.do("GET", f"{BASE}/{full}")
        exists = True
    except Exception as e:  # noqa: BLE001
        if "does not exist" not in str(e).lower() and "not found" not in str(e).lower():
            raise
        exists = False

    if not exists:
        w.api_client.do("POST", BASE, query={"parent": f"schemas/{CATALOG}.{SCHEMA}", "model_service_id": svc_id},
                        body=body)
        print(f"✅ created {full}")
    elif RESET:
        w.api_client.do("PATCH", f"{BASE}/{full}",
                        query={"update_mask": "config.routing.destinations,config.routing.fallback.destinations"},
                        body={"config": {"routing": {
                            "destinations": body["config"]["routing"]["destinations"],
                            "fallback": body["config"]["routing"].get("fallback", {"destinations": []}),
                        }}})
        print(f"♻️  reset routing on {full}")
    else:
        print(f"👍 {full} already exists — left untouched (set reset_routing=yes to restore defaults)")

# COMMAND ----------

# MAGIC %md ### 3 · Smoke test
# MAGIC New services take **up to ~1 minute** to become callable. This cell waits for them.

# COMMAND ----------

import time

for svc_id in SERVICES:
    full = f"{CATALOG}.{SCHEMA}.{svc_id}"
    for attempt in range(18):
        try:
            r = w.api_client.do("POST", "/ai-gateway/mlflow/v1/responses",
                                body={"model": full, "max_output_tokens": 16,
                                      "input": [{"role": "user", "content": "Reply with only: ok"}]})
            print(f"✅ {full} answered (served by {r.get('model')})")
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 17:
                print(f"⚠️  {full} not callable yet: {str(e)[:200]}")
            time.sleep(10)

host = w.config.host.rstrip("/")
print(f"\nOpen them in the UI: {host}/explore/data/{CATALOG}/{SCHEMA}")
