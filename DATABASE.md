# Database (host PostgreSQL `ai_pin_db`)

The system of record: users, tasks, messages, sessions, the agent registry, and the reminder `jobs` table. It is a **host**
PostgreSQL 16 install on the VM (not a container). Infrastructure basics are in
[OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

## What / where

| | |
|---|---|
| Engine | PostgreSQL **16.15** (Ubuntu package), host install |
| Databases | `ai_pin_db` (live) and PostgreSQL's own `postgres`. No staging DB: `agent_rehearsal` was dropped 2026-10-06 |
| Data dir | `/var/lib/postgresql/16/main` (boot disk) |
| Roles | `appuser` (app/worker login, owner of `ai_pin_db`, **not** superuser), `postgres` (admin) |
| Extensions | `plpgsql`, `pg_cron` 1.6 (loaded, no jobs configured) |
| Application tables (11) | `agents`, `agent_registry`, `chat_members`, `chats`, `jobs`, `messages`, `pending_text_message_jobs`, `relationships`, `sessions`, `tasks`, `users`. Full schema in [Schema](#schema). `agent_tasks` was dropped 2026-10-06 |

Migrated from Azure PostgreSQL Flexible Server `ai-pin-server` (PG 16.14, West US 3). The 2026-09-01 dump was accepted as the
final copy; the app cut over to `ai_pin_db` on 2026-09-11. There is no migration tool: `agents` is auto-created at app
startup (`ensure_agents_table`), `jobs` by the worker (`deploy/sql/001_jobs.sql`), and the rest was restored from the dump
and altered by hand since. [Schema](#schema) below is the source of truth.

## Schema

Live `ai_pin_db` as of **2026-10-06**. Row counts are tiny (single digits to 11 per table).
Primary keys are **PK** and foreign keys are **FK**. Every FK was checked to have no orphan rows before it was added.

```
users ◄──┬── sessions            (1:1, CASCADE)
         ├── tasks ──► agents     (tasks.user_id CASCADE; tasks.agent_id SET NULL) ──► jobs (enqueue_sequence_id SET NULL)
         ├── agent_registry ──► agents   (both CASCADE)
         ├── chat_members ──► chats      (user CASCADE)
         ├── messages ──► chats          (sender_id RESTRICT)
         ├── relationships (uid1, uid2)  (CASCADE)
         └── pending_text_message_jobs   (CASCADE)
```

### `users`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `user_id` | uuid | no | | **PK** |
| `first_name`, `last_name`, `username` | text | yes | | |
| `firebase_uid` | text | yes | | |
| `timezone` | text | no | `'UTC'` | Drives Kairos time parsing |
| `device_prefix` | text | yes | | |

### `sessions`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `user_id` | uuid | no | | **PK**, **FK** → `users` ON DELETE CASCADE |
| `scratchpad` | text | yes | | Cleared when the session goes inactive |
| `is_active` | boolean | no | | The worker defers wakes while this is true |

Inserted on first connect by `create_session()`. Because of the FK, a connect with a user_id that isn't in `users` now
errors instead of creating a session.

### `tasks`: the master task table

One table for the user's own reminders (Kairos voice tools, the app's `/tasks` routes) and orchestrator-dispatched agent
tasks ([ORCHESTRATOR_V2_TOOL_CALLS.md](ORCHESTRATOR_V2_TOOL_CALLS.md) §10.5).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `task_id` | uuid | no | | **PK**. Also the agent's idempotency key |
| `user_id` | uuid | no | | **FK** → `users` ON DELETE CASCADE |
| `task_info` | jsonb | yes | | `{"info": "<text>"}` from Kairos and the app. Agent tasks add `intent`, `slots` |
| `status` | text | no | `'pending'` | CHECK: `pending`, `dispatching`, `running`, `input_required`, `completed`, `failed`, `cancelled`, `timed_out`. Kairos uses only `pending` and `completed` |
| `time_to_execute` | timestamptz | yes | | **NULL = unscheduled.** Only scheduled tasks get a `jobs` row |
| `enqueue_sequence_id` | bigint | yes | | **FK** → `jobs.id` ON DELETE SET NULL. The pending wake |
| `is_scheduled` | boolean | (generated) | | `GENERATED ALWAYS AS (time_to_execute IS NOT NULL) STORED` |
| `kind` | text | no | `'reminder'` | CHECK: `reminder`, `agent_task` |
| `created_by` | text | yes | | `kairos`, `app`, `orchestrator` or `agent`. NULL for rows created before 2026-10-06 |
| `agent_id` | uuid | yes | | **FK** → `agents` ON DELETE SET NULL. CHECK: required when `kind = 'agent_task'` |
| `notify` | text | no | `'device'` | CHECK: `device` (wake the pin), `next_session`, `silent` |
| `question` | text | yes | | Set while `input_required` |
| `result` | jsonb | yes | | `{say, output, error}` |
| `deadline_at`, `finished_at` | timestamptz | yes | | |
| `delivered_at` | timestamptz | yes | | NULL until the user has heard the result |
| `delivered_via` | text | yes | | `live`, `device_wake` or `next_session` |
| `agent_informed_at` | timestamptz | yes | | NULL until the owning agent got `task.closed` |
| `created_at` | timestamptz | no | `now()` | Rows from before 2026-10-06 carry that date |
| `updated_at` | timestamptz | no | `now()` | Kept current by trigger `tasks_touch_updated_at` |

Indexes: `(user_id, status)`; `(user_id, time_to_execute) WHERE time_to_execute IS NOT NULL`; `(user_id) WHERE
kind = 'agent_task' AND delivered_at IS NULL AND status IN (completed, failed, timed_out)` (undelivered results);
`(agent_id)` and `(enqueue_sequence_id)` where not null.

### `jobs`

The scheduled-wake queue polled by the worker ([SCHEDULER.md](SCHEDULER.md)). Defined in `deploy/sql/001_jobs.sql`.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | bigint | no | `nextval('jobs_id_seq')` | **PK**. Referenced by `tasks.enqueue_sequence_id` |
| `kind` | text | no | | `task` or `text_message` |
| `payload` | jsonb | no | | Wake body forwarded to the device |
| `deliver_at` | timestamptz | no | `now()` | |
| `created_at` | timestamptz | no | `now()` | |
| `done_at` | timestamptz | yes | | NULL = pending |
| `attempts` | integer | no | `0` | The worker gives up at 5 |

Index: `(deliver_at) WHERE done_at IS NULL`. `cancel_job()` deletes pending rows, which nulls the task's pointer through
the FK.

### `agents`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `agent_id` | uuid | no | | **PK** |
| `agent_info` | jsonb | yes | | `{name, summary, service_id, keywords[], capabilities[], user_intents[], active, …}` |
| `agent_url` | text | yes | | Bridge WebSocket URL |

Indexes: `(agent_info->>'service_id')`, `lower(agent_info->>'name')`. Also auto-created by `ensure_agents_table()`
(now jsonb).

### `agent_registry`

Which agents each user has.

| Column | Type | Null | Notes |
|---|---|---|---|
| `user_id` | uuid | no | **PK** (with `agent_id`). **FK** → `users` ON DELETE CASCADE |
| `agent_id` | uuid | no | **FK** → `agents` ON DELETE CASCADE. Indexed |

### `chats`, `chat_members`, `messages`

| Table | Column | Type | Null | Default | Notes |
|---|---|---|---|---|---|
| `chats` | `chat_id` | uuid | no | | **PK** |
| `chat_members` | `chat_id` | uuid | no | | **PK** (with `user_id`). **FK** → `chats` |
| | `user_id` | uuid | no | | **FK** → `users` ON DELETE CASCADE. Indexed |
| `messages` | `chat_id` | uuid | no | | **PK** (with `message_id`). **FK** → `chats` |
| | `message_id` | uuid | no | | |
| | `sender_id` | uuid | no | | **FK** → `users` ON DELETE **RESTRICT** (history is kept; delete messages first). Indexed |
| | `content` | text | yes | | |
| | `created_at` | timestamptz | no | `now()` | Index `(chat_id, created_at)` |
| | `is_read` | boolean | no | `false` | |

### `pending_text_message_jobs`

| Column | Type | Null | Notes |
|---|---|---|---|
| `user_id` | uuid | no | **PK** (with `message_id`). **FK** → `users` ON DELETE CASCADE |
| `message_id` | uuid | no | No FK: `messages` is keyed on `(chat_id, message_id)`. **All 6 current rows point at message_ids that no longer exist** |

### `relationships`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `relationship_id` | uuid | no | `gen_random_uuid()` | **PK** |
| `uid1`, `uid2` | uuid | no | | **FK** → `users` ON DELETE CASCADE. `uid2` indexed |
| `rel_type` | text | no | | UNIQUE `(uid1, uid2, rel_type)` |

Schema changes are applied by hand as postgres, in one transaction, after a `pg_dump` into `/home/ubuntu/db-backups/`
(e.g. `pre-tasks-master-20261006T045614Z.dump`, taken before the 2026-10-06 changes). `jobs` is the exception: the worker
creates it from `deploy/sql/001_jobs.sql` at startup.

## How the app reaches it (container → host bridge)

The app and worker run in Docker; PostgreSQL runs on the host. They connect over the **`app-backend` bridge network**
(`172.30.0.0/24`), not localhost. What was configured (all reversible, `.bak` files kept on the VM):

- **`listen_addresses`** = `localhost,172.30.0.1` via drop-in `/etc/postgresql/16/main/conf.d/10-app-backend.conf`.
- **`pg_hba.conf`** line: `host ai_pin_db,agent_rehearsal appuser 172.30.0.0/24 scram-sha-256`. The `agent_rehearsal`
  entry is now inert (that DB was dropped); reuse or edit it when a real staging DB is set up.
- **Firewall**: iptables INPUT rule allowing `172.30.0.0/24 → 172.30.0.1:5432`, persisted to `/etc/iptables/rules.v4`.
- **systemd** drop-in `postgresql@16-main.service.d/10-after-docker.conf` (`After=docker.service`) so the bridge gateway
  exists before PostgreSQL binds it on boot. **Only validated on a live restart, not a full reboot** — see the reboot-check
  hardening item in the [infra doc](OCI_INFRASTRUCTURE.md#hardening--pending-before-this-is-production-safe).

So the app uses `DB_HOST=172.30.0.1`, `DB_USER=appuser`, `DB_NAME=ai_pin_db`. This route is **private and IPv4-only** — no
public port, no TLS hostname cert needed (the snakeoil cert stays). The device never touches PostgreSQL directly.

## Connecting from a laptop (Beekeeper / TablePlus / psql)

PostgreSQL listens only on `127.0.0.1` and the Docker bridge, so every laptop connection goes through an **SSH tunnel**.

1. **Get the password** (don't paste it into shared chat). It is `appuser`'s password in the app env file:

   ```sh
   ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232 'grep ^DB_PASSWORD= /home/ubuntu/app-backend-config/backend.env'
   ```

2. **Beekeeper Studio** → new PostgreSQL connection, enable **SSH Tunnel**:

   | Field | Value |
   |---|---|
   | SSH host / port / user | `146.235.229.232` / `22` / `ubuntu` |
   | SSH key | `/Users/rishi/projects/keys/agent/id_ed25519` |
   | DB host / port | `127.0.0.1` / `5432` |
   | User | `appuser` |
   | Password | from step 1 |
   | Database | `ai_pin_db` |
   | SSL | off / prefer (the tunnel encrypts the hop) |

   Save it as **OCI — live (`ai_pin_db`)**. TablePlus/DBeaver/pgAdmin use the same
   settings. Terminal equivalent:

   ```sh
   ssh -i /Users/rishi/projects/keys/agent/id_ed25519 -L 15432:127.0.0.1:5432 ubuntu@146.235.229.232 -N &
   psql "host=127.0.0.1 port=15432 dbname=ai_pin_db user=appuser"
   ```

Admin work (create DB, extensions, roles) needs the `postgres` superuser: `ssh …` then `sudo -u postgres psql`. `appuser` is
rejected from the `postgres` database by HBA — that's expected.

The password was rotated during migration (a fresh 32-char value generated on the VM, applied with `ALTER ROLE appuser`) and
may be rotated again; the env file is always current, so re-read it if a saved password stops working.

## Staging

There is **no staging database** right now. `agent_rehearsal`, a migration-era clone of `ai_pin_db`, was out of date and
unused, and was dropped on 2026-10-06. A final dump is kept at
`/home/ubuntu/db-backups/agent_rehearsal-final-20261006T050108Z.dump` (+ `.sha256`). A proper staging DB is planned.
Until then, test a migration by running it against `ai_pin_db` inside a transaction that ends in `ROLLBACK`, after a backup.

**Gotcha for whoever builds staging:** `CREATE DATABASE … TEMPLATE ai_pin_db` fails because `pg_cron`'s launcher holds a
permanent session on `ai_pin_db`. Clone with `pg_dump`/`pg_restore` instead, filtering the `pg_cron` extension, its comment
and the `cron.*` objects from the restore list, since `pg_cron` can only live in `cron.database_name`.

## Backups

Manual custom-format dumps exist in `/home/ubuntu/db-backups/` (e.g. pre-cutover, pre-Kairos-URL, pre-task-delete), each with
a `.sha256`. **These are on the VM's boot disk only** — not disaster recovery. Off-VM backups to OCI Object Storage are a
tracked hardening item in the [infra doc](OCI_INFRASTRUCTURE.md#hardening--pending-before-this-is-production-safe).

## TODOs

- [ ] **Off-VM backups** — see the infra doc (highest-priority hardening item).
- [ ] **Reboot check** — validate the systemd `After=docker` ordering across a real reboot (infra doc).
- [ ] Set up a real staging DB (see [Staging](#staging)).
- [ ] Clean up the 6 orphaned `pending_text_message_jobs` rows (their messages are gone).
- [ ] Optional cleanup: `REASSIGN OWNED BY postgres TO appuser` inside `ai_pin_db` so `appuser` owns every table (today
  `tasks` is `appuser`-owned, the rest `postgres`-owned; runtime grants are fine, this only matters for future DDL by the app).
