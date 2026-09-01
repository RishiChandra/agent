# Deploying the agent backend to the Oracle Cloud VM

Everything runs on one Always-Free ARM VM (Ubuntu 24.04, `ubuntu@146.235.229.232`,
repo at `/home/ubuntu/agent`) under docker compose:

| service     | image                | role                                                                  | public |
|-------------|----------------------|-----------------------------------------------------------------------|--------|
| `caddy`     | `caddy:2`            | :80/:443, Let's Encrypt for both sslip.io hosts, reverse proxy to app | yes    |
| `app`       | built from `Dockerfile` | FastAPI/uvicorn on :8000 — HTTP API, `/ws/...`, `/ws/developer/...`, static site at `/` | via caddy |
| `worker`    | same image           | `python listener/worker.py` — replaces the Azure Function + Service Bus | no     |
| `postgres`  | `postgres:16`        | database, named volume `aipin_postgres_data`                          | no     |
| `mosquitto` | `eclipse-mosquitto:2`| MQTT for device commands, TLS on :8883 with caddy's certificate; plaintext :1883 for the worker (compose-internal) | :8883 only |

Hostnames until there is a real domain (sslip.io resolves them to the VM):

- IPv4: `https://146-235-229-232.sslip.io`
- IPv6: `https://2603-c024-c020-3700-0-537e-9221-8587.sslip.io`

Caddy issues **one certificate per hostname** (it does not combine names into a
multi-SAN cert). Mosquitto presents the certificate for `MQTT_TLS_HOST`
(default: the IPv4 host), so the ESP32 must dial that exact name for MQTT.

## Files

```
Dockerfile, docker-compose.yml, .dockerignore   image + topology (repo root)
deploy/.env.example        every variable, documented — copy to deploy/.env
deploy/.env                secrets, gitignored, synced to the VM by deploy.sh
deploy/deploy.sh           Mac: rsync + run VM steps    |  VM: --remote
deploy/download_models.sh  Vosk model + Piper voice into ./data (idempotent)
deploy/restore_db.sh       restore a pg_dump .sql into the postgres container
deploy/caddy/Caddyfile     edge config
deploy/mosquitto/mosquitto.conf, acl.template, certsync.sh
deploy/mosquitto/passwd, acl, certs/   generated on the VM, gitignored
```

## One-time setup

1. **OCI network**: in the VCN security list (or NSG) for the VM allow ingress
   TCP 80, 443, 8883 from `0.0.0.0/0` **and** `::/0`. `deploy.sh` opens the same
   ports in the host's iptables/ip6tables (Oracle's Ubuntu image rejects
   everything but ssh by default).
2. **Secrets**: `cp deploy/.env.example deploy/.env` and fill in
   `GOOGLE_API_KEY`, `DB_PASSWORD`, `MQTT_PASSWORD`, `MQTT_DEVICE_PASSWORD`
   (`openssl rand -base64 24` makes fine passwords). Nothing else is required.
3. **SSH**: `ssh ubuntu@146.235.229.232` must work with your key.

## Deploy (from the Mac)

```sh
deploy/deploy.sh
```

Re-run it after every change; it is idempotent. What it does:

1. rsyncs the repo (minus `.git`, `data/`, `mobile_app/`, generated mosquitto
   files) and then `deploy/.env` to the VM.
2. On the VM: opens the firewall ports, runs `download_models.sh`, generates
   `deploy/mosquitto/passwd` (hashed with `mosquitto_passwd` inside the broker
   image) and renders `deploy/mosquitto/acl` from `acl.template` + `.env`, `docker compose build`
   + `up -d`, installs the root cron `*/10 * * * * deploy/mosquitto/certsync.sh`,
   waits for Let's Encrypt to issue the MQTT host's certificate and copies it
   to mosquitto, prints `compose ps` and the `/healthz` result for both hosts.

The first run builds the image on the VM (a few minutes on the A1 core) and
Let's Encrypt issuance takes ~30 s per host. Mosquitto restarts in a loop
until the certificate files exist — that is expected on the very first deploy;
jobs that fall due meanwhile are retried by the worker without counting
against their attempt limit.

## Restore the database (once)

```sh
scp ~/Documents/Development/ai_pin_migration/ai_pin_db_dump_2026-09-01.sql ubuntu@146.235.229.232:
ssh ubuntu@146.235.229.232
cd ~/agent && deploy/restore_db.sh ~/ai_pin_db_dump_2026-09-01.sql
```

It drops and recreates schema `public` in `$DB_NAME`, then replays the dump in
one transaction, skipping the Azure-only pieces (`azure`, `pgaadauth`, `pg_cron`
extensions and `cron.*` data), and finally applies `deploy/sql/*.sql` (the
`jobs` queue table that replaced Service Bus). Re-running it just restores
again. `deploy.sh` also applies `deploy/sql/*.sql` on every deploy, so a fresh
database without a dump still gets that schema.

