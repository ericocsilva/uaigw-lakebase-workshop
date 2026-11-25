# Workshop: Unity Gateway + Lakebase on Databricks Free Edition

You will deploy a small web app that **governs model traffic with Unity Gateway** and **persists everything in Lakebase** (managed Postgres):

- **Traffic split**: one model name, two models behind it, routed by percentages you control.
- **Fallback**: when a model is overloaded, the gateway retries on a backup model automatically.
- **Rate limits**: cap how many requests per minute a service accepts.
- **Guardrails** (service policies): mask PII, block jailbreaks or confidential topics.
- **Chat**: a LangGraph agent whose conversation state is checkpointed in Lakebase.
- **History**: every test you run is stored in Lakebase, and you query it with SQL at the end.

```
 Browser ──► Databricks App (FastAPI + LangGraph)  ── runs as the app's service principal
                 │                     │
                 │  model calls        │  every test + every chat step
                 ▼                     ▼
          Unity Gateway           Lakebase (Postgres)
   workspace.uaigw_workshop.*      project uaigw-workshop
   ├─ ab_split ──► llama-4-maverick 50% │ gemma-3-12b 50%   (fallback: qwen3-next-80b)
   └─ guarded_chat ──► llama-4-maverick + guardrails
```

⏱️ **About 75 minutes.** Every part starts with **Why**, a short explanation of what you are about to do and why it matters.

> 🧭 **Conventions**
> - 🖱️ = do it in the UI. ⌨️ = copy and paste. 🛟 = rescue: use it only if you get stuck.
> - UI labels change over time. If a button label in this guide doesn't match exactly, look for the closest one.
> - The names below (`uaigw_workshop`, `ab_split`, `uaigw-workshop`, …) are referenced by the app. **Use them exactly as written.**

---

## Part 0 · Prerequisites (5 min)

**Why:** Free Edition is a no-cost Databricks workspace with serverless compute, Unity Catalog, Unity Gateway, Lakebase and Apps. It's everything this workshop needs.

1. 🖱️ Sign up or log in at <https://www.databricks.com/learn/free-edition>. You're the **admin** of your own workspace, which the guardrails part requires.
2. Keep the workspace open in a browser tab. Everything happens in the browser, so you don't need to install anything locally.

---

## Part 1 · Get the code into your workspace with a Git folder (5 min)

**Why:** A *Git folder* is a clone of a Git repository that lives in your workspace. The app's source code, the helper notebooks and the SQL queries all come from it. You can deploy the app straight from this folder, and you can pull updates later.

1. 🖱️ In the left sidebar, click **Workspace**, then go to **Home** (your user folder `/Users/<your-email>`).
2. 🖱️ Click **Create ▸ Git folder**.
3. ⌨️ **Git repository URL**: `https://github.com/ericocsilva/uaigw-lakebase-workshop`
   - **Git provider**: GitHub (auto-detected). **Git folder name**: leave it as `uaigw-lakebase-workshop`.
4. 🖱️ Click **Create Git folder**. The repo is public, so no GitHub credentials are needed.

You should now see:

```
uaigw-lakebase-workshop/
├── app/            ← the Databricks App (FastAPI + LangGraph + Lakebase)
├── setup/          ← 🛟 rescue notebooks (only if you get stuck)
├── sql/            ← queries you will run in Lakebase at the end
└── WORKSHOP.md     ← this guide
```

> 💡 Open `app/app.yaml`. It holds no secrets and no database password. The Lakebase connection is injected by the platform (Part 4), and credentials are short-lived OAuth tokens.

---

## Part 2 · Unity Gateway: create the model services (15 min)

**Why:** In Unity Gateway, a **model service** is a *virtual model*: one stable, governed name (`catalog.schema.service`) that apps call, with routing, fallbacks, rate limits and guardrails configured behind it. Because a model service is a **Unity Catalog securable** (just like a table), access is controlled with ordinary UC grants. Databricks-hosted models already exist as services under `system.ai`. You'll build your own on top of them.

