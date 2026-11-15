-- =====================================================================
-- Lakebase SQL editor — workshop queries
-- Run these in: Lakebase project uaigw-workshop -> SQL editor
-- (branch: production, database: databricks_postgres)
-- Run them one at a time: put the cursor in a query and press Run.
-- =====================================================================

-- 1) What did the app create? It owns the uaigw_app schema.
SELECT table_name, table_type
FROM information_schema.tables
WHERE table_schema = 'uaigw_app'
ORDER BY table_name;

-- 2) Who owns these tables? It is the app's service principal (a UUID),
--    because the app created them on startup. You can read them because
--    the creator of a Lakebase project is a databricks_superuser.
SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'uaigw_app';

-- 3) The last 10 test runs (one row per click on "Send burst" / "Send through both").
SELECT run_id, created_at, test_type, service, n_requests, counts, elapsed_ms
FROM uaigw_app.test_runs
ORDER BY run_id DESC
LIMIT 10;

-- 4) Traffic split: observed share per backend across ALL split runs.
--    Compare with the traffic percentages you configured in Unity Gateway.
SELECT served_by,
       count(*)                                              AS requests,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1)    AS pct,
       round(avg(latency_ms))                                AS avg_ms
FROM uaigw_app.test_results r
JOIN uaigw_app.test_runs t USING (run_id)
WHERE t.test_type = 'split'
GROUP BY served_by
ORDER BY requests DESC;

-- 5) Did the split change when you changed the policy? Per-run breakdown,
--    with the routing the app saw at the time of the run (service_config).
SELECT t.run_id, t.created_at,
       (SELECT string_agg(d->>'name' || '=' || coalesce(d->>'traffic_percentage','100') || '%', ', ')
          FROM jsonb_array_elements(t.service_config->'destinations') d) AS configured,
       t.counts AS observed
FROM uaigw_app.test_runs t
WHERE t.test_type = 'split'
ORDER BY t.run_id DESC
LIMIT 10;

-- 6) Rate limiting: how many requests were rejected with 429, per run.
SELECT t.run_id, t.created_at, t.n_requests,
       count(*) FILTER (WHERE r.served_by = 'rate limited')   AS rate_limited,
       count(*) FILTER (WHERE r.served_by = 'model QPS cap')  AS model_qps_cap,
       count(*) FILTER (WHERE r.http_status = 200)            AS ok
FROM uaigw_app.test_runs t
JOIN uaigw_app.test_results r USING (run_id)
WHERE t.test_type IN ('ratelimit', 'split')
GROUP BY t.run_id, t.created_at, t.n_requests
ORDER BY t.run_id DESC
LIMIT 10;

-- 7) Guardrails: what the guarded vs unguarded service answered.
--    policy is the databricks_service_policy object when a guardrail blocked.
SELECT r.run_id, r.service, r.served_by, left(r.response_text, 120) AS answer, r.policy
FROM uaigw_app.test_results r
JOIN uaigw_app.test_runs t USING (run_id)
WHERE t.test_type = 'guardrail'
ORDER BY r.run_id DESC, r.seq
LIMIT 20;

-- 8) Chat conversations (one row per LangGraph thread).
SELECT thread_id, title, service, created_at, updated_at
FROM uaigw_app.chat_threads
ORDER BY updated_at DESC;

-- 9) LangGraph state: one checkpoint per graph step, per thread.
--    This is what lets a conversation survive app restarts.
SELECT c.thread_id, t.title, count(*) AS checkpoints,
       max(c.checkpoint_id)  AS latest_checkpoint
FROM uaigw_app.checkpoints c
LEFT JOIN uaigw_app.chat_threads t USING (thread_id)
GROUP BY c.thread_id, t.title
ORDER BY checkpoints DESC;

-- 10) Peek inside one checkpoint (replace the thread id).
--     The messages themselves are serialized in checkpoint_blobs.
SELECT checkpoint_id, parent_checkpoint_id,
       metadata->>'source' AS source, metadata->>'step' AS step
FROM uaigw_app.checkpoints
WHERE thread_id = '<paste-a-thread_id-here>'
ORDER BY checkpoint_id;
