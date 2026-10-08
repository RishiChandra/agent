-- Agent outbox (ORCHESTRATOR_V2_TOOL_CALLS.md §1.7, decision M-m).
-- Every request the orchestrator sends an agent is written here first, then
-- sent, and resent with the same msg_id until the agent replies (task.ack /
-- task.nack). Per task, rows are delivered in id order. A future pull binding
-- (Muse, Dots) would serve the same rows as the agent's inbox.
-- Applied by deploy/migrate.sh. Idempotent.

CREATE TABLE IF NOT EXISTS agent_outbox (
    id               BIGSERIAL   PRIMARY KEY,
    agent_id         uuid        NOT NULL REFERENCES agents (agent_id) ON DELETE CASCADE,
    task_id          uuid        NOT NULL REFERENCES tasks (task_id) ON DELETE CASCADE,
    msg_id           text        NOT NULL UNIQUE,   -- reused on every resend
    type             text        NOT NULL,          -- task.dispatch | task.update | ...
    envelope         jsonb       NOT NULL,          -- the full Protocol 2 message
    created_at       timestamptz NOT NULL DEFAULT now(),
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    attempts         integer     NOT NULL DEFAULT 0,
    sent_at          timestamptz,
    acked_at         timestamptz,                   -- task.ack or task.nack received
    reply            jsonb,
    expired_at       timestamptz                    -- gave up
);

CREATE INDEX IF NOT EXISTS agent_outbox_pending_idx
    ON agent_outbox (agent_id, next_attempt_at)
    WHERE acked_at IS NULL AND expired_at IS NULL;

CREATE INDEX IF NOT EXISTS agent_outbox_task_idx ON agent_outbox (task_id, id);

-- The app connects as appuser; make it the owner when migrations run as postgres.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'appuser') THEN
        ALTER TABLE agent_outbox OWNER TO appuser;
    END IF;
END $$;