### 2.1 Create the schema

1. 🖱️ Sidebar ▸ **Catalog** ▸ catalog **`workspace`** ▸ **Create schema**.
2. ⌨️ Name: `uaigw_workshop`, then click **Create**.

> **Why a dedicated schema?** Services live in schemas, and you'll grant the app access at schema level. One grant then covers every service inside it.

### 2.2 Create `ab_split`, the traffic-split service

1. 🖱️ Sidebar ▸ **AI Gateway** (may appear as **Unity Gateway**) ▸ **Models** ▸ **+ Model**.
   *(Alternative: Catalog Explorer ▸ `workspace.uaigw_workshop` ▸ **Create ▸ Service ▸ Model service**.)*
2. ⌨️ Name `ab_split`, catalog `workspace`, schema `uaigw_workshop`.
3. 🖱️ Primary destination: **`system.ai.llama-4-maverick`**. Click **Create**.
4. 🖱️ On the service page, in the **Routing** section, click **Add another model** and pick **`system.ai.gemma-3-12b-it`** (shown as *gemma-3-12b*).
5. ⌨️ Set **Traffic percentage** to **50 / 50**. It saves automatically once the percentages add up to 100.
6. 🖱️ Still in **Routing**, add a **fallback** destination: **`system.ai.qwen3-next-80b-a3b-instruct`**.

> **Why these models?** They're fast, non-reasoning chat models available in Free Edition, and each reports a distinct model id, so the app can show which backend answered every request.
> **Why a fallback?** If the chosen model fails with a 429 or 5xx, the gateway retries on the fallback **transparently**. The caller still gets an answer. You'll trigger this live in Part 6.

### 2.3 Create `guarded_chat`, the service that will carry guardrails

1. 🖱️ **+ Model** again ▸ name `guarded_chat` ▸ catalog `workspace`, schema `uaigw_workshop` ▸ primary destination **`system.ai.llama-4-maverick`** ▸ **Create**.
   Leave it at 100% to one model. You'll attach guardrails in Part 6.

### 2.4 (Optional) Try it in the playground

🖱️ On `ab_split`, click **Chat in playground** and send a couple of messages. The response metadata shows which model answered.

> ⏳ A new service can take **up to ~1 minute** before it accepts requests.

> 🛟 **Stuck?** Open `setup/01_create_gateway_services` in your Git folder, attach **Serverless**, and click **Run all**. It creates exactly the same two services, it's safe to rerun, and its widget `reset_routing=yes` restores the 50/50 defaults later.

---

## Part 3 · Lakebase: create a Postgres project (5 min)

**Why:** The app needs **durable, transactional state**: conversation checkpoints that are rewritten on every chat turn, and one row per request in your tests. That's OLTP work, and Lakebase is serverless Postgres built for it: it autoscales, **scales to zero** when idle, and has copy-on-write branches. Authentication uses your Databricks identity, so there's no password to manage.

1. 🖱️ Open the **app switcher** (the grid icon, top right) ▸ **Lakebase Postgres** ▸ **Autoscaling** (if asked).
2. 🖱️ **New project**. Name it exactly `uaigw-workshop` and keep the default Postgres version. Click **Create**.
3. ⏳ Wait until the **production** branch's compute shows **Active** (about a minute).

What you get automatically: a **project** containing a **`production` branch**, a default database **`databricks_postgres`**, and a **compute** (endpoint) that autoscales and suspends when idle.

> **Why no tables yet?** The **app creates its own schema (`uaigw_app`) and tables on startup**, so its identity *owns* them. If you created them yourself, the app wouldn't have permission to use them. This "let the app own its schema" pattern is the standard way to avoid `permission denied` errors with Lakebase apps.

---

## Part 4 · Create the app, attach Lakebase, deploy (15 min)

**Why:** A Databricks App runs your code on serverless compute, behind workspace SSO, with its **own identity** (a *service principal*). Instead of putting connection strings in code, you declare **resources**. Attaching a Lakebase database as a resource does three things: it creates a Postgres role for the app's service principal, grants it `CONNECT` and `CREATE` on the database, and injects `PGHOST`, `PGDATABASE`, `PGUSER`… into the app's environment.

