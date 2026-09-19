# Scheduler deployment plan and status — OCI

Written 2026-09-11. Replaces audit components 3 (Service Bus `q1`), 4 (Function App `listener`) and 5 (IoT Hub) with a
PostgreSQL job table, a worker container and the Mosquitto broker already running on the VM. Source for most of the
server half: branch [`oracle-deploy`](https://github.com/RishiChandra/agent/tree/oracle-deploy) (commit `0a9ac69`, one
commit on top of `main`'s `14f7e5c`), which built the stale stack deleted on 2026-09-11.

## What the `oracle-deploy` branch gives us

| Take | File | What it is |
|---|---|---|
| Yes | `deploy/sql/001_jobs.sql` | `jobs` table: `id BIGSERIAL` (stored in `tasks.enqueue_sequence_id`), `kind` task/text_message, `payload JSONB`, `deliver_at`, `done_at`, `attempts`; partial index on pending rows |
| Yes | `listener/worker.py` | Poll loop (2 s, batch 10, `FOR UPDATE SKIP LOCKED`), same session-active rule as `function_app.py` (defer 1 min), publishes the wake command, 5 attempts then give up, transport errors retried without spending an attempt |
| Yes | `listener/mqtt_publish.py` | `send_to_device(device_id, payload)` via paho-mqtt to `{prefix}/{device_id}/cmd`, retained, QoS 1; TLS/plaintext auto by port |
| Yes | `app/enqueue/task_enqueue.py`, `edit_task_enqueue.py`, `message_enqueue.py` | Same public functions and return shapes as `main`; `insert_job`/`cancel_job` replace Service Bus schedule/cancel; `sequence_id` = `jobs.id` |
| Yes | `paho-mqtt>=2.1` | The only new dependency |
| Already on VM | `deploy/mosquitto/{mosquitto.conf,acl,passwd,certs/,certsync.sh}` | Broker config: TLS 8883 for the device with Caddy's Let's Encrypt cert (cron copies it every 10 min), plaintext 1883 inside Docker for the worker, `allow_anonymous false`, ACL: backend user `#`, device user `aipin/esp32s3/#` |
| **No** | `app/websocket_handler.py`, `app/main.py` | The branch predates `main`'s goodbye-timing fixes and Silero preload; taking them would regress `main` |
| No | `requirements.txt` pins, `docker-compose.yml`, `deploy/deploy.sh`, `restore_db.sh`, `download_models.sh` | Superseded by `requirements-oci.lock`, `docker-compose.oci.yml` and the current VM layout |
| No | Kairos `agent_url = ws://app:8000/...` rewrite | Written because "OCI's public IPv4 does not hairpin from the VM"; verified 2026-09-11 that it does now (container → public hostname → 200, and a real Kairos handoff succeeded through `wss://146-235-229-232.sslip.io`). Keep the public URL; revisit only if the hostname changes |

## Device contract (from the branch README, unchanged wire protocol)

| Item | Value |
|---|---|
| Broker | `2603-c024-c020-3700-0-537e-9221-8587.sslip.io:8883` (the **v6/AAAA** sslip name — the device is IPv6-only on LTE), TLS with a public Let's Encrypt cert (no custom CA on the device). Server `MQTT_TLS_HOST` was switched to this v6 name on 2026-09-19 so mosquitto presents the matching cert |
| Credentials | username `esp32s3` (`MQTT_DEVICE_USERNAME`), password `MQTT_DEVICE_PASSWORD` from the VM's private env |
| Subscribe | `aipin/esp32s3/cmd`, retained, QoS 1 (a sleeping LTE device gets the last command on reconnect) |
| Payloads | Same JSON IoT Hub C2D sent: `{"command":"start_websocket","reason":"session_inactive",...}` and `{"command":"start_websocket","reason":"text_message","pending_messages":true,...}` |
| WebSocket | unchanged: `wss://<host>/ws/{user_id}` |

**What the branch does not answer: whether the firmware was ever changed to use this.** IoT Hub's device MQTT uses a
different username format, SAS-token auth and the `devices/{id}/messages/devicebound/#` topic, so a firmware change is
required. Evidence it has not happened: Mosquitto logged no device connection in the 7 days before cutover (only internet
scanners), while the device credentials and ACL have existed since 2026-09-01. The firmware repository location is still unknown.

## Steps

### 1. Port the server half onto `codex/oci-application-main`

- [ ] Copy from `origin/oracle-deploy`: `deploy/sql/001_jobs.sql`, `listener/worker.py`, `listener/mqtt_publish.py`,
  `app/enqueue/task_enqueue.py`, `app/enqueue/edit_task_enqueue.py`, `app/enqueue/message_enqueue.py`.
  Diff each enqueue file against `main` first; the branch's versions were written against `main`'s call sites, but confirm
  `task_routes.py`/`task_crud.py`/`messaging_routes.py` and the Gemini create/edit-task tools still see the same return keys.
- [ ] Delete the Azure listener files that no longer have a runtime: `listener/function_app.py`, `iot_hub_mqtt.py`,
  `function.json`, `host.json`, `.funcignore`, `listener/__init__.py` stub. Keep `listener/database.py` and
  `session_management_utils.py` (the worker imports them). Remove `azure-servicebus`/`azure-iot-device` from
  `requirements.txt`, add `paho-mqtt>=2.1`, regenerate `requirements-oci.lock` on the VM (arm64) and rebuild.
- [ ] Dockerfile/.dockerignore: also copy `listener/*.py` into the image (`/app/listener`), nothing else from that directory.
- [ ] `docker-compose.oci.yml`: add a `worker` service on the same image, `command: ["python", "/app/listener/worker.py"]`,
  same `env_file`, networks `default` (DB) + `edge` (broker, `MQTT_HOST=mosquitto`, `MQTT_PORT=1883`), no ports, no models
  mount, `restart: unless-stopped`, small limits (0.25 CPU / 256 MB), read-only root. Add a liveness check later if the loop
  ever wedges (the branch has none).
- [ ] Private env additions (the user runs this; the classifier blocks me from editing the secrets file): copy
  `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_COMMAND_TOPIC_PREFIX`, `DEVICE_ID`, `WORKER_POLL_INTERVAL_SEC` from
  `/home/ubuntu/agent/deploy/.env` into `/home/ubuntu/app-backend-config/backend.env`, plus `MQTT_HOST=mosquitto`,
  `MQTT_PORT=1883`. Do **not** set `AZURE_SERVICEBUS_CONNECTION_STRING`.

**Complete when:** the image builds with the worker entrypoint and `python -c "import paho.mqtt, listener"` style imports pass.

### 2. Schema

- [ ] Apply `001_jobs.sql` to `agent_rehearsal` as `appuser` (owner), then, after Step 3 passes, to `ai_pin_db`.
  The worker also calls `ensure_jobs_table()` at start, so this is belt and braces.
- [x] Decision 2026-09-11: **delete** the 5 past-due tasks carrying Service Bus sequence ids (1588–1598, due May 2026).
  Done in both `ai_pin_db` and `agent_rehearsal` after dump `ai_pin_db-pretaskdelete-<ts>.dump`. Nothing to migrate from `q1`.
  One task created by the user's 2026-09-11 test remains in `ai_pin_db` (due 2026-09-11 14:00 UTC, `enqueue_sequence_id` NULL
  because the app ran without a queue); Step 4 must insert a job for any future task with a NULL sequence id.
