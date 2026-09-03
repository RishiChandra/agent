# Deployment Options — where to run the orchestrator after Azure credits end

Date: 2026-09-02 (revised for the relay-centric product model). Component inventory and per-component
requirements are in [DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md); this document only compares hosts. Prices are
public list prices as of this date (sources at the end), rounded; treat every dollar figure as an estimate to be
re-checked at decision time.

**Product model this is written for:** the deployable is the **orchestrator** (routing, relay, registry, HTTP API,
site). **Kairos is one first-party agent**; most agents will be built and hosted by users, reached over outbound
WebSockets, and are outside our deployment control.

---

## 0. Short answer

1. **The cloud-dependent part of the bill is small at every scale.** Per user per month, what the host controls is
   about one to two cents on a hyperscaler and under half a cent on OCI. What dominates instead is Gemini: Kairos
   minutes at about $0.0115 per minute, and the per-session routing turn at about $0.001–0.002. Decide the Kairos
   share of minutes and the routing model before deciding the cloud (§1.1).
2. **Network is the largest *hosting* line once minutes are relayed.** Every relayed minute leaves your network twice
   (down to the device, out to the agent) and the agent leg is also NAT-metered on AWS, GCP and Azure. That is
   where OCI's free egress shows up: roughly $6–8k versus $0.4k a month at 1M users (§1.1).
3. **Privacy and security are now platform-policy and code problems, not cloud problems.** User audio goes to third
   parties you do not control; the orchestrator dials user-supplied URLs; registration and ping are unauthenticated.
   The fixes are the same on every provider and are prerequisites for any move (§1.3, §1.4, §5).
4. **Build the data plane portable** (containers, Postgres, a Postgres-backed scheduler, a standard MQTT broker,
   your own domain). With that design, transferring providers is a dump/restore plus a DNS change measured in days;
   with provider-native queues and IoT services it is weeks and a firmware release (§1.6).
5. **Recommended path:** do the portable refactor and the security fixes **while still on Azure** (apply to Founders
   Hub to cover the interim), then host wherever the largest credit grant lands, with **GCP as the default** (Kairos'
   Gemini traffic, Firebase, and the AI-first credit tier) and **OCI for the relay tier** once network and relay CPU
   are material and someone owns the operations. The four providers score within 0.3 of each other (§4); the
   decision is not worth a long delay.

---

## 1. What drives the decision

### 1.1 Cost at scale

Two kinds of minutes flow through the orchestrator:

| Minute type | What we pay for | Hyperscaler (AWS/GCP/Azure) | OCI |
|---|---|---|---|
| **Relayed** to a user-built agent | ~0.18 MB egress to the device + ~0.18 MB egress to the agent (Opus ≈ 24 kbps) + NAT on the agent leg + ~0.03 vCPU of relay CPU | ≈ $0.00006 | ≈ $0.00001 |
| **Kairos** (first-party, Gemini Live) | Gemini Live audio in $3/1M tokens, out $12/1M at 25 tokens/s ≈ $0.005 in, $0.018 out; ≈ $0.0115/min at a 50/50 talk split; relay overhead is internal | ≈ $0.012 | ≈ $0.012 |
| **Routing turn** (per session, before a bridge) | Silero + Vosk + Piper CPU for a few seconds, plus one Gemini text call (~1.5k tokens) | ≈ $0.001–0.002 | ≈ $0.001–0.002 |

A Kairos minute costs about 200× a relayed minute on a hyperscaler and about 1,000× on OCI. Per user per month
(150 minutes, 30 sessions), by Kairos share of minutes:

| Kairos share | Gemini (Kairos + routing) | Cloud-dependent hosting (network + CPU) | Total per user-month |
|---|---|---|---|
| 100 % (assistant-first product) | ≈ $1.77 | ≈ $0.01 | ≈ $1.78 |
| 20 % (relay-heavy baseline) | ≈ $0.40 | ≈ $0.01–0.02 | ≈ $0.42 |
| 0 % (pure router) | ≈ $0.05 (routing only) | ≈ $0.01–0.02 (OCI ≈ $0.003) | ≈ $0.06–0.07 |

Hosting at 1M users in the relay-heavy baseline (150M minutes, 30M sessions a month):

