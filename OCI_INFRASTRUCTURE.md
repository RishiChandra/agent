# OCI infrastructure

The AI-pin backend runs entirely on a single **Oracle Cloud Always Free** VM. This replaced an Azure stack
(App Service + PostgreSQL Flexible Server + Service Bus + Function App + IoT Hub) in September 2026; Azure has since
been retired. This file describes the machine, networking, Docker layout, secrets, and release conventions shared by
every component. Per-component detail lives in the component docs listed at the bottom.

## The VM

| | |
|---|---|
| Name | `ai-assistant-server` |
| Shape | Ampere `A1.Flex`, **ARM64**, 2 OCPU / 12 GB RAM (Always Free) |
| Region / AD | `us-sanjose-1` |
| OS | Ubuntu 24.04 LTS (aarch64) |
| Boot disk | ~45 GB (one boot volume; no separate data disk). ~45% used as of 2026-09-20 |
| Public IPv4 | `146.235.229.232` (**ephemeral** — see hardening TODOs) |
| Public IPv6 | `2603:c024:c020:3700:0:537e:9221:8587` (dual-stack) |
| Hostnames | sslip.io names resolve to the IPs: `146-235-229-232.sslip.io` (A) and `2603-c024-c020-3700-0-537e-9221-8587.sslip.io` (AAAA). No real domain yet |

**SSH access:** `ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232`. The `ubuntu` account has
passwordless sudo. Keep the private key off Git.

**Why the IPv6 name matters:** the physical device is on IPv6-only LTE (T-Mobile PPP hands out no IPv4), so anything the
device dials — the app WebSocket and the MQTT broker — must use the **v6** sslip name, and the TLS cert presented must be
valid for it. See [DATABASE](DATABASE.md) is v4-internal only; [BACKEND](BACKEND.md) and [SCHEDULER](SCHEDULER.md) carry the
v6 specifics.

## Networking & firewall

