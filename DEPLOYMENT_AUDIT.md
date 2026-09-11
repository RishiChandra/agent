# Deployment Audit — what needs to be deployed

Original audit date: 2026-09-02; reviewed and corrected 2026-09-09
Repo: `RishiChandra/agent`, `main` @ `4b1fc89`
Originally reported host: Azure, subscription **Microsoft Azure Sponsorship**, resource group **`ai-pin`**

**Evidence status:** code facts were checked against this checkout. Resource names, live sizes, versions,
regions, firewall rules, and deployment dates below are historical observations from the original audit,
not independently revalidated: Azure CLI and raw inspection records were unavailable. Firmware is in another
repository and was not inspected. A code reference proves a dependency, not its current deployed settings.

**Current direction (updated 2026-09-10):** the initial OCI PostgreSQL copy is verified. Move the application
from GitHub main next, then return to database connectivity/cutover and recovery. See [the OCI application
plan](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md) and [database progress](POSTGRES_MIGRATION_PLAN.md). Revised 2026-09-10 evening: Azure
connectivity work is skipped; the OCI app will use host `ai_pin_db` on the same VM, and the scheduler path (Service Bus,
Function listener, IoT Hub) is a separate decision recorded in the app plan.
The mobile app is unused and may break; mobile and application-auth work remain deferred.

Purpose: inventory the code's dependencies and preserve the original deployment observations with explicit
confidence limits. The validation history below explain the corrections.

---

## 1. What is deployed and needed

The product is a voice assistant for an ESP32-S3 "pin" device plus a Flutter phone app. The original audit reports that the deployed server-side components run on Azure, in one resource group, deployed **manually from a developer laptop** (no CI/CD).

The backend hosts **two distinct WebSocket endpoints** in one server process: **Kairos** (`/ws/{user_id}`, the Gemini Live
personal-assistant agent with its own tool agents) and the **orchestrator** (`/ws/developer/{user_id}`, the developer
voice pipeline that bridges calls out to registered agents, Kairos included). They are different agents with
different protocols; co-hosting them is a deployment choice, not an architectural one.

| # | Component | Current Azure resource | Role | Deployed how |
|---|---|---|---|---|
| 1 | **Backend**: Kairos WS server + orchestrator WS server + HTTP API + Agent Registry site | App Service `websocket-ai-pin`, plan `ASP-aipin-9950` (B1 Linux, 1 worker, Python 3.12, West US 2) | All application logic; long-lived WebSocket audio sessions | `azure-deploy.sh` zip deploy |
| 2 | **Database** | PostgreSQL Flexible Server `ai-pin-server` (PG 16.14, Standard_B1ms, 32 GB, West US 3) | System of record: users, tasks, messages, sessions, agent registry (~9 MB) | Portal, by hand |
| 3 | **Scheduled job queue** | Service Bus namespace `ai-pin` (Basic), queue `q1` (West US) | Delay timer with cancel-by-id for task reminders and text-message jobs | Portal, by hand |
| 4 | **Queue worker** | Function App `listener` (Flex Consumption, Python 3.13, West US) + its storage account `aipin93a7` | Consumes `q1`, checks session state, wakes the device | `func azure functionapp publish` |
| 5 | **Device wake-up channel** | IoT Hub `ai-pin-iot-hub` (F1 free, West US 2), device `esp32s3` | Cloud-to-device `start_websocket` command over MQTT | Portal, by hand |
| 6 | **Agent Registry website** (Register page + Test orchestrator page) | **No separate resource.** Static files `agent_directory/{index,test}.html, style.css` inside the App Service zip, served by the backend at `/` | Register/edit/delete agents (calls `/api/agents…`), and a browser mic client for `/ws/developer/{user_id}` used to test the orchestrator | Part of `azure-deploy.sh` zip (`walk_into(zf, "agent_directory")`) |

Outside Azure (details in §3): Google Gemini API, ESP32 firmware (separate repo), third-party relay agents,
and speech-model assets. The repository also contains an unused Flutter app and its Firebase Auth integration;
maintaining that app is not required for this migration.

---

## 2. Component details

### 2.1 Backend — App Service `websocket-ai-pin`