| Hosting line | Hyperscaler | OCI | Notes |
|---|---|---|---|
| Internet egress, 54 TB (two legs) | ≈ $4.5–6.5k | ≈ $0.4k | AWS $0.09/GB, Azure $0.087, GCP $0.12 falling with volume; OCI 10 TB free then $0.0085 |
| NAT gateway on the agent leg, 27 TB | ≈ $1.2k | $0 | $0.045/GB on AWS, GCP and Azure; avoid by giving relay instances public IPs |
| Relay CPU (~10k peak concurrent × 0.03 vCPU) | ≈ $2–4k | ≈ $1–2k | VMs, not per-vCPU serverless containers |
| Routing-turn CPU (Vosk/Piper/Silero) | ≈ $3–5k | ≈ $2–3k | ~0.5 vCPU-min per session |
| Postgres with HA + replica | ≈ $0.3–1k | ≈ $0.3–1k | tiny data |
| Scheduler, MQTT broker, secrets, logs | ≈ $1–2k | ≈ $1–2k | |
| **Hosting total** | **≈ $12–18k / month** | **≈ $5–8k / month** | versus Gemini ≈ $400k at 20 % Kairos, ≈ $50k as a pure router |

Conclusions that hold regardless of provider:

- The provider decision is worth roughly $7–10k a month at 1M users and nothing below ~100k users.
- The routing model is worth more: Gemini Flash at ~$0.0015 a session is ≈ $45k a month at 1M users; a Flash-Lite
  or rules-first router (the registry already does exact and fuzzy name matching) cuts that 5–10×.
- Kairos minutes are the product decision that sets the bill; every 10 % of minutes on Kairos ≈ $0.17 per
  user-month.

### 1.2 Latency (US-wide first)

- You own the middle hop only. Device → orchestrator is yours; orchestrator → agent is whatever the builder chose,
  often a Cloudflare quick tunnel. Provider choice affects the first leg and your own processing time.
- One central-US region reaches the whole country in ≤ 40 ms RTT (GCP `us-central1`, AWS `us-east-2`, Azure
  `Central US`, OCI `us-chicago-1`). Put the database in the same region; today app and DB are in different regions.
- Keep the relay a pass-through (no transcoding) and keep routing turns short; the README's 9-second
  end-of-speech-to-audio measurement was CPU time on a throttled shared core, not network.
- Measure and expose per-agent latency (handshake time, first-audio time), store a region hint in the registry, and
  warn or de-prioritize slow agents. That is the only lever on the leg you do not own.
- Multi-region later improves the device leg only; use geo-DNS with sticky WebSockets and a single-writer database.
- Provider-specific WebSocket constraints: Cloud Run caps a request, WebSocket included, at 60 minutes; AWS App
  Runner has no WebSocket support; ALB and Azure ingress need keep-alive pings (already sent).

### 1.3 Data privacy

User audio and transcripts leave your control by design when a call is bridged. The controls that matter are:

| Exposure | Fix (provider-independent) | Where providers differ |
|---|---|---|
| Audio relayed to third-party agents | Builder terms (data use, retention, no recording without disclosure), a spoken consent moment on bridge ("connecting you to X"), audit log of every bridge session, agent revocation and allow-lists | None |
| Real `user_id` sent in the bridge hello frame (`app/developer_ws/bridge.py`) | Send a per-session opaque token; never a stable user identifier | None |
| Routing turns and Kairos minutes through Gemini | Confirm the Google project is on the **paid** tier (paid-tier content is "not used to improve our products"; free-tier content is used and may be human-reviewed) | Only Vertex AI on GCP gives a BAA-covered, region-pinned Gemini endpoint with CMEK and audit logs; this now covers routing turns plus the Kairos share only |
| Mobile app connects to Postgres with credentials compiled in | Move the app to the HTTP API; close the `0.0.0.0/0` firewall; rotate | None |
| Transcripts in `sessions.scratchpad`, logs | Retention window and deletion job | None |
| Compliance paperwork (HIPAA and similar) | Cannot be promised platform-wide unless every agent builder is under agreement; scope any such claim to Kairos and first-party paths | All four sign BAAs for covered services |

