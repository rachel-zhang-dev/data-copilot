# Demo deploy checklist

Step-by-step recipe to take `main` → live Fly.io URL. Everything
below has been pre-flight checked on this machine; the only steps
left are the ones that require **your** API keys / Fly account /
interactive login.

Estimated time: **30-90 minutes** (mostly waiting on first-time
deploys + waiting for secrets to propagate).

---

## Prerequisites

- [ ] `flyctl` installed (`brew install flyctl`)
- [ ] Fly.io account + billing card on file (free tier covers ~3
      shared-CPU machines)
- [ ] DeepSeek API key
- [ ] SiliconFlow API key
- [ ] Postgres provider decided (option A or B below)

---

## Step 1 — Confirm local images still build (5 min, **already done**)

These were verified during week 12.6. Re-run if `main` has moved:

```bash
docker build -f apps/api/Dockerfile -t copilot-api:check .
docker build -f apps/web/Dockerfile -t copilot-web:check .
```

Expected: both succeed, images land at ≈580 MB and ≈310 MB.

---

## Step 2 — Authenticate with Fly (1 min, manual)

```bash
fly auth login
```

Opens a browser tab. Already done means `fly auth whoami` returns
your email.

---

## Step 3 — Create the two apps (one-time, ~2 min)

`--no-deploy` so we set secrets before the first push.

```bash
fly launch \
  --config apps/api/fly.toml \
  --dockerfile apps/api/Dockerfile \
  --no-deploy \
  --copy-config \
  --name data-copilot-api \
  --region iad

fly launch \
  --config apps/web/fly.toml \
  --dockerfile apps/web/Dockerfile \
  --no-deploy \
  --copy-config \
  --name data-copilot-web \
  --region iad
```

If `data-copilot-api` is already taken globally, pick a different
suffix (e.g. `data-copilot-api-rzhang`) and update the two URLs
referenced below.

---

## Step 4 — Stand up Postgres (5-30 min depending on option)

Pick one:

### Option A — Fly Postgres (recommended for demo)

```bash
fly postgres create --name data-copilot-pg --region iad --vm-size shared-cpu-1x --volume-size 1
fly postgres attach data-copilot-pg --app data-copilot-api
```

Attach sets `DATABASE_URL` automatically. The pgvector extension is
**not** included in Fly's default Postgres image — see "Enabling
pgvector" below before deploying the API.

### Option B — Supabase / Neon (managed, pgvector-friendly)

* Create a free-tier instance, enable the `vector` extension.
* Copy the connection string.
* Skip "Enabling pgvector" below — both providers ship it preloaded.

### Enabling pgvector on Fly Postgres

Fly's stock image doesn't ship pgvector. Quickest fix is to
override the cluster's image to a pgvector-enabled flavour:

```bash
fly postgres connect -a data-copilot-pg
# Inside psql:
CREATE EXTENSION IF NOT EXISTS vector;
\q
```

If `CREATE EXTENSION vector` errors with "extension not available",
recreate the postgres app with a community pgvector image:

```bash
fly apps destroy data-copilot-pg --yes
fly machines run pgvector/pgvector:pg16 \
  --app data-copilot-pg \
  --region iad \
  --port 5432:5432
```

(For real production we'd build our own Fly image; for the demo,
Supabase / Neon is the easier path.)

---

## Step 5 — Set secrets (3 min)

API:

```bash
fly secrets set --app data-copilot-api \
  DEEPSEEK_API_KEY=sk-... \
  SILICONFLOW_API_KEY=sk-... \
  DATABASE_URL='postgres://...' \
  CORS_ORIGINS='https://data-copilot-web.fly.dev' \
  LANGSMITH_API_KEY=lsv2_... \
  LANGSMITH_TRACING=true
```

