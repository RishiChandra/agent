# Database (host PostgreSQL `ai_pin_db`)

The system of record: users, tasks, messages, sessions, the agent registry, and the reminder `jobs` table. It is a **host**
PostgreSQL 16 install on the VM (not a container). Infrastructure basics are in
[OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

## What / where

| | |
|---|---|
| Engine | PostgreSQL **16.15** (Ubuntu package), host install |
| Databases | `ai_pin_db` (live), `agent_rehearsal` (staging clone), plus PostgreSQL's own `postgres` |
| Data dir | `/var/lib/postgresql/16/main` (boot disk) |
| Roles | `appuser` (app/worker login, owner of `ai_pin_db`, **not** superuser), `postgres` (admin) |
| Extensions | `plpgsql`, `pg_cron` 1.6 (loaded, no jobs configured) |
| Application tables (11) | `agents`, `agent_registry`, `agent_tasks`, `chat_members`, `chats`, `messages`, `pending_text_message_jobs`, `relationships`, `sessions`, `tasks`, `users` — plus `jobs` (see [SCHEDULER.md](SCHEDULER.md)) |

Migrated from Azure PostgreSQL Flexible Server `ai-pin-server` (PG 16.14, West US 3). The 2026-09-01 dump was accepted as the
final copy; the app cut over to `ai_pin_db` on 2026-09-11. The schema has no migration tool — `agents` is auto-created at app
startup (`ensure_agents_table`); the rest was created by hand / restored from the dump.

## How the app reaches it (container → host bridge)

The app and worker run in Docker; PostgreSQL runs on the host. They connect over the **`app-backend` bridge network**
(`172.30.0.0/24`), not localhost. What was configured (all reversible, `.bak` files kept on the VM):

- **`listen_addresses`** = `localhost,172.30.0.1` via drop-in `/etc/postgresql/16/main/conf.d/10-app-backend.conf`.
- **`pg_hba.conf`** line: `host ai_pin_db,agent_rehearsal appuser 172.30.0.0/24 scram-sha-256`.
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
   | Database | `ai_pin_db` (live) or `agent_rehearsal` (staging) |
   | SSL | off / prefer (the tunnel encrypts the hop) |

   Save two connections: **OCI — live (`ai_pin_db`)** and **OCI — rehearsal**. TablePlus/DBeaver/pgAdmin use the same
   settings. Terminal equivalent:

   ```sh
   ssh -i /Users/rishi/projects/keys/agent/id_ed25519 -L 15432:127.0.0.1:5432 ubuntu@146.235.229.232 -N &
   psql "host=127.0.0.1 port=15432 dbname=ai_pin_db user=appuser"
   ```

Admin work (create DB, extensions, roles) needs the `postgres` superuser: `ssh …` then `sudo -u postgres psql`. `appuser` is
rejected from the `postgres` database by HBA — that's expected.

The password was rotated during migration (a fresh 32-char value generated on the VM, applied with `ALTER ROLE appuser`) and
may be rotated again; the env file is always current, so re-read it if a saved password stops working.

## `agent_rehearsal`

A staging clone of `ai_pin_db`, created during the migration for tests. Because `pg_cron`'s launcher holds a permanent session
on `ai_pin_db`, `CREATE DATABASE … TEMPLATE ai_pin_db` fails; it was made via `pg_dump`/`pg_restore` with the pg_cron
extension/objects filtered out. To re-clone: stop anything using it, `DROP DATABASE agent_rehearsal`, and repeat that
dump/restore. **Gotcha:** any future restore of `ai_pin_db` into a differently named DB must filter the `pg_cron` extension,
its comment, and the `cron.*` objects from the restore list — `pg_cron` can only live in `cron.database_name`.

## Backups

Manual custom-format dumps exist in `/home/ubuntu/db-backups/` (e.g. pre-cutover, pre-Kairos-URL, pre-task-delete), each with
a `.sha256`. **These are on the VM's boot disk only** — not disaster recovery. Off-VM backups to OCI Object Storage are a
tracked hardening item in the [infra doc](OCI_INFRASTRUCTURE.md#hardening--pending-before-this-is-production-safe).

## TODOs

- [ ] **Off-VM backups** — see the infra doc (highest-priority hardening item).
- [ ] **Reboot check** — validate the systemd `After=docker` ordering across a real reboot (infra doc).
- [ ] Decide whether to **drop `agent_rehearsal`** or keep it as the standing staging DB.
- [ ] Optional cleanup: `REASSIGN OWNED BY postgres TO appuser` inside `ai_pin_db` so `appuser` owns every table (today
  `tasks` is `appuser`-owned, the rest `postgres`-owned; runtime grants are fine, this only matters for future DDL by the app).
