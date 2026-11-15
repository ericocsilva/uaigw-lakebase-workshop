# Databricks notebook source
# MAGIC %md
# MAGIC # 🩺 Check (and optionally fix) my workshop setup
# MAGIC
# MAGIC Run this whenever something in the app shows a red status chip. It checks every piece the workshop builds,
# MAGIC prints ✅ / ❌ with the reason, and — **only if you set `fix_problems = yes`** — repairs the two steps
# MAGIC people most often get wrong:
# MAGIC
# MAGIC 1. **Grants**: gives the app's service principal `USE SCHEMA` + `EXECUTE` on the workshop schema.
# MAGIC 2. **App ↔ Lakebase wiring**: attaches the Lakebase database to the app as a resource.
# MAGIC
# MAGIC After a fix, **redeploy the app** (Apps → your app → Deploy) so it picks up the change.
# MAGIC
# MAGIC ▶️ Attach to **Serverless** and click **Run all**.

# COMMAND ----------

# MAGIC %pip install -q "psycopg[binary]>=3.2"

# COMMAND ----------

dbutils.widgets.text("catalog", "workspace", "Catalog")
dbutils.widgets.text("schema", "uaigw_workshop", "Schema")
dbutils.widgets.text("app_name", "uaigw-workshop", "App name")
dbutils.widgets.text("lakebase_project", "uaigw-workshop", "Lakebase project id")
dbutils.widgets.dropdown("fix_problems", "no", ["no", "yes"], "Fix problems?")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
APP = dbutils.widgets.get("app_name").strip()
PROJECT = dbutils.widgets.get("lakebase_project").strip()
FIX = dbutils.widgets.get("fix_problems") == "yes"

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
me = w.current_user.me().user_name
problems: list[str] = []


def ok(msg):
    print("✅", msg)


def bad(msg, hint):
    print("❌", msg, "\n   ↳", hint)
    problems.append(msg)


print(f"Checking as {me}  ·  fix_problems={FIX}\n")

# COMMAND ----------

# MAGIC %md ### 1 · Unity Gateway model services

# COMMAND ----------

services = {}
for svc in ("ab_split", "guarded_chat"):
    full = f"{CATALOG}.{SCHEMA}.{svc}"
    try:
        s = w.api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{full}")
        dests = [(d["name"], d.get("traffic_percentage")) for d in s["config"]["routing"].get("destinations", [])
                 if not d.get("is_deleted")]
        fb = [d["name"] for d in (s["config"]["routing"].get("fallback") or {}).get("destinations", [])]
        rl = s["config"].get("rate_limits") or []
        services[svc] = s
        ok(f"{full}: routing={dests} fallback={fb or '—'} rate_limits={len(rl)}")
    except Exception as e:  # noqa: BLE001
        bad(f"{full} not found ({str(e)[:120]})",
            "Create it in the UI (Part 2) or run setup/01_create_gateway_services.")

# COMMAND ----------

# MAGIC %md ### 2 · The app and its service principal

# COMMAND ----------

app = None
try:
    app = w.api_client.do("GET", f"/api/2.0/apps/{APP}")
    sp = app.get("service_principal_client_id")
    ok(f"app '{APP}' exists · state={app.get('compute_status', {}).get('state')} · "
       f"deployment={(app.get('active_deployment') or {}).get('status', {}).get('state', 'none yet')} · SP={sp}")
    print("   URL:", app.get("url"))
except Exception as e:  # noqa: BLE001
    bad(f"app '{APP}' not found ({str(e)[:120]})", "Create it in Compute → Apps (Part 4).")

# COMMAND ----------

# MAGIC %md ### 3 · Grants for the app on the workshop schema
# MAGIC The app calls Unity Gateway **as its own service principal**, so that identity needs
# MAGIC `USE CATALOG` + `USE SCHEMA` + `EXECUTE` (EXECUTE on the schema is inherited by every service in it).

# COMMAND ----------

def effective(securable, full, principal):
    r = w.api_client.do("GET", f"/api/2.1/unity-catalog/effective-permissions/{securable}/{full}",
                        query={"principal": principal})
    return {p["privilege"] for a in r.get("privilege_assignments", []) for p in a.get("privileges", [])}


if app:
    sp = app["service_principal_client_id"]
    try:
        cat_privs = effective("catalog", CATALOG, sp)
        sch_privs = effective("schema", f"{CATALOG}.{SCHEMA}", sp)
        missing_sch = {"USE_SCHEMA", "EXECUTE"} - sch_privs
        if "USE_CATALOG" not in cat_privs:
            missing_cat = True
            bad(f"SP lacks USE CATALOG on {CATALOG}", "Grant USE CATALOG on the catalog to the app's service principal.")
        else:
            missing_cat = False
            ok(f"SP has USE CATALOG on {CATALOG}")
        if missing_sch:
            bad(f"SP lacks {sorted(missing_sch)} on {CATALOG}.{SCHEMA}",
                "Catalog Explorer → schema → Permissions → Grant → pick the app → USE SCHEMA + EXECUTE (Part 5).")
        else:
            ok(f"SP has USE SCHEMA + EXECUTE on {CATALOG}.{SCHEMA}")

        if FIX and (missing_sch or missing_cat):
            if missing_cat:
                w.api_client.do("PATCH", f"/api/2.1/unity-catalog/permissions/catalog/{CATALOG}",
                                body={"changes": [{"principal": sp, "add": ["USE_CATALOG"]}]})
            if missing_sch:
                w.api_client.do("PATCH", f"/api/2.1/unity-catalog/permissions/schema/{CATALOG}.{SCHEMA}",
                                body={"changes": [{"principal": sp, "add": sorted(missing_sch)}]})
            print("   🔧 FIXED: granted. Takes ~1 min to apply — then refresh the app.")
    except Exception as e:  # noqa: BLE001
        bad(f"could not read grants ({str(e)[:160]})", "Does the schema exist?")