### 1.4 Security of a relay platform (new, cloud-neutral, blocking)

The orchestrator opens outbound WebSockets to URLs supplied by anyone who can call the registry:

- `app/routes/agent_routes.py` accepts any string as an agent URL and `agents_registry.resolve_bridge_url` hands it
  to the bridge. A registrant can point an agent at internal services, the database, or the cloud metadata
  address `169.254.169.254`. Cloud metadata services mostly require special headers or tokens that a WebSocket
  handshake does not carry (AWS IMDSv1 and OCI's legacy v1 endpoint are the exceptions and should be disabled),
  so the larger exposure is anything else reachable on the orchestrator's network, plus response text leaking into
  logs. Treat it as SSRF: allow `wss://` only with valid TLS, resolve DNS and reject private, link-local and
  loopback ranges (re-check at connect time), and put the relay behind an egress firewall.
- `POST /developer/register` and `POST /developer/ping/{user_id}` are unauthenticated: anyone can overwrite another
  agent's URL (hijack routing) or trigger calls to any user. Issue per-agent tokens at registration; require them on
  ping and unregister; rate-limit both.
- Run the relay with **no** powerful cloud identity (workload identity scoped to nothing beyond logs and the broker),
  so a successful SSRF has nothing to steal.
- Health-check registered agents (the unmerged website branch already has a "test connection" probe) and expire
  dead tunnels; quick tunnels are ephemeral.

Every provider supports egress firewalling and minimal-scope identities; none does this for you.

### 1.5 Ease of engineering implementation (new criterion)

Rough engineer-weeks from the current code, excluding the security fixes above (needed everywhere, ~1 week):

| Option | What has to be built | Estimate |
|---|---|---|
| Azure, stay as-is | nothing | 0 |
| Azure, stay + portable refactor (§1.6) | containerize with models; Postgres-backed scheduler; broker abstraction | ≈ 2 weeks |
| AWS, provider-native | ECS/ALB, RDS, EventBridge Scheduler client in `app/enqueue/*`, Lambda worker, IoT Core publish + **firmware** | ≈ 3–5 weeks |
| AWS, portable design | container on EC2/ECS, RDS, jobs table, EMQX/Mosquitto + firmware | ≈ 2–3 weeks |
| GCP, provider-native | GCE/GKE, Cloud SQL, Cloud Tasks + 30-day deferral logic, Cloud Run worker, Vertex AI client switch, MQTT SaaS + firmware | ≈ 3–5 weeks |
| GCP, portable design | container on GCE/GKE, Cloud SQL, jobs table, broker + firmware | ≈ 2–3 weeks |
| OCI (portable by necessity) | VM + Caddy, self-hosted or OCI Postgres, jobs table, Mosquitto + firmware, backups | ≈ 2–4 weeks |

Any move off IoT Hub carries a firmware release and device re-provisioning; that is the same on AWS, GCP and OCI.

### 1.6 Ease of transferring to another provider (new criterion)

Lock-in is the count of provider-specific integrations in the data plane. Today there are four: Service Bus
(enqueue code and the Functions trigger), IoT Hub (C2D code and the device firmware), App Service zip/Oryx deploy,
and Azure-only Postgres extensions.

| Design | Provider-specific pieces | Exit effort |
|---|---|---|
| **Provider-native** (Service Bus/IoT Hub, EventBridge/IoT Core, Cloud Tasks/Vertex) | scheduler client, device channel, worker trigger, deploy tooling, firmware endpoint | ≈ 3–5 weeks + firmware release |
| **Portable** (container image with models; Postgres; `jobs` table polled by a worker; EMQX/Mosquitto or a SaaS broker; Cloudflare + own domain in front) | none in code; only endpoints and credentials in configuration | ≈ days: dump/restore Postgres, re-issue device broker credentials, change DNS |

Where managed services are still worth it under the portable design: managed Postgres (always), a managed MQTT
broker (optional; MQTT is a standard so the broker is swappable), object storage for model assets. Where they are
not: proprietary schedulers and device-messaging services, because they are the exact pieces that made the Azure
stack hard to leave.

---

## 2. Provider fit, component by component

Component names follow [DEPLOYMENT_AUDIT.md §4](DEPLOYMENT_AUDIT.md#4-what-each-piece-needs-from-a-host).

### 2.1 AWS

| Component | AWS mapping | Notes |
|---|---|---|
| Orchestrator (relay + routing + API) | EC2 (Graviton `t4g`/`c7g`) or ECS on EC2 behind an ALB; public IPs on relay instances to skip NAT | ALB idle timeout up to 4000 s. **App Runner is out** (no WebSockets). Fargate works but costs ≈ 3× EC2 per vCPU |
| Database | RDS PostgreSQL `db.t4g.micro` ≈ $12–14/month with storage; Aurora later | |
| Scheduler | Portable: `jobs` table. Native alternative: EventBridge Scheduler one-time schedules ($1 per 1M after 14M free), best managed fit for schedule/cancel | |
| Device wake-up | Portable: EMQX/Mosquitto on EC2 or SaaS. Native: AWS IoT Core (≈ $0.042 per device-year + $1 per 1M messages) | Firmware change either way |
| Network | $0.09/GB egress; NAT $0.045/GB if used | |
| Security controls | IMDSv2-only instances, security groups for egress, IAM roles with minimal scope | Mature |
| Credits | Free tier $100 → $200 (6 months); Activate Founders $1k; up to $100k investor-backed | |
| Implementation / exit | ≈ 2–3 weeks portable; exit in days if portable | |

### 2.2 Google Cloud

| Component | GCP mapping | Notes |
|---|---|---|
| Orchestrator | Compute Engine (`e2`/`t2a` ARM) or GKE Autopilot; public IPs on relay nodes | **Cloud Run caps WebSockets at 60 min** with best-effort affinity; fine for the API, not for long relays |
| Database | Cloud SQL PostgreSQL `db-f1-micro` ≈ $11/month + storage; AlloyDB later | |
| Scheduler | Portable: `jobs` table. Native: Cloud Tasks (`scheduleTime`, delete = cancel, $0.40 per 1M) with a **30-day horizon** | |
| Device wake-up | No managed MQTT since IoT Core retired (2023): EMQX/Mosquitto on GCE, HiveMQ Cloud (free ≤ 100 devices), EMQX Cloud Serverless (free 1M session-minutes ≈ 23 always-on devices) | Firmware change |
| LLM | **Vertex AI Gemini Live and text**, regional, BAA-eligible; committed-use / Flexible Savings Plans | Covers Kairos and routing turns; the `google-genai` client switches with project/location settings |
| Auth | Firebase Auth, already here | |
| Network | $0.12/GB premium egress falling with volume; Cloud NAT $0.045/GB if used | Priciest of the four |
| Credits | $300 trial; Google for Startups $2k (MVP) → up to $200k, **$350k AI-first**, spendable on Vertex | Largest program; Kairos and routing spend qualifies |
| Implementation / exit | ≈ 2–3 weeks portable; exit in days if portable | |

### 2.3 Azure (stay, pay-as-you-go)

| Component | Azure mapping | Notes |
|---|---|---|
| Orchestrator | Keep App Service now (B1 → P1v3 ≈ $113/month for CPU); later a VM or Container Apps for the relay tier | Zero migration work today |
| Database | Existing Flexible Server B1ms ≈ $12/month + storage; move it next to the app | |
| Scheduler / worker | Existing Service Bus + Functions work; replace with the `jobs` table when doing the portable refactor | |
| Device wake-up | Existing IoT Hub F1 (free, 8k msgs/day; S1 $25/unit at volume); **no firmware change** while staying | Most mature managed IoT of the four |
| Network | $0.087/GB egress; NAT $0.045/GB if used | Cheapest hyperscaler egress |
| Credits | Founders Hub self-serve $1k → $5k; $100–150k investor-backed | |
| Hosting today | ≈ $30–35/month list; ≈ $130 with P1v3 | |
| Implementation / exit | 0 now; exit is 3–5 weeks + firmware as-is, days after the portable refactor | |

### 2.4 Oracle Cloud (OCI)

| Component | OCI mapping | Notes |
|---|---|---|
| Orchestrator / relay tier | Ampere A1 VMs at $0.01/OCPU-hour + $0.0015/GB-hour (4 OCPU / 24 GB ≈ $56/month); public IPs, no NAT charge | **Always Free is now 2 OCPU / 12 GB** (cut from 4/24 on 2026-06-15 without announcement; over-limit instances terminated after 2026-08-18; idle instances reclaimed; capacity often unavailable). Plan on paid A1 |
| Database | OCI Database with PostgreSQL (per-OCPU, no micro tier) or self-hosted Postgres with backups | |
| Scheduler / worker | `jobs` table + worker (portable design) | |
| Device wake-up | Self-hosted Mosquitto/EMQX or a SaaS broker | Firmware change |
| Network | **10 TB/month free, then $0.0085/GB; no NAT data charge** | The decisive OCI advantage for a relay |
| Security controls | Security lists/NSGs for egress; disable legacy IMDS v1; instance principals scoped minimally | Adequate, less tooling |
| Credits | Oracle for Startups $500 → up to $100k plus a 70 % discount for two years | |
| Implementation / exit | ≈ 2–4 weeks; exit in days (nothing proprietary) | |

### 2.5 Side-by-side

| | AWS | GCP | Azure (stay) | OCI |
|---|---|---|---|---|
| Relay-tier fit (cheap sustained cores, public IPs, no NAT) | good (Graviton) | good (`t2a`/GKE) | fair (App Service) / good (VM) | best (A1, free egress) |
| Always-on 1 vCPU / 2 GB VM, list | ≈ $12 | ≈ $13 | ≈ $13 (B1) | ≈ $9 |
| Managed Postgres, smallest | ≈ $12–14 | ≈ $11 + storage | ≈ $12 | per-OCPU |
| Egress first 10 TB / NAT | $0.09 / $0.045 | $0.12 / $0.045 | $0.087 / $0.045 | free / none |
| Network at 1M users (relay-heavy) | ≈ $6–7k | ≈ $7–8k | ≈ $6–7k | ≈ $0.4k |
| Managed scheduler with cancel | EventBridge Scheduler | Cloud Tasks (30-day cap) | Service Bus (have it) | none (table) |
| Managed MQTT | IoT Core | none | IoT Hub (have it) | none |
| Gemini path | external | in-network, BAA, credits | external | external |
| Startup credits (self-serve / backed) | $1k / $100k | $2k / $200–350k | $1–5k / $150k | $0.5k / $100k |
| Implementation effort (portable design) | 2–3 wk | 2–3 wk | 0 now, 2 wk refactor | 2–4 wk |
| Exit effort (portable design) | days | days | days (weeks as-is) | days |
| Firmware change to adopt | yes | yes | no | yes |
| Ops burden | medium | medium | low | high |

---

## 3. One provider or several?

**Rule: one provider and one region for the data plane** — orchestrator, database, scheduler, worker — because every
routing turn touches Postgres, inter-cloud traffic is billed as internet egress on both sides plus NAT, credits are
per provider, and one IAM/secrets/logging story is enough to run.

**What the relay model changes:**

- The **relay tier is separable** from the routing tier. Relay instances need cheap sustained cores, public IPs and
  cheap egress and hold no data; routing instances need CPU for Vosk/Piper and database access. They can be
  different instance types, and at scale the relay tier is the one piece where a different provider (OCI) pays for
  itself. Doing that splits the WebSocket session across providers only if routing and relay are different
  processes; keep them one process until the network bill justifies the split.
- **Kairos should be deployed as a separate service that registers itself through the same contract as third-party
  agents**, colocated with the orchestrator (same region, private network). That dogfoods the platform, keeps
  Gemini Live out of the orchestrator's dependencies, and lets Kairos follow Gemini economics (Vertex, credits)
  without dragging the relay tier along.
- Third-party agents are, by definition, multi-provider; the platform's job is to make that safe (§1.4) and
  observable (§1.2), not to host them.

**Satellites that are fine anywhere:** Gemini (in-network only on GCP), Firebase Auth, the MQTT broker (kilobytes
per day), Cloudflare at the edge (DNS, TLS, WAF, and the tunnels the agent contract already assumes), model asset
downloads.

**Multi-cloud for resilience** is still not worth it; a second region on the same provider is the next step.

---

## 4. Recommendation

Scoring assumes the **portable design** for the data plane (1–5, higher is better):

| | Weight | AWS | GCP | Azure (stay as-is) | OCI |
|---|---|---|---|---|---|
| Cost at scale (network, relay CPU, credits) | 0.25 | 3 | 3.5 | 3 | 4.5 |
| Latency (US now, worldwide later) | 0.15 | 4 | 4 | 4 | 3 |
| Privacy and security posture | 0.15 | 4 | 4.5 | 3.5 | 3 |
| Ease of engineering implementation | 0.15 | 3 | 3 | 5 | 3.5 |
| Ease of transferring to another provider | 0.15 | 4 | 4 | 2.5 (4 after refactor) | 4.5 |
| Ops burden | 0.15 | 3 | 3 | 5 | 2 |
| **Weighted** | | **3.45** | **3.65** | **3.75** | **3.53** |

The spread is 0.3 points; nothing here justifies a long deliberation or a big-bang migration.

**Path:**

1. **Now, on Azure:** apply to Founders Hub; do the security fixes (§1.4), the privacy items (§1.3), and the portable
   refactor (§5). Move the database next to the app. This costs about three engineer-weeks, none of it wasted
   whichever provider wins, and it turns "transfer" from weeks into days.
2. **Then choose the host by credits and Kairos economics.** Apply to all four programs. Default to **GCP
   `us-central1`** if the Google grant comes through: Kairos and routing turns run on Vertex AI in-region under a
   BAA, Firebase is already there, and the AI-first credit tier is the largest. **AWS** is an equal engineering fit
   with the best managed scheduler and IoT service if its grant is larger. Staying on Azure remains fine until a
   grant or the Gemini spend tips it.
3. **At scale, move the relay tier to OCI** when network plus relay CPU exceeds roughly $5k a month and there is an
   owner for VM operations. Nothing in the portable design has to change to do it.

**Not recommended:** provider-native schedulers and IoT services as the primary design; Cloud Run for long
WebSocket relays; App Runner at all; building on Oracle's Always Free tier.

---

## 5. Provider-independent preparation

Do these before any move; each is required regardless of the destination:

1. **Own a hostname.** Buy a domain and point firmware, the mobile app, the registry site and the `agents` table at
   `api.<domain>` behind Cloudflare, so provider changes become DNS changes.
2. **Security fixes (§1.4):** URL validation and private-range denial in the bridge, per-agent tokens on register,
   ping and unregister, rate limits, egress firewall, minimal-scope identity for the relay, agent health checks.
3. **Privacy items (§1.3):** opaque session token in the hello frame, consent on bridge, bridge audit log, builder
   terms, transcript retention, confirm the Gemini paid tier.
4. **Portable data plane:** one container image with the models baked in and `libopus` installed, configured by
   environment variables; a `jobs` table behind the existing `app/enqueue/*` interface (schedule-at, cancel-by-id);
   `send_to_device()` backed by a standard MQTT broker; secrets in a secret manager.
5. **Mobile app through the API only**; remove the direct Postgres connection and embedded credentials; close the
   firewall; rotate the committed Google key in `test-docker.sh` and the database password.
6. **Kairos as a self-registering service** on the same contract third-party agents use.
7. **Database dump without the Azure extensions** (`azure`, `pgaadauth`, `pg_cron`) and a checked-in schema.
8. **Routing-turn cost control:** rules-first agent matching before the LLM call; a smaller model for the LLM step.

---

## 6. Assumptions to validate

- Share of minutes on Kairos versus relayed (sets the bill; assumed 20 %).
- Sessions per user per day and routing turns per session (sets the routing-LLM line; assumed 1 and 1–2).
- Minutes of conversation per user per day (assumed 5).
- Longest expected WebSocket session (decides Cloud Run vs VM/GKE on GCP).
- Device count and wake-ups per device per day (managed vs self-hosted broker).
- Where agent builders actually host (tunnels vs clouds) and the latency they add.
- Whether HIPAA-style obligations apply, and to which paths.
- Whether the current `GOOGLE_API_KEY` project is on the paid tier.

---

## Sources

- Gemini API pricing (Live API audio rates, 25 tokens/s, paid-tier data use): https://ai.google.dev/gemini-api/docs/pricing
- Gemini API billing and free-vs-paid data use: https://ai.google.dev/gemini-api/docs/billing
- Gemini / Vertex AI HIPAA and BAA scope: https://www.strac.io/blog/is-gemini-hipaa-compliant , https://ibl.ai/blog/is-gemini-hipaa-compliant-2026
- Vertex AI Live API (regional only): https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/multimodal-live , https://cloud.google.com/vertex-ai/generative-ai/docs/live-api
- Vertex AI pricing and committed-use / Flexible Savings Plans: https://www.cloudzero.com/blog/google-vertex-ai-pricing/
- Oracle Always Free A1 cut to 2 OCPU / 12 GB (June 2026): https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/ , https://terminalbytes.com/oracle-cloud-free-tier-changes-2026/ , https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- OCI Ampere A1 paid pricing: https://www.oracle.com/cloud/compute/arm/ , https://www.oracle.com/cloud/price-list/
- OCI networking and NAT (no data-processing charge): https://www.oracle.com/cloud/networking/virtual-cloud-network/pricing/ , https://oraclelicensingexperts.com/blog/oracle-oci-networking/
- NAT gateway data processing: https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-pricing.html , https://cloud.google.com/nat/pricing , https://azure.microsoft.com/en-us/pricing/details/azure-nat-gateway/
- Egress pricing by provider: https://egresscost.com/compare/ , https://rackdog.com/blog/egress-fees-index
- AWS Free Tier credits: https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan/ , https://aws.amazon.com/free/terms
- AWS Activate tiers: https://cloudkompas.com/blog/aws-activate-complete-guide-2026 , https://creditforstartups.com/resources/aws-startup-credits
- Google for Startups Cloud Program: https://cloud.google.com/startup , https://cloudkompas.com/blog/google-cloud-for-startups-2026-credits-guide
- Microsoft for Startups Founders Hub: https://creditforstartups.com/resources/microsoft-azure-startup-credits , https://cloudkompas.com/blog/microsoft-for-startups-2026
- Oracle for Startups: https://grantedai.com/grants/oracle-cloud-credits-for-startups-oracle-3bdb4a48 , https://cloudkompas.com/blog/free-cloud-credits-for-startups-AWS-azure-google-cloud-oci
- Cloud Run WebSockets and 60-minute request timeout: https://docs.cloud.google.com/run/docs/triggering/websockets , https://docs.cloud.google.com/run/docs/configuring/request-timeout
- EventBridge Scheduler pricing: https://aws.amazon.com/eventbridge/pricing/
- Cloud Tasks 30-day limit and deletion: https://docs.cloud.google.com/tasks/docs/manage-queues-and-tasks , https://dev.to/mailmeteor/how-to-schedule-tasks-in-more-than-30-days-in-google-cloud-tasks-api-35f
- AWS IoT Core pricing: https://aws.amazon.com/iot-core/pricing/
- HiveMQ Cloud free tier: https://www.hivemq.com/products/mqtt-cloud-broker/ ; EMQX Cloud Serverless free quota: https://www.emqx.com/en/cloud/serverless-mqtt
- Google IoT Core retirement and MQTT alternatives on GCP: https://docs.cloud.google.com/architecture/connected-devices/mqtt-broker-architecture , https://www.cedalo.com/blog/best-google-iot-core-alternatives
- Serverless container pricing comparison: https://sliplane.io/blog/comparing-prices-aws-fargate-vs-azure-container-apps-vs-google-cloud-run , https://www.vantage.sh/blog/fargate-pricing , https://cloud.google.com/run/pricing , https://azure.microsoft.com/en-us/pricing/details/container-apps/
- Managed Postgres smallest-instance pricing: https://www.bytebase.com/blog/postgres-hosting-options-pricing-comparison/ , https://aiven.io/tools/instances/db.t4g.micro
- ALB WebSocket idle timeout: https://websocket.org/guides/infrastructure/aws/alb/ ; App Runner lacks WebSockets: https://repost.aws/questions/QU0jOAcOoTQqigUj6B9oGDxg/websockets-on-apprunner
- Azure Container Apps ingress timeouts: https://learn.microsoft.com/en-us/azure/container-apps/ingress-environment-configuration
