-- Referential integrity, a few query indexes, and agents.agent_info json -> jsonb.
-- Verified 2026-10-05 on ai_pin_db: no orphan rows for any foreign key added here.
--
-- Run as the postgres superuser (most tables are postgres-owned), in ONE transaction:
--   sudo -u postgres psql -d ai_pin_db -v ON_ERROR_STOP=1 -1 -f 003_integrity_indexes.sql
-- Idempotent: safe to re-run.
--
-- Behaviour change to know about: rows can no longer reference a user_id that is not in `users`.
-- E.g. the legacy /ws handler's create_session() for an unknown user_id now errors instead of creating a session.

-- 1. Foreign keys. User-owned rows go with the user (CASCADE); message history is kept (RESTRICT),
--    so deleting a user who sent messages must deal with those messages explicitly.
DO $$
DECLARE
    fk RECORD;
BEGIN
    FOR fk IN SELECT * FROM (VALUES
        ('sessions',                  'sessions_user_id_fkey',                  'user_id', 'users (user_id)',   'CASCADE'),
        ('agent_registry',            'agent_registry_user_id_fkey',            'user_id', 'users (user_id)',   'CASCADE'),
        ('agent_registry',            'agent_registry_agent_id_fkey',           'agent_id','agents (agent_id)', 'CASCADE'),
        ('chat_members',              'chat_members_user_id_fkey',              'user_id', 'users (user_id)',   'CASCADE'),
        ('messages',                  'messages_sender_id_fkey',                'sender_id','users (user_id)',  'RESTRICT'),
        ('relationships',             'relationships_uid1_fkey',                'uid1',    'users (user_id)',   'CASCADE'),
        ('relationships',             'relationships_uid2_fkey',                'uid2',    'users (user_id)',   'CASCADE'),
        ('pending_text_message_jobs', 'pending_text_message_jobs_user_id_fkey', 'user_id', 'users (user_id)',   'CASCADE')
    ) AS t(tbl, name, col, ref, on_delete)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = fk.name) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES %s ON DELETE %s',
                           fk.tbl, fk.name, fk.col, fk.ref, fk.on_delete);
        END IF;
    END LOOP;
END $$;

-- 2. Indexes: FK columns not already leading an index (fast cascades), plus the common chat queries.
CREATE INDEX IF NOT EXISTS agent_registry_agent_id_idx ON agent_registry (agent_id);
CREATE INDEX IF NOT EXISTS chat_members_user_id_idx    ON chat_members (user_id);
CREATE INDEX IF NOT EXISTS messages_chat_created_idx   ON messages (chat_id, created_at);
CREATE INDEX IF NOT EXISTS messages_sender_id_idx      ON messages (sender_id);
CREATE INDEX IF NOT EXISTS relationships_uid2_idx      ON relationships (uid2);

-- 3. messages: defaults the inserts already assume, then make them rules (no NULLs in live data).
ALTER TABLE messages
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN is_read    SET DEFAULT false;
UPDATE messages SET created_at = now() WHERE created_at IS NULL;
UPDATE messages SET is_read = false   WHERE is_read IS NULL;
ALTER TABLE messages
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN is_read    SET NOT NULL;

-- 4. agents.agent_info json -> jsonb (indexable, and what the router's ->> lookups want).
--    psycopg2 returns a dict for both types, so app code is unaffected; jsonb does not keep key order.
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agents' AND column_name = 'agent_info') = 'json' THEN
        ALTER TABLE agents ALTER COLUMN agent_info TYPE jsonb USING agent_info::jsonb;
    END IF;
END $$;

-- Lookups the registry runs on every register/heartbeat and spoken-name resolve.
CREATE INDEX IF NOT EXISTS agents_service_id_idx ON agents ((agent_info->>'service_id'));
CREATE INDEX IF NOT EXISTS agents_name_lower_idx ON agents (lower(agent_info->>'name'));
