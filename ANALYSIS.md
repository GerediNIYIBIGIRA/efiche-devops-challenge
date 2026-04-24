# eFiche Challenge — Written Analysis
## Deliverables 1a – 1d

---

## 1a. The Migration Problem

### Why the single-statement ALTER TABLE is wrong

```sql
-- WRONG for a 2.1M-row table under constant write load:
ALTER TABLE visit_invoices ADD COLUMN billing_status VARCHAR(20) NOT NULL DEFAULT 'pending';
```

This statement acquires an **ACCESS EXCLUSIVE lock** on the entire `visit_invoices` table.
While the lock is held, every concurrent `INSERT`, `UPDATE`, `DELETE`, and even `SELECT` is
queued behind it. On PostgreSQL versions prior to 11, adding a column with a stored default
triggers a full **table rewrite** — every row is physically rewritten to include the new column
value. On a 2.1-million-row table, this rewrite takes minutes. During those minutes, every
nurse in every clinic attempting to access patient data is blocked. That is a clinical incident.

On PostgreSQL 11+, adding a column with a constant default is a metadata-only operation
(O(1)) — PostgreSQL stores the default in `pg_attribute` and materialises it on read without
rewriting rows. The ACCESS EXCLUSIVE lock is still acquired, but it is held for only
milliseconds in the simple case. However, under high write load, even a millisecond
ACCESS EXCLUSIVE lock creates a **lock queue**: every in-flight write waits for it to clear,
and every new write queues behind them. On a system doing 3,000 visits per day (roughly
one write every 30 seconds at average, but bursty at shift changes), a lock queue during
peak hours can cascade into visible latency or timeouts.

The safest path is the three-step migration below, which never takes an ACCESS EXCLUSIVE
lock for more than a catalog update.

---

### The three-step migration

The SQL is in `migrations/add_billing_status.sql`. The steps and their ordering are:

**Step 1 — Add column as nullable, no default stored**

```sql
ALTER TABLE visit_invoices
    ADD COLUMN IF NOT EXISTS billing_status VARCHAR(20) NULL;
```

PostgreSQL performs a catalog-only change (`pg_attribute` update). No rows are read or
written. The ACCESS EXCLUSIVE lock is held for single-digit milliseconds. Writes continue
uninterrupted immediately after.

Why nullable, not NOT NULL? Because adding NOT NULL at this stage requires a table scan to
verify no existing rows violate the constraint. We do not want that scan — yet. New rows
inserted after this step will have `NULL` in `billing_status` until the application layer
or the next step provides a value.

**Step 2 — Backfill existing rows in small batches**

```sql
DO $$
DECLARE
    batch_size   INT := 5000;
    rows_updated INT;
BEGIN
    LOOP
        UPDATE visit_invoices
        SET billing_status = 'pending'
        WHERE id IN (
            SELECT id FROM visit_invoices
            WHERE billing_status IS NULL
            LIMIT batch_size
            FOR UPDATE SKIP LOCKED
        );
        GET DIAGNOSTICS rows_updated = ROW_COUNT;
        EXIT WHEN rows_updated = 0;
        PERFORM pg_sleep(0.05);
    END LOOP;
END;
$$;
```

Each batch is a short, independent transaction. Only the 5,000 rows in that batch are
row-locked, briefly. Concurrent writes to other rows proceed without interruption.
`FOR UPDATE SKIP LOCKED` ensures that rows currently locked by concurrent transactions
(e.g., a nurse updating an invoice) are skipped and picked up in the next batch rather
than causing the backfill to wait.

The 50ms sleep between batches prevents the backfill from saturating I/O on the primary
and allows replication lag to stay within normal bounds.

**Step 3 — Enforce NOT NULL without a full-table lock**

