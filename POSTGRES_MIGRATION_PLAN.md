# Move PostgreSQL from Azure to OCI

**Revised 2026-09-10 evening: the database work now runs in parallel with the application move, and the end state is
the OCI app on the same VM talking to host `ai_pin_db`.** The user chose to skip Azure connectivity entirely: no Azure
service will be pointed at this database, so the earlier steps about Azure egress addresses, a public database
hostname, hostname-verified TLS and Azure App Service/Function settings are dropped. Follow the
[application plan](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md) for the app side; this file owns everything about the database.

**Starting point: keep host `ai_pin_db` on `ai-assistant-server`.** The [inspection](#verified-postgresql-inspection--2026-09-10)
verified that all 11 application tables match the 2026-09-01 Azure dump in schema and data, and that `appuser` has the
runtime grants. No new VM, PostgreSQL installation or database rename is needed. **Do not restore over `ai_pin_db`.**
The Docker `aipin` database and the stale app/worker containers were **deleted on 2026-09-11** at the user's request (logical dump
kept in the Step 1 recovery directory). Host `ai_pin_db` is now the only application database on the VM besides the `agent_rehearsal` clone.

This guide moves **only the database**. Mobile-app repair, application auth, role redesign and the scheduler are
separate work. The one Azure-related choice left is whether to take a final snapshot (Step 2).

## Progress as of 2026-09-10 (revised)

Checked boxes mean verified complete. This table replaces the earlier ten-step Azure-oriented list.

| Step | Status | What remains |
|---|---|---|
| 1. Record connection details | Done for OCI | Nothing; Azure details only matter if Step 2's snapshot is taken |
| 2. Decide on a final Azure snapshot | **Decided 2026-09-10: accept the 2026-09-01 copy** | Nothing; Azure data after Sept 1 is abandoned |
| 3. Connect the OCI app container to the host database | **Done 2026-09-11 UTC** | Nothing; reboot ordering is checked in Step 7 |
| 4. Rehearsal database and restore procedure | **Done 2026-09-11 UTC** | Nothing; re-clone procedure recorded below |
| 5. Test the OCI app against the rehearsal copy | **Done 2026-09-11 UTC** | Nothing; CRUD + voice results in app plan Step 4 |
| 6. Backups | Pending | Scheduled off-VM logical dumps to OCI Object Storage; one restore test |
| 7. Reboot verification | Pending | Planned reboot; PostgreSQL, Docker stack and new app all come back |
| 8. Cutover | **Done 2026-09-11 UTC** | OCI app writes to `ai_pin_db`; it is now the system of record |
| 9. Observe and retire Azure PostgreSQL | Pending | Final off-Azure dump, then delete `ai-pin-server` |

## Step 1 — Connection details

**Status: done.**

- [x] OCI region `us-sanjose-1`; VM `ai-assistant-server`, A1, 2 OCPUs / 12 GB RAM, Ubuntu 24.04 ARM64.
- [x] SSH as `ubuntu` at `146.235.229.232` with `/Users/rishi/projects/keys/agent/id_ed25519`; passwordless sudo.
- [x] Destination: host PostgreSQL 16.15, database `ai_pin_db` owned by `appuser`; roles present: `postgres`, `appuser`.
- [x] Host PostgreSQL currently listens on `127.0.0.1:5432` only; HBA allows local peer and loopback scram only.
- [x] Host firewall: iptables-nft, persisted in `/etc/iptables/rules.v4`; INPUT accepts 22, 80, 443, 8883 and rejects the rest.
- [x] Docker: default bridge `172.17.0.0/16`; the old stack uses `aipin_default`; the new compose project gets its own network.

Admin access to the database is `sudo -u postgres psql` over SSH, or an SSH tunnel to `127.0.0.1:5432` from TablePlus/pgAdmin.

## Step 2 — Decide whether to take a final Azure snapshot

**Status: decided 2026-09-10. The user accepted the 2026-09-01 copy as final.** The commands below are kept only in
case that changes before cutover.

`ai_pin_db` equals the 2026-09-01 dump. If the device or anyone else has used the Azure app since then, Azure holds newer
tasks/messages/sessions. Two choices:

- **Accept the 2026-09-01 copy as final.** Nothing to do; skip to Step 3. Anything written to Azure after September 1 is abandoned.
- **Take one last dump from the Mac.** Needs the Azure host, user and password (in `~/.pgpass`, never in this file):