### 4.1 Create the app

1. 🖱️ App switcher ▸ **Databricks Apps** (or sidebar ▸ **Compute ▸ Apps**) ▸ **+ Create app** ▸ **Create a custom app**.
2. ⌨️ Name: `uaigw-workshop`. App names can't be changed later.
3. 🖱️ Skip **Configure Git** (click **Next: Configure**). This workshop deploys from your Git folder instead.
4. 🖱️ In **App resources**, click **+ Add resource ▸ Database** and set:
   - **Project** `uaigw-workshop` · **Branch** `production` · **Database** `databricks_postgres`
   - **Permission**: *Can connect and create* · **Resource key**: leave the default `postgres`
5. 🖱️ **Create app**. Wait for the app compute to become **Running** (2–3 min).

### 4.2 Deploy the code

1. 🖱️ On the app page, click **Deploy**.
2. 🖱️ For the source code path, browse to your Git folder's **`app`** sub-folder:
   `/Workspace/Users/<your-email>/uaigw-lakebase-workshop/app`
3. 🖱️ **Deploy**. Watch the **Deployments** / **Logs** tab. The app installs `langgraph`, `psycopg`, etc. from `requirements.txt`, then starts.
4. 🖱️ Open the app **URL**.

### 4.3 Read the status chips (expected: some red ❗)

At the top of the app you'll see:

| Chip | Expected now | Why |
|---|---|---|
| **Lakebase: connected** | 🟢 | The resource from 4.1 worked. The app created schema `uaigw_app` and its tables. |
| **✗ workspace.uaigw_workshop.ab_split** | 🔴 | The app's service principal has **no Unity Catalog privileges** on your schema yet. |
| **✗ workspace.uaigw_workshop.guarded_chat** | 🔴 | Same reason. |
| **App identity: `<uuid>`** | — | This is the app's service principal. **Copy this id.** |

This is Unity Catalog governance at work: the app is a separate identity, and you haven't let it use your services yet. Part 5 fixes that.

> 💡 The app also prints a **self-check** at startup. Open **Logs** and look for `SELF-CHECK` lines. They report, for Lakebase, each gateway service, and a LangGraph round trip, whether it works and why not. This is the first place to look whenever something is red.

---

## Part 5 · Grant the app access to the gateway services (5 min)

**Why:** Model services use Unity Catalog privileges. A caller needs **`USE CATALOG`** on the catalog, **`USE SCHEMA`** on the schema, and **`EXECUTE`** on the service. Granting `EXECUTE` **on the schema** is inherited by every service inside it, now and in the future. The app's service principal already has `USE CATALOG` on `workspace`, because every workspace identity does in Free Edition.

1. 🖱️ **Catalog** ▸ `workspace` ▸ `uaigw_workshop` ▸ **Permissions** tab ▸ **Grant**.
2. 🖱️ **Principals**: type `uaigw-workshop` and pick the service principal named like `app-xxxx uaigw-workshop`. It's the app's identity, the same UUID as the chip.
3. 🖱️ Tick **USE SCHEMA** and **EXECUTE** ▸ **Confirm**.
4. ⏳ Wait ~1 minute, then **reload the app**. All chips should be 🟢.

<details><summary>⌨️ Prefer SQL? (SQL editor, Serverless Starter Warehouse)</summary>

```sql
-- replace with the "App identity" UUID shown in the app
GRANT USE SCHEMA, EXECUTE ON SCHEMA workspace.uaigw_workshop TO `00000000-0000-0000-0000-000000000000`;
```
</details>

> **Why doesn't the app just use *my* permissions?** It could, via *user authorization* (on-behalf-of the user). This workshop uses **app authorization** on purpose, so you can watch a non-human identity being governed. In production you choose per use case.