```sql
ALTER TABLE visit_invoices ALTER COLUMN billing_status SET DEFAULT 'pending';

ALTER TABLE visit_invoices
    ADD CONSTRAINT visit_invoices_billing_status_not_null
    CHECK (billing_status IS NOT NULL) NOT VALID;

ALTER TABLE visit_invoices
    VALIDATE CONSTRAINT visit_invoices_billing_status_not_null;

ALTER TABLE visit_invoices ALTER COLUMN billing_status SET NOT NULL;

ALTER TABLE visit_invoices DROP CONSTRAINT IF EXISTS visit_invoices_billing_status_not_null;
```

The `NOT VALID` flag tells PostgreSQL: *enforce this constraint on all new writes immediately,
but do not scan existing rows to validate them now*. This takes only a brief lock.

`VALIDATE CONSTRAINT` then scans existing rows, but it holds a **SHARE UPDATE EXCLUSIVE
lock** — not ACCESS EXCLUSIVE. SHARE UPDATE EXCLUSIVE allows concurrent reads and writes
to proceed normally. It only blocks other schema-change commands.

Once the constraint is validated, `SET NOT NULL` is a catalog-only operation — PostgreSQL
knows from the validated constraint that no NULLs exist, so no scan is needed. The
redundant CHECK constraint is then dropped.

**Why order matters**: Step 1 must happen before Step 2 because you cannot backfill a
column that does not exist. Step 2 must be confirmed complete (zero NULL rows) before
Step 3 because `VALIDATE CONSTRAINT` will fail loudly if residual NULLs remain — this
is intentional and correct. Do not force through Step 3 with residual NULLs; re-run
Step 2 until `SELECT COUNT(*) FROM visit_invoices WHERE billing_status IS NULL` returns 0.

---

## 1b. The Ghost Column Problem

### Why this error happens

```
ERROR:  column "billing_status" of relation "visit_invoices" does not exist
REPLICATION SLOT: replication_slot_rpi_node_7
```

PostgreSQL **logical replication replicates DML only** — `INSERT`, `UPDATE`, `DELETE`.
It does **not** replicate DDL (`ALTER TABLE`, `CREATE TABLE`, `DROP COLUMN`).

The sequence of events that produces this error:

1. The migration operator runs Step 1 on the **primary**: `ALTER TABLE visit_invoices ADD COLUMN billing_status VARCHAR(20) NULL`. The primary's schema now has this column.
2. The operator proceeds immediately to Step 2 (backfill) on the primary without first verifying replicas received the schema change.
3. The backfill `UPDATE` runs on the primary. Each updated row is encoded in the Write-Ahead Log as: `{id: X, patient_id: Y, ..., billing_status: 'pending'}` — a row that includes the new column.
4. The WAL sender on the primary encodes this row change and sends it to the logical replication subscriber on `rpi_node_7`.
5. The subscriber on `rpi_node_7` tries to apply this row change to its local `visit_invoices` table.
6. The replica's `visit_invoices` table has no `billing_status` column — the DDL was never replicated.
7. The WAL applier raises: `ERROR: column "billing_status" of relation "visit_invoices" does not exist`.
8. The replication slot `replication_slot_rpi_node_7` **stops processing**. No further changes from the primary are applied to this replica until the error is resolved.

---

### Recovery without data loss

**Step 1 — Do not drop the replication slot.** The slot on the primary is still buffering all
unprocessed WAL. Dropping it discards that buffer and makes data loss possible.

Run on the **replica** (`rpi_node_7`):

```sql
-- Confirm the subscription name and current state
SELECT subname, subenabled, suberrorcount, suberrormsg
FROM pg_subscription;
```

```sql
-- Disable the subscription: pauses replication, keeps the slot alive on primary
ALTER SUBSCRIPTION sub_rpi_node_7 DISABLE;
```

```sql
-- Apply the DDL that the primary already has (same statement, same column definition)
ALTER TABLE visit_invoices
    ADD COLUMN IF NOT EXISTS billing_status VARCHAR(20) NULL;
```

```sql
-- Re-enable: replication resumes from the last confirmed LSN, replaying all buffered WAL
ALTER SUBSCRIPTION sub_rpi_node_7 ENABLE;
```

On the **primary**, confirm the slot was not affected:

