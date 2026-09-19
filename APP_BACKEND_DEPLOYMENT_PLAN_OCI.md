# App backend deployment plan and status — OCI

Updated 2026-09-10 after read-only inspection of the VM and GitHub `main`.

**Current direction (revised 2026-09-10 evening): move the application and the database to OCI in parallel and
finish with a self-contained app on OCI.** The user chose to skip all Azure-connectivity work (no Azure service will be
pointed at the OCI database, and no OCI service will be validated against Azure PostgreSQL). The OCI app connects to the
host `ai_pin_db` on the same VM. The only remaining Azure question is the scheduler path (Service Bus + Function listener +
IoT Hub); see the dependency inventory and decision below. Mobile and application-auth work remain deferred; MQTT and
TTS upgrades are separate follow-ups. Steps 1–2 (recovery artifacts, ARM64 image) are complete and unchanged.

The six migration documents are:

- [Deployment audit](DEPLOYMENT_AUDIT.md): original Azure inventory, corrections and historical OCI inventory.
- [Deployment options](DEPLOYMENT_OPTIONS.md): OCI decision and corrected cost/provider analysis.
- [PostgreSQL migration](POSTGRES_MIGRATION_PLAN.md): completed database work, remaining checks and inspection SQL.
- [Website](WEBSITE_DEPLOYMENT_PLAN_OCI.md): Agent Registry site fixes so the OCI deployment is testable end to end.
- [Scheduler](SCHEDULER_DEPLOYMENT_PLAN_OCI.md): jobs table + worker + Mosquitto replacing Service Bus, Function listener and IoT Hub.
- This file: VM application investigation and the implementation plan.

## What is running on the VM

Inspected `ubuntu@146.235.229.232`, VM `ai-assistant-server`, A1 ARM64, 2 OCPUs / 12 GB RAM.
No deployment, restart, source edit or deletion was performed on the VM.

**`/home/ubuntu/agent` is a source directory without `.git`.** It is not a checkout on which we can safely run
`git switch main` or identify a branch name. We verified GitHub's current `refs/heads/main` as
`4b1fc891a4d62b62f2acf5bd7d6e3cf0fd06d1b1` (`4b1fc89`), which matches this local checkout's HEAD. The local
branch is named `refactor`; that branch name does not change the fact that its current commit matches main.

Compared Python/HTML/CSS/JS files under `app`, `listener` and `agent_directory`: 16 shared files differ,
2 files exist only on the VM, and 4 exist only on main. All 57 Python files inspected inside the running
app image match the VM's corresponding files. The image therefore contains the inspected migration variant;
its original branch/commit cannot be established from the available metadata.

| Component | Current behavior |
|---|---|
| `aipin-app` | Image `aipin-agent:latest`; Python 3.11.16; Uvicorn `main:app` from `/app/app`, internal port 8000 |
| `aipin-worker` | Same image, runs `python listener/worker.py`; polls SQL jobs and publishes MQTT wake-ups |
| `aipin-caddy` | Caddy 2; public TCP 80/443; TLS and reverse proxy to `app:8000` |
| `aipin-postgres` | PostgreSQL 16; Docker DB `aipin`, user `aipin`; app and worker both connect here |
| `aipin-mosquitto` | Mosquitto 2; public MQTT/TLS 8883; configuration and certificates under `deploy/mosquitto` |
| Host PostgreSQL | Separate installation holding **`ai_pin_db`**, which the user wants to keep; the current app does not use it |

All five containers were up about eight days with `unless-stopped` restart policies. App and Docker PostgreSQL
report healthy. The worker has no container health check, so running status does not prove successful delivery.
Compose uses network `aipin_default` and `/home/ubuntu/agent/docker-compose.yml`.

The app image ID begins `sha256:3b4ed4d2b768`; implementation should record the full digest for rollback.
Python code is baked into the image, not mounted from the host. Editing the host's `app/` alone would not
update the running server. The static site and model directory **are** read-only bind mounts:

- `/home/ubuntu/agent/agent_directory` → `/app/agent_directory`
- `/home/ubuntu/agent/data` → `/data`

The models selected by the current environment are Vosk `vosk-model-en-us-0.22-lgraph` and Piper
`en_US-amy-medium.onnx`. A smaller Vosk model also exists. Observed image versions include Pipecat 1.2.1,
Vosk 0.3.45, Piper 1.7.0 and ONNX Runtime 1.24.4; these are evidence of the old image, not a tested lockfile for main.

Caddy serves `146-235-229-232.sslip.io` and `2603-c024-c020-3700-0-537e-9221-8587.sslip.io`.
A GET to the IPv4 hostname's HTTPS `/healthz` returned **200 with certificate verification successful**.
An internal app health request also returned 200. This confirms public TLS/proxy/basic HTTP operation;
we did not test a real voice session, model output, database writes, scheduled delivery or firmware behavior.
IPv6 reachability was not tested. The IP is ephemeral, so the IP-derived names are not a durable production address.

## How the deployed code differs from main

