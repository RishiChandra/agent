# Deployment Plan — OCI (portable-by-construction)

Date: 2026-09-08
Repo: `RishiChandra/agent`, target base `main` @ `4b1fc89`
Depends on: [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md) (what must be deployed) and
[DEPLOYMENT_OPTIONS.md](DEPLOYMENT_OPTIONS.md) (why; cost/latency/portability model).

**Decision this plans for:** host the orchestrator stack on **Oracle Cloud (OCI)**, built so that **any future move
of any component is as cheap as possible** — ideally a Terraform re-apply + `pg_dump`/restore + DNS change measured
in days, not a rewrite. OCI is not the doc's default (that was GCP, §4 there), but it wins on the two axes we are
optimizing here — **cost at scale** (free egress, cheap Ampere cores) and **exit effort** (nothing proprietary) —
and the portable design below means choosing it costs us nothing if we later change our minds.

> Non-goal: human-grade cloud TTS. Per [DEPLOYMENT_OPTIONS.md §1.7](DEPLOYMENT_OPTIONS.md#17-orchestrator-voice-quality-tts-new-criterion-decided-2026-09-08),
> the orchestrator voice stays **self-hosted** (better Piper voice → Kokoro-82M). This is also the only sane choice
> on OCI, which has no first-party TTS — so the TTS decision and the OCI decision reinforce each other.

---

## 0. The plan in one screen

The move is **95% a provider-neutral refactor** (done once, reusable anywhere) and **5% an OCI binding**. Do the
refactor first, on Azure, so no work is wasted and cutover is low-risk.

| Phase | What | Where it runs | Reusable if we leave OCI later? |
|---|---|---|---|
| **0. Portabilize** | jobs-table scheduler, `send_to_device()` over MQTT, one container image with models baked in, own domain, security/privacy fixes, TTS voice swap | Azure (still live) | **100% — this is the product, not the host** |
| **1. Stand up OCI** | Terraform: VCN, Ampere A1, Postgres, MQTT broker, object storage, egress firewall | OCI (parallel to Azure) | Terraform module swaps per provider |
| **2. Migrate data** | `pg_dump` → restore, re-point `agents.agent_url`, checked-in schema | OCI | trivial anywhere |
| **3. Cut over** | DNS flip; **firmware release** + device re-provision; mobile-app repoint | coordinated | DNS-swappable by design |
| **4. Decommission** | tear down Azure after a validation window | — | — |

**The one hard dependency** is the **ESP32 firmware** (separate repo): leaving Azure IoT Hub for any MQTT broker is
a firmware release + device re-provisioning. That cost is identical on AWS/GCP/OCI, so it is not an argument against
OCI — but it is the critical-path item and needs an owner and a date.

---

## 1. Design principle: contract vs. binding

The whole plan rests on one idea: express every component as a **stable contract** (an interface in code or a DNS
name), and treat OCI as just one **binding** behind it. "Moving a component" then means writing a new binding, never
touching the callers. Most of these seams already exist in the codebase.

| Component | Portable contract (stays constant) | OCI binding (this plan) | Rebind elsewhere = |
|---|---|---|---|
| Orchestrator process | one Docker image, configured by env vars; WSS on `api.<domain>` | container on an Ampere A1 VM behind Cloudflare | run the image on the new host; repoint DNS |
| TTS | `app/developer_ws/tts.py:synthesize_speech_pcm24_stream(text) -> PCM` | Piper/Kokoro model **baked into the image** | none — travels with the image |
| Database | plain **PostgreSQL 16**, `DB_*` env vars | OCI Database w/ PostgreSQL **or** self-hosted PG on a VM | `pg_dump`/restore + change `DB_*` |
| Scheduler | `app/enqueue/*` funcs: `enqueue_task`, `cancel_scheduled_task_for_task_id`, `enqueue_text_message` | a **`jobs` table** in Postgres (schedule-at + cancel-by-id) | none — it is just Postgres |
| Queue worker | a process that polls due jobs and acts | container/systemd unit on a small A1 VM | run the same process anywhere |
| Device wake-up | **`send_to_device(device_id, payload)`** (already exists, `listener/iot_hub_mqtt.py:288`) | MQTT publish to a broker (Mosquitto/EMQX) | swap the function body; re-issue device creds |
| Config/secrets | environment variables only | injected by Terraform / systemd env (OCI Vault optional) | copy env vars |
| Public identity | `api.<domain>` + `broker.<domain>` behind Cloudflare | Cloudflare → OCI public IPs | change one DNS record |

Two seams already exist (`send_to_device()`, `app/enqueue/*`) — we re-implement their bodies, not their callers.
The rest of Phase 0 is making the remaining seams (image, domain, jobs table) real.

**Anti-lock-in rules (do-not-do list):** no OCI Streaming/Queue as the scheduler; no OCI-proprietary device service;
no OCI-only Postgres extensions; no OCI hostnames baked into firmware, the mobile app, `agent_directory/*.html`, or
the `agents` table — everything client-facing points at `*.<domain>`.

---

## 2. Target architecture on OCI

One region, US-central for ≤40 ms nationwide (e.g. `us-chicago-1`); **database colocated with the app** (today
Azure splits them across regions — do not reproduce that).

```
                 ┌────────────── Cloudflare (DNS, TLS, WAF) ──────────────┐
   ESP32 ──MQTT──┤ broker.<domain>                       api.<domain>     │──WSS/HTTPS── Flutter app
     │           └───────────────┬───────────────────────────┬───────────┘
     │                           │                            │
     │                   ┌───────▼────────┐          ┌────────▼─────────┐
     └── C2D wake ◄──────┤ MQTT broker    │          │ Orchestrator     │──outbound WSS──► relay agents
                         │ (A1 VM)        │          │ container (A1 VM) │──HTTPS────────► Gemini API
                         └───────┬────────┘          │  Piper/Kokoro TTS │
                                 │                    │  Vosk STT, VAD   │
                         ┌───────▼────────┐          └────────┬─────────┘
                         │ Worker (A1 VM) │◄──jobs table──────┤
                         │ polls jobs     │          ┌────────▼─────────┐
                         └────────────────┘──────────► PostgreSQL 16    │
                                                     └──────────────────┘
        Object Storage: model assets (Vosk/Piper/Kokoro) + Postgres backups
```

- **Relay/orchestrator instances get public IPs** (OCI charges no NAT data processing, and egress is free to 10 TB)
  — this is the structural reason OCI is cheap for a relay.
- **Egress firewall via NSG/security lists** on the orchestrator subnet — required for the bridge SSRF fix (§4).
- Compute: **Ampere A1** (Arm), **paid** flavor — the Always Free tier was cut to 2 OCPU/12 GB on 2026-06-15 and is
  capacity-constrained; do not build on it. A 4 OCPU/24 GB A1 ≈ $56/mo covers early scale.
- Keep the orchestrator and worker as plain **containers** (Docker on the VM, or OCI Container Instances). Avoid OKE
  until scale needs it — it adds ops without adding portability.

---

## 3. Component-by-component

### 3.1 Orchestrator backend (Kairos + orchestrator + HTTP API + registry site)
- **Contract:** one always-on Python 3.12 process; long-lived WebSockets, no idle timeout; ~130 MB models on disk;
  `libopus`; public WSS/HTTPS on a stable hostname.
- **OCI binding:** the container on an A1 VM; Cloudflare in front (passes WS upgrades, no idle cap). HTTPS-only.
- **Work:** build one image = `app/` + `agent_directory/` + `requirements.txt` + **models baked in** + `libopus`
  installed (kills the per-cold-start `apt install libopus0`). Config purely via env vars. Health check `/healthz`.
- **Portability note:** the image is the portable unit. Nothing OCI-specific goes inside it.

### 3.2 TTS (self-hosted — the branch's purpose)
- **Contract:** `synthesize_speech_pcm24_stream(text) -> 24 kHz int16 PCM`, consumed by `PiperTTSProcessor`.
- **Binding:** models **baked into the image**; selected by env var. Per [§1.7](DEPLOYMENT_OPTIONS.md#17-orchestrator-voice-quality-tts-new-criterion-decided-2026-09-08):
  (1) swap `en_US-amy-medium` → `en_US-lessac-high` now; (2) add **Kokoro-82M** as a config-selectable backend
  behind the same seam if more naturalness is needed.
- **Why it fits OCI:** OCI has no first-party TTS, so self-hosted is the only option anyway — and it keeps TTS at
  $0 marginal and fully portable. This is the first coding task after this plan is approved.

### 3.3 Database — PostgreSQL 16
- **Contract:** plain PG 16, 11 small tables (~9 MB), reached via `DB_*` env vars.
- **Binding:** **OCI Database with PostgreSQL** (managed; per-OCPU, no micro tier — verify smallest shape/cost at
  provisioning) **or** self-hosted PG on an A1 VM with backups to Object Storage. Recommendation: **self-hosted PG
  on the app VM to start** (cheapest, most portable, tiny data), move to managed if HA is needed.
- **Work:** strip Azure-only extensions (`azure`, `pgaadauth`, `pg_cron`) from the dump; **check a schema file into
  the repo** (today schema is created ad hoc by the app and the mobile app — `test/setup_local_postgres.py` is the
  closest reference); re-point `agents.agent_url` to `api.<domain>`; **close the `0.0.0.0/0` firewall** (only the
  app/worker reach the DB once the mobile app moves to the HTTP API — §3.7).

### 3.4 Scheduler — replace Service Bus with a `jobs` table
- **Contract (unchanged):** `enqueue_task(...)`, `cancel_scheduled_task_for_task_id(...)`, `enqueue_text_message(...)`
  in `app/enqueue/*` — schedule-at-time returning a cancellable id, cancel-by-id, at-least-once to one worker.
- **Binding:** a `jobs(id, run_at, payload, state, ...)` Postgres table; enqueue = insert, cancel = delete/mark,
  the worker `SELECT ... FOR UPDATE SKIP LOCKED WHERE run_at <= now()`. The +1-min deferral logic (`message_enqueue`
  dedupe, listener re-schedule) ports directly.
- **Portability note:** this removes the single biggest Azure lock-in and needs **no** provider service anywhere.

### 3.5 Queue worker (the `listener`)
- **Contract:** a small always-on process with DB access that emits device wake-ups.
- **Binding:** the worker as a container/systemd unit polling the `jobs` table (replaces the Service Bus trigger in
  `listener/function_app.py`). Same behavior: check `sessions.is_active`, re-schedule if active, else wake the
  device; for `text_message` jobs also read unread messages and send a second wake.
- **Work:** strip the stale v1 leftovers (`listener/__init__.py`, `function.json`); the v2 logic is the keeper.

### 3.6 Device wake-up — IoT Hub → MQTT broker (critical path)
- **Contract:** **`send_to_device(device_id, payload)`** — already the abstraction in `listener/iot_hub_mqtt.py:288`.
- **Binding:** a self-hosted **Mosquitto or EMQX** broker on an A1 VM (TLS, per-device auth), reached at
  `broker.<domain>`; `send_to_device` publishes to the device's command topic. Only the function body changes.
- **Firmware (separate repo):** the ESP32 must switch its MQTT client from IoT Hub to `broker.<domain>` with new
  credentials, and keep its audio WSS client pointed at `api.<domain>`. **This is the release that gates cutover.**
  Mitigation: point firmware at DNS names (`broker.<domain>`, `api.<domain>`) so future host moves are DNS-only, not
  another firmware release.

### 3.7 Satellites
| Piece | Action on OCI | Portability |
|---|---|---|
| **Gemini API** | unchanged (AI Studio key, outbound HTTPS). Confirm the project is on the **paid** tier. Vertex/in-network is a GCP-only benefit we forgo by choosing OCI — fine for cost (egress free), but see the HIPAA caveat in §7. | none |
| **Firebase Auth** | unchanged | none |
| **Mobile app** | **repoint to `api.<domain>`; remove the direct Postgres connection and embedded credentials** (move those reads onto the HTTP API); rotate. This is what lets us close the DB firewall. | app release |
| **Model assets** | baked into the image; **OCI Object Storage** (S3-compatible) as the source of record + PG backups | S3-compatible = portable |
| **Relay agents** | unchanged — public `register`/`ping` on `api.<domain>`, free outbound WSS from OCI | none |

---

## 4. Security & privacy prerequisites (blocking — do with Phase 0)

From [DEPLOYMENT_OPTIONS.md §1.4/§1.3](DEPLOYMENT_OPTIONS.md#14-security-of-a-relay-platform-new-cloud-neutral-blocking);
these are required regardless of host and are cheap to do while refactoring:

- **Bridge SSRF:** allow only `wss://` with valid TLS; resolve DNS and reject private/link-local/loopback ranges
  (re-check at connect time); put the orchestrator subnet behind an **egress firewall (OCI NSG)**; disable OCI legacy
  IMDS v1; run the instance with a **minimal-scope instance principal** (nothing beyond logs + broker).
- **Auth:** per-agent tokens issued at `POST /developer/register`; required on `ping`/`unregister`; rate-limit both.
- **Privacy:** opaque per-session token in the bridge hello frame (not the real `user_id`); spoken consent on bridge;
  bridge audit log; transcript retention/deletion job; **rotate the committed Google key in `test-docker.sh` and the
  DB password**.
- **Agent health checks** to expire dead tunnels.

---

## 5. Infrastructure-as-code & repeatability

Portability is only real if standing the stack up is scripted.

- **Terraform (OCI provider)** for VCN, subnets/NSGs, A1 instances, Postgres, broker VM, Object Storage, DNS. The
  Terraform is the "OCI binding" made concrete; a future move is **a sibling Terraform module** for the new provider
  against the same contracts — the app image and data are untouched.
- **One image, built in CI**, pushed to OCIR (or any registry — keep it registry-agnostic).
- **Secrets** stay env-var based (optionally sourced from OCI Vault at boot) so nothing depends on a specific vault.
- Keep a `docker-compose.yml` that runs the whole stack (app + PG + broker + worker) locally — it doubles as the
  portability proof: if it runs in compose, it runs on any VM.

---

## 6. Migration & cutover sequence

**Phase 0 — Portabilize (on Azure, no downtime, ~2–3 eng-weeks incl. security fixes).** Everything in §1 seams + §4.
None of it is OCI-specific; it also just makes the current Azure deploy better. Ship TTS voice swap here too.

**Phase 1 — Stand up OCI (parallel).** `terraform apply`; deploy the image; restore a snapshot of the DB for testing;
run the `docker-compose` smoke test in the cloud.

**Phase 2 — Data migration.** `pg_dump` (extensions stripped) → restore into OCI Postgres; re-point `agents.agent_url`;
apply the checked-in schema; verify row counts.

**Phase 3 — Cutover (coordinated).**
1. Freeze writes briefly; final DB sync.
2. Flip Cloudflare DNS: `api.<domain>` → OCI.
3. **Firmware release + device re-provision** to `broker.<domain>` + new MQTT creds.
4. Mobile-app release pointed at `api.<domain>`, off direct Postgres.
- **Rollback:** DNS flip back to Azure (kept warm through the validation window); firmware/broker is the only piece
  that can't instantly roll back, which is why it goes last and behind a DNS name.

**Phase 4 — Decommission Azure** after a clean validation window (App Service, Service Bus, IoT Hub, Function App,
Postgres). Keep a final DB dump in Object Storage.

---

## 7. Cost, risks, open questions

**Cost on OCI** (from [§1.1/§2.4](DEPLOYMENT_OPTIONS.md#11-cost-at-scale)): early ≈ **$60–120/mo** (one/two A1 VMs +
small PG + broker + Cloudflare free tier). At 1M users ≈ **$5–8k/mo hosting** vs $12–18k on a hyperscaler — the free
egress is the swing. Gemini remains the dominant bill regardless of host.

**Risks / assumptions:**
- **Firmware ownership & timeline** (separate repo) — the critical path; needs an owner + date before Phase 3.
- **OCI A1 capacity** can be unavailable in a region — provision early, have a fallback AD/region.
- **OCI managed Postgres** shape/cost (per-OCPU, no micro tier) — verify; self-hosting PG is the cheaper/more
  portable fallback.
- **Ops burden is high on OCI** (§2.4 scored it 2/5): we own the broker, backups, patching, HA. Confirm there's an
  owner for VM ops; this is the main thing we're trading for the cost/portability win.
- **HIPAA/BAA:** choosing OCI means Gemini stays external (no GCP Vertex in-network/BAA path). If HIPAA-style
  obligations apply to any first-party path, that argues for GCP for the LLM even if the rest runs on OCI — flag now,
  don't discover later.
- Carry forward the assumptions in [DEPLOYMENT_OPTIONS.md §6](DEPLOYMENT_OPTIONS.md#6-assumptions-to-validate)
  (Kairos share, sessions/user, longest WS session, device wake frequency).

---

## 8. First coding tasks (once this plan is approved)

In order, front-loading the reusable + branch-relevant work:
1. **TTS voice swap** (`en_US-lessac-high`) + config-selectable backend seam, Kokoro-82M option — this branch's core.
2. **`jobs` table** behind `app/enqueue/*` (kills the biggest lock-in) + worker poller.
3. **`send_to_device()` MQTT binding** + local Mosquitto in `docker-compose`.
4. **One container image** with models baked in + `libopus`; `docker-compose` for the full stack.
5. **Security fixes** (§4) — SSRF guard, register/ping tokens.
6. **Own the domain**; move mobile app to the HTTP API; checked-in schema.

Then Phase 1 Terraform for OCI.