The dump's `public.agents.agent_url` names the old Azure WebSocket URL
(`wss://websocket-ai-pin-….azurewebsites.net/ws/{user_id}`); the orchestrator
dials it from inside the `app` container to bridge to the Kairos agent. The
script repoints it to `ws://app:8000/ws/{user_id}` — the compose-internal
name, because OCI's public IPv4 does not hairpin from the VM. To do it by hand:

```sql
UPDATE agents
   SET agent_url = regexp_replace(agent_url, '^wss?://[^/]+', 'ws://app:8000')
 WHERE agent_url LIKE '%azurewebsites.net/%';
```

## Verify

```sh
curl -4 https://146-235-229-232.sslip.io/healthz
curl -6 https://2603-c024-c020-3700-0-537e-9221-8587.sslip.io/healthz
# WebSocket (pip install websockets):
python -m websockets wss://146-235-229-232.sslip.io/ws/developer/test-user
# MQTT (brew install mosquitto): subscribe as the device
mosquitto_sub -h 146-235-229-232.sslip.io -p 8883 -u esp32s3 -P '<MQTT_DEVICE_PASSWORD>' -t 'aipin/esp32s3/cmd' -v
# ... and publish a retained command as the backend user from another shell
mosquitto_pub -h 146-235-229-232.sslip.io -p 8883 -u backend -P '<MQTT_PASSWORD>' -t 'aipin/esp32s3/cmd' -r -q 1 -m '{"command":"start_websocket","reason":"test"}'
```

Both `mosquitto_*` commands verify the Let's Encrypt chain against the system
CA store; no `--cafile` is needed. A `mosquitto_pub` by the device user to a
topic outside `aipin/esp32s3/` is silently dropped by the ACL.

## Day-to-day

Always pass the env file to compose (`deploy.sh` does):

```sh
cd ~/agent
docker compose --env-file deploy/.env ps
docker compose --env-file deploy/.env logs -f app worker
docker compose --env-file deploy/.env restart app
docker compose --env-file deploy/.env exec postgres psql -U aipin -d aipin
sudo deploy/mosquitto/certsync.sh          # force a cert sync; log in /var/log/aipin-certsync.log
docker kill -s HUP aipin-mosquitto         # reload passwd / acl / certs
```

- **Rotate MQTT passwords**: edit `deploy/.env` on the Mac, run `deploy/deploy.sh`
  (it regenerates `passwd`, re-renders `acl` and HUPs the broker).
- **Change hostnames / add a real domain**: edit `SITE_HOST_V4`/`SITE_HOST_V6`/
  `MQTT_TLS_HOST` in `deploy/.env`, point DNS at the VM, run `deploy/deploy.sh`.
  Caddy fetches the new certificates; `certsync.sh` follows `MQTT_TLS_HOST`.
- **Certificates**: caddy renews ~30 days before expiry; the cron copies a new
  cert to mosquitto within 10 minutes and sends SIGHUP. Nothing to do.
- **Backups**: `docker compose --env-file deploy/.env exec postgres pg_dump -U aipin aipin > backup.sql`.

## Device contract (unchanged wire protocol)

- WebSocket: `wss://<host>/ws/developer/{user_id}` — same messages as before;
  caddy passes the upgrade straight through to uvicorn.
- MQTT: host `MQTT_TLS_HOST`, port 8883, TLS with a public Let's Encrypt cert,
  username/password `MQTT_DEVICE_USERNAME`/`MQTT_DEVICE_PASSWORD`, subscribe to
  `{MQTT_COMMAND_TOPIC_PREFIX}/{DEVICE_ID}/cmd` (retained, QoS 1). Payloads are
  the same JSON the IoT Hub C2D path sent, e.g. `{"command":"start_websocket", ...}`.

## Troubleshooting

- `docker logs aipin-caddy` — ACME errors mean port 80/443 is not reachable
  from the internet (OCI security list) or the sslip name does not resolve.
- IPv6 not working: `ip -6 addr` must show the global address; docker publishes
  ports on the host's IPv6 addresses too (`ss -ltnp | grep 443` shows `*:443`).
- `docker logs aipin-mosquitto` says it cannot load the key: run
  `sudo deploy/mosquitto/certsync.sh`; the key must be owned by uid 1883 (the
  container's `mosquitto` user), which the script enforces.
- App unhealthy: `docker compose --env-file deploy/.env logs app` — a missing
  `GOOGLE_API_KEY` or `DB_*` value fails at import/startup.
- Worker logs `certificate verify failed: Hostname mismatch`: it is dialling the
  TLS port under the compose service name. Use the compose-internal plaintext
  listener (`MQTT_HOST=mosquitto`, `MQTT_PORT=1883`, the `.env.example`
  default); if you must use 8883, `MQTT_TLS_INSECURE=1` skips only the hostname
  check (the chain is still verified; this is also the automatic default
  whenever `MQTT_HOST` differs from `MQTT_TLS_HOST`).
- Models: `ls ~/agent/data` should show `vosk-model-small-en-us-0.15/` and
  `piper_voices/en_US-amy-medium.onnx{,.json}`; re-run `deploy/download_models.sh`.