```sh
umask 077 && cd ~/private-migration
pg_dump --dbname='service=azure_source' --format=custom --file=azure-final.dump
psql -X 'service=azure_source' -f row_counts.sql > azure-final-counts.txt
```

  Then compare counts with `ai_pin_db` (row_counts.sql over SSH). If they differ, restore the dump into a fresh empty
  database `ai_pin_db_final` using the Step 4 procedure and point the app at that name at cutover. Do not restore into `ai_pin_db`.

No Azure network changes are needed for this: the Mac's IP is presumably already allowed, as it produced the September 1 dump.

## Step 3 — Connect the OCI app container to the host database

**Status: done 2026-09-11 UTC (2026-09-10 Pacific evening).** What was applied on the VM, in order:

- [x] Docker network `app-backend` created by hand (`docker network create --driver bridge --subnet 172.30.0.0/24 --gateway 172.30.0.1 app-backend`);
  the subnet was confirmed unused. `docker-compose.oci.yml` references it as `external: true` (compose refused to adopt a
  hand-made network as its own, and an external network also survives `compose down`, which matters because PostgreSQL binds its gateway address).
- [x] `/etc/postgresql/16/main/conf.d/10-app-backend.conf` sets `listen_addresses = 'localhost,172.30.0.1'`. Originals backed up as
  `postgresql.conf.bak-<ts>` / `pg_hba.conf.bak-<ts>` in the same directory.
- [x] `pg_hba.conf` gained one line: `host ai_pin_db,agent_rehearsal appuser 172.30.0.0/24 scram-sha-256`.
- [x] systemd drop-in `/etc/systemd/system/postgresql@16-main.service.d/10-after-docker.conf` (`After=docker.service`, `Wants=docker.service`)
  so the bridge address exists before PostgreSQL binds it on boot. Verified only on a live restart so far; reboot check is Step 7.
- [x] iptables INPUT rule 6 (before the final REJECT): `-s 172.30.0.0/24 -d 172.30.0.1 -p tcp --dport 5432 -j ACCEPT`, persisted to
  `/etc/iptables/rules.v4` (backup `rules.v4.bak-<ts>`).
- [x] `/home/ubuntu/app-backend-config/backend.env` (`0700` dir, `0600` file) created on the VM: Gemini keys and tuning values copied from the old
  private env by key name, `DB_HOST=172.30.0.1`, `DB_PORT=5432`, `DB_NAME=agent_rehearsal`, `DB_USER=appuser`, and a freshly generated
  32-character `DB_PASSWORD` applied with `ALTER ROLE appuser`. No value was printed or left the VM. No Azure or MQTT variables.
- [x] `postgresql@16-main` restarted; now listening on `127.0.0.1:5432` and `172.30.0.1:5432`.
- [x] Verified from a throwaway `postgres:16` container on `app-backend`: scram login to `agent_rehearsal` and `ai_pin_db` as `appuser` works
  (`inet_server_addr() = 172.30.0.1`), an insert/rollback on a real table works, `dbname=postgres` is refused by HBA, and the same login
  from the default bridge gets "No route to host" (firewall). Loopback admin access unchanged. All five old `aipin-*` containers untouched.

The original instructions are kept below for reference. Each change is reversible: delete the drop-in files, remove the HBA line,
`iptables -D` the rule, restore the `.bak` files, restart PostgreSQL.

1. **Fixed-subnet network in `docker-compose.oci.yml`** (repo change):

```yaml
networks:
  default:
    name: app-backend
    driver: bridge
    ipam:
      config:
        - subnet: 172.30.0.0/24
          gateway: 172.30.0.1
```

   The app's `DB_HOST` is `172.30.0.1`. Check the subnet is unused first: `docker network inspect` on all networks and `ip route`.

2. **PostgreSQL listens on the bridge gateway too.** Add a drop-in `/etc/postgresql/16/main/conf.d/10-app-backend.conf`:

```
listen_addresses = 'localhost,172.30.0.1'
```

   The address must exist before PostgreSQL starts, so create the Docker network first (`docker network create` with the
   same subnet/gateway, or `docker compose ... up --no-start`), and note that a reboot orders Docker before PostgreSQL only if
   the network is created at Docker start; verify in Step 7. Alternative that avoids the ordering problem: `listen_addresses = '*'`
   with HBA and the firewall doing all the restriction. Prefer the explicit address; fall back to `'*'` if Step 7 shows a race.