(If you skip `LANGSMITH_*`, traces just won't ship — agent works fine.)

Optional: Redis (see ADR 0010). For the demo you can skip — the
in-memory cache works on a single replica.

```bash
fly secrets set --app data-copilot-api REDIS_URL='rediss://default:...@...upstash.io:6379'
```

Frontend:

```bash
fly secrets set --app data-copilot-web \
  API_BASE_URL='https://data-copilot-api.fly.dev'
```

---

## Step 6 — Seed the database (5 min)

The schema + Northwind data ship as `data/seed/*.sql`. Two options:

### Option A — psql from your laptop

```bash
# Get the connection string
fly postgres connect -a data-copilot-pg
# Or for Supabase/Neon, use their dashboard's "psql" command.

# In another terminal:
PGURL='postgres://...'
psql "$PGURL" -f data/seed/01-northwind.sql
psql "$PGURL" -f data/seed/02-schema-embeddings.sql
```

### Option B — fly proxy + psql

```bash
fly proxy 15432:5432 -a data-copilot-pg &
psql 'postgres://postgres:...@localhost:15432/postgres' -f data/seed/01-northwind.sql
psql 'postgres://postgres:...@localhost:15432/postgres' -f data/seed/02-schema-embeddings.sql
# Kill the proxy when done.
```

---

## Step 7 — Deploy both apps (~5 min)

```bash
./scripts/deploy.sh api
./scripts/deploy.sh web
# Or both in order:
./scripts/deploy.sh all
```

`deploy.sh` runs `fly deploy --remote-only` so the build happens on
Fly's builders, not your laptop — much faster on a residential
connection.

---

## Step 8 — Build the schema index (~3 min)

The `schema_embeddings` table is empty until the indexer runs. SSH
into the deployed API machine and trigger it:

```bash
fly ssh console --app data-copilot-api
# Inside the container:
python -m copilot.indexer
exit
```

Or run it locally pointed at the production DB (faster, no SSH):

```bash
cd apps/api
DATABASE_URL='postgres://...' \
SILICONFLOW_API_KEY=sk-... \
uv run python -m copilot.indexer
```

---

## Step 9 — Smoke-test (~3 min)

```bash
./scripts/deploy.sh smoke
```

Expected output:

```
>> smoke-checking data-copilot-api /health
{ "status": "ok", "version": "0.12.0" }
>> smoke-checking data-copilot-api /admin/stats
in-memory
>> smoke-checking data-copilot-web /api/health
{ "status": "ok", "upstream": "ok", "upstreamVersion": "0.12.0" }
```

Then open the front-end in a browser:

```
https://data-copilot-web.fly.dev
```

Ask "How many customers are there?" — should see phase events
stream, a KPI tile render, and an Analyst panel pop in below.

---

## What to do when it breaks

| Symptom | First check |
|---|---|
| `fly deploy` builds but times out | `fly logs --app data-copilot-api` — usually a missing secret |
| `/health` returns 503 | Postgres unreachable / pgvector missing extension |
| First `/ask` hangs ~30s | Fly machine cold start; happens after `auto_stop` |
| SSE stream cuts off after 15s | Cloudflare timeout — we ship a heartbeat at 15s, but if you front Fly with Cloudflare it can still close. Set `Cache-Control: no-cache` is already in place. |
| `chart_kind=null` on every answer | `visualize_node` failed silently — `fly logs` for the warning |
| Analyst always silent | `ANALYST_ENABLED=true` env var; check `fly secrets list` |

---

## Tear-down

```bash
fly apps destroy data-copilot-web --yes
fly apps destroy data-copilot-api --yes
fly apps destroy data-copilot-pg --yes   # if you used Fly Postgres
```

Free-tier usage doesn't bill, but always good to clean up.

---

## What this checklist does NOT cover

Production-grade items deliberately deferred per
[ADR 0015](decisions/0015-production-readiness-checklist.md):

* No authentication on any endpoint — anyone with the URL can
  spend your DeepSeek credits.
* No rate limiting.
* No backup / restore drill.
* No CI gate on the deploy (CI runs but doesn't block `deploy.sh`).
* `/admin/stats` and `/metrics` are publicly readable.

Acceptable for a demo URL on your CV. **Not acceptable** for real
users until those items close.
