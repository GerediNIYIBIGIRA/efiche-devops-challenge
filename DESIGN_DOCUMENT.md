# eFiche — Design Document
## Zero-Downtime Deployment and Replication Health System

---

## 3a. RPi Edge Node Deployment Strategy

### Constraints

- Raspberry Pi 4, 2GB RAM
- Intermittent internet connectivity (rural clinics may be offline for hours)
- Zero-downtime: the running container must not go down during a deployment attempt
- Docker image pulled from a central registry in Kigali

---

### How to ship a new version to a node that may be offline for hours

Use a **pull-based deployment** driven by a systemd timer on each RPi node. The node checks
for a new image on a schedule — it does not wait for a push from CI. If the registry is
unreachable, the running container is untouched.

`/opt/efiche/deploy.sh` on each RPi node:

```bash
#!/bin/bash
set -euo pipefail

REGISTRY="registry.efiche.africa"
IMAGE="$REGISTRY/efiche-backend"
COMPOSE_FILE="/opt/efiche/docker-compose.yml"
LOG="/var/log/efiche-deploy.log"

echo "$(date -Iseconds) Starting deployment check" >> "$LOG"

# Preserve current image as rollback target before touching anything
docker tag "$IMAGE:current" "$IMAGE:previous" 2>/dev/null || true

# Attempt to pull the new image — exit silently if offline
if docker pull "$IMAGE:latest" --quiet 2>>"$LOG"; then
    docker tag "$IMAGE:latest" "$IMAGE:current"

    # Restart only the application container, not the database or Redis
    docker compose -f "$COMPOSE_FILE" up -d --no-deps --remove-orphans backend

    # Wait for health check before declaring success
    sleep 15
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' efiche_backend 2>/dev/null || echo "unknown")
    if [ "$STATUS" != "healthy" ]; then
        echo "$(date -Iseconds) Health check failed ($STATUS) — rolling back" >> "$LOG"
        docker tag "$IMAGE:previous" "$IMAGE:current"
        docker compose -f "$COMPOSE_FILE" up -d --no-deps backend
    else
        echo "$(date -Iseconds) Deployment successful" >> "$LOG"
        docker image prune -f --filter "until=24h" >> "$LOG" 2>&1
    fi
else
    echo "$(date -Iseconds) Registry unreachable — continuing on current image" >> "$LOG"
fi
```

`/etc/systemd/system/efiche-deploy.timer`:

```ini
[Unit]
Description=eFiche deployment check

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/efiche-deploy.service
[Unit]
Description=eFiche deployment

[Service]
Type=oneshot
ExecStart=/opt/efiche/deploy.sh
```

The timer runs every 15 minutes. If the node has been offline for 4 hours, it catches up
on the next connectivity window automatically. The systemd `Persistent=true` flag means
the timer fires immediately on reconnect if a run was missed.

---

### How to prevent a failed deployment from taking down the running system

Three layers of protection:

**1. Tag before pull** — `docker tag current previous` runs before any pull. If anything
goes wrong after this point, the rollback is a single `docker tag previous current`.

**2. Docker health check in `docker-compose.yml`** — the container declares itself healthy
via an HTTP probe. If health checks fail after startup, Docker stops the new container
but does not restart the previous one automatically (`restart: unless-stopped` only
restarts on crash, not on explicit stop).

```yaml
# /opt/efiche/docker-compose.yml on each RPi node
services:
  backend:
    image: registry.efiche.africa/efiche-backend:current
    restart: unless-stopped
    ports:
      - "8080:8080"
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 30s
    env_file:
      - /opt/efiche/.env
```

**3. Active health check in `deploy.sh`** — the script explicitly checks container health
15 seconds after `up -d` and rolls back if the status is not `healthy`.

**Manual rollback command** (always works regardless of deploy state):

```bash
docker tag efiche-backend:previous efiche-backend:current
docker compose -f /opt/efiche/docker-compose.yml up -d --no-deps backend
```

---

### How to handle a node that is 2 hours behind when deploying a migration-dependent version

This is a **schema-application ordering problem**. The rule is strict:

> Never ship the application code that uses a new column in the same image push as the
> migration that creates it.

Use a **two-phase deployment** with a deliberate gap between phases:

**Phase 1 — Schema only (no application change):**
- Apply Step 1 of the migration to all replicas manually (see §3b).
- Apply Step 1 to the primary.
- Wait for all replicas to confirm the column exists.
- Proceed to Steps 2 and 3 of the migration on the primary.
- Verify replication lag is below 30 seconds on all nodes.