3. **HBA line**, appended to `/etc/postgresql/16/main/pg_hba.conf` before the default rules:

```
host    ai_pin_db,agent_rehearsal    appuser    172.30.0.0/24    scram-sha-256
```

4. **Firewall**: insert before the final REJECT in INPUT and persist to `/etc/iptables/rules.v4`:

```sh
sudo iptables -I INPUT 5 -s 172.30.0.0/24 -d 172.30.0.1 -p tcp --dport 5432 -j ACCEPT
sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
```

5. **Password for `appuser`.** Generate on the VM and write it straight into the private env file, never echoed:

```sh
sudo install -d -m 0700 -o ubuntu /home/ubuntu/app-backend-config
PW=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER ROLE appuser WITH PASSWORD '$PW';" >/dev/null
(umask 077; printf 'DB_PASSWORD=%s\n' "$PW" >> /home/ubuntu/app-backend-config/backend.env); unset PW
```

   The old Docker stack does not use `appuser`, so changing this password affects nothing that is running.

6. `sudo systemctl reload postgresql` for HBA; `restart` for `listen_addresses`. A restart drops the idle psql session seen at
   inspection; nothing else connects to host PostgreSQL today.

7. **Verify from a throwaway container on the new network**, with password prompt, TLS optional on this private path:

```sh
docker run --rm -it --network app-backend postgres:16 psql "host=172.30.0.1 dbname=agent_rehearsal user=appuser sslmode=prefer" -c 'select current_user, inet_server_addr()'
```

   Also verify a connection from the default bridge or from the public IP is refused.

**Done when:** the throwaway container connects with scram; loopback admin access still works; rules survive `iptables-restore`.

## Step 4 — Rehearsal database and restore procedure

**Status: done 2026-09-11 UTC.** `agent_rehearsal` exists, owned by `appuser`, all 11 tables owned by `appuser`, row counts
identical to `ai_pin_db` (7/0/7/2/1/11/6/1/3/5/3). Dump/restore round trip therefore validated at the same time.

**Deviation:** `CREATE DATABASE ... TEMPLATE ai_pin_db` failed because the `pg_cron` launcher (`cron.database_name = ai_pin_db`)
holds a permanent session on the source database. Used dump/restore instead:

```sh
sudo -u postgres pg_dump --format=custom --file=/tmp/x.dump ai_pin_db        # kept as /home/ubuntu/db-backups/ai_pin_db-20260911T050002Z.dump (+ .sha256, 0600)
pg_restore --list x.dump | grep -vE 'EXTENSION - pg_cron|COMMENT - EXTENSION pg_cron| cron ' > reviewed.list   # pg_cron can only live in cron.database_name
sudo -u postgres psql -c "CREATE DATABASE agent_rehearsal OWNER appuser;"
sudo -u postgres pg_restore --dbname=agent_rehearsal --no-owner --no-privileges --role=appuser --exit-on-error --single-transaction --use-list=reviewed.list x.dump
```

Excluded from the restore: the `pg_cron` extension, its comment, the empty `cron.job`/`cron.job_run_details` data and two cron sequences.
Nothing in `public` was excluded. To re-clone later, `DROP DATABASE agent_rehearsal` (stop the staging app first) and repeat.
The same list-filter is needed for any future restore of `ai_pin_db` into a differently named database.

The template approach, kept for reference, only works if pg_cron is disabled or pointed elsewhere first:

```sh
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE agent_rehearsal WITH TEMPLATE ai_pin_db OWNER appuser;"
```

`CREATE DATABASE ... TEMPLATE` needs no other sessions on `ai_pin_db`; there are none today. Re-clone (drop and recreate
`agent_rehearsal` only) whenever staging needs a clean slate. `pg_cron` is a shared extension; the clone carries its empty tables.

Separately, validate a repeatable **dump → restore** procedure once, because it is also the backup restore test (Step 6):

