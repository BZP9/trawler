-- RowInfer base schema. Idempotent. Run on a fresh DB.

CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector — required for enc.* tables

CREATE SCHEMA IF NOT EXISTS gen;
CREATE SCHEMA IF NOT EXISTS enc;
CREATE SCHEMA IF NOT EXISTS cfg;
CREATE SCHEMA IF NOT EXISTS raw;   -- user data tables; default schema for source.from_db()

-- cfg.system_prompt: prompt source. Snapshotted into _gen_log.config at register.
CREATE TABLE IF NOT EXISTS cfg.system_prompt (
  name             text PRIMARY KEY,
  content          text NOT NULL,
  expected_output  text NOT NULL CHECK (expected_output IN ('t','j')),
  description      text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- cfg.model_type: per-run transport. Resolved → base_url via env at register.
-- base_url_env nullable for protocols that don't need HTTP (e.g. sentence_transformers).
CREATE TABLE IF NOT EXISTS cfg.model_type (
  name          text PRIMARY KEY,
  protocol      text NOT NULL,
  base_url_env  text,
  api_key_env   text,
  description   text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE cfg.model_type ALTER COLUMN base_url_env DROP NOT NULL;

-- cfg.decoder / cfg.encoder: model rows. No transport info baked in.
CREATE TABLE IF NOT EXISTS cfg.decoder (
  name        text PRIMARY KEY,
  repo_name   text NOT NULL,
  format      jsonb,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cfg.encoder (
  name        text PRIMARY KEY,
  repo_name   text NOT NULL,
  dim         integer NOT NULL,
  format      jsonb,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- gen._gen_log: source of truth for generate runs.
CREATE TABLE IF NOT EXISTS gen._gen_log (
  run_id                uuid PRIMARY KEY,
  name                  text NOT NULL,
  model                 text NOT NULL,
  status                text NOT NULL CHECK (status IN ('running','complete','failed','interrupted','early_stopped','exported','cleaned','partial')),
  started_at            timestamptz NOT NULL DEFAULT now(),
  ended_at              timestamptz,
  n_rows                integer,
  n_done                integer NOT NULL DEFAULT 0,
  error                 text,
  system_prompt_content text,
  n_failed              integer NOT NULL DEFAULT 0,
  config                jsonb NOT NULL
);
ALTER TABLE gen._gen_log ADD COLUMN IF NOT EXISTS system_prompt_content text;
ALTER TABLE gen._gen_log ADD COLUMN IF NOT EXISTS n_failed integer NOT NULL DEFAULT 0;
-- 'cleaned' status added 2026-07-14: `trawler clean --yes` now stamps status
-- directly (was only config->>'stage'='cleaned') so psql is the live status
-- view. 'partial' added same day: an offload job with some rows imported but
-- main task NOT complete, and NOT known to be stopped, used to overload
-- 'interrupted' (which now means "actually stopped": watchdog exit 2 /
-- parked in queue/interrupted/, or a local run ^C). 'running' is reused
-- (not new) for "remote actively processing" — same meaning as a local
-- in-progress run. Widen-only — never narrows/drops existing rows; historical
-- 'interrupted' rows from before this split are left as-is (self-correct on
-- next sync/import, not backfilled). Live DBs created before this change need:
--   ALTER TABLE gen._gen_log DROP CONSTRAINT _gen_log_status_check;
--   ALTER TABLE gen._gen_log ADD CONSTRAINT _gen_log_status_check
--     CHECK (status IN ('running','complete','failed','interrupted','early_stopped','exported','cleaned','partial'));
DO $$
BEGIN
  ALTER TABLE gen._gen_log DROP CONSTRAINT IF EXISTS _gen_log_status_check;
  ALTER TABLE gen._gen_log ADD CONSTRAINT _gen_log_status_check
    CHECK (status IN ('running','complete','failed','interrupted','early_stopped','exported','cleaned','partial'));
END $$;

-- enc._enc_log: source of truth for encode runs.
CREATE TABLE IF NOT EXISTS enc._enc_log (
  run_id      uuid PRIMARY KEY,
  name        text NOT NULL,
  model       text NOT NULL,
  dim         integer,
  status      text NOT NULL CHECK (status IN ('running','complete','failed','interrupted','early_stopped')),
  started_at  timestamptz NOT NULL DEFAULT now(),
  ended_at    timestamptz,
  n_rows      integer,
  n_done      integer NOT NULL DEFAULT 0,
  n_failed    integer NOT NULL DEFAULT 0,
  error       text,
  config      jsonb NOT NULL
);
ALTER TABLE enc._enc_log ADD COLUMN IF NOT EXISTS n_failed integer NOT NULL DEFAULT 0;
ALTER TABLE enc._enc_log ADD COLUMN IF NOT EXISTS source_table text;
ALTER TABLE enc._enc_log ADD COLUMN IF NOT EXISTS source_run_id uuid;
ALTER TABLE gen._gen_log ADD COLUMN IF NOT EXISTS source_table text;
ALTER TABLE gen._gen_log ADD COLUMN IF NOT EXISTS source_run_id uuid;

-- Per-output tables (gen.<system_prompt.name>, enc.<encoder.name>) created dynamically by run.base.BaseRun._ensure_out_table.