- [x] `001_jobs.sql` applied to `agent_rehearsal` as `appuser` (table, sequence and index owned by `appuser`).

### 3. Test on `agent_rehearsal` with a fake device

- [ ] Point the app + worker at `agent_rehearsal` (`DB_NAME`), start both.
- [ ] On the VM, subscribe as the device: `mosquitto_sub` (from the `eclipse-mosquitto` image) to `aipin/esp32s3/cmd` on
  `8883` with the device credentials, TLS verified against the system CAs. This stands in for the firmware.
- [ ] `POST /tasks` with `time_to_execute` 1 minute ahead → `jobs` row with `deliver_at`; `tasks.enqueue_sequence_id` = its id.
  After the minute: worker log shows the claim and publish; the subscriber prints `{"command":"start_websocket","reason":"session_inactive",...}`;
  `done_at` set.
- [ ] Session-active rule: set the user's `sessions.is_active` true, repeat → worker defers by 1 minute, no publish; clear it → publish.
- [ ] Edit (`PUT /tasks` with `reenqueue`) cancels the old job and inserts a new one; `DELETE` cancels.
- [ ] `POST /messages` → `text_message` job 1 minute later → wake with `pending_messages: true`; dedupe (second message
  while one is pending does not add a job).
- [ ] Failure path: stop Mosquitto briefly → job retried without spending an attempt; restart → delivered.
- [ ] Record memory/CPU of the worker.

**Complete when:** every row above passes and the test rows are removed.

### 4. Go live on `ai_pin_db`