| Item | Value | Evidence |
|---|---|---|
| Resource | Linux App Service, plan `ASP-aipin-9950` **B1 Basic (1 vCPU, dedicated App Service plan compute, 1 worker)**, `PYTHON\|3.12`, Always On, WebSockets on, HTTPS-only, no managed identity, no custom domain | `azure-deploy.sh:10-15`; `az webapp show` |
| Hostname | `websocket-ai-pin-fbbrhfawfkb7ecf3.westus2-01.azurewebsites.net` — **hardcoded in the device firmware, the test clients, the Agent Registry site, and the Kairos row of the `agents` table** | `README.md` "URLs"; `agent_directory/index.html:87`; `test/app/test_ws.py:104` |
| Two WebSocket servers | **Kairos** at `/ws/{user_id}` (`app/websocket_handler.py`): Gemini Live agent + the `app/agents/` tool agents, Opus downlink; answers the bridge handshake as `service_id: "kairos"`. **Orchestrator** at `/ws/developer/{user_id}` (`app/developer_ws/`): Pipecat pipeline with Silero VAD → Vosk STT → Gemini text → Piper TTS, plus the `start_remote_audio_bridge` tool that dials an outbound WebSocket to whatever URL the `agents` table holds for the named agent (Kairos included). Distinct agents and wire protocols. | `app/main.py:90-91`; `app/websocket_handler.py:133`; `app/developer_ws/pipeline.py:116-135`; `app/agents_registry.py` |
| HTTP API | tasks (`/tasks…`, `/enqueue-task`), messaging (`/messages`, `/messages/enqueue`), agent registry (`/api/agents…`, `/developer/register`, `/developer/unregister`), `/developer/ping/{user_id}`, `/healthz`; static Agent Registry site at `/` from `agent_directory/`. No authentication; CORS `*`. | `app/routes/*.py`; `app/main.py` |
| Startup | inline command installs `libopus0` via apt on every cold start, then `uvicorn main:app --port 8000 --ws websockets` (the repo's `startup.sh` is unused) | `azure-deploy.sh:30`; live `appCommandLine` |
| Persistent assets | `/home/data/vosk-model-small-en-us-0.15/` (~68 MB) and `/home/data/piper_voices/en_US-amy-medium.onnx` (+`.json`, ~63 MB), uploaded once via Kudu; paths passed as `VOSK_MODEL_PATH` / `PIPER_MODEL_PATH`. The shared process preloads them at startup for the orchestrator. A Piper voice needs its `.onnx` and companion configuration; the planned naturalness upgrade (DEPLOYMENT_OPTIONS.md §1.7) swaps it for `en_US-lessac-high`, with Kokoro-82M as a candidate local backend; quality, CPU and memory need evaluation. | `README.md` "One-time bootstrap"; `app/main.py` lifespan |
| Deploy process | zip of `app/`, `agent_directory/`, `requirements.txt` → `az webapp deploy --type zip` → Oryx builds `requirements.txt` on the server (pipecat pulls ~500 MB of wheels; 5–10 min builds). Last deployed 2026-09-02. | `azure-deploy.sh:159-215` |
| Settings the code needs | `GOOGLE_API_KEY` (optionally `GEMINI_API_KEY`, `GEMINI_TEXT_MODEL`), `DB_HOST/PORT/NAME/USER/PASSWORD`, `AZURE_SERVICEBUS_CONNECTION_STRING`, `VOSK_MODEL_PATH`, `PIPER_MODEL_PATH`, tuning knobs `DEVELOPER_WS_*` (deploy script pins `END_SILENCE_SEC=1.0`, `SILERO_STOP_SECS=0.8`), `WEBSITES_PORT=8000`. Also set but **unused by this code**: `AZURE_OPENAI_*`, `IOT_HUB_CONNECTION_STRING`. | `azure-deploy.sh:134-157`; grep of `os.environ` under `app/` |
| Outbound dependencies | Gemini API (Live + `generateContent`, API key), Postgres, Service Bus (schedule / cancel only), outbound WebSockets to registered agents | `app/gemini_config.py`; `app/database.py`; `app/enqueue/*`; `app/developer_ws/bridge.py` |

### 2.2 Database — PostgreSQL Flexible Server `ai-pin-server`

| Item | Value |
|---|---|
| Server | `ai-pin-server.postgres.database.azure.com`, **West US 3** (different region from the app), PostgreSQL **16.14**, **Standard_B1ms Burstable**, 32 GB, 7-day backups, no HA |
| Network | Public access enabled; firewall rule allows **every IP** (`0.0.0.0`–`255.255.255.255`), historically used to accommodate direct mobile connections; the mobile app is now unused |
| Database in use | the default `postgres` database, one non-superuser SQL role. Size ≈ 9 MB |
| Tables (live, `public`) | `users`, `chats`, `chat_members`, `relationships`, `sessions`, `tasks`, `messages`, `pending_text_message_jobs`, `agents`, plus `agent_registry` and `agent_tasks` (referenced by no code; created by hand) |
| Extensions | `azure`, `pgaadauth`, `pg_cron` (no jobs defined), `plpgsql`. `azure` and `pgaadauth` are Azure-specific; `pg_cron` is portable open source. Review dependencies before omitting extensions from a restore |
| Schema management | None. `agents` is auto-created by the backend at startup (`app/agents_registry.py:ensure_agents_table`); `users` is created by the **mobile app** at launch (`mobile_app/lib/backend/database_service.dart`); the rest by hand. `test/setup_local_postgres.py` is the closest schema reference |
| Direct clients | (1) backend via psycopg2; (2) Function App via psycopg2 (`listener/database.py`); (3) **the currently unused Flutter app**, with host/user/password compiled in (`mobile_app/lib/backend/database_service.dart:29-33`); (4) developers via pgAdmin / test scripts |
| Data of note | `agents` holds the Kairos row (`agent_url` = the backend's `/ws/{user_id}` on the current hostname) plus six placeholder rows |

### 2.3 Scheduled job queue — Service Bus `ai-pin`, queue `q1`

| Item | Value |
|---|---|
| Namespace | `sb://ai-pin.servicebus.windows.net`, **Basic** tier, West US; one queue **`q1`** (1 GB, lock 1 min, TTL 14 d, max delivery 10); no topics |
| Auth | `RootManageSharedAccessKey` connection string, shared by the backend, the Function App, and test scripts |
| Producers | `app/enqueue/task_enqueue.py` (`schedule_messages` at `time_to_execute`; stores the returned sequence number in `tasks.enqueue_sequence_id`), `app/enqueue/edit_task_enqueue.py` (`cancel_scheduled_messages` by that number, then re-schedule), `app/enqueue/message_enqueue.py` (`text_message` job +1 min, intended to be de-duplicated per user via `pending_text_message_jobs`, but its check/insert pattern needs concurrency validation), the listener itself (re-schedules +1 min to defer), `listener/testing/quick_enqueue.py` |
| Consumer | only `listener` / `QueueWorker` |
| Primitives the code relies on | schedule-at-time returning a cancellable id, cancel-by-id, at-least-once delivery to a single worker |

### 2.4 Queue worker — Function App `listener`

| Item | Value |
|---|---|
| Resource | Linux Function App, **Flex Consumption** plan `ASP-aipin-9a49`, Python **3.13**, 2048 MB instances, max 100. Backing storage account `aipin93a7` (host storage + deployment package) |
| Deploy process | `func azure functionapp publish listener --python` from `listener/` (Core Tools 4.x). Settings were entered by hand in the portal |
| Code | Python v2 model `listener/function_app.py`: `serviceBusTrigger` on `q1`. (`listener/__init__.py` + `function.json` are stale v1 leftovers.) Dependencies: `azure-functions`, `azure-servicebus`, `psycopg2-binary`, `azure-iot-device`, `requests` |
| Behavior | per message: parse JSON → look up the user's `sessions` row → if an existing session is active, re-schedule the same body +1 min → if an existing session is inactive, send IoT Hub cloud-to-device `{"command":"start_websocket", "reason":"session_inactive", …}` to device `esp32s3`; for `text_message` jobs also read unread messages and send a second wake with `reason: text_message`. Marking messages read is done by the backend when the device connects |
| Settings the code needs | `AZURE_SERVICEBUS_CONNECTION_STRING`, `DB_HOST/PORT/NAME/USER/PASSWORD`, `IOT_HUB_SERVICE_CONNECTION_STRING` (C2D via IoT Hub HTTPS REST; the `azure-iot-hub` SDK is deliberately not installed). Functions-host settings: `AzureWebJobsServiceBus`, `AzureWebJobsStorage`, `DEPLOYMENT_STORAGE_CONNECTION_STRING` |

### 2.5 Device wake-up channel — IoT Hub `ai-pin-iot-hub`

| Item | Value |
|---|---|
| Resource | **F1 free tier**, 1 unit, West US 2, `ai-pin-iot-hub.azure-devices.net` |
| Device | single identity **`esp32s3`** (hardcoded `DEFAULT_DEVICE_ID`, `listener/iot_hub_mqtt.py:41`) |
| Usage | **only** cloud-to-device JSON commands from the listener. Nothing consumes device-to-cloud telemetry |
| Code outside this repo that depends on it | the ESP32-S3 firmware: IoT Hub MQTT device client (C2D), WebSocket audio client to the backend hostname, BLE provisioning target for the mobile app (`mobile_app/lib/screens/esp_prov_page.dart`, `flutter_esp_ble_prov`) |

### 2.6 Agent Registry website (component 6) — added 2026-09-11

Not a separate Azure resource: the backend mounts `agent_directory/` as static files at `/` (`app/main.py`, `_STATIC_DIR`).
On Azure the site is reached at the App Service hostname; on OCI it is already served by the new container at
`https://146-235-229-232.sslip.io/` (verified 200 for `/`, `/test.html`, `/style.css` on 2026-09-11).

Three things in the site and its data still point at Azure and matter for testing the OCI deployment:

| Item | Where | Effect on OCI today |
|---|---|---|
| `index.html` API base rule: same-origin only when the hostname is `localhost` or is contained in the hardcoded `ORCHESTRATOR` (Azure) URL, otherwise the Azure URL | `agent_directory/index.html:87-95` | **The Register page loaded from the OCI hostname sends every registry call to Azure**, so agents registered "on OCI" land in the Azure database and the list shown is Azure's |
| `test.html` API base rule: same-origin for any `http(s)` page | `agent_directory/test.html:64-71` | Correct on OCI; only the `file://` fallback constant is Azure |
| Kairos row in the `agents` table: `agent_url = wss://websocket-ai-pin-….azurewebsites.net/ws/{user_id}` | `ai_pin_db` (copied from Azure) | The OCI orchestrator dials **Azure** when handing a call to Kairos |

Plan: [WEBSITE_DEPLOYMENT_PLAN_OCI.md](WEBSITE_DEPLOYMENT_PLAN_OCI.md).

### 2.7 Provisioned on Azure but not needed

Present in the resource group, referenced by no live code path, and therefore not part of what must be re-deployed:
Azure OpenAI `aipin-openai-94e8` (`app/agents/openai_client.py` is imported by nothing since PR #49), Web PubSub
`web-pub-sub-ai-pin`, storage account `aipin8880`, two Application Insights components with their alert rules, and
the Log Analytics workspace in `DefaultResourceGroup-WUS`.

---

## 3. Components outside Azure

| Component | Where it runs | What it provides / needs | Evidence |
|---|---|---|---|
| **Google Gemini API** (AI Studio key) | Google | Live API voice model `gemini-3.1-flash-live-preview` for Kairos; text model `gemini-3-flash-preview` for the tool agents and the orchestrator; `google_search` grounding. Needs outbound HTTPS/WSS and `GOOGLE_API_KEY`. Vertex AI is not used. | `app/gemini_config.py:25-29`; `app/agents/gemini_client.py:16` |
| **Firebase Auth**, project `ai-health-assistant-a3e73` | Google | Email/password auth for the mobile app (usernames mapped to `<username>@wanda.com`). Auth only; profiles live in Postgres. | `mobile_app/firebase.json`; `lib/backend/auth_service.dart` |
| **Flutter mobile app** (`com.example.mobile_app`, Android + iOS) | Phones via `flutter run`; no store or CI | Sign-in, tasks, chat, BLE provisioning of the pin. Talks to Firebase, **directly to Postgres**, and to the backend HTTP API for `/tasks` and `/messages` — its hardcoded API base URL is a retired hostname, repointing and repair are deferred because this app is unused and may break. | `mobile_app/lib/utils/task_service.dart:24`; `messaging_service.dart:5`; `lib/backend/database_service.dart` |
| **ESP32-S3 firmware** | the device; **separate repo** | IoT Hub MQTT device `esp32s3`; WebSocket audio client to the backend hostname; BLE provisioning. A changed hardcoded backend hostname or wake-up transport likely requires a firmware update; confirm in that repository. Neither changes during the DB migration. | inferred from `listener/`, `mobile_app/lib/screens/esp_prov_page.dart` |
| **Developer relay agents** | anywhere, typically behind Cloudflare quick tunnels (`*.trycloudflare.com`) | Self-register a public `wss://` URL via `POST /developer/register`, may trigger calls via `POST /developer/ping/{user_id}`, and are dialed by the orchestrator. Needs the backend's register/ping endpoints on a public HTTPS hostname and unrestricted outbound WebSocket egress. | `app/developer_ws/BUILD_SERVICE_PROMPT.md`; `app/routes/agent_routes.py` |
| **Speech-model assets** | fetched at setup from alphacephei.com (Vosk) and huggingface.co/rhasspy (Piper) | ~130 MB on the backend's disk plus the native `libopus` library (`opuslib`); Silero VAD ships inside the pipecat wheel. | `scripts/setup_vosk_model.py`; `scripts/setup_piper_voice.py`; `requirements.txt` |

---

## 4. What each piece needs from a host

| Piece | Functional role | Minimum requirement for any host |
|---|---|---|
| Backend (Kairos + orchestrator + HTTP API + site) | One always-on Python 3.12 process; **long-lived WebSockets** carrying audio (minutes-long sessions); CPU-bound Vosk/Piper/Silero inference; ~130 MB models on disk; `libopus` native lib; public HTTPS/WSS hostname | Always-on container or VM (the README reports significant local inference time, but its shared/burstable B1 explanation is incorrect); a proxy that passes WebSocket upgrades, with tested heartbeats and reconnect handling; persistent or baked-in model files; outbound internet to Gemini and to relay agents; one stable public hostname (firmware, mobile app, registry site and the `agents` table all hardcode it) |
| Database | Postgres 16, 11 small tables, ~9 MB | Any Postgres 16; review/remove Azure-specific extension entries and dependent objects; omit unused `pg_cron` only if appropriate; the unused mobile app may break; defer its API/auth refactor. Keep `agents.agent_url` unchanged during a DB-only move |
| Scheduled job queue | "Deliver this payload at time T" with cancel-by-id and retry; single consumer | Any scheduler primitive with delayed delivery and cancellation (a managed queue with scheduling, or a database table polled by the worker) |
| Queue worker | Small long-running consumer with DB access that emits device wake-ups | Any always-on worker process or serverless trigger bound to the chosen queue |
| Device wake-up channel | Push a small JSON command to one ESP32 over MQTT | Any MQTT broker with TLS and per-device auth, or another push path the firmware can be changed to use; replacing IoT Hub requires firmware work; the PostgreSQL-first migration keeps IoT Hub and firmware unchanged |
| Gemini, Firebase | External SaaS | Unchanged; only outbound access and the existing keys |
| Relay agents | External, developer-run | Public register/ping endpoints and outbound WebSocket egress from the backend |

Cross-cutting notes for the options discussion:

- The backend and the database are in different regions today (West US 2 vs West US 3); eventual colocation removes a
  cross-region hop on every query. PostgreSQL-first temporarily adds a cross-cloud connection, so measure it.
- Secrets are plain environment variables everywhere (`.env` on laptops, App Settings on Azure); nothing depends on
  a vault, so any host with environment-variable injection is sufficient.
- Two hostnames are load-bearing and hardcoded on the client side: the backend's public hostname (firmware, mobile
  app, registry site, `agents` table) and the IoT Hub hostname (firmware). The eventual app/broker move needs coordinated client changes. A DB-only move needs no firmware change, API hostname change or agent URL rewrite. The mobile app may
  break; its API/auth refactor is deferred.
- Orchestrator TTS will be improved for naturalness but stays **self-hosted** (a higher-quality Piper voice, then
  Kokoro-82M behind the existing `synthesize_speech_pcm24_stream` seam), so it remains a CPU-bound, on-disk model
  dependency with no new metered TTS API fee; CPU, memory and latency must be benchmarked — deliberately not moved to a cloud TTS
  (DEPLOYMENT_OPTIONS.md §1.7).

## 5. Corrections that affect deployment decisions

- The remote bridge sends base64 **16 kHz int16 PCM**, not Opus (`app/developer_ws/bridge.py:261`).
  Device audio is decoded/encoded in `app/developer_ws/audio_io.py`; measure both legs before estimating cost.
- Backend and listener DB connections use `DB_*` but do not explicitly configure TLS verification or timeouts
  (`app/database.py`, `listener/database.py`). Add those controls before cross-cloud access.
- The phone creates and reads profiles directly; its schema differs from the local test schema. The live schema
  must be the migration baseline. Do not apply `test/setup_local_postgres.py` to production.
- The listener catches several wake failures. Queue delivery guarantees do not prove successful device delivery.
- Deferred until after migration: registration, website registry CRUD, tasks/messages and WebSockets need an authentication/authorization review;
  protecting just ping/unregister is incomplete. The bridge URL can also disclose `user_id`.
- Existing Docker/compose files are scaffolding; the Dockerfile uses Python 3.11 and omits the complete runtime
  assets. Validate Python 3.12 and ARM64 dependencies before moving the application to A1.

Primary references: [App Service dedicated tiers](https://learn.microsoft.com/en-us/azure/app-service/overview-hosting-plans),
[pg_cron upstream](https://github.com/citusdata/pg_cron),
[Cloudflare WebSocket behavior](https://developers.cloudflare.com/network/websockets/).


# OCI account inventory — historical API snapshot

The following snapshot predates SSH inspection. Later verified host details appear in the PostgreSQL
runbook and application investigation in the OCI plan; its proposed next steps are historical.

# Existing OCI account — read-only inventory

**Later update:** SSH inspection succeeded on 2026-09-10. See
[POSTGRES_MIGRATION_PLAN.md#verified-postgresql-inspection--2026-09-10](POSTGRES_MIGRATION_PLAN.md#verified-postgresql-inspection--2026-09-10): the A1 VM already runs an application stack and
two populated PostgreSQL installations. Statements below that host contents are unknown describe the
earlier API-only snapshot and are superseded by that report.

Inspected 2026-09-09 Pacific time (2026-09-10 UTC), using the local `OCI_REVIEW` session.
No cloud resources were created, stopped, resized, reset or deleted.

## Main finding

**There is already a running A1 VM with 2 OCPUs and 12 GB RAM.** Inspect and consider reusing it before
trying to create another VM. Its capacity is already allocated, so reuse avoids another launch request.
It also uses the full A1 CPU/RAM allocation discussed in the free-tier plan. Actual billable usage depends
on the account's applicable allowances and other resource charges; this inventory is not an invoice.

The VM is not yet confirmed safe to repurpose: OCI metadata does not show its installed applications,
containers, database contents, disk usage or active users. SSH inspection is the next step.

## Account scope

- Region: **San Jose (`us-sanjose-1`)**, the only subscribed region and the home region.
- Availability domains: **one**, `dejv:US-SANJOSE-1-AD-1`. There is no second AD to try here.
- Compartment listing returned no child compartments. Resources inspected below are in the root compartment.
- Budget `ai-pin-free-guard`: $1 monthly amount; reported actual and forecast spend both $0 at inspection.
  This does not establish a spending cap or prove all future usage will be free.

## Compute and attached storage

| Resource | What exists | Recommendation |
|---|---|---|
| `ai-assistant-server` | RUNNING; `VM.Standard.A1.Flex`; 2 OCPUs, 12 GB RAM; Ubuntu 24.04 aarch64 image; created September 1 | First candidate to reuse. Inspect services and data before installing or resetting anything |
| `instance-20260519-2136` | RUNNING; `VM.Standard.E2.1.Micro`; 1 GB RAM; created May 20 UTC | Existing workload unknown; keep until inspected |
| `ai-assistant-server (Boot Volume)` | 47 GB, attached to A1 VM | Keep with the VM |
| `instance-20260519-2136 (Boot Volume)` | 47 GB, attached to Micro VM | Keep with the VM |
| `instance-20260519-1827 (Boot Volume)` | 47 GB, no current boot-volume attachment returned | Potential cleanup candidate; determine contents/recovery value first |

Total boot storage listed: **141 GB**. No separate block data volumes were returned.
An AVAILABLE boot volume is not necessarily unused: the attachment inventory distinguishes the two in use
from the one detached volume.

## Network layout

| Network | Subnets and attachment | Recommendation |
|---|---|---|
| `ai-assistant` (`10.0.0.0/16`) | Public `10.0.0.0/24`, hosts A1; private `10.0.1.0/24` | Reuse candidate for the migration; no need for another VCN if this layout fits |
| `vcn-20260519-1831` (`10.0.0.0/16`) | Public `10.0.0.0/24`, hosts Micro | Keep while the Micro workload remains unknown |

Both VCNs have public-subnet routes to their respective internet gateways. The A1 VCN has IPv4 and IPv6
default internet routes. Its private subnet's route table is empty; it is not an existing Azure-to-OCI
private connection. Both VCNs use overlapping IPv4 ranges, so do not assume they can be privately peered
without a network redesign. Keeping the migration within one VCN avoids that issue.

A1 connection details:

- Public IPv4: `146.235.229.232`, **ephemeral**.
- Private IPv4: `10.0.0.159`.
- Public subnet: `public subnet-ai-assistant`.
- No network security groups attached to the primary VNIC.

Micro connection details:

- Public IPv4: `129.159.34.188`.
- Private IPv4: `10.0.0.54`.
- Public subnet: `subnet-20260519-1831`.
- No network security groups attached to the primary VNIC.

The attached A1 security list permits TCP 22, 80, 443 and 8883 from all IPv4 and IPv6 sources.
The Micro subnet list permits TCP 22, 80 and 443 from all IPv4 sources. These are network permissions,
not proof that those ports have listening services. No PostgreSQL 5432 ingress rule was present in these
lists. Reusing A1 for an Azure-connected database will still require the planned restricted DB route/TLS
setup, or an admin SSH tunnel for initial inspection. Nothing was opened or closed during this review.

The separate `inbound-rules` security list is not referenced by any of the three listed subnets. It is a
possible cleanup candidate, but removing it is optional and does not help with compute capacity.

## Database and backups

- Managed OCI PostgreSQL: the service API returned an empty database-system list.
- Object Storage: no buckets returned in this compartment/region.
- Boot-volume backups: none returned.
- Block-volume backups: none returned.

This does **not** establish that there is no PostgreSQL installation or backup inside the VMs, or in an
external service. Those require host inspection. No files or database rows inside either VM have been read.

## What this changes for the migration

1. Pause new-VM creation attempts while inspecting `ai-assistant-server`.
2. Obtain SSH access using the key already authorized on that VM. OCI API login is separate from SSH login.
3. Read the running services/container names, listening ports, filesystem usage and existing PostgreSQL
   installations. Avoid printing environment files, secrets or container environment variables.
4. If there is a workload or data to keep, back it up and choose a coexistence or separate-host plan.
5. If the VM is suitable, use it as the destination for the trial restore in the migration guide. One-host
   deployment remains an option; do not resize or terminate the existing A1 just to recreate it without a reason.
6. Review the detached boot disk and old Micro workload separately. Neither deletion is required to inspect
   or reuse A1, and neither is authorized by this inventory.

Mobile and application-auth work remain deferred. The database dump/restore, testing and rollback steps
still apply. Selecting this existing VM does not authorize overwriting anything already stored on it.

## Evidence and limits

Read operations: region subscriptions; compartments; structured resource search; compute instances and image;
VNIC and boot attachments; VCNs/subnets/security lists/routes; public IP; boot/block volumes and backups;
Object Storage namespace/buckets; managed PostgreSQL systems; budget; availability domains.

Resource search was cross-checked with service-specific lists for the migration-relevant resources. This is
an infrastructure inventory, not a complete audit of all OCI services, IAM permissions, billing, application
health or data contents. No reset/deletion decision should be based only on a resource's age or name.


# Original document validation — historical review

# Deployment document validation

Reviewed 2026-09-09 against repository HEAD `4b1fc89` and current official vendor documentation. The three inputs were `DEPLOYMENT_AUDIT.md`, `DEPLOYMENT_OPTIONS.md`, and `APP_BACKEND_DEPLOYMENT_PLAN_OCI.md`.

**Follow-up:** the three documents have now been revised to incorporate these findings and retain OCI as the destination, with PostgreSQL first. See [POSTGRES_MIGRATION_PLAN.md](POSTGRES_MIGRATION_PLAN.md) for the detailed first migration. Findings and line numbers below refer to the original versions; this report remains the historical review, not the current execution plan. Vendor claims were researched; live Azure inventory and runtime/load tests remain outstanding. The user has since clarified that the mobile app is unused and may break, and that mobile/auth changes will follow the migration. Recommendations below about making those prerequisites are superseded by that scope; retain the findings as historical analysis.

**Verdict:** the code inventory is broadly sound. The provider comparison and OCI plan contain material factual errors and unsupported estimates; they are not yet a reliable cost forecast or cutover runbook.

## Material findings

### 1. Relay bandwidth and CPU estimates do not describe the implemented protocol

**Options lines 53–76, 96; OCI plan lines 217–219.** The model assumes 0.18 MB/min of Opus on each outgoing leg and a pass-through relay. In `app/developer_ws/bridge.py:261`, the bridge actually sends **base64-encoded 16 kHz, int16 mono PCM in JSON**. `app/developer_ws/audio_io.py` decodes device Opus and encodes returned PCM for the device.

One minute of continuously forwarded uplink is 16,000 × 2 × 60 = **1.92 MB PCM**, or **2.56 MB base64 before JSON**, versus the assumed 0.18 MB. This is a payload calculation, not a measured network bill: speech duty cycle, silence filtering, negotiated WebSocket compression, framing and TLS affect actual transferred bytes. At 120M third-party relay minutes, that continuous-uplink scenario alone is 307.2 TB before transport compression. The published 54 TB total and downstream hosting totals need recalculation from measured traffic. Relay transcoding CPU also needs benchmarking.

The table labels its scenario 20% Kairos but calculates both external legs over all 150M minutes, despite describing Kairos routing as internal. Model the paths separately. NAT is optional with suitable public-IP VM networking, and processes return traffic as well as outgoing traffic; the 27 TB NAT line needs both directions. [Google Cloud NAT pricing](https://cloud.google.com/nat/pricing).

### 2. The stated Cloudflare design cannot carry ordinary MQTT on the free proxy

**OCI plan §§1–2, 3.1, 7.** Raw MQTT/TLS on port 8883 is not an ordinary Cloudflare HTTP proxy service. Choose DNS-only `broker.<domain>` with TLS terminated at the broker, MQTT over WSS with corresponding firmware support, or an appropriate Spectrum plan. The architecture and cost must name the choice. [Cloudflare ports](https://developers.cloudflare.com/fundamentals/reference/network-ports/), [Spectrum](https://developers.cloudflare.com/spectrum/).

The claim that Cloudflare has “no idle cap” is also false. Cloudflare closes idle WebSockets and recommends heartbeat traffic; infrastructure restarts can terminate sessions. Specify heartbeat and reconnect behavior. [Cloudflare WebSockets](https://developers.cloudflare.com/network/websockets/).

### 3. OCI does have first-party text-to-speech

**Options §1.7; OCI plan introduction and §3.2.** Oracle documents OCI Speech synthesis and the `oci speech synthesize-speech` command. Remove the claim that self-hosting is the only option on OCI. Self-hosting can remain a product preference, and third-party TTS APIs are also callable from OCI. [Oracle TTS documentation](https://docs.oracle.com/en-us/iaas/Content/speech/using/using-tts-create.htm).

### 4. DNS rollback alone does not roll back state

**OCI plan §6, especially lines 207–208.** Once OCI accepts writes, Azure's pre-cutover database is stale. Flipping DNS back can lose visibility of new tasks/messages and split writes between databases. Existing WebSockets also survive DNS changes.

Define the authoritative database during the validation window, write fencing, session draining, reverse synchronization or shared-database rollback, and rollback acceptance checks. Include migration/draining of outstanding Service Bus scheduled jobs and cancellation IDs; a DB dump does not copy queued messages. Pause the old consumer before enabling the new one to avoid duplicate wake-ups. Decide whether the broker stays on OCI during application rollback and test that route.

### 5. The security prerequisites leave alternate access paths open

**Options §1.4; OCI plan §4.** Tokens on ping/unregister do not prevent unauthenticated registration from overwriting an existing identity. The website CRUD routes in `app/routes/agent_routes.py` mutate the same registry and also need ownership checks. Moving the phone to the HTTP API must include user authentication and resource authorization for tasks/messages and WebSocket sessions, not just repointing its URL.

Replacing `user_id` in the bridge hello alone does not anonymize a call: `app/developer_ws/pipeline.py:_resolve_bridge_url` also substitutes it into the URL. Define a scoped identity contract for first-party Kairos, which currently needs user identity to access personal data, and a distinct policy for third parties.

OCI NSGs are not a complete metadata-access guard: Oracle explicitly excludes `169.254.0.0/16` from security-rule enforcement. Retain application destination checks and IMDSv2, and specify host/container enforcement for metadata access. [OCI security rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm).

### 6. Vertex AI access is not limited to applications hosted on GCP

**Options §§1.3, 2.2, 2.5; OCI plan §§3.7, 7.** An OCI-hosted application can call Vertex AI with Google Cloud authentication. Choosing OCI does not force the Gemini Developer API or foreclose a Vertex path. Network locality and service eligibility are separate questions. Google explicitly supports authentication from other clouds. [External-workload authentication](https://docs.cloud.google.com/docs/authentication/set-up-adc-on-premises).

BAA coverage must be checked for the exact service, model, preview status and configuration; simply hosting compute on GCP does not establish HIPAA compliance. Replace the secondary blog citations with Google's covered-services guidance and avoid promising that every Live endpoint has all the listed residency/CMEK features. [Google HIPAA guidance](https://cloud.google.com/security/compliance/hipaa).

### 7. Azure B1 and PostgreSQL extension descriptions are wrong

**Audit §2.1; Options §1.2.** App Service Basic is a dedicated-compute tier, not the shared/burstable tier described here. The README repeats this error, so citing it does not validate the claim. Its latency breakdown also includes silence detection and Gemini time, not solely CPU. [Microsoft App Service plans](https://learn.microsoft.com/en-us/azure/app-service/overview-hosting-plans).

**Audit §§2.2, 4; Options §5; OCI plan §3.3.** `pg_cron` is an open-source PostgreSQL extension, not Azure-only. Omit it if unused or unsupported on the destination, but distinguish that decision from removing Azure-specific `azure` and `pgaadauth`. [pg_cron upstream](https://github.com/citusdata/pg_cron).

## Estimates and implementation gaps to label explicitly

- **TTS:** the adapter produces 24 kHz PCM by resampling the model's native output. A voice swap needs the new ONNX file and companion configuration installed, as well as `PIPER_MODEL_PATH`. “$0 marginal” means no metered vendor fee; inference still consumes capacity. The naturalness scores, “only cloud is human,” universal model rankings, and unchanged Kokoro CPU budget are not established by repository evidence or a controlled evaluation. Benchmark on the intended A1 hardware.
- **Scale:** 150M minutes over a 30-day month gives approximately 3,472 average concurrent sessions; 10k peak and 0.03 vCPU/session are assumptions. Routing CPU, database sizing at 1M users, HA cost, and migration duration lack measured support. Today's 9 MB database is not a scale forecast.
- **Latency:** a central-US region does not guarantee ≤40 ms RTT nationwide. Treat this as a target to test across actual device networks.
- **Scheduler:** a jobs table is a reasonable design, but `SKIP LOCKED` alone is not an at-least-once worker. Specify leases/crash recovery, retries, acknowledgement, cancellation races and idempotent wake-ups. The sketch places SQL clauses in the wrong order; `WHERE` precedes `FOR UPDATE SKIP LOCKED`. The current listener catches several delivery failures and returns normally, so queue-level at-least-once delivery does not establish successful device delivery.
- **Portability:** a Dockerfile and compose file already exist, but need substantial updates (Python 3.11 base, missing model/static assets and libopus, incomplete services/config). Validate an ARM64 build and native dependencies before committing to A1; a container image is not automatically architecture-independent.
- **LLM bill:** the Live audio list rates ($3/M input, $12/M output) are supported. The $0.0115/min estimate is conditional on a 50/50 split using rounded per-minute rates; it is not an all-in measured session cost. Include text/context, thinking, tool-agent calls and search grounding where used. [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).
- **Cloud TTS bill:** $25k at $4/M characters and $180k at $30/M imply roughly 6B characters/month, or 200 characters/session at 30M sessions. State and validate that missing assumption; different speech durations materially change the conclusion.
- **Credits and SKU prices:** not all startup-program awards, region-specific smallest-instance prices or discount eligibility were independently validated. Treat these as provisional until checked against official program terms and a dated region-specific estimate.

## Confirmed and unverified inventory

Confirmed in the checkout: base commit; both WebSocket endpoints and shared process; Gemini model IDs; Vosk/Piper/Silero pipeline; registry CRUD and unauthenticated routes; Service Bus schedule/cancel integration; listener's fixed device ID and C2D abstraction; Flutter's direct DB credentials and differing HTTP hostname; existing Docker/compose files. No secret values are reproduced here.

Oracle's current official documentation **does confirm 2 OCPU/12 GB Always Free A1**, so that figure should not be reverted to 4/24. The exact historical change/termination dates and “without announcement” wording were not independently established. [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

The audit's live Azure SKU/runtime versions, regions, deployed settings, last deployment date, database size/table contents/extensions/firewall rules and unused-resource inventory remain **unverified historical observations**. Azure CLI is unavailable in this environment, and no raw dated inspection artifacts accompany the files. Firmware source is in a separate repository and was not available. Hardcoded endpoints in this checkout corroborate dependencies but cannot prove the current deployment or firmware configuration.

No application changes or deployment actions were made. This review used static code inspection, arithmetic and official documentation, not a runtime/load test or live cloud audit.
