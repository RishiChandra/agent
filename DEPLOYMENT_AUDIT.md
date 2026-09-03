# Deployment Audit — what needs to be deployed

Audit date: 2026-09-02
Repo: `RishiChandra/agent`, `main` @ `4b1fc89`
Current host: Azure, subscription **Microsoft Azure Sponsorship**, resource group **`ai-pin`**

Purpose: enumerate every component the running system depends on, what each one does, and what it needs from a
host, so deployment options can be compared against a complete list. Facts come from the code (cited as
`file:line`) and from read-only inspection of the live Azure subscription and public endpoints. No secret values
are reproduced.

---

## 1. What is deployed and needed

The product is a voice assistant for an ESP32-S3 "pin" device plus a Flutter phone app. Everything server-side that
this repo deploys runs on Azure, in one resource group, deployed **manually from a developer laptop** (no CI/CD).

The backend hosts **two distinct WebSocket servers** in one process: **Kairos** (`/ws/{user_id}`, the Gemini Live
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

Outside Azure but required (details in §3): Google Gemini API, Firebase Auth, the Flutter mobile app, the ESP32
firmware (separate repo), third-party relay agents reachable over public WebSocket, and ~130 MB of speech-model
assets on the backend's disk.

---

## 2. Component details

### 2.1 Backend — App Service `websocket-ai-pin`

| Item | Value | Evidence |
|---|---|---|
| Resource | Linux App Service, plan `ASP-aipin-9950` **B1 Basic (1 shared vCPU, 1 worker)**, `PYTHON\|3.12`, Always On, WebSockets on, HTTPS-only, no managed identity, no custom domain | `azure-deploy.sh:10-15`; `az webapp show` |
| Hostname | `websocket-ai-pin-fbbrhfawfkb7ecf3.westus2-01.azurewebsites.net` — **hardcoded in the device firmware, the test clients, the Agent Registry site, and the Kairos row of the `agents` table** | `README.md` "URLs"; `agent_directory/index.html:87`; `test/app/test_ws.py:104` |
| Two WebSocket servers | **Kairos** at `/ws/{user_id}` (`app/websocket_handler.py`): Gemini Live agent + the `app/agents/` tool agents, Opus downlink; answers the bridge handshake as `service_id: "kairos"`. **Orchestrator** at `/ws/developer/{user_id}` (`app/developer_ws/`): Pipecat pipeline with Silero VAD → Vosk STT → Gemini text → Piper TTS, plus the `start_remote_audio_bridge` tool that dials an outbound WebSocket to whatever URL the `agents` table holds for the named agent (Kairos included). Distinct agents and wire protocols. | `app/main.py:90-91`; `app/websocket_handler.py:133`; `app/developer_ws/pipeline.py:116-135`; `app/agents_registry.py` |
| HTTP API | tasks (`/tasks…`, `/enqueue-task`), messaging (`/messages`, `/messages/enqueue`), agent registry (`/api/agents…`, `/developer/register`, `/developer/unregister`), `/developer/ping/{user_id}`, `/healthz`; static Agent Registry site at `/` from `agent_directory/`. No authentication; CORS `*`. | `app/routes/*.py`; `app/main.py` |
| Startup | inline command installs `libopus0` via apt on every cold start, then `uvicorn main:app --port 8000 --ws websockets` (the repo's `startup.sh` is unused) | `azure-deploy.sh:30`; live `appCommandLine` |
| Persistent assets | `/home/data/vosk-model-small-en-us-0.15/` (~68 MB) and `/home/data/piper_voices/en_US-amy-medium.onnx` (+`.json`, ~63 MB), uploaded once via Kudu; paths passed as `VOSK_MODEL_PATH` / `PIPER_MODEL_PATH`. Both servers preload them at startup. | `README.md` "One-time bootstrap"; `app/main.py` lifespan |
| Deploy process | zip of `app/`, `agent_directory/`, `requirements.txt` → `az webapp deploy --type zip` → Oryx builds `requirements.txt` on the server (pipecat pulls ~500 MB of wheels; 5–10 min builds). Last deployed 2026-09-02. | `azure-deploy.sh:159-215` |
| Settings the code needs | `GOOGLE_API_KEY` (optionally `GEMINI_API_KEY`, `GEMINI_TEXT_MODEL`), `DB_HOST/PORT/NAME/USER/PASSWORD`, `AZURE_SERVICEBUS_CONNECTION_STRING`, `VOSK_MODEL_PATH`, `PIPER_MODEL_PATH`, tuning knobs `DEVELOPER_WS_*` (deploy script pins `END_SILENCE_SEC=1.0`, `SILERO_STOP_SECS=0.8`), `WEBSITES_PORT=8000`. Also set but **unused by this code**: `AZURE_OPENAI_*`, `IOT_HUB_CONNECTION_STRING`. | `azure-deploy.sh:134-157`; grep of `os.environ` under `app/` |
| Outbound dependencies | Gemini API (Live + `generateContent`, API key), Postgres, Service Bus (schedule / cancel only), outbound WebSockets to registered agents | `app/gemini_config.py`; `app/database.py`; `app/enqueue/*`; `app/developer_ws/bridge.py` |

### 2.2 Database — PostgreSQL Flexible Server `ai-pin-server`

| Item | Value |
|---|---|
| Server | `ai-pin-server.postgres.database.azure.com`, **West US 3** (different region from the app), PostgreSQL **16.14**, **Standard_B1ms Burstable**, 32 GB, 7-day backups, no HA |
| Network | Public access enabled; firewall rule allows **every IP** (`0.0.0.0`–`255.255.255.255`), required because the mobile app connects directly |
| Database in use | the default `postgres` database, one non-superuser SQL role. Size ≈ 9 MB |
| Tables (live, `public`) | `users`, `chats`, `chat_members`, `relationships`, `sessions`, `tasks`, `messages`, `pending_text_message_jobs`, `agents`, plus `agent_registry` and `agent_tasks` (referenced by no code; created by hand) |
| Extensions | `azure`, `pgaadauth`, `pg_cron` (no jobs defined), `plpgsql`. The first three are Azure-only and must be stripped from any dump moved elsewhere |
| Schema management | None. `agents` is auto-created by the backend at startup (`app/agents_registry.py:ensure_agents_table`); `users` is created by the **mobile app** at launch (`mobile_app/lib/backend/database_service.dart`); the rest by hand. `test/setup_local_postgres.py` is the closest schema reference |
| Direct clients | (1) backend via psycopg2; (2) Function App via psycopg2 (`listener/database.py`); (3) **the Flutter app from the phone**, with host/user/password compiled in (`mobile_app/lib/backend/database_service.dart:29-33`); (4) developers via pgAdmin / test scripts |
| Data of note | `agents` holds the Kairos row (`agent_url` = the backend's `/ws/{user_id}` on the current hostname) plus six placeholder rows |

### 2.3 Scheduled job queue — Service Bus `ai-pin`, queue `q1`

| Item | Value |
|---|---|
| Namespace | `sb://ai-pin.servicebus.windows.net`, **Basic** tier, West US; one queue **`q1`** (1 GB, lock 1 min, TTL 14 d, max delivery 10); no topics |
| Auth | `RootManageSharedAccessKey` connection string, shared by the backend, the Function App, and test scripts |
| Producers | `app/enqueue/task_enqueue.py` (`schedule_messages` at `time_to_execute`; stores the returned sequence number in `tasks.enqueue_sequence_id`), `app/enqueue/edit_task_enqueue.py` (`cancel_scheduled_messages` by that number, then re-schedule), `app/enqueue/message_enqueue.py` (`text_message` job +1 min, de-duplicated per user via `pending_text_message_jobs`), the listener itself (re-schedules +1 min to defer), `listener/testing/quick_enqueue.py` |
| Consumer | only `listener` / `QueueWorker` |
| Primitives the code relies on | schedule-at-time returning a cancellable id, cancel-by-id, at-least-once delivery to a single worker |

### 2.4 Queue worker — Function App `listener`

| Item | Value |
|---|---|
| Resource | Linux Function App, **Flex Consumption** plan `ASP-aipin-9a49`, Python **3.13**, 2048 MB instances, max 100. Backing storage account `aipin93a7` (host storage + deployment package) |
| Deploy process | `func azure functionapp publish listener --python` from `listener/` (Core Tools 4.x). Settings were entered by hand in the portal |
| Code | Python v2 model `listener/function_app.py`: `serviceBusTrigger` on `q1`. (`listener/__init__.py` + `function.json` are stale v1 leftovers.) Dependencies: `azure-functions`, `azure-servicebus`, `psycopg2-binary`, `azure-iot-device`, `requests` |
| Behavior | per message: parse JSON → look up the user's `sessions` row → if `is_active`, re-schedule the same body +1 min → else send IoT Hub cloud-to-device `{"command":"start_websocket", "reason":"session_inactive", …}` to device `esp32s3`; for `text_message` jobs also read unread messages and send a second wake with `reason: text_message`. Marking messages read is done by the backend when the device connects |
| Settings the code needs | `AZURE_SERVICEBUS_CONNECTION_STRING`, `DB_HOST/PORT/NAME/USER/PASSWORD`, `IOT_HUB_SERVICE_CONNECTION_STRING` (C2D via IoT Hub HTTPS REST; the `azure-iot-hub` SDK is deliberately not installed). Functions-host settings: `AzureWebJobsServiceBus`, `AzureWebJobsStorage`, `DEPLOYMENT_STORAGE_CONNECTION_STRING` |

### 2.5 Device wake-up channel — IoT Hub `ai-pin-iot-hub`

| Item | Value |
|---|---|
| Resource | **F1 free tier**, 1 unit, West US 2, `ai-pin-iot-hub.azure-devices.net` |
| Device | single identity **`esp32s3`** (hardcoded `DEFAULT_DEVICE_ID`, `listener/iot_hub_mqtt.py:41`) |
| Usage | **only** cloud-to-device JSON commands from the listener. Nothing consumes device-to-cloud telemetry |
| Code outside this repo that depends on it | the ESP32-S3 firmware: IoT Hub MQTT device client (C2D), WebSocket audio client to the backend hostname, BLE provisioning target for the mobile app (`mobile_app/lib/screens/esp_prov_page.dart`, `flutter_esp_ble_prov`) |

### 2.6 Provisioned on Azure but not needed

Present in the resource group, referenced by no live code path, and therefore not part of what must be re-deployed:
Azure OpenAI `aipin-openai-94e8` (`app/agents/openai_client.py` is imported by nothing since PR #49), Web PubSub
`web-pub-sub-ai-pin`, storage account `aipin8880`, two Application Insights components with their alert rules, and
the Log Analytics workspace in `DefaultResourceGroup-WUS`.

---

## 3. Components outside Azure

| Component | Where it runs | What it provides / needs | Evidence |
|---|---|---|---|
| **Google Gemini API** (AI Studio key) | Google | Live API voice model `gemini-3.1-flash-live-preview` for Kairos; text model `gemini-3-flash-preview` for the tool agents and the orchestrator; `google_search` grounding. Needs only outbound HTTPS and `GOOGLE_API_KEY`. Vertex AI is not used. | `app/gemini_config.py:25-29`; `app/agents/gemini_client.py:16` |
| **Firebase Auth**, project `ai-health-assistant-a3e73` | Google | Email/password auth for the mobile app (usernames mapped to `<username>@wanda.com`). Auth only; profiles live in Postgres. | `mobile_app/firebase.json`; `lib/backend/auth_service.dart` |
| **Flutter mobile app** (`com.example.mobile_app`, Android + iOS) | Phones via `flutter run`; no store or CI | Sign-in, tasks, chat, BLE provisioning of the pin. Talks to Firebase, **directly to Postgres**, and to the backend HTTP API for `/tasks` and `/messages` — its hardcoded API base URL is a retired hostname, so it must be repointed at whatever hosts the HTTP API. | `mobile_app/lib/utils/task_service.dart:24`; `messaging_service.dart:5`; `lib/backend/database_service.dart` |
| **ESP32-S3 firmware** | the device; **separate repo** | IoT Hub MQTT device `esp32s3`; WebSocket audio client to the backend hostname; BLE provisioning. Any change of backend hostname or of the wake-up transport requires a firmware update. | inferred from `listener/`, `mobile_app/lib/screens/esp_prov_page.dart` |
| **Developer relay agents** | anywhere, typically behind Cloudflare quick tunnels (`*.trycloudflare.com`) | Self-register a public `wss://` URL via `POST /developer/register`, may trigger calls via `POST /developer/ping/{user_id}`, and are dialed by the orchestrator. Needs the backend's register/ping endpoints on a public HTTPS hostname and unrestricted outbound WebSocket egress. | `app/developer_ws/BUILD_SERVICE_PROMPT.md`; `app/routes/agent_routes.py` |
| **Speech-model assets** | fetched at setup from alphacephei.com (Vosk) and huggingface.co/rhasspy (Piper) | ~130 MB on the backend's disk plus the native `libopus` library (`opuslib`); Silero VAD ships inside the pipecat wheel. | `scripts/setup_vosk_model.py`; `scripts/setup_piper_voice.py`; `requirements.txt` |

---

## 4. What each piece needs from a host

| Piece | Functional role | Minimum requirement for any host |
|---|---|---|
| Backend (Kairos + orchestrator + HTTP API + site) | One always-on Python 3.12 process; **long-lived WebSockets** carrying audio (minutes-long sessions); CPU-bound Vosk/Piper/Silero inference; ~130 MB models on disk; `libopus` native lib; public HTTPS/WSS hostname | Always-on container or VM (B1-class CPU was already a latency bottleneck per `README.md` "Improvements"); a proxy that passes WebSocket upgrades and imposes no idle timeout; persistent or baked-in model files; outbound internet to Gemini and to relay agents; one stable public hostname (firmware, mobile app, registry site and the `agents` table all hardcode it) |
| Database | Postgres 16, 11 small tables, ~9 MB | Any Postgres 16; strip `azure` / `pgaadauth` / `pg_cron` from the dump; re-point `agents.agent_url`; either keep a public endpoint for the mobile app's direct connection or move the app onto the HTTP API |
| Scheduled job queue | "Deliver this payload at time T" with cancel-by-id and retry; single consumer | Any scheduler primitive with delayed delivery and cancellation (a managed queue with scheduling, or a database table polled by the worker) |
| Queue worker | Small long-running consumer with DB access that emits device wake-ups | Any always-on worker process or serverless trigger bound to the chosen queue |
| Device wake-up channel | Push a small JSON command to one ESP32 over MQTT | Any MQTT broker with TLS and per-device auth, or another push path the firmware can be changed to use; **every option here implies a firmware change** |
| Gemini, Firebase | External SaaS | Unchanged; only outbound access and the existing keys |
| Relay agents | External, developer-run | Public register/ping endpoints and outbound WebSocket egress from the backend |

Cross-cutting notes for the options discussion:

- The backend and the database are in different regions today (West US 2 vs West US 3); colocating them removes a
  cross-region hop on every query.
- Secrets are plain environment variables everywhere (`.env` on laptops, App Settings on Azure); nothing depends on
  a vault, so any host with environment-variable injection is sufficient.
- Two hostnames are load-bearing and hardcoded on the client side: the backend's public hostname (firmware, mobile
  app, registry site, `agents` table) and the IoT Hub hostname (firmware). A move implies coordinated firmware and
  mobile-app updates.
