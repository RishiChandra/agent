-- Make `tasks` the single task table for Kairos reminders AND orchestrator-dispatched agent tasks,
-- and drop the never-used `agent_tasks` table. See ORCHESTRATOR_V2_DESIGN.md §10.5.
--
-- Run as the postgres superuser (agent_tasks is postgres-owned), in ONE transaction:
--   sudo -u postgres psql -d ai_pin_db -v ON_ERROR_STOP=1 -1 -f 002_tasks_master.sql
-- Idempotent: safe to re-run. Additive for existing code: every new column is nullable or defaulted, and the existing
-- status values ('pending', 'completed') stay valid, so the Kairos tools and /tasks routes keep working unchanged.

-- 1. Drop agent_tasks, refusing if anything was ever written to it.
DO $$
BEGIN
    IF to_regclass('public.agent_tasks') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM agent_tasks) THEN
            RAISE EXCEPTION 'agent_tasks is not empty; migrate its rows before dropping it';
        END IF;
        DROP TABLE agent_tasks;
    END IF;
END $$;

-- 2. New columns on tasks.
ALTER TABLE tasks
    -- Scheduled = has a time to fire. Derived, so it can never disagree with time_to_execute.
    ADD COLUMN IF NOT EXISTS is_scheduled      BOOLEAN GENERATED ALWAYS AS (time_to_execute IS NOT NULL) STORED,
    -- 'reminder' = user's own task (Kairos / app); 'agent_task' = work dispatched to a sub-agent (Protocol 2).
    ADD COLUMN IF NOT EXISTS kind              TEXT        NOT NULL DEFAULT 'reminder',
    -- Who created it: 'kairos' | 'app' | 'orchestrator' | 'agent'. NULL for rows created before this migration.
    ADD COLUMN IF NOT EXISTS created_by        TEXT,
    -- The sub-agent that owns/executes the task (agent tasks only).
    ADD COLUMN IF NOT EXISTS agent_id          UUID,
    -- How to tell the user when it finishes: wake the device, wait for the next session, or don't announce.
    ADD COLUMN IF NOT EXISTS notify            TEXT        NOT NULL DEFAULT 'device',
    -- Agent-task lifecycle data.
    ADD COLUMN IF NOT EXISTS question          TEXT,                    -- set while status = 'input_required'
    ADD COLUMN IF NOT EXISTS result            JSONB,                   -- {say, output, error}
    ADD COLUMN IF NOT EXISTS deadline_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS finished_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivered_at      TIMESTAMPTZ,             -- NULL until the user has heard the result
    ADD COLUMN IF NOT EXISTS delivered_via     TEXT,                    -- 'live' | 'device_wake' | 'next_session'
    ADD COLUMN IF NOT EXISTS agent_informed_at TIMESTAMPTZ,             -- NULL until the owning agent got task.closed
    -- Bookkeeping. Existing rows get the migration time as created_at (true creation time is unknown).
    ADD COLUMN IF NOT EXISTS created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ NOT NULL DEFAULT now();

-- 3. status is always set by the app; make that a rule (live rows are all 'pending').
UPDATE tasks SET status = 'pending' WHERE status IS NULL;
ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'pending', ALTER COLUMN status SET NOT NULL;

-- 4. Constraints (ADD CONSTRAINT has no IF NOT EXISTS, hence the guard).
DO $$
BEGIN
    -- Deleting a user deletes their tasks. Verified 2026-10-05: no orphan user_ids.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_user_id_fkey') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
    END IF;
    -- cancel_job() DELETEs pending jobs; SET NULL keeps that working and lets old jobs be pruned.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_enqueue_sequence_id_fkey') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_enqueue_sequence_id_fkey
            FOREIGN KEY (enqueue_sequence_id) REFERENCES jobs (id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_status_check') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (status IN (
            'pending',                                   -- not started / waiting for its time (Kairos today)
            'dispatching', 'running', 'input_required',  -- agent task in flight
            'completed',                                 -- done (Kairos today; agent task succeeded)
            'failed', 'cancelled', 'timed_out'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_kind_check') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN ('reminder', 'agent_task'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_notify_check') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_notify_check CHECK (notify IN ('device', 'next_session', 'silent'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_agent_task_has_agent') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_agent_task_has_agent CHECK (kind <> 'agent_task' OR agent_id IS NOT NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_agent_id_fkey') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_agent_id_fkey
            FOREIGN KEY (agent_id) REFERENCES agents (agent_id) ON DELETE SET NULL;
    END IF;
END $$;

-- 5. Indexes for the queries the app and orchestrator run.
CREATE INDEX IF NOT EXISTS tasks_user_status_idx    ON tasks (user_id, status);
CREATE INDEX IF NOT EXISTS tasks_user_scheduled_idx ON tasks (user_id, time_to_execute) WHERE time_to_execute IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_undelivered_idx    ON tasks (user_id)
    WHERE kind = 'agent_task' AND delivered_at IS NULL AND status IN ('completed', 'failed', 'timed_out');
CREATE INDEX IF NOT EXISTS tasks_agent_idx          ON tasks (agent_id) WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_enqueue_seq_idx    ON tasks (enqueue_sequence_id) WHERE enqueue_sequence_id IS NOT NULL;  -- FK lookups on job delete

-- 6. Keep updated_at current on every UPDATE, so existing code doesn't have to set it.
CREATE OR REPLACE FUNCTION tasks_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tasks_touch_updated_at ON tasks;
CREATE TRIGGER tasks_touch_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION tasks_touch_updated_at();