**Phase 2 — Application code (no schema change):**
- Deploy the new image that reads and writes `billing_status`.
- By this point, even the 2-hour-behind node will have the column — the column was
  added in Phase 1, and 2 hours is well within the time window between phases.

During the Phase 1–Phase 2 window, the application must be **backward-compatible**: it
reads `billing_status` as `'pending'` if NULL, and does not crash if the column returns
NULL on old rows.

```php
// Application-layer backward compatibility during migration window:
$status = $invoice->billing_status ?? 'pending';
```

For a node that is confirmed behind: efiche-ops `/replication-health` endpoint returns
the lag and trend. A deploy script can query this endpoint and refuse to proceed if any
node's lag exceeds a configurable threshold (e.g., 120 seconds).

---

## 3b. Migration Safety in a Replication Context

### End-to-end protocol for deploying the three-step `billing_status` migration

The critical principle: **in PostgreSQL logical replication, DDL does not replicate
automatically. You must apply schema changes to replicas manually, and in the correct
order.**

---

### Order of operations: replicas first, then primary

**Step 1 DDL must go to replicas before the primary.**

If the primary receives the DDL first and immediately begins the backfill (Step 2), the
WAL events for those backfill updates arrive at replicas before the replicas have the
column. Replication halts. See ANALYSIS.md §1b for the exact failure sequence.

**Full deployment protocol:**

```
PRE-FLIGHT
  1. Check current replication lag on all nodes via /replication-health.
     Proceed only if all nodes: lag < 30s, trend != "growing".

PHASE 1 — Schema change
  2. On EACH replica, apply Step 1:
       ALTER TABLE visit_invoices
           ADD COLUMN IF NOT EXISTS billing_status VARCHAR(20) NULL;
     Run this on all nodes in parallel. Verify success on each:
       SELECT column_name FROM information_schema.columns
       WHERE table_name = 'visit_invoices' AND column_name = 'billing_status';

  3. On PRIMARY, apply Step 1:
       ALTER TABLE visit_invoices
           ADD COLUMN IF NOT EXISTS billing_status VARCHAR(20) NULL;

  4. Confirm replication is healthy. Wait for lag < 5s on all nodes.

PHASE 2 — Backfill
  5. On PRIMARY only, run Step 2 (the batched backfill DO block).
     Monitor progress:
       SELECT COUNT(*) FROM visit_invoices WHERE billing_status IS NULL;
     Alert if count is non-zero and not decreasing for > 120 seconds.

  6. After backfill: verify zero NULL rows:
       SELECT COUNT(*) FROM visit_invoices WHERE billing_status IS NULL;
     Must return 0 before proceeding.

PHASE 3 — Constraint enforcement
  7. On PRIMARY, apply Step 3 (NOT VALID + VALIDATE + SET NOT NULL).
     If VALIDATE fails, residual NULLs remain — go back to step 5.

  8. On each REPLICA, apply Step 3:
       ALTER TABLE visit_invoices
           ADD CONSTRAINT visit_invoices_billing_status_not_null
           CHECK (billing_status IS NOT NULL) NOT VALID;
       ALTER TABLE visit_invoices
           VALIDATE CONSTRAINT visit_invoices_billing_status_not_null;
       ALTER TABLE visit_invoices ALTER COLUMN billing_status SET NOT NULL;
       ALTER TABLE visit_invoices
           DROP CONSTRAINT IF EXISTS visit_invoices_billing_status_not_null;

POST-DEPLOYMENT
  9. Verify final schema on all nodes:
       SELECT column_name, is_nullable, column_default
       FROM information_schema.columns
       WHERE table_name = 'visit_invoices' AND column_name = 'billing_status';
     Expected: is_nullable = NO, column_default = 'pending'.
```

---

### What the application does during the backfill window

During Steps 5–6, `billing_status` exists on all nodes but some existing rows have `NULL`.

- **Reads**: the application treats `NULL` as `'pending'` at the service layer (not at the
  database layer). No SELECT query should return an error; NULLs are handled gracefully.
  ```php
  $status = $invoice->billing_status ?? 'pending';
  ```
- **Writes**: all INSERT and UPDATE statements must explicitly set `billing_status`. Do not
  rely on the column default alone during the window — some nodes may have the default set
  differently depending on when Step 3a (`SET DEFAULT 'pending'`) ran.