| Area | VM variant | GitHub main | Migration implication |
|---|---|---|---|
| `app/enqueue/*.py` | Inserts/cancels PostgreSQL `jobs`; returns SQL job IDs as sequence IDs | Azure Service Bus schedule/cancel calls | Restore Azure queue configuration; do not reinterpret SQL IDs as Service Bus IDs |
| Listener | `worker.py` and `mqtt_publish.py` | Azure Functions `function_app.py` and `iot_hub_mqtt.py` | Keep the Azure Function listener for this application move |
| Voice pipeline | Different endpoint, pipeline, tools and LLM adapter; no `vad.py` | Includes Silero VAD and newer pipeline behavior | Validate main on ARM64 with its own speech dependencies and full voice tests |
| Other code | Different `main.py`, WebSocket handler, Gemini config and AI client files | Current main versions | Deploy main as a whole; avoid copying old application code into it |
| Website | Different `index.html` and `test.html` | Main's matching client files | Ship site and backend from the same release |
| Docker packaging | Multi-stage build, native libraries, static files, model mounts, Caddy stack | Minimal Python 3.11 Dockerfile and development bind-mounted compose | Add deployment packaging to main through a reviewed change |
| Dependencies | Adds MQTT, omits Azure queue SDK, pins some speech packages | Includes Azure SDKs and Silero-related requirements | Build from main requirements, then lock the tested ARM64 dependency set |

The changed shared files also include `listener/testing/quick_enqueue.py`; VM-only files are
`listener/worker.py` and `listener/mqtt_publish.py`. Main-only files are `app/developer_ws/vad.py`,
`listener/__init__.py`, `listener/function_app.py`, and `listener/iot_hub_mqtt.py`.

The deployed environment contains MQTT configuration but no `AZURE_SERVICEBUS_CONNECTION_STRING` key.
Reusing it unchanged will leave main's scheduling path unconfigured. We inspected environment names and
selected non-secret values only; credentials and complete environment files were not printed.

## Component map: everything in the audit moves to OCI

Goal restated by the user on 2026-09-10: **every component in the deployment audit ends up on OCI.** Caddy is the OCI
replacement for what Azure App Service did implicitly (TLS certificate, port 443, forwarding HTTP and WebSocket to the app).