```sql
SELECT slot_name,
       active,
       restart_lsn,
       confirmed_flush_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS buffered_wal
FROM pg_replication_slots
WHERE slot_name = 'replication_slot_rpi_node_7';
```

Monitor catchup on the replica:

```sql
SELECT subname,
       received_lsn,
       latest_end_lsn,
       extract(epoch from (now() - latest_end_time)) AS seconds_behind
FROM pg_stat_subscription
WHERE subname = 'sub_rpi_node_7';
```

**Where data loss is possible and how to prevent it:**

If `buffered_wal` above is very large, the primary may have reached `max_slot_wal_keep_size`
and partially cleaned up WAL past the slot's `restart_lsn`. In that case, the slot is
"invalidated" and the replica requires a full resync (`pg_dump` from primary, rebuild
subscription from scratch). **Prevention**: do not proceed to Step 2 on the primary until
all replicas confirm the Step 1 DDL via a direct schema query:

```sql
-- Run on each replica before proceeding:
SELECT column_name
FROM information_schema.columns
WHERE table_name = 'visit_invoices' AND column_name = 'billing_status';
-- Must return one row before Step 2 is allowed to run.
```

This check is documented in `migrations/add_billing_status.sql` and enforced in the
deployment protocol in `DESIGN_DOCUMENT.md §3b`.

---

## 1c. CI Pipeline Gaps

Three missing checks that would catch real production bugs in a Laravel modular monolith
with parallel development.

---

### Check 1 — Migration dry-run (database migrations)

**The class of incident it prevents:**  
A developer adds a migration that uses a raw `ALTER TABLE ADD COLUMN NOT NULL DEFAULT` (the
exact bug in deliverable 1a). Or two branches are merged simultaneously and their migration
timestamps conflict, causing one migration to silently overwrite the other's state. Or a
migration references a column or table that was removed in a parallel branch.

None of these are caught by PHPStan or syntax checks — they only fail at runtime on a live
database.

**The job:**

```yaml
migration_check:
  stage: lint
  image: php:8.2-cli
  services:
    - name: postgres:15-alpine
      alias: postgres
  variables:
    POSTGRES_DB: efiche_test
    POSTGRES_USER: postgres
    POSTGRES_PASSWORD: ""
    DB_CONNECTION: pgsql
    DB_HOST: postgres
    DB_DATABASE: efiche_test
    DB_USERNAME: postgres
    DB_PASSWORD: ""
    APP_KEY: base64:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
  before_script:
    - apt-get update -qq && apt-get install -y -qq libpq-dev git unzip
    - docker-php-ext-install pdo_pgsql
    - curl -sS https://getcomposer.org/installer | php && mv composer.phar /usr/local/bin/composer
    - composer install --no-interaction --prefer-dist --quiet
    - cp .env.example .env
  script:
    # Run all migrations up
    - php artisan migrate --force --no-interaction
    # Verify the last migration is reversible (catches data-destructive rollbacks)
    - php artisan migrate:rollback --step=1 --force --no-interaction
    # Run forward again to confirm idempotency
    - php artisan migrate --force --no-interaction
    # Fail if any migration file has a locking operation
    - |
      grep -rn "ADD COLUMN.*NOT NULL.*DEFAULT\|CHANGE COLUMN\|MODIFY COLUMN" \
        database/migrations/ && echo "WARN: potentially blocking migration detected" || true
```

---

### Check 2 — Environment/configuration validation (application configuration)

**The class of incident it prevents:**  
A developer adds a new required service (Redis, an external API) and uses a new env var
(`REDIS_URL`, `OPS_API_KEY_WRITE`, `STRIPE_SECRET`) in the code. They add it to their local
`.env` but forget to add it to `.env.example`. The pipeline passes. The image deploys to
production. The first request hits the new code path and fails because the env var is
unset. This is one of the most common production incidents in multi-developer PHP projects.

**The job:**