> 🛟 **Still red?** Run `setup/02_check_and_fix` (Serverless ▸ Run all). It checks every part of the workshop and tells you exactly what's missing. With `fix_problems = yes` it also grants the privileges and attaches the Lakebase resource for you, after which you click **Deploy** again.

---

## Part 6 · Use the app and change the policies (20 min)

Each test you run is stored in Lakebase, and you'll query all of it in Part 7. After changing anything in Unity Gateway, **allow 1–2 minutes** for it to propagate.

### 6.1 Traffic split

1. 🖱️ App ▸ **1 · Traffic split** ▸ **Send burst** (20). The bar shows roughly **50/50** between `meta-llama-4-maverick` and `gemma-3-12b-it`. The **Routing now** line is read live from Unity Catalog.
2. 🖱️ Change the policy: AI Gateway ▸ `ab_split` ▸ **Routing** ▸ set **90 / 10**.
3. ⏳ Wait ~1–2 min, then in the app click **Send burst** again. The split moves towards 90/10.

> **Why it matters:** That's a **canary rollout** or **A/B test** with zero application changes. The app keeps calling `ab_split`, and you moved the traffic. Over many requests the observed share converges to the configured percentages, so small bursts are noisy.

### 6.2 Fallback, live

1. 🖱️ Set the burst **count to 60** and **Send burst**.
2. Some answers now show **`qwen3-next-instruct (fallback)`**. Free Edition caps each pay-per-token model at a few requests per second (a per-model *QPS cap*). When the burst exceeded it, the primary returned **429**, and the gateway transparently retried on the **fallback** model. A few requests may still show **model QPS cap** if the fallback was saturated too.

> **Why it matters:** Your app saw answers where it would otherwise have seen errors. That's resiliency configured once in the gateway rather than coded into every client.

### 6.3 Rate limits

1. 🖱️ AI Gateway ▸ `ab_split` ▸ find the **Rate limits** section on the service page ▸ add a **service-wide** (endpoint) limit of **5 requests per minute**.
2. ⏳ Wait **~2 min**. Then app ▸ **3 · Rate limits**: the **Rate limits now** line shows your limit. Click **Fire burst** (15).
3. Expect about 5 ✅ and the rest **`rate limited`** (HTTP 429, *"User defined rate limit(s) exceeded"*).
4. 🖱️ Try a **per-user** limit, or set the limit to **0** (everything is blocked with HTTP 403). Then **remove the limit** so the other tabs work again.

> **Why it matters:** Rate limits protect shared capacity and budgets. They're enforced **before** the model is called, so rejected calls cost nothing.
> **Two kinds of 429:** `rate limited` is **your** policy, while `model QPS cap` is Free Edition's platform limit. The app labels them differently.

### 6.4 Guardrails (service policies)

**Why:** Grants decide *who* may call a service. **Service policies** decide *how each interaction proceeds*, based on the content of the request and the response. They run before the model (input) and after it (output), and they can **mask** or **block**.

**a) Turn on the preview (one time, admin):** 🖱️ your avatar (top right) ▸ **Previews** ▸ search **Unity Gateway Service Policies** — the label sometimes appears as **Enforce Unity Gateway** instead ▸ turn it **On**.

**b) Baseline:** 🖱️ App ▸ **2 · Guardrails** ▸ prompt **PII** ▸ **Send through both**. Both sides behave the same, since no policy is attached yet.

