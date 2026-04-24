-- Stub replica init for local development.
-- Creates get_replication_lag_seconds() — a custom function controlled
-- via the _stub_config table. Used when DEV_MODE=true.
-- In production, the real pg_last_xact_replay_timestamp() is used instead.

CREATE TABLE IF NOT EXISTS _stub_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO _stub_config (key, value) VALUES ('lag_seconds', '2.0')
ON CONFLICT (key) DO NOTHING;

-- Custom function that returns a controllable lag value.
-- Update _stub_config to change the returned lag during testing.
CREATE OR REPLACE FUNCTION get_replication_lag_seconds()
RETURNS NUMERIC AS $$
DECLARE lag_secs NUMERIC;
BEGIN
    SELECT value::NUMERIC INTO lag_secs FROM _stub_config WHERE key = 'lag_seconds';
    RETURN lag_secs;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS visit_invoices (
    id         BIGSERIAL PRIMARY KEY,
    patient_id BIGINT NOT NULL,
    visit_date DATE NOT NULL DEFAULT CURRENT_DATE,
    amount     NUMERIC(10,2) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO visit_invoices (patient_id, visit_date, amount)
SELECT
    (random() * 10000)::BIGINT,
    CURRENT_DATE - (random() * 365)::INT,
    (random() * 500)::NUMERIC(10,2)
FROM generate_series(1, 1000);