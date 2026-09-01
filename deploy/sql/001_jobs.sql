-- Postgres-backed job queue. Replaces the Azure Service Bus queue "q1":
--   * app/enqueue/*.py INSERT rows here (scheduled delivery = deliver_at)
--   * listener/worker.py polls due rows and wakes the device over MQTT
-- Applied by deploy/restore_db.sh after the pg_dump restore. Idempotent.

CREATE TABLE IF NOT EXISTS jobs (
    id          BIGSERIAL   PRIMARY KEY,               -- stored in tasks.enqueue_sequence_id
    kind        TEXT        NOT NULL,                   -- 'task' | 'text_message'
    payload     JSONB       NOT NULL,                   -- same JSON body the Service Bus message carried
    deliver_at  TIMESTAMPTZ NOT NULL DEFAULT now(),     -- earliest time the worker may run the job
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at     TIMESTAMPTZ,                            -- NULL = pending
    attempts    INTEGER     NOT NULL DEFAULT 0          -- failed processing attempts (worker gives up at 5)
);

-- Worker poll: WHERE deliver_at <= now() AND done_at IS NULL ORDER BY deliver_at
CREATE INDEX IF NOT EXISTS jobs_pending_deliver_at_idx
    ON jobs (deliver_at)
    WHERE done_at IS NULL;