# COMMAND ----------

# MAGIC %md ### 4 · Lakebase project and the app's database resource

# COMMAND ----------

branch = f"projects/{PROJECT}/branches/production"
database = None
endpoint = None
try:
    dbs = w.api_client.do("GET", f"/api/2.0/postgres/{branch}/databases").get("databases", [])
    database = dbs[0]["name"] if dbs else None
    eps = w.api_client.do("GET", f"/api/2.0/postgres/{branch}/endpoints").get("endpoints", [])
    endpoint = eps[0] if eps else None
    ok(f"Lakebase project '{PROJECT}' · database={database} · "
       f"endpoint state={endpoint and endpoint.get('status', {}).get('current_state')}")
except Exception as e:  # noqa: BLE001
    bad(f"Lakebase project '{PROJECT}' not found ({str(e)[:120]})",
        "Create it from the app switcher → Lakebase → New project (Part 3).")

if app:
    pg = [r for r in app.get("resources", []) if "postgres" in r]
    if pg:
        ok(f"app has a Lakebase resource: {pg[0]['postgres'].get('database')}")
    else:
        bad("app has NO Lakebase resource attached",
            "Apps → your app → Edit → App resources → + Add resource → Lakebase database (Part 4).")
        if FIX and database:
            resources = [r for r in app.get("resources", []) if "postgres" not in r] + [
                {"name": "postgres", "postgres": {"branch": branch, "database": database,
                                                  "permission": "CAN_CONNECT_AND_CREATE"}}]
            w.api_client.do("POST", f"/api/2.0/apps/{APP}/update",
                            body={"update_mask": "resources", "app": {"resources": resources}})
            print("   🔧 FIXED: Lakebase resource attached. Now click Deploy on the app.")

# COMMAND ----------

# MAGIC %md ### 5 · What the app has written to Lakebase
# MAGIC Connects as **you** (the project owner) with a short-lived OAuth token, like any Postgres client.

# COMMAND ----------

import psycopg

if endpoint:
    host = endpoint["status"]["hosts"]["host"]
    token = w.api_client.do("POST", "/api/2.0/postgres/credentials",
                            body={"endpoint": endpoint["name"]})["token"]
    pgdb = "databricks_postgres"
    with psycopg.connect(host=host, dbname=pgdb, user=me, password=token, sslmode="require",
                         autocommit=True) as conn:
        # Ownership: the app should own uaigw_app. Removing and re-adding the Lakebase
        # resource reassigns the app's objects to whoever removed it, and the app's new
        # role then gets "permission denied". The current owner can grant access back.
        owner = conn.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='uaigw_app'").fetchone()
        sp_id = app["service_principal_client_id"] if app else None
        granted = bool(owner and sp_id and conn.execute(
            """SELECT has_schema_privilege(%s, 'uaigw_app', 'USAGE,CREATE')
                      AND coalesce(bool_and(has_table_privilege(%s, c.oid, 'SELECT,INSERT,UPDATE,DELETE')), true)
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'uaigw_app' AND c.relkind = 'r'""", (sp_id, sp_id)).fetchone()[0])
        if owner and sp_id and owner[0].strip('"') != sp_id and granted:
            ok(f"schema uaigw_app is owned by {owner[0]}, and the app has been granted full access")
        elif owner and sp_id and owner[0].strip('"') != sp_id:
            bad(f"schema uaigw_app is owned by {owner[0]}, not by the app ({sp_id})",
                "Happens after removing and re-adding the app's Lakebase resource. Re-run with fix_problems = yes.")
            if FIX:
                for stmt in (f'GRANT USAGE, CREATE ON SCHEMA uaigw_app TO "{sp_id}"',
                             f'GRANT ALL ON ALL TABLES IN SCHEMA uaigw_app TO "{sp_id}"',
                             f'GRANT ALL ON ALL SEQUENCES IN SCHEMA uaigw_app TO "{sp_id}"'):
                    conn.execute(stmt)
                print("   🔧 FIXED: granted the app full access to uaigw_app. Restart the app (Stop ▸ Start).")
        elif owner:
            ok(f"schema uaigw_app is owned by the app ({sp_id})")
        rows = conn.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'uaigw_app' ORDER BY 1""").fetchall()
        if not rows:
            bad("schema uaigw_app has no tables yet",
                "Deploy the app and open it once — it creates its tables on startup.")
        else:
            for (t,) in rows:
                n = conn.execute(f'SELECT count(*) FROM uaigw_app."{t}"').fetchone()[0]
                ok(f"uaigw_app.{t:<24} {n:>6} rows")

# COMMAND ----------

print("\n" + ("🎉 Everything looks good!" if not problems else
              f"{len(problems)} problem(s) found." + ("" if FIX else "  Re-run with fix_problems = yes to auto-fix grants and the app resource.")))