**c) Mask sensitive data:**
1. 🖱️ AI Gateway ▸ `guarded_chat` ▸ **Policies** ▸ **New policy**. Name: `mask-pii`.
2. 🖱️ **Guardrail type**: *Sensitive Data Detection*. **Classification tags**: `class.email_address`, `class.credit_card`, `class.us_ssn`. **Action**: *redact*/*mask*. **Phase**: Input **and** Output. **Rank** `1`. Under **Advanced options** keep **Enforce** ▸ **Create policy**.
3. ⏳ ~2 min, then **Send through both** again. The **Guarded** side shows placeholder tokens (such as `[US_SSN]`) where the email, card and SSN were, while **Unguarded** repeats the PII.

> ⚠️ **Both sides refuse instead of showing the difference?** Some models — including current `llama-4-maverick` — refuse on their own to repeat sensitive data, so guarded and unguarded look identical even though the policy is working. ⌨️ In the app, edit the prompt to one the model will obey verbatim:
>
> ```text
> Reply with exactly the following text, word for word, and nothing else: Contact: alice.smith@example.com, card 4111 1111 1111 1111, SSN 123-45-6789.
> ```
>
> Now the **Guarded** side echoes the placeholder tokens (`[EMAIL_ADDRESS]`, `[CREDIT_CARD]`, `[US_SSN]`) while **Unguarded** repeats the raw PII — the expected result of step 3.

**d) Block jailbreaks:** 🖱️ New policy `block-jailbreak` ▸ Guardrail type *Jailbreak* ▸ Phase Input ▸ Rank `2` ▸ Create. Then in the app use the **Jailbreak** prompt. The guarded side shows **⛔ BLOCKED BY POLICY** plus the policy decision (`databricks_service_policy`). The block comes back as a normal HTTP 200 so chat clients don't break.

**e) (Optional) Your own rule as a SQL function:** in the **SQL editor** (Serverless Starter Warehouse) run:

```sql
CREATE OR REPLACE FUNCTION workspace.uaigw_workshop.block_codename(event VARIANT)
RETURNS VARIANT
LANGUAGE SQL
RETURN CASE
  WHEN event:type::string = 'request'
   AND contains(lower(event:context.message::string), 'project aurora')
  THEN to_variant_object(named_struct('result','DENY','reason','Requests about confidential projects are not permitted.'))
  ELSE to_variant_object(named_struct('result','ALLOW','reason',''))
END;
```

Then 🖱️ `guarded_chat` ▸ **Policies** ▸ **New policy** `block-codename` ▸ Guardrail type **Custom** ▸ select the function ▸ Rank `10` ▸ Create. Test with the **Codename** prompt.

> **Why it matters:** The same guardrails apply to **every** client of `guarded_chat` (this app, notebooks, agents, the playground) without changing any code. Policies **fail closed**: if a policy errors, the call is denied, not let through.

### 6.5 Chat with memory (LangGraph + Lakebase)

1. 🖱️ App ▸ **4 · Chat**. Model service: `guarded_chat`. Send: *"My name is Ana and I work with Lakebase. Remember it."*, then *"What's my name?"*
2. 🖱️ Watch the **N checkpoints in Lakebase** counter. LangGraph saves the graph state after every step, keyed by `thread_id`.
3. 🖱️ Prove it's durable: on the app page click **Stop**, then **Start** (or redeploy). Reopen the app ▸ Chat ▸ click your conversation in the list. The history reloads from Lakebase. Ask *"What do I work with?"* to show the agent continues from that state.
4. 🖱️ Switch the model service to `ab_split` mid-conversation. The thread continues, now through the traffic split. Try the PII prompt on `guarded_chat` too, so the guardrails apply to the agent as well.

> **Why it matters:** Agents are stateful. Putting their state in managed Postgres (rather than in app memory) gives you persistence across restarts, scale-out across app instances, and an auditable trail you can query with SQL.

---

## Part 7 · Query everything in Lakebase (10 min)

**Why:** All of this is just Postgres. You can inspect the app's data with standard SQL, and as the project creator you can read the tables even though the app owns them.

1. 🖱️ App switcher ▸ **Lakebase Postgres** ▸ project `uaigw-workshop` ▸ **SQL Editor** ▸ branch `production`, database `databricks_postgres`.
2. ⌨️ Open `sql/lakebase_queries.sql` from your Git folder in another tab and run the queries **one at a time**:

| # | Question it answers |
|---|---|
| 1–2 | Which tables did the app create, and **who owns them**? (the app's service principal) |
| 3 | What were my last test runs? |
| 4 | Across all split runs, what share did each backend get? Does it match the configured %? |
| 5 | Per run: configured routing vs observed result. Can you spot your 50/50 → 90/10 change? |
| 6 | How many requests were rejected by **my** rate limit vs the platform QPS cap? |
| 7 | What did guarded vs unguarded answer, and which policy fired? |
| 8–10 | LangGraph's conversation state: threads, checkpoints per thread, and the checkpoint chain |

3. ⌨️ Try a Postgres meta-command: `\dt uaigw_app.*`, then `\d uaigw_app.test_results`.

> 💡 **Bonus (Lakehouse side):** in the regular **SQL editor** run this to see the gateway's own audit log, every request, including the ones that were rate limited:
> ```sql
> SELECT event_time, endpoint_name, destination_name, status_code, requester
> FROM system.ai_gateway.usage
> WHERE endpoint_name LIKE 'workspace.uaigw_workshop.%'
> ORDER BY event_time DESC LIMIT 50;
> ```
> (The system table can lag a few minutes behind.)

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Creating the Git folder fails: *Linked GitHub app credentials expired…* (or asks you to link GitHub) | An old or unfinished GitHub link on your user | Avatar ▸ **Settings ▸ Linked accounts** ▸ re-link GitHub (or remove the stale credential), then retry. The repo is public, so any working link is enough |
| Chip **✗ ab_split**: *does not have USE SCHEMA* | Part 5 not done, or not propagated yet | Grant `USE SCHEMA` + `EXECUTE` on the schema to the app SP, wait 1 min, reload |
| Chip ✗: *… does not exist* | Service name or schema typo | Names must be exactly `workspace.uaigw_workshop.ab_split` / `guarded_chat` (or run 🛟 `01_create_gateway_services`) |
| **Lakebase: not connected** · *PGHOST not set* | No database resource on the app | App ▸ Edit ▸ resources ▸ + Add resource ▸ Database (Part 4.1), then **Deploy** |
| `permission denied for schema uaigw_app` (Lakebase chip red, or in Logs) | The Lakebase resource was **removed and re-added**: Databricks then reassigns the app's tables to you and gives the app a new role. Or someone created `uaigw_app` by hand. | Run 🛟 `setup/02_check_and_fix` with `fix_problems = yes` (it grants the app access, keeping the data), then **Stop ▸ Start** the app |
| App shows **Stopped**, *"stopped due to workspace or account status"* | Free Edition stops idle app compute | App page ▸ **Start**, wait for *Running*. Your data is safe in Lakebase |
| Split bar is never ~50/50 | Small sample | Use bursts of 40+ and look at query 4 in Part 7 |
| Changed routing, rate limit or policy but nothing changes | Propagation delay | Wait 1–2 min. The **Routing now** / **Rate limits now** lines show the config, which applies shortly after |
| Everything returns `rate limited` | Your rate limit is still on | Remove it from `ab_split`, wait 1–2 min |
| **No Previews entry** for service policies | Preview not available yet | Skip 6.4. Everything else works without it |
| App deploy fails | See **Logs** ▸ *BUILD* lines | Usually a wrong source path: it must end in `/app` |
| Anything else | | Run 🛟 `setup/02_check_and_fix` and read the ❌ lines |

---

## Clean up (optional)

1. Apps ▸ `uaigw-workshop` ▸ **Delete**.
2. Lakebase ▸ project `uaigw-workshop` ▸ **Settings ▸ Delete project** (this deletes all its data).
3. Catalog ▸ `workspace.uaigw_workshop` ▸ delete the services and policies, then the schema.

---

### What you learned

- **Unity Gateway model services** are governed, virtual model endpoints. Traffic splits, fallbacks, rate limits and guardrails all change **without touching app code**.
- **Unity Catalog** governs AI services like any data asset: grants decide who calls, and service policies decide how.
- **Lakebase** gives apps and agents a serverless Postgres with OAuth-based auth, ideal for agent memory (LangGraph checkpoints) and operational data.
- **Databricks Apps** wire it all together with a dedicated identity and declarative resources, with no secrets in code.
