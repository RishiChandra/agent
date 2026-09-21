# Scheduler (reminders): jobs table + worker + MQTT wake

Delivers scheduled reminders and pushed text-messages to the device. This replaced three Azure services — Service Bus queue
`q1`, Function App `listener`, and IoT Hub — with a PostgreSQL `jobs` table, a worker container, and the Mosquitto broker.
The device firmware wake path is documented here too, since it is the delivery end. Infrastructure basics:
[OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

## End-to-end flow

```
user schedules a task (voice via Kairos, or POST /tasks)
   → app/enqueue/*.py inserts a row in the `jobs` table (deliver_at = when)
   → worker (listener/worker.py) polls every 2s; at deliver_at, if the user's session is inactive,
     publishes {"command":"start_websocket", ...} to Mosquitto topic aipin/esp32s3/cmd (retained, QoS 1)
   → the chip (subscribed over TLS) receives it and opens a WSS call to /ws/developer/{user_id}
```

Verified live 2026-09-19: a reminder due +60 s fired on time; the physical cellular pin woke and called in ~2–3 s after the
worker's publish.

## `jobs` table

`deploy/sql/001_jobs.sql` (owner `appuser`). Columns: `id BIGSERIAL` (stored back into `tasks.enqueue_sequence_id`), `kind`
(`task` | `text_message`), `payload JSONB` (the wake body), `deliver_at`, `created_at`, `done_at` (NULL = pending), `attempts`.
Partial index on pending rows by `deliver_at`. The worker also calls `ensure_jobs_table()` at startup, so a fresh DB
self-provisions; apply the SQL by hand for any brand-new database.

The enqueue code lives in `app/enqueue/{task_enqueue,edit_task_enqueue,message_enqueue}.py`: `insert_job` / `cancel_job`
replace the old Service Bus schedule/cancel, and return the `jobs.id` as `sequence_id`. `PUT /tasks` re-enqueues on edit and
`DELETE` cancels the job (both fixed during migration — previously only the voice edit tool kept the queue in sync).

## Worker

`app-backend-worker-1`, same image as the app, `command: ["python","/app/listener/worker.py"]`. Defined in
`docker-compose.oci.yml`; joins `app-backend` (DB) + `aipin_default` (broker). Behavior (`listener/worker.py`): poll every 2 s,
claim due rows with `FOR UPDATE SKIP LOCKED`, and per job — if the user's `sessions.is_active` is true, defer 1 minute;
otherwise publish the wake and (for `text_message`) also send a `pending_messages` wake. 5 attempts then give up; **transport
errors** (broker down) are retried without spending an attempt, so a wake is never lost to a momentary broker outage. A task
job whose task was deleted or is no longer `pending` is dropped (safety net).

> The worker service sets `healthcheck: disable: true` in compose — it does not serve `/healthz`, so the image's HTTP probe
> doesn't apply to it.

Env it needs (in `backend.env`): `DB_*`, `MQTT_HOST=mosquitto`, `MQTT_PORT=1883`, `MQTT_USERNAME`/`MQTT_PASSWORD` (backend,
full-topic), `MQTT_COMMAND_TOPIC_PREFIX=aipin`, `DEVICE_ID=esp32s3`, `WORKER_POLL_INTERVAL_SEC`.

## Mosquitto broker

`aipin-mosquitto` (`eclipse-mosquitto:2`). Config under `/home/ubuntu/agent/deploy/mosquitto/`:
- Two listeners: **`:8883` TLS** (device-facing, public) and **`:1883` plaintext** (worker-facing, internal to
  `aipin_default` only — not published to the host).
- `allow_anonymous false`; `passwd` + `acl`. ACL: backend user → all topics; device user `esp32s3` → `aipin/esp32s3/#` only.
- **TLS cert:** `certsync.sh` (root cron, every 10 min) copies the Let's Encrypt cert Caddy holds for `MQTT_TLS_HOST` into the
  broker and `SIGHUP`s it. **`MQTT_TLS_HOST` was set to the IPv6 sslip name** (`2603-c024-c020-3700-0-537e-9221-8587.sslip.io`)
  on 2026-09-19, because the device is IPv6-only and must verify the v6 hostname. Broker listens on `[::]:8883` and ip6tables
  allows it.

## Device firmware wake path

The device is an **ESP32-S3** on **SIM7670G LTE** (repo: `github.com/itismejy/ai_pin`, ESP-IDF). Before migration it was
outbound-call-only with **no MQTT client** — the scheduler reached nothing. The wake subscriber was added on branch
**`modem-lte-mqtt-wake`**:

- `main/net/wake_mqtt.c` — persistent TLS MQTT subscriber (esp-mqtt, cert via `esp_crt_bundle`) to `aipin/esp32s3/cmd`
  (QoS 1). On `{"command":"start_websocket"}` it signals the existing call task (`on_server_wake` in `main.c`), placing the
  same call the ACTION button does — **from IDLE only** (a wake mid-call is dropped, never a hang-up).
- **Dials the v6 sslip name** for the broker (LTE is IPv6-only), matching the cert switch above.
- **Retained handling:** the broker publishes retained (a reconnecting device gets the last command). The firmware **acts only
  on live deliveries** (retain flag clear) and ignores retained replays — firing a reminder hours late is worse than dropping
  it. Correct under the "modem attached, device powered" assumption (the pin is always subscribed, so the fire-time publish
  arrives live).
- **Credentials in NVS** via a `mqtt_set` console command (`mqtt_pass` required; `mqtt_url`/`mqtt_user`/`mqtt_topic` optional,
  with compiled defaults). With no password set the module no-ops and the unit is a manual-call device.

### Flashing / provisioning a device

Flash the branch (UART0 only — USB-C is power-only), then on the `ai_pin>` serial console set the device MQTT password and
reboot:

```
mqtt_set <MQTT_DEVICE_PASSWORD>
```

Read `MQTT_DEVICE_PASSWORD` (device user `esp32s3`, ACL-scoped) from the VM — **not** the backend `MQTT_PASSWORD`:

```sh
ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232 'grep ^MQTT_DEVICE_PASSWORD= /home/ubuntu/agent/deploy/.env'
```

Expect the log `wake_mqtt: subscribed, waiting for wake commands`, and the broker log `New client connected … as esp32s3`.

### Power model caveat

This is a **push** design: it only works while the pin is powered with the modem attached. It does **not** survive
`AT+CPOF`/deep-sleep standby (modem off, USB host down). On this hardware deep sleep saves almost nothing anyway (a ~50 mA
R_DUMMY board floor), so keeping the modem attached is essentially free. If standby-through-reminders is ever required, the
fallback is a poll-on-RTC-timer variant (device pulls the jobs table on a timer) — larger firmware + a new server endpoint.

## Testing the path

- **Without a device:** subscribe with `mosquitto_sub` as `esp32s3` over the v6 name:8883 (TLS verified against system CAs),
  create a task due +1 min via the app for a user whose `sessions.is_active` is false, and watch the wake arrive.
- **With the device:** create a reminder, watch `docker logs app-backend-worker-1` for `sent start_websocket`, then
  `docker logs app-backend-app-1` for `connected user_id=…` (the chip calling in).

## TODOs

- [ ] **Announce the triggering task on a woken call.** The pin wakes on time, but the orchestrator says nothing about the
  reminder. Two-sided gap: (a) the firmware drops the wake's `system_message` — `wake_mqtt.c` parses only `command`/`reason`
  and `on_server_wake` fires a plain call; (b) the `/ws/developer` orchestrator has no announce path (its receive loop handles
  only `interrupt`/`audio`/`turn_complete`; the "fetch task and tell the user" logic exists only in the legacy `/ws/` handler,
  `app/websocket_handler.py:235-267`). **Fix:** add a `SpeechPipeline.on_pending_task()` + a `pending_task` branch in
  `app/developer_ws/endpoint.py` (server, no reflash), and have the firmware relay the wake's `system_message` as the first WS
  frame after connect (needs a reflash).
- [ ] **Task-owner identity mismatch.** In the 2026-09-19 test the task was created under user `2ba330c0` while the device
  connects as its compiled identity `4dd16650`. The wake still worked (topic is device-fixed), but the session-active defer
  check and task ownership key off the wrong user. Align Kairos task creation with the id the device authenticates as.
- [ ] **`jobs` table grows** — delivered rows keep `done_at` set and are never removed. Add a periodic prune if it grows.
- [ ] **Retained topic housekeeping** — the broker keeps the last wake retained on `aipin/esp32s3/cmd`. The firmware ignores
  retained, so it's harmless, but clear it (`mosquitto_pub … -r -n`) if you want a clean topic.
- [ ] **Coupled to the stable-address hardening item:** `MQTT_TLS_HOST` and the cert follow the public hostname; changing the
  address means re-issuing the cert and reflashing the device to the new broker host. Do the hostname change before wider use.