- **No application errors during the transition**: the column exists on all nodes before
  any application code referencing it is deployed (two-phase deployment from §3a).

---

### How efiche-ops detects if Step 2 failed partway through

The `/replication-health` endpoint does not directly monitor migration state, but two
mechanisms catch a partial backfill:

1. **Direct count query** — scheduled in efiche-ops after the backfill is triggered:
   ```sql
   SELECT COUNT(*) AS null_count FROM visit_invoices WHERE billing_status IS NULL;
   ```
   If this is non-zero and not decreasing for 2 minutes, raise an alert. This runs
   against the primary; the replica count is checked via the replication lag — if lag is
   within normal range, replica data matches primary.

2. **Step 3 self-checks**: `VALIDATE CONSTRAINT` will fail with
   `ERROR: found row violating check constraint` if any NULLs remain. This is an explicit
   failure, not a silent skip. The migration halts and the operator must re-run Step 2.

---

## 3c. What eFiche-Ops Detects But Does NOT Auto-Remediate in V1

### Case 1 — Replication slot error / subscription stopped (technically feasible, wrong for healthcare)

**What efiche-ops detects:** A replication slot enters error state
(`pg_subscription.suberrorcount > 0`), replication lag stops advancing, and the
`/replication-health` endpoint begins returning `"degraded": true`.

**Why not auto-remediate:** Automatically running `ALTER SUBSCRIPTION sub_X ENABLE`
would resume replication and allow the replica to start serving reads again. But a
stopped subscription often means the replica's data is inconsistent in a specific,
diagnosable way (e.g., the ghost column problem from §1b). Resuming replication before
a human verifies the root cause means nurses at the clinic may read stale or partially
applied data and make clinical decisions based on it. In healthcare, a read based on
incorrect data is worse than a system being flagged as unavailable and the nurse being
directed to the central system.

**What efiche-ops does instead:** Alert the on-call engineer with the subscription name,
error message, and a pre-formatted recovery runbook link. Block the affected replica
from serving reads by returning a `503` on the replica's ops health endpoint.

---

### Case 2 — Partial migration backfill detected on primary

**What efiche-ops detects:** After a migration is triggered, a scheduled count query
returns a non-zero `null_count` for `billing_status` after a timeout window (2 minutes).

**Why not auto-remediate:** Automatically re-triggering the backfill is technically
trivial — re-run the Step 2 `DO` block. But if the backfill stalled because of a
deadlock, resource exhaustion, or a bug in the migration script, re-running it
automatically may compound the problem or mask the root cause. A partially-applied
migration on a healthcare database requires human sign-off before any further automated
action. The engineer must confirm that the partial state is understood before proceeding.

**What efiche-ops does instead:** Alert with the count and the timestamp the migration
started. Provide a `curl` command in the alert body that the engineer can run to check
progress interactively.

---

### Case 3 — Docker container OOM-killed on RPi node

**What efiche-ops detects:** A container on a clinic RPi node exits with OOM kill
(`docker inspect` shows `OOMKilled: true`). Docker's `restart: unless-stopped` policy
restarts it once — efiche-ops detects if the container is in a restart loop (3+ restarts
within 10 minutes).

**Why not auto-remediate (even though Docker restart policy handles one restart):**
On a 2GB RPi, an OOM kill means the node is memory-exhausted. Automatically restarting
in a loop thrashes the SD card (a limited write-cycle resource on RPi), spikes CPU, and
delays recovery. The root cause is usually one of: image too large (fix: CI size gate
from deliverable 1c), a memory leak in the application (fix: code change), or competing
processes (fix: system audit). None of these are fixable by restarting the container.

Allowing Docker to restart **once** on OOM is acceptable — transient memory pressure does
occur. But after the second restart-within-window, efiche-ops suppresses further auto-
restarts, alerts the on-call engineer with the container memory stats and the list of
processes on the node, and marks the node as degraded in the ops dashboard.

**What efiche-ops does instead:**
```bash
# Alert body includes:
docker stats --no-stream efiche_backend
docker inspect efiche_backend --format='OOMKilled: {{.State.OOMKilled}}'
cat /proc/meminfo | grep -E "MemTotal|MemAvailable|MemFree"
```
The engineer decides whether to kill non-essential co-located processes, reduce the
container memory limit, or schedule a hardware review. V2 could auto-kill non-critical
exporters to free memory, but V1 should not take that action autonomously on a clinical
system.
