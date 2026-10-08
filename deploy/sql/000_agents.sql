-- Agent registry (routing table for the orchestrator). Matches production as of
-- 2026-10-07. Every orchestrator v2 field lives inside agent_info (see DATABASE.md).
-- Applied by deploy/migrate.sh. Idempotent.

CREATE TABLE IF NOT EXISTS agents (
    agent_id   uuid PRIMARY KEY,
    agent_info jsonb,
    agent_url  text
);

CREATE INDEX IF NOT EXISTS agents_service_id_idx ON agents ((agent_info->>'service_id'));
CREATE INDEX IF NOT EXISTS agents_name_lower_idx ON agents (lower(agent_info->>'name'));