```sh
sudo -u postgres pg_dump --format=custom --file=/home/ubuntu/db-backups/ai_pin_db-$(date -u +%Y%m%dT%H%M%SZ).dump ai_pin_db
sudo -u postgres createdb --owner=appuser restore_test
sudo -u postgres pg_restore --dbname=restore_test --no-owner --no-privileges --role=appuser --exit-on-error --single-transaction <dumpfile>
# row_counts.sql on restore_test vs ai_pin_db, then: sudo -u postgres dropdb restore_test
```

Ownership note: `tasks` is owned by `appuser`, the other tables by `postgres` (restored as `postgres` originally). The
`--role=appuser` restore normalizes that in copies; for `ai_pin_db` itself, `REASSIGN OWNED BY postgres TO appuser` inside that
database is a one-line optional cleanup so the app can run future DDL. Not required for this migration.

**Done when:** `agent_rehearsal` exists and a dump/restore round trip matches row counts.

## Step 5 — Test the OCI app against the rehearsal copy

**Status: connectivity and reads done 2026-09-11 UTC; behavior tests are app plan Step 4.** From inside the packaged app
container: `readiness.py --database` passed; app startup logged `agents table ensured` against `agent_rehearsal`;
`GET /api/agents` and `GET /tasks/{user_id}` returned 200 with data. The app opens a connection per query (none persistent).
App plan Step 4 then ran create/read/update/delete on tasks, messages and the agent registry plus two voice sessions against
`agent_rehearsal`; all passed and the test rows were removed (counts match `ai_pin_db` again).

Settings the app container needs (in the private env file, not here):

| Setting | Value |
|---|---|
| `DB_HOST` | `172.30.0.1` |
| `DB_PORT` | `5432` |
| `DB_NAME` | `agent_rehearsal` for staging; `ai_pin_db` (or `ai_pin_db_final`) at cutover |
| `DB_USER` | `appuser` |
| `DB_PASSWORD` | From Step 3 |

`PGSSLMODE`/`PGSSLROOTCERT` are not needed on the private bridge; the snakeoil certificate stays. Start with
`readiness.py --database`, then real CRUD through the API; app startup runs `ensure_agents_table`, which is a no-op
against the existing `agents` table.

## Step 6 — Backups

**Status: pending.** WAL archiving is off and no backup timer exists. One manual custom-format dump now exists at
`/home/ubuntu/db-backups/ai_pin_db-20260911T050002Z.dump` with a `.sha256`, and its restore was exercised in Step 4; it is still on the same boot disk.

1. Create an OCI Object Storage bucket (private) in `us-sanjose-1`, e.g. `ai-pin-db-backups`, with a lifecycle rule (30 days).
2. Give the VM access without long-lived keys: an OCI **dynamic group** for the instance + a policy allowing
   `manage objects` on that bucket; install the OCI CLI on the VM and use instance-principal auth.
3. systemd timer (daily 09:00 UTC) running: `pg_dump -Fc ai_pin_db` to `/home/ubuntu/db-backups/`, sha256, `oci os object put`,
   prune local copies older than 7 days. Log to journald.
4. Restore test: download yesterday's object, restore into `restore_test` (Step 4 procedure), compare counts, drop.

Point-in-time recovery (pgBackRest + WAL) is a later upgrade if daily granularity is not enough.

**Done when:** one scheduled backup exists in the bucket and was restored successfully elsewhere.

## Step 7 — Reboot verification

**Status: pending.** After Steps 3–4 and before cutover, `sudo reboot` during a quiet window and confirm:

- host PostgreSQL active, listening on `127.0.0.1` and `172.30.0.1` (see the ordering caveat in Step 3.2);
- the old `aipin` stack and the new app project both restarted (`unless-stopped`);
- the app's `readiness.py --database` passes again; iptables rules present.

## Step 8 — Cutover