```yaml
config_validation:
  stage: lint
  image: php:8.2-cli
  before_script:
    - composer install --no-interaction --prefer-dist --quiet
    - cp .env.example .env
    - php artisan key:generate --quiet
  script:
    # All keys in .env.example must be non-empty or have a placeholder
    - |
      echo "Validating .env.example completeness..."
      MISSING=0
      while IFS= read -r line; do
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        KEY=$(echo "$line" | cut -d= -f1)
        if [ -z "$KEY" ]; then continue; fi
        if ! grep -q "^${KEY}=" .env.example; then
          echo "ERROR: $KEY present in code but missing from .env.example"
          MISSING=1
        fi
      done < <(grep -rho 'env(\x27[A-Z_]*\x27' app/ | sed "s/env('//g")
      [ "$MISSING" -eq 0 ] || exit 1
    # Laravel config must load without errors
    - php artisan config:cache
    - php artisan config:clear
```

---

### Check 3 — Docker image size gate (RPi deployment constraint)

**The class of incident it prevents:**  
A developer switches the base image from `php:8.2-cli-alpine` to `php:8.2-cli` (adds ~400MB),
or accidentally includes the full `vendor/` directory with dev dependencies, or leaves
large test fixtures in the build context. The Docker image builds fine on a CI server with
8GB RAM and fast disk. The image is pushed to the registry. On the Raspberry Pi 4 with 2GB
RAM, `docker pull` partially succeeds but `docker run` causes an OOM because the image +
running container memory exceeds available RAM. The clinic goes offline.

**The job:**

```yaml
docker_size_check:
  stage: build
  script:
    - docker build -t efiche-backend:$CI_COMMIT_SHA .
    - |
      SIZE=$(docker image inspect efiche-backend:$CI_COMMIT_SHA --format='{{.Size}}')
      LIMIT=$((512 * 1024 * 1024))
      SIZE_MB=$(( SIZE / 1024 / 1024 ))
      LIMIT_MB=$(( LIMIT / 1024 / 1024 ))
      echo "Image size: ${SIZE_MB} MB  |  Limit: ${LIMIT_MB} MB"
      if [ "$SIZE" -gt "$LIMIT" ]; then
        echo "FAIL: Image ${SIZE_MB} MB exceeds RPi limit of ${LIMIT_MB} MB"
        exit 1
      fi
      echo "PASS"
  needs:
    - docker_build
```

---

## 1d. API Key Security Decision

**Internal Engineering Memo**

**To:** eFiche Engineering (all 4)
**From:** DevOps Lead
**Re:** OPS_API_KEY security posture — proposed change

**The problem with the current approach:**  
We use a single `OPS_API_KEY` for every ops dashboard action: reading metrics, viewing
replication lag, restarting replication subscriptions, forcing schema syncs, triggering
backfills, and restarting Docker containers on remote clinic nodes. A single key controls
actions that differ by two orders of magnitude in destructive potential.

The specific risk is operational, not adversarial: this key will eventually end up in a
monitoring tool integration (Grafana alert webhook, PagerDuty runbook, a script on
someone's laptop). At that point, the entity holding the key can restart containers at
rural clinics. That is not appropriate for a read-only monitoring integration.

**Why not OAuth2 + per-user JWT + RBAC:**  
We are four engineers on an internal ops tool with no external users and no cross-team
token sharing. OAuth2 requires a token issuer, refresh flows, and key management
infrastructure that would take a sprint to build and maintain. The security benefit over
what I am proposing does not justify that cost at our scale.

**The decision: two-tier static keys**

- `OPS_API_KEY_READ`: permitted on all read-only endpoints (metrics, replication health,
  lag history, health checks). Lives in shared ops config, CI dashboards, monitoring
  integrations.
- `OPS_API_KEY_WRITE`: permitted on destructive endpoints (restart-subscription,
  force-schema-sync, trigger-backfill, restart-container). Lives only in each engineer's
  personal `.env.local` and a restricted CI secret. **Never** in shared tooling.

Rotation: change one env var, redeploy. No user management, no token issuance, no RBAC
tables. Total complexity: one env check per request in `middleware.py`.

This is already implemented. Action required from each engineer: stop using the write key
in monitoring integrations.