- [ ] Apply the schema to `ai_pin_db`, flip `DB_NAME`, `compose up -d`. Reminders now work for any device subscribed to Mosquitto.
- [ ] Update the [app plan component map](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md#component-map-everything-in-the-audit-moves-to-oci) rows 3–4 to done.

### Public-path proof (2026-09-11 07:55 UTC, no device needed)

A `mosquitto_sub` stand-in using the **device** credentials connected to `146-235-229-232.sslip.io:8883` with the TLS chain
verified against the system CA store (certificate `CN=146-235-229-232.sslip.io`, Let's Encrypt, valid to 2026-11-30); a
`mosquitto_pub` with the backend credentials over the same public port delivered `{"command":"start_websocket",...}` to it;
a publish by the device user outside `aipin/esp32s3/#` was dropped by the ACL; no retained test payload was left on the topic.
Everything the chip needs to do is therefore exercised end to end except the chip itself.

### 5. Device

**Firmware repo:** github.com/itismejy/ai_pin, branch `modem-lte` (ESP-IDF, ESP32-S3, SIM7670G LTE). Investigated 2026-09-19.

Finding: the firmware was **outbound-call-only** — a WSS session opened only on an ACTION button hold; it shipped **no MQTT
client at all** (verified: `main/idf_component.yml` has no esp-mqtt; three adversarial searches found no server-reachable wake
path). So the deployed scheduler reached nothing. The device is on **IPv6-only** T-Mobile PPP, so it dials the v6 sslip name
(as WSS already does). On this hardware deep sleep saves almost nothing (a ~50 mA R_DUMMY board floor), so keeping the modem
attached for a persistent subscription costs essentially nothing extra — a straight MQTT push is the simplest fit.

- [x] **Firmware written** (branch `modem-lte-mqtt-wake`, patch delivered): `main/net/wake_mqtt.c` — persistent TLS MQTT
  subscriber to `aipin/esp32s3/cmd` (QoS 1, cert via `esp_crt_bundle`); on `{"command":"start_websocket"}` it signals the
  existing call task, placing the same call the ACTION button does (IDLE only; a wake mid-call is dropped). Acts only on LIVE
  messages — a retained replay on reconnect is ignored as stale. Creds in NVS via a new `mqtt_set` console command.
- [x] **Server cert fixed:** mosquitto now presents the **v6** Let's Encrypt cert (VM `MQTT_TLS_HOST` → v6 name + certsync);
  the device's TLS verification of the v6 host now passes. Broker already listens on `[::]:8883`; ip6tables allows it.
- [x] **End-to-end verified 2026-09-19** with a TLS-verified IPv6 `mosquitto_sub` standing in for the device: a task created
  via the live public app fired through the worker and the wake arrived **live** on `aipin/esp32s3/cmd` ~72 s later (a 70 s
  scheduled delay, i.e. on time). Payload exactly what the firmware parses.
- [ ] **Flash the device** (UART0 only — USB-C is power-only on this board), then `mqtt_set <MQTT_DEVICE_PASSWORD>` on the
  serial console (the password from the VM's `deploy/.env`, never in source), reboot. Read the device password (username
  `esp32s3`; the broker ACL restricts it to `aipin/esp32s3/#`) straight from the VM so it never lands in a doc or chat:

  ```sh
  ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232 'grep ^MQTT_DEVICE_PASSWORD= /home/ubuntu/agent/deploy/.env'
  ```

  (The backend/worker uses the separate `MQTT_USERNAME`/`MQTT_PASSWORD` in the same file — full-topic access — which the device must NOT use.)
- [x] **Flashed and provisioned** 2026-09-19: the pin connected to mosquitto as `esp32s3` over TLS 1.2 (broker log
  `New client connected ... as esp32s3 (k60)`), and correctly ignored the retained wake on subscribe (no spurious call).
- [x] **On-device field test PASSED 2026-09-19 ~08:47 UTC** with the real cellular pin: a reminder created via the live
  public app for the device's user (`4dd16650…`, session inactive), due +60 s, fired on time — worker published the wake at
  08:47:52, and the physical pin opened `/ws/developer/4dd16650…` at 08:47:54 (app log `connected user_id=4dd16650…`).
  Wake→call-in ~2–3 s (LTE + TLS + WSS); job marked done, 0 retries. Complete path proven: OCI scheduler → Mosquitto →
  cellular ESP32 → WSS back into the app.
- [ ] Retire Azure Service Bus `ai-pin`, Function App `listener` (+ storage `aipin93a7`), IoT Hub `ai-pin-iot-hub` — now
  unblocked; do it after the [before-go-live checklist](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md#before-the-device-goes-live-on-oci)
  (off-VM backups, reboot check, stable hostname).

**Known caveats to carry:** (1) push only works while the pin is powered with the modem attached — it does NOT survive
`AT+CPOF`/deep-sleep standby (a poll-on-RTC-timer variant is the fallback if standby-through-reminders is ever needed);
(2) the `jobs` table keeps delivered rows (`done_at` set) — harmless, but add a periodic prune if it grows;
(3) the broker leaves the last wake **retained** on the topic; the firmware ignores retained, but clear it if you ever want a
clean topic.

**Blocked on:** firmware repo location and its current transport. Nothing in Steps 1–4 depends on it.

### Coupled to the stable-address step

`MQTT_TLS_HOST` and the certificate Mosquitto presents follow the public hostname. When the IP/hostname changes, update
`MQTT_TLS_HOST` (and `SITE_HOST_*`) in the old `deploy/.env`, let Caddy issue the new cert, `certsync.sh` copies it, and the
firmware's broker hostname changes with it. Do the hostname change before flashing the device.

## Status

- [x] Server-half source identified and reviewed (branch `oracle-deploy`).
- [x] Firmware wake path written (github.com/itismejy/ai_pin branch `modem-lte-mqtt-wake`); server cert switched to v6.
- [x] **Full chain LIVE 2026-09-19:** flashed to the real pin, and a live scheduler reminder woke the cellular ESP32 on time
  (it opened `/ws/developer/…` ~2–3 s after the wake publish). Components 3+4+5 are done on OCI.
- [x] Device MQTT contract documented; firmware status unknown.
- [x] Step 1 (2026-09-11 UTC): code ported to the branch (six files from `oracle-deploy`; Azure listener files and packages
  removed; `paho-mqtt==2.1.0` in requirements and lock; `listener/*.py` baked into the image; `worker` service in
  `docker-compose.oci.yml`). Image `codex-app-backend:step4-94fa561471da` built; no Azure packages inside; worker imports pass.
  **Two fixes to `main` found by the tests:** the HTTP `PUT /tasks/{user}/{id}` never passed `reenqueue=True` and
  `DELETE` never cancelled the queued job (only the voice edit tool did), so an edited or deleted task would still wake the
  device at the old time. Fixed in `app/routes/task_routes.py`; and `listener/worker.py` now drops a task job whose task was
  deleted or is no longer `pending` as a safety net.
- [x] Step 2: schema on `agent_rehearsal` and on `ai_pin_db` (owner `appuser`); stale tasks deleted.
- [x] Step 3 (2026-09-11 07:15 UTC): fake-device suite `sched_test.sh` (kept in the VM build directory), 20/20 checks:
  task → job → worker → `mosquitto_sub` as `esp32s3` received `start_websocket/session_inactive` with the task as `system_message`;
  active session defers by 1 min then delivers when inactive; edit cancels the old job and inserts a new one; delete cancels;
  text message → one `text_message` job (+1 min), second message deduped, device received `pending_messages: true`;
  broker stopped → job stays pending with `attempts=0`, delivered after broker restart. Worker 18 MiB / idle CPU.
  Test containers and rows removed; the retained command on the topic was cleared so no real device sees test payloads.

- [x] Step 4 (2026-09-11 07:45 UTC): **live.** The user appended the five MQTT/worker lines to the private env; `compose up -d`
  with `codex-app-backend:step4-94fa561471da` started `app-backend-worker-1` (networks `app-backend` + `aipin_default`,
  `mosquitto:1883`, device `esp32s3`, poll 2 s) and recreated the app on the same image; public `/healthz` 200 throughout.
  The one future task ("brush my teeth", due 2026-09-11 14:00 UTC) was re-enqueued through `PUT /tasks` and now has job #1.
  The worker service has `healthcheck: disable: true` because the image's HTTP probe does not apply to it.
  Reminders now work for any device subscribed to Mosquitto; the real device is still on IoT Hub (Step 5).
- [x] Step 5: flashed + on-device field test PASSED 2026-09-19 (real pin woke on time from a live reminder).
- [ ] Retire Azure queue/listener/IoT Hub, after the before-go-live checklist.

Operational notes: worker logs via `docker logs app-backend-worker-1`; pending work via `SELECT * FROM jobs WHERE done_at IS NULL`;
a stuck job can be re-armed with `UPDATE jobs SET deliver_at = now() WHERE id = …`. `deploy/sql/001_jobs.sql` is not in the image
(the worker logs that it assumes the table exists); apply it by hand for any new database.