**Status: pending; executed in [app plan Step 5](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md#5-cut-the-public-endpoint-over-to-the-new-app-on-ai_pin_db).**

**Done 2026-09-11 UTC:** pre-cutover dump `ai_pin_db-precutover-20260911T053014Z.dump` taken; `DB_NAME` flipped to `ai_pin_db`;
app readiness and startup (`ensure_agents_table`, a no-op) verified against it; public endpoint switched. `ai_pin_db` on this VM is
now the system of record. `agent_rehearsal` is kept for now; drop it after the observation window. There is no write fencing on Azure because the
Azure app is not being used as the source at cutover (see Step 2); once the OCI app writes to `ai_pin_db`, OCI is the system of record.

## Step 9 — Observe, then retire Azure PostgreSQL

**Status: pending.**

After the app plan's observation window and one successful scheduled backup: take a final `pg_dump` of the Azure server from
the Mac into private storage (even if Step 2 chose to abandon its newer rows, it is the cheap insurance), then delete
PostgreSQL Flexible Server `ai-pin-server`. App Service, Function App, Service Bus and IoT Hub are handled by the app plan's Step 7.

## If something goes wrong

**Before cutover:** nothing on Azure changed, so there is nothing to undo. Drop and re-clone `agent_rehearsal` if
staging corrupted it.

**After cutover:** the app-plan rollback (Caddy upstream back to the old stack) returns the device to the old OCI stack and
its Docker `aipin` database, not to Azure. If `ai_pin_db` itself is damaged, restore the pre-cutover dump into a new
database name and point the app at it. Azure PostgreSQL is a stale snapshot from the moment the OCI app first writes.

## Which app should I use to inspect the database?

Try **TablePlus** for everyday browsing and editing on your Mac. Keep using pgAdmin if its administration tools
or familiarity suit you; neither choice changes this migration. Use the CLI commands above for repeatable copies.

| Tool | Why choose it? |
|---|---|
| TablePlus | My first choice for a focused Mac workflow; built-in SSH and Safe Mode. [Product/pricing](https://tableplus.com/pricing), [Safe Mode](https://docs.tableplus.com/gui-tools/code-review-and-safemode/safe-mode) |
| Beekeeper Studio Community | Free alternative to try for a simple SQL/table interface. [Community edition](https://www.beekeeperstudio.io/community/) |
| DBeaver Community | Free and useful when you work with several database engines. [DBeaver](https://dbeaver.io/) |
| pgAdmin | Still useful for PostgreSQL administration and backup/restore tools. [Documentation](https://www.pgadmin.org/docs/pgadmin4/latest/backup_and_restore.html) |

This comparison was researched on 2026-09-09, not tested hands-on. Save clearly named connections for
`Azure — old`, `OCI — rehearsal` and `OCI — live`. Connect through SSH for admin access, and verify the client's
TLS hostname settings work with the tunnel. Test edits on a disposable rehearsal row before editing live data.

## Work deliberately left until after this migration

Mobile-app repair/release, profile endpoints, Firebase token verification, API/WebSocket authentication,
registry ownership rules, a broader DB-role redesign, TTS changes, and queue/broker migration are separate work.
No migration step depends on implementing them.

Next database action: **Step 6 (off-VM backups) and Step 7 (reboot check)**, in parallel with app plan Step 4.
Bridge route and rehearsal clone are done; cutover follows the app plan.


# Verified PostgreSQL inspection — 2026-09-10

# OCI VM and PostgreSQL inspection

Read-only inspection, 2026-09-10. OCI API and SSH access both succeeded. No services, configuration,
application records or cloud resources were changed. SQL inspection used read-only transactions.

## Verdict for `ai_pin_db`

**The initial database copy is in good shape. The full PostgreSQL-first migration is not complete yet.**
The user confirmed that host `ai_pin_db` is the database to keep; the Docker stack and other previous attempts
are stale. No cleanup was performed. Keep the host data directory and the Azure dump out of future cleanup.

On 2026-09-10, we inspected `/home/ubuntu/ai_pin_db_dump_2026-09-01.sql` on the VM without printing its records.
The file is 19,139 bytes, has a dump-complete marker, and reports source PostgreSQL **16.14** and pg_dump
**17.9 (Homebrew)**. Its filename suggests September 1; this does not establish the current state of Azure.

| Check | Result |
|---|---|
| Application data | All 11 public tables match the dump's COPY rows, including values and duplicate counts, ignoring row order |
| Public schema | All 25 extracted CREATE TABLE, ADD CONSTRAINT and CREATE INDEX definitions match after whitespace normalization |
| Constraints | All 14 public constraints report validated |
| Runtime access | `appuser` has public schema USAGE and SELECT/INSERT/UPDATE/DELETE on all 11 tables |
| Application sequences | No public sequences exist, so no public sequence grants are missing |
| Ownership | Database and `tasks` owned by `appuser`; other public tables owned by `postgres` |
| Scheduler | Both dumped cron tables and current cron tables are empty; no jobs need migrating from this snapshot |
| Azure connectivity | Incomplete: localhost binding/HBA and OCI rules currently prevent direct Azure access |
| Server certificate | Default snakeoil certificate; planned hostname verification is not configured |
| Recovery | Automated off-VM backups and a successful restore test remain unverified |
| Final cutover | Current Azure data, writer shutdown, app connection and worker resume remain unverified |

The data comparison used read-only COPY TO operations on the VM and compared the same columns as each dump
COPY section. Application values were never returned to the client. The schema comparison is scoped to the
listed object types; it is not a complete equivalence proof for every database setting, function or privilege.
Password login and real application writes were not exercised. No live Azure comparison was performed.

### How your setup instructions compare with the plan

1. **Starting PostgreSQL and creating a database/user were appropriate.** Keep `ai_pin_db` and `appuser`;
   the earlier plan's example names are not requirements. PostgreSQL 16.15 keeps the source's major version.
2. **The dump includes data, despite the example name `schema_dump.sql`.** Your actual file contains no DROP,
   OWNER TO or GRANT statements, so it differs from the example `--clean` command (or was edited afterward).
3. **Database grants alone would not have been sufficient.** The actual server has the required table DML
   grants already. Restoring as `postgres` explains the mixed ownership; runtime access is currently present.
   Later DDL changes require the appropriate owner/admin; future new tables need appropriate grants.
   See [PostgreSQL GRANT](https://www.postgresql.org/docs/16/sql-grant.html).
4. **The restore procedure needs error handling for future copies.** Plain `psql -f` can continue after SQL
   errors. Use `-X -v ON_ERROR_STOP=1 --single-transaction` for a reviewed, compatible SQL restore to an empty
   trial database. The actual dump requests `azure` and `pgaadauth`, which are absent on this host; those
   Azure-specific commands require review/removal rather than blind replay. We did not recover an original
   restore log, so cannot establish which errors occurred. See [psql restore options](https://www.postgresql.org/docs/16/app-psql.html).
5. **Localhost is appropriate for SSH-based administration**, but the planned Azure backend/worker need a
   restricted network path plus verified TLS. No need to open the database broadly to the internet.
6. **Matching this dump is an initial copy milestone.** Check current Azure before treating it as final data.
   Do not rerun the dump over `ai_pin_db`; preserve it and use a separate empty database for any fresh trial.

## Connection that works

| Item | Verified value |
|---|---|
| VM | `ai-assistant-server` |
| Region | `us-sanjose-1` |
| Public IPv4 | `146.235.229.232` |
| Private IPv4 | `10.0.0.159` |
| Shape | A1 Flex, 2 OCPUs / 12 GB RAM |
| OS | Ubuntu 24.04.4 LTS, ARM64 |
| SSH user | `ubuntu` |
| SSH key path on the Mac | `/Users/rishi/projects/keys/agent/id_ed25519` |

```sh
ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232
```

The existing SSH host key matched the Mac's known-hosts file. The Ubuntu account has working passwordless
sudo, so local PostgreSQL inspection did not require requesting or exposing a database password.

## Important: there are two separate PostgreSQL installations

| | Ubuntu host installation | Docker installation |
|---|---|---|
| Server version | PostgreSQL 16.15, Ubuntu package | PostgreSQL 16.15, Debian package inside `postgres:16` |
| Service/container | `postgresql@16-main` | `aipin-postgres` |
| Application database | `ai_pin_db` | `aipin` |
| Database size at inspection | 8,412,183 bytes, about 8 MiB | 8,240,151 bytes, about 7.9 MiB |
| Application role | `appuser`, not superuser | `aipin`, superuser |
| Storage | `/var/lib/postgresql/16/main` | Named Docker volume `aipin_postgres_data` |
| Listening | Host localhost port 5432 | Container port 5432; not published on the host |
| TLS | On, default snakeoil certificate | Off inside Docker network |
| Extensions | `plpgsql`, `pg_cron` 1.6 | `plpgsql` |
| WAL archiving | Off | Off |

**The running Docker app and worker are configured for the Docker database**, with `DB_HOST=postgres`,
`DB_PORT=5432`, `DB_NAME=aipin`, `DB_USER=aipin`, on `aipin_default`. A database connection from
`172.18.0.4` was present in the Docker database. These observations do not prove that Azure has cut over or
that the device is using the OCI application.

The host database also has data. Its `pg_cron` scheduler is loaded but has **zero configured jobs** and
zero recorded runs. It had a local idle psql session. Do not treat this database as an empty target.

Host PostgreSQL accepts only local connections under the inspected HBA configuration. Azure cannot connect
directly to it over the public IP as configured. The Docker DB is reachable by its container peers but has no
published host port. An admin SSH tunnel to host port 5432 reaches **`ai_pin_db`'s server**, not Docker PostgreSQL.
This distinction matters when configuring pgAdmin or TablePlus.

## Tables and exact counts

Counts matched between the two databases for all eleven shared public tables at inspection time:

| Table | Host rows | Docker rows |
|---|---:|---:|
| agent_registry | 7 | 7 |
| agent_tasks | 0 | 0 |
| agents | 7 | 7 |
| chat_members | 2 | 2 |
| chats | 1 | 1 |
| messages | 11 | 11 |
| pending_text_message_jobs | 6 | 6 |
| relationships | 1 | 1 |
| sessions | 3 | 3 |
| tasks | 5 | 5 |
| users | 3 | 3 |

Docker additionally has `public.jobs`, currently **0 rows**. The host database does not have that table.
No message contents, user profiles or other application rows were printed. Matching counts do not establish
matching values, schema equivalence, or agreement with the current Azure database.

## Existing application stack

All five containers were running, with restart policy `unless-stopped`:

| Container | Image | Observed status / ports |
|---|---|---|
| `aipin-app` | `aipin-agent:latest` | Healthy according to Docker; internal port 8000 |
| `aipin-worker` | `aipin-agent:latest` | Running |
| `aipin-postgres` | `postgres:16` | Healthy according to Docker; internal port 5432 |
| `aipin-caddy` | `caddy:2` | Host ports 80 and 443 published |
| `aipin-mosquitto` | `eclipse-mosquitto:2` | Host port 8883 published |

Compose labels identify `/home/ubuntu/agent/docker-compose.yml` as the configuration file, project `aipin`.
The app has bind mounts for `agent_directory` and `/home/ubuntu/agent/data`. PostgreSQL, Caddy and Mosquitto
have persistent Docker volumes. Container environments were filtered to non-secret database fields;
passwords, API keys and complete environment/configuration files were not displayed.

The running stack is ahead of this checkout's original Azure-only deployment assumptions. Do not redeploy
the local compose file over this server without first comparing the intended and deployed configurations.

## Capacity and recovery observations

- Root disk: about 45 GB usable, 13 GB used, 32 GB available; 29% used.
- Memory: about 1.8 GiB used and 9.8 GiB available at inspection; load average near zero. This is a snapshot,
  not a concurrency or voice-latency benchmark.
- The VM has one boot disk and no separate mounted data disk. Both PostgreSQL installations ultimately store
  data on that boot disk; Docker persistence is not an off-VM backup.
- WAL archiving is disabled in both databases. No pgBackRest or Barman binary was found on the host PATH.
- The inspected systemd timer list had no obvious PostgreSQL backup timer. `dpkg-db-backup` backs up package
  metadata, not the application database. Cron scripts, external backup destinations and restore success
  have not been fully inspected, so **backup coverage remains unverified**.
- UFW is not installed. This does not imply that there is no firewall: OCI network rules and other Linux
  firewall mechanisms may still apply. The earlier OCI network inventory is a separate snapshot.

## What to do next

1. Preserve `ai_pin_db` and its dump; inventory stale resources before any separately authorized cleanup.
2. Verify whether Azure still has active writers and compare its current schema/data with this snapshot.
3. Finish the stable address, restricted Azure network access and hostname-verified TLS in the migration guide.
4. Verify off-VM backups and restore one into a separate empty database.
5. Test the Azure backend/worker against a trial copy, then perform the final stopped-writer cutover if needed.

Application auth and mobile changes remain deferred. No server settings or application data were changed.


# Read-only inspection SQL

Create these two files in your private working folder outside Git before running the inspection commands.
They replace the separate repository helper files. Neither script prints application row values.

## inventory.sql

```sh
cat > inventory.sql <<'SQL'
-- Run with psql -X -v ON_ERROR_STOP=1 "service=azure_source" -f inventory.sql
-- Metadata only: no passwords, query text, function bodies, or application rows.
-- Store output outside Git; metadata can still reveal operational details.
\set ON_ERROR_STOP on
\pset pager off
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '30s';
SET LOCAL lock_timeout = '5s';

SELECT current_database() AS database_name, current_user AS inspection_role,
       current_setting('server_version') AS server_version,
       current_setting('server_encoding') AS encoding,
       current_setting('TimeZone') AS session_timezone,
       pg_database_size(current_database()) AS database_bytes;

SELECT datname, pg_encoding_to_char(encoding) AS encoding, datcollate, datctype,
       datlocprovider, daticulocale, datcollversion
FROM pg_database WHERE datname = current_database();

SELECT nspname AS schema_name, pg_get_userbyid(nspowner) AS owner
FROM pg_namespace
WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
ORDER BY 1;

SELECT extname, extversion, extnamespace::regnamespace AS schema_name
FROM pg_extension ORDER BY 1;

SELECT n.nspname AS schema_name, c.relname, c.relkind,
       pg_get_userbyid(c.relowner) AS owner,
       pg_total_relation_size(c.oid) AS total_bytes,
       c.reltuples::bigint AS estimated_rows, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r', 'p', 'm', 'v', 'S', 'f')
ORDER BY 1, 2;

SELECT table_schema, table_name, ordinal_position, column_name, data_type,
       udt_name, is_nullable, is_identity, is_generated
FROM information_schema.columns
WHERE table_schema NOT LIKE 'pg_%' AND table_schema <> 'information_schema'
ORDER BY 1, 2, 3;

SELECT n.nspname AS schema_name, c.relname, con.conname, con.contype,
       con.convalidated, con.condeferrable, con.condeferred
FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 3;

SELECT n.nspname AS schema_name, t.relname AS table_name,
       i.relname AS index_name, x.indisvalid, x.indisready, x.indisunique
FROM pg_index x JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
ORDER BY 1, 2, 3;

SELECT schemaname, sequencename, sequenceowner, data_type, start_value,
       min_value, max_value, increment_by, cycle, cache_size, last_value
FROM pg_sequences
WHERE schemaname NOT LIKE 'pg_%' AND schemaname <> 'information_schema'
ORDER BY 1, 2;

SELECT n.nspname AS schema_name, p.proname, p.prokind,
       pg_get_userbyid(p.proowner) AS owner, p.prosecdef AS security_definer
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
ORDER BY 1, 2;

SELECT schemaname, tablename, policyname, permissive, roles, cmd
FROM pg_policies
WHERE schemaname NOT LIKE 'pg_%' AND schemaname <> 'information_schema'
ORDER BY 1, 2, 3;

SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
       rolcanlogin, rolreplication, rolbypassrls
FROM pg_roles ORDER BY 1;

SELECT table_schema, table_name, grantee, privilege_type
FROM information_schema.table_privileges
WHERE table_schema NOT LIKE 'pg_%' AND table_schema <> 'information_schema'
ORDER BY 1, 2, 3, 4;

SELECT count(*) AS large_object_count FROM pg_largeobject_metadata;

-- Visibility depends on the inspection role. No query text is selected.
SELECT usename, application_name, client_addr, state, count(*) AS connections
FROM pg_stat_activity WHERE datname = current_database()
GROUP BY 1, 2, 3, 4 ORDER BY 1, 2;

COMMIT;
SQL
```

## row_counts.sql

```sh
cat > row_counts.sql <<'SQL'
-- Exact counts for user ordinary/partitioned tables. Can be expensive on large DBs.
-- RLS/permissions may hide rows: use an authorized migration reader and verify scope.
-- Partition children are counted separately as well as through their parent.
-- Execute against a write-fenced source/target for a migration comparison.
\set ON_ERROR_STOP on
\pset pager off
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '60s';
SET LOCAL lock_timeout = '5s';
-- Fail rather than silently count an RLS-filtered subset.
SET LOCAL row_security = off;

SELECT format(
  'SELECT %L AS table_name, count(*) AS exact_rows FROM %I.%I;',
  n.nspname || '.' || c.relname, n.nspname, c.relname
)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  AND c.relkind IN ('r', 'p')
ORDER BY n.nspname, c.relname
\gexec

COMMIT;
SQL
```

