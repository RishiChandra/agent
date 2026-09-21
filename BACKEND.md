# Backend (app + TLS front door)

The application: a FastAPI/uvicorn server that provides the HTTP API, both WebSocket voice endpoints, and serves the static
[Agent Registry site](WEBSITE.md) at `/`. It runs as one container behind Caddy, which terminates TLS and reverse-proxies to
it. Infrastructure basics (VM, networks, secrets, build conventions) are in [OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

## What it does

- **HTTP API** (`app/routes/*.py`): tasks (`/tasks…`, `/enqueue-task`), messaging (`/messages`, `/messages/enqueue`), agent
  registry (`/api/agents…`, `/developer/register`, `/developer/unregister`), `/developer/ping/{user_id}`, `/healthz`. No
  auth; CORS `*` (v1).
- **Two WebSocket voice servers**, same wire protocol:
  - `/ws/developer/{user_id}` — the in-house orchestrator (Vosk STT → Gemini text agent + tools/agent-registry/bridge →
    Piper TTS, 24 kHz). **This is the v3 default** and the path the device uses.
  - `/ws/{user_id}` — legacy bridge straight to Gemini Live. Kept for A/B.
- **Static site** at `/` from `agent_directory/` (see [WEBSITE.md](WEBSITE.md)).
- **Startup** runs `ensure_agents_table()` (idempotent) and preloads Vosk/Piper/Silero.

Outbound dependencies: Google **Gemini** API (voice + text), host PostgreSQL, and — for reminders — the Mosquitto broker via
the worker (see [SCHEDULER.md](SCHEDULER.md)). Azure OpenAI env vars exist in the code but nothing imports them; leave unset.

## How it is packaged

Built from the repo on the VM (ARM64). Key files:

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage. `python:3.11-slim` pinned by digest (3.13 dropped `audioop`, which the audio path needs). Installs `libopus0`/`libatomic1`/`libgomp1`, a venv from `requirements.txt` constrained by `requirements-oci.lock`, NLTK `punkt_tab` offline, non-root uid 10001, an HTTP healthcheck, single uvicorn worker (`--workers 1` — session/bridge registries are process-local). Bakes `app/`, `agent_directory/`, `listener/`, `deploy/app_backend/` into the image |
| `.dockerignore` | Strict allowlist; excludes `.env*`, keys, `*.sql`, dumps, `data/`, `*.onnx` from the build context |
| `requirements-oci.lock` | ARM64-resolved constraints (regenerate on the VM if deps change) |
| `docker-compose.oci.yml` | The `app` (and `worker`) service definitions |
| `deploy/app_backend/healthcheck.py` | Cheap `/healthz` liveness (the container HEALTHCHECK) |
| `deploy/app_backend/readiness.py` | Release validation: loads Opus/Vosk/Piper/Silero/NLTK; `--database` also does `SELECT 1` |

The `app` compose service runs read-only-root with a tmpfs `/tmp`, mounts the models dir read-only, publishes only
`127.0.0.1:18000` (loopback; public traffic comes via Caddy), and joins two networks:
- `app-backend` (default) → reaches host PostgreSQL at `172.30.0.1` (see [DATABASE.md](DATABASE.md)).
- `aipin_default` (external, Caddy's network) with **alias `app-main`** → the Caddyfile proxies `app-main:8000`.

Required env (in `/home/ubuntu/app-backend-config/backend.env`): `DB_HOST=172.30.0.1`, `DB_PORT`, `DB_NAME=ai_pin_db`,
`DB_USER=appuser`, `DB_PASSWORD`, `GOOGLE_API_KEY` (+ optional `GEMINI_API_KEY`, `GEMINI_TEXT_MODEL`,
`DEVELOPER_GEMINI_SYSTEM_INSTRUCTION`, `DEVELOPER_WS_*` tuning), plus the `MQTT_*` vars the worker uses.

## The TLS front door (Caddy)

`aipin-caddy` (`caddy:2`) owns public `:80`/`:443`, obtains Let's Encrypt certs for both sslip hostnames, and reverse-proxies
to the app. Config: `/home/ubuntu/agent/deploy/caddy/Caddyfile`; the upstream line is `reverse_proxy app-main:8000`. WebSocket
upgrades pass straight through. Hostnames come from `SITE_HOST_V4`/`SITE_HOST_V6` in `deploy/.env`.

> Compose adds the service name (`app`) as an alias on every network it joins. The Caddyfile deliberately uses the unique
> `app-main` alias to avoid ambiguity. Do not start a second service named `app` on `aipin_default`.

## Deploy / redeploy

From the build directory on the VM, with the env vars set:

```sh
cd /home/ubuntu/releases/app-backend-step2-20260911
export APP_IMAGE=codex-app-backend:step4-94fa561471da \
       APP_ENV_FILE=/home/ubuntu/app-backend-config/backend.env \
       APP_MODELS_DIR=/home/ubuntu/app-backend-assets/models APP_BIND_PORT=18000
docker compose -p app-backend -f docker-compose.oci.yml up -d          # recreates app + worker on $APP_IMAGE
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/healthz # expect 200
```

Public check: `curl https://146-235-229-232.sslip.io/healthz`. Readiness (models + DB):

```sh
docker compose -p app-backend -f docker-compose.oci.yml exec -T app python /app/deploy/app_backend/readiness.py --database
```

To build a new image, see the build recipe in [OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md#build--release-conventions).

## Rollback

Redeploy the previous image tag (e.g. `codex-app-backend:step3-0c9b6894a43f` or `step2-e4cd5e194c70`) via the same
`compose up -d` with `APP_IMAGE` changed. The old pre-migration stack is gone, so rollback is image-swap or a DB restore, not
a switch back to the old containers.

## History (for context)

Replaced Azure App Service `websocket-ai-pin` (B1, Python 3.12, West US 2). Cut over 2026-09-11: Caddy's upstream was flipped
from the old container to `app-main` and the old `aipin-app`/`aipin-worker` were stopped and later removed. Public HTTPS, the
static pages, `/ws/{user}` hello/ack, and full voice turns (including a Kairos bridge handoff) were verified end to end.

## TODOs

- [ ] **Merge the migration branch to `main` and rebuild the image from the merged commit**, so the image revision label is a
  clean `main` SHA instead of `…-worktree`; then redeploy and record the digest. (See the provenance caveat in the infra doc.)
- [ ] Low priority: move Caddy (and Mosquitto) into the `app-backend` compose project so `/home/ubuntu/agent` can be retired.