- **Caddy** terminates TLS on public `:80`/`:443` (Let's Encrypt, auto-renew) and reverse-proxies to the app. It holds
  certs for **both** sslip names.
- **Mosquitto** listens on public `:8883` (device MQTT, TLS) and internal `:1883` (plaintext, worker only).
- **Host firewall:** iptables (v4) + ip6tables (v6), both allowing inbound TCP `22, 80, 443, 8883` and rejecting the rest.
  v4 rules persist in `/etc/iptables/rules.v4`. The OCI security list must allow the same ingress.
- **Docker networks:**
  - `app-backend` — external bridge, subnet `172.30.0.0/24`, gateway `172.30.0.1`. The app/worker reach **host
    PostgreSQL** at `172.30.0.1:5432` over this (see [DATABASE](DATABASE.md)). Created by hand, referenced `external: true`
    in compose so it survives `compose down`.
  - `aipin_default` — Caddy's network; the app joins it with alias `app-main` so the Caddyfile can proxy `app-main:8000`,
    and the worker joins it to reach `mosquitto:1883`.

## Docker layout

Everything runs under Docker. Current containers:

| Container | Image | Role |
|---|---|---|
| `app-backend-app-1` | `codex-app-backend:step4-94fa561471da` | FastAPI app + static site ([BACKEND](BACKEND.md)) |
| `app-backend-worker-1` | same image | Reminder/job worker ([SCHEDULER](SCHEDULER.md)) |
| `aipin-caddy` | `caddy:2` | TLS front door |
| `aipin-mosquitto` | `eclipse-mosquitto:2` | Device MQTT broker |

The app and worker are one compose project, **`app-backend`**, defined by `docker-compose.oci.yml` (in the repo). Caddy and
Mosquitto are **not** in that project — they are the surviving services from the original `oracle-deploy` stack and are
configured under `/home/ubuntu/agent/deploy/` (`caddy/Caddyfile`, `mosquitto/`), with a root cron running
`deploy/mosquitto/certsync.sh` every 10 min. Moving Caddy and Mosquitto into the `app-backend` compose project so
`/home/ubuntu/agent` can be retired is a low-priority follow-up.

> **Never run `docker compose up` in `/home/ubuntu/agent`.** That is the deleted stale stack's compose file; it would try to
> recreate the old app/worker/Postgres containers. The live project is `app-backend` with `docker-compose.oci.yml`.

### On-VM directories

| Path | What |
|---|---|
| `/home/ubuntu/releases/app-backend-step2-20260911/` | Build context: repo files synced here, `docker build` run here, `CURRENT_IMAGE.env` records the live tag |
| `/home/ubuntu/app-backend-config/backend.env` | App/worker private env (secrets) — `0600` |
| `/home/ubuntu/agent/deploy/.env` | Caddy/Mosquitto/MQTT env (secrets) — gitignored |
| `/home/ubuntu/app-backend-assets/models/` | Vosk + Piper models, mounted read-only into the app |
| `/home/ubuntu/db-backups/` | PostgreSQL dumps (on the boot disk only — see hardening TODOs) |
| `/home/ubuntu/deployment-backups/` | One-time recovery archive from the original cutover |

## Secrets & credentials

No secrets in Git. They live in two private files on the VM:

- **App/worker:** `/home/ubuntu/app-backend-config/backend.env` — `DB_*`, `GOOGLE_API_KEY`, Gemini/tuning vars, `MQTT_*`.
- **Caddy/Mosquitto/MQTT:** `/home/ubuntu/agent/deploy/.env` — `SITE_HOST_*`, `MQTT_TLS_HOST`, `MQTT_USERNAME`/`MQTT_PASSWORD`
  (backend, full-topic), `MQTT_DEVICE_USERNAME`/`MQTT_DEVICE_PASSWORD` (device, ACL-scoped to `aipin/esp32s3/#`).

Read a value without printing it into a shared transcript, e.g.:

```sh
ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232 'grep ^MQTT_DEVICE_PASSWORD= /home/ubuntu/agent/deploy/.env'
```

The auto-mode permission classifier blocks edits to these secrets files; ask a human to run any `sed`/append against them.

## Build & release conventions

Images are built **on the VM** (ARM64) from the synced repo:

```sh
cd /home/ubuntu/releases/app-backend-step2-20260911
TREE=$(find Dockerfile requirements.txt requirements-oci.lock app agent_directory listener deploy -type f \
  ! -name '*.env*' ! -name '*.key' ! -path '*__pycache__*' ! -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64)
DOCKER_BUILDKIT=0 docker build --build-arg SOURCE_REVISION=<git-sha> --build-arg SOURCE_TREE_SHA256=$TREE \
  -t codex-app-backend:step<N>-${TREE:0:12} .
```

The image carries OCI labels `org.opencontainers.image.revision` and `io.aipin.source-tree-sha256`. The live tag is recorded
in `CURRENT_IMAGE.env`. Deploy/rollback is `docker compose -p app-backend -f docker-compose.oci.yml up -d` with `APP_IMAGE`
set (details in [BACKEND](BACKEND.md)).

> **Image provenance caveat:** the running image's revision label ends in `-worktree` because it was built from an
> uncommitted worktree during the migration. After the migration branch merges to `main`, rebuild from the clean `main`
> commit so the label is a real SHA, then redeploy and record the digest.

## Hardening — pending before this is production-safe

These are the gap between "working" (it is) and "safe to depend on". None blocks day-to-day use.

1. **Off-VM backups.** Every copy of `ai_pin_db` is on the VM's single boot disk (`/home/ubuntu/db-backups/`), so a lost or
   reclaimed VM loses the data. Set up scheduled logical dumps to **OCI Object Storage** using an instance-principal
   (dynamic group + policy; the `oci` CLI is not yet installed on the VM), a daily systemd timer running `pg_dump -Fc`,
   upload, prune local copies, and one verified restore test. Interim stopgap: periodically `scp` a dump to the Mac.
2. **Reboot check.** Host PostgreSQL binds `172.30.0.1`, which only exists once Docker has created the `app-backend` bridge.
   A systemd drop-in orders `postgresql@16-main` `After=docker.service`, but it has only been validated on a live restart,
   not a full reboot. Do one planned `sudo reboot` in a quiet window and confirm PostgreSQL comes back listening on both
   `127.0.0.1` and `172.30.0.1`, the app/worker reconnect, and `readiness.py --database` passes. Failure mode is loud (app
   healthcheck fails) and the fix is one command: `sudo systemctl restart postgresql@16-main`.
3. **Stable address.** The public IP is **ephemeral** — stop/start the VM and it changes. The sslip hostnames (used by the
   device's WebSocket URL and the MQTT broker cert) change with it, which forces a device reflash. Reserve an OCI public IP
   (note: OCI cannot convert an ephemeral IP in place — reserving yields a **new** address) or, better, put a real DNS name
   in front. Then update `SITE_HOST_*` / `MQTT_TLS_HOST` in `deploy/.env`, let Caddy re-issue, `certsync.sh` copies the MQTT
   cert, and reflash the device to the new host. Do this **before** the pin is in daily use, since it is coupled to a reflash.

## Component docs

- [BACKEND.md](BACKEND.md) — FastAPI app + Caddy TLS front door + static site.
- [DATABASE.md](DATABASE.md) — host PostgreSQL `ai_pin_db`, the container→host bridge route, and how to connect.
- [SCHEDULER.md](SCHEDULER.md) — reminders: jobs table + worker + Mosquitto + the device firmware wake path.
- [WEBSITE.md](WEBSITE.md) — the Agent Registry site served by the app.