| # | Audit component (Azure) | OCI replacement | Status |
|---|---|---|---|
| 1 | Backend App Service `websocket-ai-pin` | Caddy (`aipin-caddy`, TLS + reverse proxy) + `main`-based app container | Built, staged, tested; cutover in Step 5 |
| 2 | PostgreSQL Flexible Server `ai-pin-server` | Host PostgreSQL 16 `ai_pin_db` on the VM | Done; off-VM backups pending |
| 3 | Service Bus `q1` (delayed messages + cancel by sequence id) | `jobs` table in `ai_pin_db` written by the app | **Live 2026-09-11** ([scheduler plan](SCHEDULER_DEPLOYMENT_PLAN_OCI.md)) |
| 4 | Function App `listener` (queue consumer, wakes device) | `app-backend-worker-1` container (same image, `listener/worker.py`) | **Live 2026-09-11** |
| 5 | IoT Hub `ai-pin-iot-hub` (cloud-to-device MQTT) | Mosquitto (`aipin-mosquitto`, TLS 8883); worker publishes to `aipin/esp32s3/cmd` | **Live 2026-09-19** — firmware MQTT subscriber flashed; a real reminder woke the cellular pin on time end to end ([scheduler plan](SCHEDULER_DEPLOYMENT_PLAN_OCI.md#5-device)) |
| – | Gemini API | Gemini API | External, unchanged |

### Dual-stack (IPv4 + IPv6) status — verified 2026-09-18

The server accepts connections over **both IPv4 and IPv6**; no change was needed (the dual-stack setup came in with the
oracle-deploy stack and survived the cutover).

| Layer | IPv4 | IPv6 |
|---|---|---|
| VM address | `146.235.229.232` | `2603:c024:c020:3700:0:537e:9221:8587/128` (global, dynamic via RA; exactly matches the sslip v6 name) |
| Default route | present | present (`via fe80::… proto ra`) |
| Listeners 80/443/8883 | `0.0.0.0` (docker-proxy) | `[::]` (docker-proxy) |
| Host firewall | `/etc/iptables/rules.v4` allows 22/80/443/8883 | `/etc/iptables/rules.v6` allows 22/80/443/8883 + ICMPv6 |
| Caddy vhost | `{$SITE_HOST_V4}` | `{$SITE_HOST_V6}` (same reverse_proxy to `app-main:8000`) |
| Let's Encrypt cert | issued for the v4 host | **issued for the v6 host** (Sep 1 – Nov 30) |
| `/healthz` HTTPS | 200 (external, from the Mac) | 200 with a verified cert (hairpin from the VM; the Mac has no IPv6) |

**Why the IPv6 path is proven end-to-end despite no local IPv6 client:** the v6 hostname `2603-…sslip.io` publishes an
AAAA record only (sslip.io returns no A for a dashed-hex-v6 name). Caddy nevertheless holds a current Let's Encrypt
certificate for it, which can only be obtained if Let's Encrypt's validators, out on the public internet, connected to the
VM **over IPv6** on port 80/443 and completed the ACME challenge. That traffic traversed the OCI VCN security list and the
host ip6tables from the outside, so external inbound IPv6 on 80/443 is confirmed. Caddy renews over the same path (~30 days
before the Nov 30 expiry), so it stays proven.

Not independently confirmed from outside: **8883 (MQTT) over IPv6** — the host listens on `[::]:8883` and ip6tables allows
it, but no external v6 client has exercised it and the OCI security-list v6 stanza for 8883 was not read directly (the OCI
CLI session is expired). This does not matter for the current device contract: `MQTT_TLS_HOST` is the **v4** name
(`146-235-229-232.sslip.io`), which the IPv6-only LTE device reaches via NAT64. If MQTT ever needs native v6, read the OCI
security list to confirm an inbound-8883 rule for `::/0` and, if the device dials the v6 name, switch `MQTT_TLS_HOST` to it
so Mosquitto presents the matching certificate. The probe tooling available here (this Mac, free multi-node checkers) is
IPv4-only and cannot resolve or reach an AAAA-only name, so a live external v6 probe was not possible from here.

Pre-cutover observation (2026-09-11 UTC): Caddy, the old app and Mosquitto logs show no device or WebSocket traffic in the
last 72 hours (Mosquitto sees only internet scanners). The device is therefore still using Azure, or is off. Cutting the OCI
endpoint over affects no live user; the real user-facing switch happens when the device's URL/firmware moves.

### Scheduler component (replaces the earlier "Option A: defer")

Plan: [SCHEDULER_DEPLOYMENT_PLAN_OCI.md](SCHEDULER_DEPLOYMENT_PLAN_OCI.md) (2026-09-11). The server half already exists on
branch `oracle-deploy`; the device half is blocked on the firmware repository. Original outline:

1. `jobs` table in `ai_pin_db` (id, user_id, kind task/text_message, payload json, due_at, status, attempts, cancelled_at); index on (status, due_at).
2. `app/enqueue/*.py` variant that inserts/cancels `jobs` rows instead of Service Bus calls, returning the row id as `enqueue_sequence_id`. The stale VM stack's `app/enqueue` is a reference implementation; main's behavior (schedule, cancel, re-schedule on edit) must be preserved.
3. Worker container (`listener/worker.py` reference on the VM): claim due jobs with `FOR UPDATE SKIP LOCKED`, apply the session-active rule from `function_app.py`, publish the wake command to Mosquitto, mark done/retry.
4. Device: firmware subscribes to Mosquitto instead of IoT Hub. Needs firmware source and a device test; blocking for retiring IoT Hub, not for the app.
5. Migrate outstanding Service Bus messages: read pending `tasks` with `enqueue_sequence_id` and re-enqueue them as `jobs`; no need to drain `q1` itself.

## Azure dependencies of the `main` application

Inventory taken 2026-09-10 by reading every `os.environ` / `os.getenv` use and every `azure*` import under `app/`.

| Dependency | Where | Required? | What happens on OCI without it |
|---|---|---|---|
| **PostgreSQL** (`DB_HOST/PORT/NAME/USER/PASSWORD`) | `app/database.py`, `app/agents_registry.py`, all routes | **Yes.** System of record | App starts but every task/message/session/agent call fails. Replaced by host `ai_pin_db` on the VM |
| **Service Bus queue `q1`** (`AZURE_SERVICEBUS_CONNECTION_STRING`) | `app/enqueue/{task,edit_task,message}_enqueue.py`, called from task/messaging routes and the Gemini create/edit-task tools | No, soft | All call sites use the `*_safe` wrappers: the task or message row is still written, the response carries `enqueue_warning`, and **no reminder or text-message wake-up is ever delivered**. Also a debug line prints the full connection string when set (`task_enqueue.py`); remove before setting a real value |
| **Function App `listener`** + **IoT Hub `ai-pin-iot-hub`** | Not in `app/`; `listener/function_app.py` consumes `q1`, reads `sessions`/`messages` from the DB, sends the cloud-to-device `start_websocket` command to device `esp32s3` | Indirect | This is the delivery half of scheduling. It needs the same database as the app, so it cannot keep running on Azure once the app uses the OCI database unless Azure gets network access to the OCI database, which is exactly the work being skipped |
| Azure OpenAI (`AZURE_OPENAI_ENDPOINT/API_KEY`) | `app/agents/openai_client.py` | No, dead code | Nothing imports this module; leave both variables unset |
| Gemini (`GOOGLE_API_KEY`, optional `GEMINI_API_KEY`, `GEMINI_TEXT_MODEL`) | `app/gemini_config.py`, `app/developer_ws/pipecat_llm.py` | Yes, but Google not Azure | Same key as today; copy from the VM's existing private env file |

So the user's belief is close: the database is the only hard dependency. The one real gap is **scheduled delivery**
(reminders and text-message wake-ups), which today is Service Bus → Azure Function → IoT Hub → device.

### Scheduler decision (needed before Step 6, not before Steps 3–5)

| Option | What runs where | Azure still used | Effort | Reminders work? |
|---|---|---|---|---|
| **A. Defer (default assumption)** | Nothing. `AZURE_SERVICEBUS_CONNECTION_STRING` left unset on OCI | None for the app path | None | No, until the separate scheduler/MQTT migration lands. Tasks are still saved |
| **B. Listener-on-OCI shim** | A small OCI container that does what `function_app.py` does: receive from `q1`, read the local DB, call IoT Hub C2D. App keeps enqueueing to `q1` | Service Bus `q1` and IoT Hub as outbound-only services (no inbound Azure access needed) | About 80 lines: replace the Functions trigger with a `ServiceBusClient` receiver loop; reuse `iot_hub_mqtt.send_to_device` and `listener/database.py` | Yes, same behavior as today |
| C. Keep the Azure Function | Function App stays on Azure, must reach the OCI DB | Everything, plus OCI DB exposure + TLS | The work the user chose to skip | Yes |

Decision 2026-09-10: A for the app cutover (queue variables unset), then the **scheduler component above replaces it**
because the goal is to leave Azure entirely. Option B is no longer planned.

## Target architecture (OCI-only)

```text
Device / browser -- HTTPS + WSS --> Caddy (existing, OCI) --> main-based app container (new compose project)
                                                                    |
                                                                    v  private Docker bridge, scram auth
                                                            host PostgreSQL 16  ai_pin_db  (same VM)

Gemini API (outbound)                      Option B only: app --> Service Bus q1 --> listener shim (OCI) --> IoT Hub --> device
```

The app reaches the host database over a dedicated Docker bridge network with a fixed subnet; PostgreSQL listens on that
bridge gateway address in addition to localhost, and `pg_hba.conf` allows only `appuser` to `ai_pin_db` from that subnet.
Inside a container `127.0.0.1` is the container itself, so `DB_HOST` is the bridge gateway, not localhost. No public
database port is opened and no TLS hostname certificate is needed for this path. Details are in the
[PostgreSQL plan, Step 3](POSTGRES_MIGRATION_PLAN.md#step-3--connect-the-oci-app-container-to-the-host-database).

## Implementation steps

### 1. Preserve the current deployment and establish the release source

**Status: Step 1 preparation complete (2026-09-10 Pacific / 2026-09-11 UTC).**

- [x] Renamed this component plan to `APP_BACKEND_DEPLOYMENT_PLAN_OCI.md` and updated document links.
- [x] Fetched GitHub main and created local branch `codex/oci-application-main` at
  `4b1fc891a4d62b62f2acf5bd7d6e3cf0fd06d1b1`.
- [x] Saved private recovery artifacts at
  `/home/ubuntu/deployment-backups/app-backend-step1-20260911T033947Z` on the VM.
- [x] Created a separate clean Git checkout at `/home/ubuntu/releases/agent-main-4b1fc891a4d6`,
  detached at that exact main commit. Git integrity check passed; origin points to the GitHub repository.
- [x] Preserved the active `/home/ubuntu/agent` directory, host `ai_pin_db`, SQL dump and existing volumes.
- [ ] Packaging implementation, review/merge, image revision tagging and deployment remain later steps.
  The new checkout is a source baseline, not a deployed or newly approved packaged release.

The recovery directory contains the old source/configuration/site/model archive (including private env and
broker certificates), container/volume metadata, all four distinct running images, logical dumps and role
exports for both PostgreSQL installations, host PostgreSQL configuration, and Caddy/Mosquitto volume archives.
It holds about **776 MB** of artifacts plus a SHA-256 manifest. The directory is private (`0700`) and artifact
files are `0600`; do not copy their contents into Git or logs. The Git bundle is separately retained at
`/home/ubuntu/releases/agent-main-step1.bundle`.

**Verification:** archive catalogs and the image manifest were readable, both PostgreSQL dump catalogs parsed,
and the pinned checkout passed `git fsck` with a clean working tree. All 13 artifact checksums matched.
All five containers retained their original start times and stayed running; host PostgreSQL was active.
Public HTTPS `/healthz` returned 200 with successful TLS verification. These are same-VM recovery copies, not an off-VM backup or a tested full restore.
Live broker/proxy volume archives are best-effort copies; database backups use logical dumps rather than raw
copies of running PostgreSQL data files. Database recovery work remains deferred.

**Failures encountered and resolved:**

1. The first source archive attempt could not read root-owned MQTT ACL, password and private-key files.
   Retried using existing sudo access and replaced the partial archive; the complete archive passed inspection.
2. Default cloning from the bundle did not select its remote-tracking ref and produced an empty checkout.
   Explicitly fetched `refs/remotes/origin/main`, checked out the exact commit, and verified the repository.
   This occurred only in the new release directory; the active source directory was unaffected.

The original Step 1 instructions are retained below for context. Item 4's merge/tag requirements apply when
the packaging work from Step 2 is ready, before production deployment.


1. Privately save the existing compose/proxy configuration, environment file, model locations, image digests
   and volumes before replacement. Preserve host `/var/lib/postgresql/16/main` and the Azure SQL dump.
2. Keep `/home/ubuntu/agent` intact during staging: its models, site and Caddy configuration are active mounts.
   Do not run `docker compose down -v`, prune volumes, or run the stale deployment/restore scripts.
3. Start a fresh local branch such as `codex/oci-application-main` from a freshly fetched `origin/main`.
   Add only the deployment changes below; use main's application, enqueue and listener code.
4. Review and merge the packaging change into main before deploying it as the main release. On the VM,
   create a separate Git clone/release directory, then check out the exact approved main commit.
   Record that commit in the image tag and OCI image revision label. Avoid mutable `latest` as release identity.

**Preparation complete:** recovery artifacts are saved and the source baseline is traceable to fetched main.
**Before deployment:** merge the tested packaging changes into main and pin/tag that resulting release commit.

### 2. Make main's container complete on ARM64

**Status: Step 2 complete (2026-09-10 Pacific / 2026-09-11 UTC). Test image built and validated on the VM; nothing deployed.**

- [x] Packaging files added to branch `codex/oci-application-main` (listed below); the VM build directory holds byte-identical copies (SHA-256 matched for all seven files).
- [x] Native `linux/arm64` build on the VM succeeded in two passes: `step2-bootstrap` (unpinned, used to resolve the dependency set) then the pinned image
  `codex-app-backend:step2-e4cd5e194c70`, image ID `sha256:e754934d01cb9864905fb03580f9d68ecc64a38fa157bc03cd259b71bd15a1a4`, 1.66 GB.
  Labels: `org.opencontainers.image.revision=4b1fc891a4d62b62f2acf5bd7d6e3cf0fd06d1b1-worktree`,
  `io.aipin.source-tree-sha256=e4cd5e194c70043b622211462e17f21ba24aed833cbe1a42935fa14e16517a8e`. `pip check` passed during the build.
- [x] Image content check: 61 files under `/app` (`app/`, `agent_directory/`, `deploy/`); no `.env*`, `*.key`, `*.sql` or `*.pem` present;
  runs as uid 10001 `app`; Python 3.11.16; `APP_REVISION` is set inside the container. The build directory intentionally contains
  `app/.env.packaging-test` and `app/packaging-test.key` as exclusion fixtures, and the image confirmed they were not copied.
- [x] Audio readiness (`readiness.py`, no `--database`) passed in a read-only container with the models mounted read-only:
  Opus round-trip, Vosk `vosk-model-en-us-0.22-lgraph`, Piper `en_US-amy-medium.onnx` plus companion JSON, Silero VAD and NLTK `punkt_tab`
  all loaded; wall time about 9 s.
- [x] Compose start test with `docker-compose.oci.yml` (project `app-backend-step2test`, loopback port 18000, placeholder-only env file,
  since Step 3 configuration does not exist yet): `config --quiet` passed; `/healthz` returned 200 about 10 s after start; `/` (static site)
  returned 200; Vosk, Piper and Silero preloaded at startup; idle memory about 618 MiB of the 3 GiB limit. The container, network and
  placeholder env file were removed afterwards. All five existing `aipin-*` containers kept running throughout.
- [ ] Not covered: database connectivity (`ensure_agents_table` logged a connection-refused error against the placeholder host and the app
  continued, which is main's existing behavior), provider access, WebSocket voice sessions, scheduler delivery, load. These belong to Steps 3–4.

**Failures encountered:** none in the validation runs. The build itself needed two passes by design (bootstrap resolve, then pinned rebuild).

**Before production:** review and merge these packaging changes into main, then rebuild from the merged commit so the revision label is a
clean main SHA rather than `-worktree`, and record that image digest.


Packaging files: `Dockerfile`, `.dockerignore`, `docker-compose.oci.yml`, `requirements-oci.lock`, and
`deploy/app_backend/{healthcheck,readiness}.py`. The original development compose file is retained; use the
explicit OCI compose filename for this backend. No SQL worker, database or MQTT broker is included.

Speech assets have been copied outside the release trees to `/home/ubuntu/app-backend-assets/models`.
All 33 files matched their source SHA-256 hashes; the asset manifest is stored beside that directory.
The app image contains the matching main static site, with no source/site bind mounts.

The production-shaped compose configuration initially binds only loopback port 18000. It runs one app
process with a read-only root filesystem, read-only models, temporary writable `/tmp`, bounded logs, and
default limits of 1 CPU / 3 GB RAM. These are initial limits, not measured production capacity. The existing
Caddy proxy is not connected to this new service yet; its network/upstream change belongs to cutover.

Do not use `/healthz` as proof of working models or database access. Run the explicit readiness command
for audio; add `--database` only with the selected test/runtime DB configuration. The latter uses read-only
`SELECT 1`, while normal app startup can create the registry table.

The build/test directory on the VM is `/home/ubuntu/releases/app-backend-step2-20260911`. Test images
are marked as a worktree build based on main, with a separate source snapshot checksum; they are not yet
a merged main release.

1. Use a tested Python version below 3.13 because the audio code uses `audioop`. Start with the current
   deployment's Python 3.11 baseline; validate main's dependency requirements before locking it.
2. Include `libopus0`, `libatomic1` and `libgomp1` plus any build-only compilers needed by actual dependency
   installation. The old VM Dockerfile is a useful packaging reference; omit its SQL-worker/job-schema pieces.
3. Install main's requirements, including Azure Service Bus and its full voice pipeline; produce reproducible
   dependency pins after a successful `linux/arm64` build. Do not substitute the stale VM requirements wholesale.
4. Copy `app/` and `agent_directory/` from the same commit. Run Uvicorn from `/app/app` so the existing bare
   imports resolve. Use the current startup script's WebSocket ping settings as the initial baseline.
5. Mount verified Vosk/Piper assets read-only outside the release directory; include the Piper companion JSON.
   Check that main's Silero model loads. Keep voice selection unchanged for this move.
6. Run as a non-root user, add an HTTP health check that fails on non-2xx responses, and allow enough startup
   time for model preload. `/healthz` already exists; add separate deployment checks for DB/model readiness.
7. Exclude `.env`, deployment secrets, keys, SQL dumps and local data from the build context. Bake source/site
   into the image instead of mounting the stale source directory over the new release.
8. Begin with one Uvicorn process because sessions/developer registry are process-local. Measure resource use
   alongside host PostgreSQL before increasing concurrency; keep room for the database and an image build.

**Complete when:** the main-derived ARM64 image starts, reports its revision and loads all audio dependencies/assets.

#### Running the packaged backend after configuration is ready

Use the OCI compose file explicitly. Set these outside Git before starting a configured staging deployment:

```sh
export APP_IMAGE=YOUR_VALIDATED_IMMUTABLE_IMAGE_TAG
export APP_ENV_FILE=/absolute/path/to/private/backend.env
export APP_MODELS_DIR=/home/ubuntu/app-backend-assets/models
export APP_BIND_PORT=18000
docker compose -p app-backend -f docker-compose.oci.yml config --quiet
```

Only after Step 3 provides isolated test configuration, start the staging service and run its readiness check:

```sh
docker compose -p app-backend -f docker-compose.oci.yml up -d
docker compose -p app-backend -f docker-compose.oci.yml exec -T app \
  python /app/deploy/app_backend/readiness.py --database
```

The default `/healthz` check is cheap liveness. The explicit readiness command loads/uses Opus, Vosk, Piper,
Silero and NLTK assets and, with `--database`, checks `SELECT 1` in a read-only transaction. It does not prove
schema compatibility, application writes, provider access or scheduler delivery; those remain in Steps 3–4.
The image must be rebuilt from the reviewed main commit before production release. Python package versions
and the Python base digest are pinned; OS package repositories and NLTK asset downloads are still build-time
inputs, so retain the resulting image digest rather than assuming future builds are byte-identical.


### 3. Create the private OCI configuration for the app

**Status: complete 2026-09-11 UTC. Nothing touched Azure.** Scheduler decision: **A (defer)**; queue variables unset.

- [x] Database route done and verified ([PostgreSQL plan Step 3](POSTGRES_MIGRATION_PLAN.md#step-3--connect-the-oci-app-container-to-the-host-database)); `agent_rehearsal` created (Step 4 there).
- [x] `/home/ubuntu/app-backend-config/backend.env` created on the VM (`0600`), keys copied by name from the old private env, DB values for the bridge, new `appuser` password. Never printed.
- [x] `docker-compose.oci.yml` now joins the external `app-backend` network (repo change, synced to the VM build directory).
- [x] `readiness.py --database` from the packaged container: `{"ready": true, "audio": true, "database": "passed"}`.
- [x] Staging service started as compose project `app-backend` on `127.0.0.1:18000` with the real env file: `/healthz` 200 in about 10 s,
  startup logged `agents table ensured` (a no-op on the existing table), `GET /api/agents` 200, `GET /tasks/{user_id}` 200, container healthy, 618 MiB idle.
  **It is left running on loopback for Step 4.** Stop with `docker compose -p app-backend -f docker-compose.oci.yml down` from the build directory (the network is external and stays).
- [x] Old `aipin-*` stack untouched.

**Failures encountered:** compose would not adopt a hand-created network as its default (fixed by `external: true`); the
rehearsal clone via `TEMPLATE` was blocked by the `pg_cron` launcher session (dump/restore used instead). Both recorded in the PostgreSQL plan.

Original Step 3 instructions:

1. Database side first: complete [PostgreSQL plan Step 3](POSTGRES_MIGRATION_PLAN.md#step-3--connect-the-oci-app-container-to-the-host-database)
   (fixed-subnet bridge network, `listen_addresses`, `pg_hba.conf`, host firewall rule, new `appuser` password written
   only to the private env file on the VM). Then create the rehearsal copy `agent_rehearsal` from `ai_pin_db`
   ([Step 4](POSTGRES_MIGRATION_PLAN.md#step-4--rehearsal-database-and-restore-procedure)).
2. Create `/home/ubuntu/app-backend-config/backend.env` (directory `0700`, file `0600`, outside Git). Populate it on the
   VM by copying selected keys from the existing private `/home/ubuntu/agent/deploy/.env` without printing values:
   `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GEMINI_TEXT_MODEL`, `DEVELOPER_GEMINI_SYSTEM_INSTRUCTION`, `LLM_TIMEOUT_MS`,
   `DEVELOPER_WS_END_SILENCE_SEC`, `DEVELOPER_WS_VAD_RMS`, `DEVELOPER_WS_BARGE_RMS`. Add `DB_HOST` = bridge gateway,
   `DB_PORT=5432`, `DB_NAME=agent_rehearsal` (staging) then `ai_pin_db` (cutover), `DB_USER=appuser`, `DB_PASSWORD`.
   Do not copy MQTT_*, SITE_HOST_*, WORKER_* or DEVICE_ID; they belong to the stale stack. Leave
   `AZURE_SERVICEBUS_CONNECTION_STRING` and `AZURE_OPENAI_*` unset (Option A).
3. Add the fixed-subnet network to `docker-compose.oci.yml` and document `DB_HOST` there. If Option B is chosen later,
   remove the connection-string debug print in `app/enqueue/task_enqueue.py` first.
4. Run the readiness command with `--database` against `agent_rehearsal`.

**Complete when:** `readiness.py --database` reports ready against the rehearsal database from inside the container.

### 4. Stage beside the current server and test main behavior

**Status: complete 2026-09-11 UTC (automated parts). Staging container still up on `127.0.0.1:18000` against `agent_rehearsal`.**
Tests ran from a throwaway container of the same image on the `app-backend` network (`stage_test.py`, kept in the VM build
directory), so they exercised the real container-to-container and container-to-host-database paths. Test rows were deleted afterwards;
`agent_rehearsal` counts match `ai_pin_db` again.

| Area | Result |
|---|---|
| Static site | `/`, `/test.html`, `/style.css`, `/healthz` all 200 |
| Agent registry | `POST /developer/register` ok → listed by `GET /api/agents` → `POST /developer/unregister` ok (`changed: true`) |
| Tasks | `POST /tasks` (enqueue=True) 200 with `enqueue_warning` and the row saved; `GET` by id and by user; `PUT` status→completed; `DELETE`; `GET` after delete → 404 |
| Messages | `POST /messages` 200, row visible in `GET /messages?chat_id`; `POST /messages/enqueue` returns 200 with `success:false` and the missing-variable message (graceful, as expected under Option A) |
| `/ws/{user_id}` | `{"type":"hello"}` → `{"type":"ack","accept":true,"service_id":"kairos"}`; Gemini Live session opened with the copied key; `bye` closed it cleanly |
| `/ws/developer/{user_id}` | Connect ding received (2 Opus frames). Turn 1: Piper-synthesized "What is two plus two?…" sent as 254 paced 20 ms PCM frames → Silero VAD STARTED/STOPPED → Vosk transcript `what is to plus to answer with just the number` → Gemini 1.4 s → Piper spoke `4` (0.8 s of Opus). Turn 2 (new session, i.e. reconnect): "repeat … the quick brown fox…" → transcript correct → Gemini 1.1 s → reply audio decoded and re-transcribed as `the quick brown fox jumps over the lazy dog`. First reply audio 2.2–2.4 s after the client stopped speaking (0.8 s of that is the VAD stop window). Disconnects logged cleanly, registry unregistered |
| Resources | App container 618 MiB idle → 741 MiB after two voice sessions, CPU idle between turns; VM 2.6 GB used of 11.9 GB with the old stack still running |

Not covered by automation, still open before or during cutover:

- **Bridge handoff/return** (`/developer/ping` → orchestrator dials a registered service): needs a reachable registered service; run `echo_server.py` from the image on the same network if you want this proven before cutover.
- **Barge-in / interrupt** was only sent as a frame after the reply; not measured.
- **Device firmware path** (ESP32 hostname/certificate expectations and IoT Hub wake-up) cannot be tested from the VM; the hostname does not change at cutover, so the risk is limited to the reminder path, which is deferred anyway.
- **Mic-in-browser test** via the SSH tunnel is optional now that the automated voice turn passed: `ssh -L 18000:127.0.0.1:18000 -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232`, then `http://localhost:18000/test.html`.

Original Step 4 list:

1. Start the `app-backend` compose project on loopback port 18000; reach it through an SSH tunnel from the Mac.
2. Test `/healthz`, the static registration/test pages, and both `/ws/{user_id}` and `/ws/developer/{user_id}`.
3. Verify real Opus decoding/encoding, Vosk transcription, Piper speech, Silero endpointing, interruption, bridge
   handoff/return and reconnect after an idle period. Fail on silent missing-model/library fallback.
4. Exercise task/message/session/agent create/read/update against the rehearsal data. Under Option A, confirm the
   task is saved and the response carries `enqueue_warning` rather than an error.
5. Record memory, CPU and voice/DB latency on A1 with host PostgreSQL running alongside. Compare against the old
   stack's idle numbers (about 1.8 GiB used at inspection).

**Complete when:** main's user-visible paths work through the new container against `agent_rehearsal`, results recorded here.

### 5. Cut the public endpoint over to the new app on `ai_pin_db`

**Status: done 2026-09-11 05:58 UTC (2026-09-10 evening Pacific). Public endpoint now serves the `main`-based app on host `ai_pin_db`.**

- [x] Pre-cutover dump: `/home/ubuntu/db-backups/ai_pin_db-precutover-20260911T053014Z.dump` (+ `.sha256`, `0600`).
- [x] `DB_NAME=ai_pin_db` in the private env file (the user ran this one line; the permission classifier blocked me from editing a
  secrets file). Backup of the previous env file kept beside it.
- [x] `docker-compose.oci.yml` now also joins Caddy's external network `aipin_default` with alias `app-main` (repo change, synced).
  Container recreated; `readiness.py --database` passed against `ai_pin_db`; startup logged `agents table ensured`.
- [x] Caddyfile upstream changed from `app:8000` to `app-main:8000` (backup `Caddyfile.bak-<ts>` beside it), `caddy reload` succeeded,
  certificate unchanged (`ssl_verify=0` from the Mac).
- [x] Verified from the Mac through `https://146-235-229-232.sslip.io`: `/healthz` 200, `/` and `/test.html` 200, WebSocket upgrade on
  `/ws/developer/{user}` returned 101 and the new app logged the session. Caddy forwarded the requests to the new container (source 172.18.0.x).
- [x] `docker stop aipin-app aipin-worker` (both exited 0, images and volumes retained). Public health still 200 afterwards.
  Caddy, `aipin-postgres` (Docker DB) and `aipin-mosquitto` keep running.
- [ ] Observation window (suggest 48 h): container health, memory (634 MiB idle after cutover), PostgreSQL and Caddy logs.
- [ ] Real device session: not possible yet, the device is not pointed at this VM (no device traffic in 72 h before cutover).
- [ ] Reserve the public IP: **note that OCI cannot convert an ephemeral IP to a reserved one in place**; reserving means a new
  address, hence a new sslip hostname (or a real DNS name) and a Caddy host change. Do this together with the device URL change.

**Gotcha recorded:** compose adds the service name (`app`) as an alias on every network it joins, so while the old `aipin-app` was
still running, `app` on `aipin_default` resolved to both containers. The Caddyfile uses the unique `app-main` alias, so this is
harmless now, but do not start the old `app` service again while the new container is attached to that network.

**Rollback:** the stale stack (old app, worker, Docker DB) was deleted on 2026-09-11 at the user's request, so rolling back to it
is no longer possible. Rollback now means redeploying the previous app image (`codex-app-backend:step2-e4cd5e194c70`) on the same
compose project, or restoring `ai_pin_db` from a dump in `/home/ubuntu/db-backups/`. Do not run `docker compose up` in
`/home/ubuntu/agent`; it would try to recreate the deleted services.

Original Step 5 instructions:

1. Reserve the VM's public IP in the OCI console so `146-235-229-232.sslip.io` stops being ephemeral (or pick a real
   hostname and add it to the Caddyfile). Record the current Caddyfile and `deploy/.env` host values privately.
2. Flip `DB_NAME` to `ai_pin_db` in the private env file. Take a fresh logical dump of `ai_pin_db` first
   ([PostgreSQL plan Step 6](POSTGRES_MIGRATION_PLAN.md#step-6--backups)).
3. Connect the new app to Caddy: attach the `app` service to the external network `aipin_default` with alias
   `app-main`, change the Caddyfile upstream from `app:8000` to `app-main:8000`, and `caddy reload`. Existing
   certificates in the `caddy_data` volume are reused, so no re-issuance. Test HTTPS and WSS through the public
   hostname before touching anything else.
4. Stop `aipin-app` and `aipin-worker` (`docker stop`, not `down -v`); keep their images, the Docker `aipin` DB and
   Mosquitto volumes until the observation window ends. Caddy and Mosquitto keep running.
5. Confirm a real voice session from the device and at least one task create/read through the public endpoint.
   Under Option A, note that reminders will not fire until the scheduler component lands.
6. Observe for an agreed window (suggest 48 h): container health, memory, PostgreSQL log, Caddy errors.

**Rollback (before or during the window):** revert the Caddyfile upstream to `app:8000`, `caddy reload`, `docker start
aipin-app aipin-worker`. That restores the previous OCI stack on its own Docker database, i.e. the state the device was
using before cutover. Azure resources are not part of rollback because nothing was changed there.

### Before the device goes live on OCI

Not blockers for the current no-user state; all must be done before the device's URL/firmware is pointed at this VM.

- [ ] **Off-VM backups** ([PostgreSQL plan Step 6](POSTGRES_MIGRATION_PLAN.md#step-6--backups)): every copy of `ai_pin_db` is on the VM's boot disk today.
  Risk is disk loss, accidental deletion, or Oracle reclaiming an idle Always Free instance. Data is ~8 MB now; the cheap interim is `scp` of a dump to the Mac.
- [ ] **Reboot check** ([PostgreSQL plan Step 7](POSTGRES_MIGRATION_PLAN.md#step-7--reboot-verification)): PostgreSQL binds `172.30.0.1`, which exists only after Docker creates the bridge;
  the systemd `After=docker.service` drop-in is untested. Failure mode is loud (app healthcheck fails) and fixed by `systemctl restart postgresql@16-main`.
- [ ] **Stable address**: the public IP is ephemeral. OCI cannot convert it in place, so reserving means a new IP, a new hostname (real DNS name preferred over sslip),
  a Caddyfile host change and a Mosquitto TLS host change. Do it before the firmware hostname is set, not after.
- [x] **Kairos agent URL row** and website API base done 2026-09-11 ([website plan](WEBSITE_DEPLOYMENT_PLAN_OCI.md)); the row must be edited again when the hostname changes.
- [x] Scheduler component live on the server side (2026-09-11); device half pending firmware.
- [ ] Drop `agent_rehearsal` or keep it as the standing staging DB (stale containers/volumes already removed 2026-09-11).

### 6. Scheduler component (decision above)

**Pending decision.** Under Option A, nothing to do in this plan; open a separate component plan for the scheduler
(SQL jobs + MQTT variant from the stale stack is one candidate). Under Option B, add `deploy/listener_shim/` and a
second compose service; it needs `AZURE_SERVICEBUS_CONNECTION_STRING`, `IOT_HUB_SERVICE_CONNECTION_STRING` and the
same `DB_*` values, and the app then gets the Service Bus variable too.

### 7. Retire Azure and clean up the VM

**Pending; after the observation window.**

1. Azure: stop App Service `websocket-ai-pin`, Function App `listener`; delete PostgreSQL Flexible Server
   `ai-pin-server` after saving one final dump off-Azure. Keep Service Bus and IoT Hub only if Option B is running.
2. VM cleanup **done 2026-09-11 UTC** (user's decision: keep only migration artifacts): removed `aipin-postgres` container and
   `aipin_postgres_data` volume (its logical dump `stale-docker-aipin.dump` remains in the Step 1 recovery directory), the
   `aipin-app`/`aipin-worker` containers, images `aipin-agent:latest`, `codex-app-backend:step2-bootstrap`, `python:3.11-slim`,
   8 dangling anonymous volumes and 2.8 GB of build cache. Remaining: `app-backend-app-1`, `aipin-caddy`, `aipin-mosquitto`,
   volumes `aipin_caddy_*`, `aipin_mosquitto_data`; images step2 (rollback), step3 (live), `postgres:16` (used for throwaway
   psql checks), bases. The old rollback to the stale stack is therefore gone; rollback is now the step2 image only.
   Still to do: move Caddy and Mosquitto into the new compose project so `/home/ubuntu/agent` can be retired.
3. Merge the packaging branch into main, rebuild the image from the merged commit, redeploy, record the digest.

## Implementation deliverables and current status

- [x] Consolidated migration documentation into four files.
- [x] Verified GitHub main commit and compared VM/image source with it.
- [x] Inspected runtime services, routing, models and current public HTTPS health.
- [x] Wrote the main-based application implementation and rollback plan.
- [x] Step 1: preserve the existing deployment and establish a separate main source baseline.
- [x] Step 2: ARM64 packaging implemented on the branch; test image built and validated on the VM (not deployed).
- [x] Inventoried the app's Azure dependencies; database is the only hard one, scheduler is the only gap.
- [x] Scheduler decision: A (defer). Final Azure snapshot: not taken; 2026-09-01 copy accepted.
- [x] Step 3: database bridge route + private env file + readiness with `--database` against `agent_rehearsal`; staging container up on loopback.
- [x] Step 4: staged on loopback; static, CRUD, `/ws` hello/ack and two full voice turns passed against `agent_rehearsal`.
- [x] Step 5: Caddy upstream switched to the new app on `ai_pin_db`; old app/worker stopped. Observation window open.
- [ ] Step 6: scheduler component (separate plan under A).
- [ ] Step 7: retire Azure, clean the VM, merge packaging into main and rebuild.

Steps 1–2 added private recovery files, a separate source checkout, a build directory and two test images on the VM.
Step 3 added host PostgreSQL listen/HBA/firewall changes, a private env file, a new `appuser` password, the `agent_rehearsal`
database and a staging container on loopback. The live `aipin` stack, Caddy and `ai_pin_db` records were not changed.
Step 5 changed the live path on the VM: Caddy now proxies to the `main`-based container on host `ai_pin_db`; the old app and
worker are stopped (not removed). Running image since the scheduler go-live: `codex-app-backend:step4-94fa561471da` (app + worker). Next: PostgreSQL Steps 6–7 (off-VM backups, reboot check), the scheduler component plan,
then device URL/firmware move and Azure retirement (Step 7).
