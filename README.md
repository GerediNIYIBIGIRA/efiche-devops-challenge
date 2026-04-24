# eFiche Ops Agent

Replication health monitoring for eFiche's PostgreSQL logical replication fleet.

---

## Repository Contents

| File | Deliverable |
|------|-------------|
| `ops_agent/replication_health.py` | Deliverable 2 — `/replication-health` endpoint |
| `migrations/add_billing_status.sql` | Deliverable 1a — three-step zero-downtime migration |
| `ops_agent/middleware.py` | Deliverable 1d — two-tier API key security |
| `ANALYSIS.md` | Deliverables 1a–1d — written analysis and security memo |
| `DESIGN_DOCUMENT.md` | Deliverable 3 — RPi strategy, migration protocol, automation limits |
| `.gitlab-ci.improved.yml` | Deliverable 1c — CI/CD pipeline (GitLab CI syntax, for eFiche's Laravel backend) |
| `.github/workflows/ci.yml` | GitHub Actions — runs tests and validates deliverables on every push |
| `tests/test_replication_health.py` | Deliverable 2 — unit and integration tests |

---

## Project Structure

```
├── ops_agent/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app entry point
│   ├── replication_health.py    # /replication-health endpoint
│   ├── database.py              # PostgreSQL async session
│   ├── dependencies.py          # Redis client
│   └── middleware.py            # Two-tier API key auth
├── migrations/
│   └── add_billing_status.sql   # Three-step zero-downtime migration
├── tests/
│   ├── __init__.py
│   └── test_replication_health.py
├── scripts/
│   └── stub_replica_init.sql    # Dev stub for pg_last_xact_replay_timestamp()
├── conftest.py                  # Adds project root to sys.path (no pip install needed)
├── pytest.ini
├── docker-compose.dev.yml       # Starts Redis + stub PostgreSQL replica
├── requirements.txt
├── .env.example
├── ANALYSIS.md                  # Written analysis: deliverables 1a–1d
├── DESIGN_DOCUMENT.md           # Design document: deliverable 3
├── .gitlab-ci.improved.yml      # CI/CD pipeline (GitLab CI): deliverable 1c
├── .github/
│   └── workflows/
│       └── ci.yml               # GitHub Actions: runs tests + validates deliverables
└── README.md
```

---

## CI Pipeline

This repository has two pipeline files — they serve different purposes:

| File | Purpose |
|------|---------|
| `.github/workflows/ci.yml` | **Runs on this repo** — executes the 26 tests, validates the migration SQL, and validates the improved GitLab CI file on every push to GitHub |
| `.gitlab-ci.improved.yml` | **Deliverable 1c** — CI/CD pipeline for eFiche's Laravel backend on their GitLab instance |

The GitHub Actions pipeline runs three jobs on every push:

- **Unit & Integration Tests** — all 26 pytest tests (trend logic, degraded flag, Redis storage, endpoint integration)
- **Validate CI Pipeline** — confirms `.gitlab-ci.improved.yml` defines all 7 required jobs across the correct stages
- **Validate Migration SQL** — confirms `migrations/add_billing_status.sql` contains all three steps including `NOT VALID`, `VALIDATE CONSTRAINT`, `SET NOT NULL`, and `SKIP LOCKED`

---

## Setup & Running Locally

### Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Create your .env file

```bash
cp .env.example .env
```

The defaults in `.env.example` already point to the local Docker services, so
no changes are needed for local development.

### Step 3 — Start Redis and the stub PostgreSQL replica

```bash
docker-compose -f docker-compose.dev.yml up -d
```

This starts:
- **Redis** on `localhost:6379`
- **PostgreSQL stub replica** on `localhost:5433` with a fake
  `pg_last_xact_replay_timestamp()` function that returns a controllable lag value

### Step 4 — Start the API

```bash
PYTHONPATH=. uvicorn ops_agent.main:app --reload --port 8080
```

The `PYTHONPATH=.` is required. It tells uvicorn to look in the current directory
for the `ops_agent` package.

### Step 5 — Test the endpoint

```bash
curl -H "X-API-Key: dev-read-key" http://localhost:8080/replication-health
```

Expected response:

```json
{
  "lag_seconds": 2.0,
  "trend": "stable",
  "degraded": false,
  "last_checked": "2026-04-20T08:14:32Z",
  "history": [
    {"lag_seconds": 2.0, "recorded_at": "2026-04-20T08:14:32Z"}
  ]
}
```

The `/health` endpoint requires no API key:

```bash
curl http://localhost:8080/health
```

---

## Running the Tests

```bash
PYTHONPATH=. pytest tests/ -v
```

No database or Redis server needed — tests use `unittest.mock` for PostgreSQL
and `fakeredis` for Redis.

---

## Simulating a Growing Lag Trend

Inject readings directly into Redis via the CLI:

```bash
redis-cli zadd replication_lag_history \
  $(( $(date +%s) - 40 )) '{"lag_seconds": 1.1, "recorded_at": "2026-04-20T08:13:47Z"}' \
  $(( $(date +%s) - 30 )) '{"lag_seconds": 3.2, "recorded_at": "2026-04-20T08:13:57Z"}' \
  $(( $(date +%s) - 20 )) '{"lag_seconds": 6.8, "recorded_at": "2026-04-20T08:14:07Z"}' \
  $(( $(date +%s) - 10 )) '{"lag_seconds": 11.4, "recorded_at": "2026-04-20T08:14:17Z"}' \
  $(( $(date +%s) - 1  )) '{"lag_seconds": 16.0, "recorded_at": "2026-04-20T08:14:27Z"}'
```

Then hit the endpoint — it will return `"trend": "growing"` and `"degraded": true`
(last 3 readings: 6.8 → 11.4 → 16.0, growth = 16.0 − 6.8 = 9.2 which is under the 10s threshold — to trigger `degraded=true` use values like 0.5/1.0/2.0/8.0/15.0 so last 3 grow by > 10).

To simulate recovering lag, change the stub database lag downward:

```bash
psql -h localhost -p 5433 -U postgres -d efiche_dev \
  -c "UPDATE _stub_config SET value = '0.5' WHERE key = 'lag_seconds';"
```

---

## What Is Stubbed vs Real

| Component | Local dev | Production |
|---|---|---|
| `pg_last_xact_replay_timestamp()` | Stubbed SQL function, value controlled via `_stub_config` table | Real PostgreSQL built-in on each RPi replica |
| Redis | Real `redis:7` Docker container | Real Redis instance |
| Trend / degraded logic | Real — no mocks in `ops_agent/` | Real — same code |
| PostgreSQL query in tests | Mocked via `unittest.mock` | Real query — never mocked in `ops_agent/` |

---

## Running the Migration

```bash
psql -h localhost -p 5433 -U postgres -d efiche_dev \
  -f migrations/add_billing_status.sql
```

For production, run against the primary only. Verify Step 1 replicated to all
replicas before running Step 2. Verify zero NULL rows before running Step 3.
See DESIGN_DOCUMENT.md §3b for the full protocol.
