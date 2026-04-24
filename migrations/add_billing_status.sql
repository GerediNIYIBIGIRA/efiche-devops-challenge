-- =============================================================================
-- Migration: Add billing_status to visit_invoices
-- Strategy: Three-step zero-downtime migration
-- =============================================================================
--
-- WHY A SINGLE ALTER TABLE IS WRONG:
-- ALTER TABLE ... ADD COLUMN billing_status VARCHAR(20) NOT NULL DEFAULT 'pending'
-- on a 3.1M row table takes an ACCESS EXCLUSIVE lock for the entire table rewrite.
-- Every write (INSERT/UPDATE/DELETE) is blocked for the duration — potentially
-- minutes. In a clinic this is a patient-data blackout and a clinical incident.
--
-- The three steps below avoid any long lock.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- STEP 1: Add column as NULLable — no constraint, no stored default.
-- -----------------------------------------------------------------------------
-- PostgreSQL performs a catalog-only change (updates pg_attribute).
-- No rows are read or written. Lock is held for milliseconds.
-- Writes continue without interruption immediately after.
--
-- CRITICAL: This step must be confirmed replicated to ALL replicas before
-- Step 2 runs. If the replica doesn't have the column yet when Step 2 WAL
-- arrives, replication halts with:
--   ERROR: column "billing_status" of relation "visit_invoices" does not exist
--
-- Verify replication before continuing:
--   SELECT EXISTS (
--     SELECT 1 FROM information_schema.columns
--     WHERE table_name='visit_invoices' AND column_name='billing_status'
--   );
-- Run that on every replica. All must return 't' before Step 2.

ALTER TABLE visit_invoices
    ADD COLUMN IF NOT EXISTS billing_status VARCHAR(20) NULL;


-- -----------------------------------------------------------------------------
-- STEP 2: Backfill existing NULL rows in batches.
-- -----------------------------------------------------------------------------
-- Updates 5,000 rows per batch with a 50ms sleep between batches.
-- Each batch is a short transaction — only those rows are locked, briefly.
-- Concurrent writes are never blocked for more than a few milliseconds.
--
-- This step may not complete perfectly (crash, network blip).
-- Step 3 is safe to run even if residual NULLs remain — it will fail loudly
-- rather than silently skip them.
--
-- Monitor progress:
--   SELECT COUNT(*) FROM visit_invoices WHERE billing_status IS NULL;

DO $$
DECLARE
    batch_size   INT := 5000;
    rows_updated INT;
    total        BIGINT := 0;
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
        total := total + rows_updated;
        EXIT WHEN rows_updated = 0;

        PERFORM pg_sleep(0.05);
        RAISE NOTICE 'Backfilled % rows so far', total;
    END LOOP;
    RAISE NOTICE 'Backfill complete. Total: %', total;
END;
$$;


-- -----------------------------------------------------------------------------
-- STEP 3: Enforce NOT NULL without a full-table lock.
-- -----------------------------------------------------------------------------
-- 3a: Set default (catalog-only, no lock).
-- 3b: ADD CONSTRAINT NOT VALID — enforces constraint on new writes immediately,
--     skips scanning existing rows (no long lock).
-- 3c: VALIDATE CONSTRAINT — scans existing rows but holds only
--     SHARE UPDATE EXCLUSIVE lock, which allows concurrent writes.
-- 3d: Promote to true NOT NULL (catalog-only, relies on validated constraint).
-- 3e: Drop the now-redundant CHECK constraint.
--
-- If VALIDATE fails, residual NULLs from Step 2 remain. Re-run Step 2,
-- then retry Step 3. Do not force through with data gaps.

ALTER TABLE visit_invoices
    ALTER COLUMN billing_status SET DEFAULT 'pending';

ALTER TABLE visit_invoices
    ADD CONSTRAINT visit_invoices_billing_status_not_null
    CHECK (billing_status IS NOT NULL)
    NOT VALID;

ALTER TABLE visit_invoices
    VALIDATE CONSTRAINT visit_invoices_billing_status_not_null;

ALTER TABLE visit_invoices
    ALTER COLUMN billing_status SET NOT NULL;

ALTER TABLE visit_invoices
    DROP CONSTRAINT IF EXISTS visit_invoices_billing_status_not_null;


-- =============================================================================
-- Verify:
--   SELECT column_name, is_nullable, column_default
--   FROM information_schema.columns
--   WHERE table_name = 'visit_invoices' AND column_name = 'billing_status';
-- Expected: is_nullable=NO, column_default='pending'
-- =============================================================================
