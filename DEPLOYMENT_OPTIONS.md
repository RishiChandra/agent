# Deployment options — OCI selected

Revised 2026-09-09 after code and vendor-documentation validation. Inventory:
[DEPLOYMENT_AUDIT.md](DEPLOYMENT_AUDIT.md). Execution order:
[APP_BACKEND_DEPLOYMENT_PLAN_OCI.md](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md). Detailed first migration:
[POSTGRES_MIGRATION_PLAN.md](POSTGRES_MIGRATION_PLAN.md). Website: [WEBSITE_DEPLOYMENT_PLAN_OCI.md](WEBSITE_DEPLOYMENT_PLAN_OCI.md).

## 0. Decision

**Move to OCI. The initial PostgreSQL copy is done; the application move is next.** The portable application design remains useful. Corrections to the
previous comparison change the cost forecast, security details and cutover sequence; they do not invalidate OCI
as the chosen host. The prior GCP-default recommendation and subjective provider scorecard are superseded by
this decision. Startup credits may reduce costs but are not a dependency of this plan.

The updated sequence (2026-09-10) moves main's application onto the existing OCI VM before finishing the
OCI database cutover. Initially keep the same live database for the app and Azure Function listener, and
retain Service Bus and IoT Hub. Then finish OCI PostgreSQL connectivity, final data verification and recovery.
The mobile app is unused and may break. Mobile changes and application auth are deferred; TTS upgrades and
scheduler/MQTT migration are separate work.

## 1. Cost model: measure before forecasting scale

The earlier $12–18k hyperscaler / $5–8k OCI hosting totals at 1M users are **withdrawn**. They assumed Opus on
both relay legs, although the current agent bridge sends base64 PCM and the device path transcodes audio.
The engineering-time estimates and claims of negligible differences below 100k users were also unvalidated.

### 1.1 Traffic and inference

| Path | Actual behavior or assumption | How to budget |
|---|---|---|
| Device → orchestrator | Raw PCM or Opus, decoded locally | Measure actual codec, duty cycle and CPU |
| Orchestrator → third-party agent | 16 kHz, int16 mono PCM, base64 in JSON | 1.92 MB raw / 2.56 MB base64 per continuously forwarded audio minute, before transport compression/framing |
| Agent → orchestrator → device | PCM accepted by bridge; device output encoded as Opus when available | Measure incoming bytes, device egress, CPU and PCM fallback |
| Kairos | Currently shares the backend process; bridges can still use the configured public hostname | Measure actual route; do not assume private/internal traffic merely because the processes are colocated |
| Routing | Vosk/Silero/Piper plus Gemini text calls | Measure prompt/output/thinking tokens, turns/session and inference seconds |

`app/developer_ws/bridge.py:261` and `app/developer_ws/audio_io.py` establish the protocol.
At 120M third-party minutes, continuously forwarded base64 uplink alone would be 307.2 TB before compression,
not the former 27 TB agent-leg estimate. This is a payload scenario, not a revised bill: silence filtering,
WebSocket compression and user behavior matter. End-to-end Opus would be a future protocol change requiring
agent/device compatibility work, not a current capability.

Use separate Kairos and third-party fractions. Network cost is measured outgoing GiB by destination and tariff,
plus **both directions** through any NAT gateway, its hourly charge, public IPs and applicable inter-region fees.
Public-IP VM designs can avoid NAT on multiple clouds; NAT is not an unavoidable hyperscaler surcharge.
[Cloud NAT pricing](https://cloud.google.com/nat/pricing).

The previously cited Gemini Live rates match the pricing page: $3/M audio input and $12/M audio output tokens.
A $0.0115 conversation-minute estimate assumes a 50/50 split using rounded audio-minute rates. It excludes
text/context, thinking, first-party tool agents and search grounding; capture billed usage before extrapolating.
[Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

For 150M conversation minutes/month, average concurrency is about 3,472 over 30 days. Peak concurrency,
0.03 vCPU/session and 0.5 vCPU-min/routing session remain unmeasured assumptions. Today's small database is
not evidence for storage, connection or HA costs at 1M users.

### 1.2 OCI budget inputs

OCI offers 10 TB/month free outbound transfer with region-dependent paid transfer tariffs; confirm geography
and applicable units in the quote. Low network pricing still makes it attractive for audio relay workloads.
[OCI network pricing](https://www.oracle.com/cloud/networking/virtual-cloud-network/pricing/).

A1 at the previously quoted $0.01/OCPU-hour plus $0.0015/GB-hour gives about $55.48 for 4 OCPU/24 GB at 730 hours,
**before** disks, backup storage and any eligible free allowance. Treat this as arithmetic using a quote to recheck,
not a total stack price. [OCI Arm compute](https://www.oracle.com/cloud/compute/arm/).

Oracle currently documents 1,500 OCPU-hours and 9,000 GB-hours free monthly (2 OCPU/12 GB equivalent), including
eligible usage in paid accounts. Capacity and idle reclamation still make free-only deployment unsuitable as a
production availability strategy. The earlier historical termination dates are not established here.
[Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

For PostgreSQL first, budget the DB VM, boot/data volumes, backups, monitoring and temporary Azure network
configuration separately. Compare that complete amount with an OCI managed PostgreSQL quote. Current managed
E5 Flex starts at 1 OCPU/16 GB; Standard3 starts at 2 OCPU/32 GB, subject to regional availability.
[Supported shapes](https://docs.oracle.com/en-us/iaas/Content/postgresql/supported-shapes.htm).

### 1.3 Privacy and security

The validation identified open application endpoints and incomplete ownership checks. Those findings remain
follow-up work. Per the current scope, application auth, registry ownership and mobile changes do not block
the PostgreSQL migration. Database login, TLS and connectivity setup remain part of moving the database.

Allow validated TLS WebSocket destinations, block private/link-local/loopback targets at connection time, and
handle redirects and DNS changes. OCI NSGs do not enforce rules for `169.254.0.0/16`, so use application and
host/container controls as well as IMDSv2. [OCI security rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm).

An opaque bridge hello is insufficient if the URL still embeds the real user ID. Define third-party identifiers,
first-party Kairos authorization, consent, retention and audit behavior together. Do not silently break Kairos's
access to the user's data while changing the handshake.

OCI-hosted workloads can call Vertex AI with Google authentication; choosing OCI does not force the Gemini
Developer API. BAA coverage depends on the exact covered service/model and configuration, including preview
status, not where the calling VM lives. [External authentication](https://docs.cloud.google.com/docs/authentication/set-up-adc-on-premises),
[Google HIPAA guidance](https://cloud.google.com/security/compliance/hipaa).

### 1.4 Latency and connections

Choose the OCI region after testing Azure-to-DB latency and eventual device-to-app latency. A central-US region
is a candidate, not a guarantee of ≤40 ms RTT nationwide. Database-first temporarily adds a cross-cloud hop;
the clients currently open a new DB connection per operation, making connection setup especially relevant.

Cloudflare supports WebSockets but closes idle connections; use heartbeat/reconnect handling. Raw MQTT on
8883 needs DNS-only routing with broker TLS or a suitable Spectrum service; MQTT-over-WSS is another option
only after firmware support is verified. [WebSockets](https://developers.cloudflare.com/network/websockets/),
[proxy ports](https://developers.cloudflare.com/fundamentals/reference/network-ports/).

### 1.5 Portability

Keep ordinary PostgreSQL, reviewed SQL migrations, environment configuration, reproducible containers and owned
DNS names. Portability reduces application changes, but a provider move still requires network provisioning,
certificate/credential work, restore validation and state-aware rollback. Do not promise a universal duration.

`pg_cron` is portable open source; only omit it when unused or unsupported. Review Azure extension dependencies
rather than blindly deleting matching lines from a dump. [pg_cron](https://github.com/citusdata/pg_cron).

### 1.6 Provider context

| Provider | Relevant advantage | Remaining tradeoff |
|---|---|---|
| Azure | Current application and device integration already run here | Database move introduces temporary cross-cloud access |
| OCI — selected | Low-cost VM/network options; ordinary PostgreSQL can be portable | Self-hosting requires backups, restore drills, monitoring and patch ownership |
| GCP | Vertex and Firebase integrations; managed database options | These APIs can also be used from OCI; credits/latency are workload-specific |
| AWS | Mature managed PostgreSQL and IoT services | Native scheduler/device adoption adds migration work |

Do not select a provider based on the former weighted scores or unverified startup grant ceilings. Those are
replaced by the user's OCI decision and a concrete staged plan.

### 1.7 Orchestrator voice quality

Keep self-hosted TTS as the selected direction, independent of PostgreSQL migration. Install a better Piper
voice plus its companion configuration and set `PIPER_MODEL_PATH`; evaluate lessac-high with representative
utterances. Add Kokoro behind `synthesize_speech_pcm24_stream` only after an A1 latency/memory/concurrency
benchmark. The adapter resamples the model output to 24 kHz PCM.

There is no per-character API fee for local inference, but capacity is not free. Withdraw unsupported numeric
naturalness rankings and claims that only cloud audio can sound human. OCI **does** offer first-party TTS;
self-hosting is a preference, not a provider limitation. [Oracle synthesis](https://docs.oracle.com/en-us/iaas/Content/speech/using/using-tts-create.htm).

The former $25k–180k cloud-TTS estimates implicitly assumed roughly 6B characters/month (200/session at 30M
sessions). Without actual spoken-character volume and selected-model pricing, they do not justify a cost claim.

### 1.8 Website (added 2026-09-11)

The Agent Registry site is static HTML/JS served by the backend itself; it has no compute, storage or bandwidth
cost of its own beyond the backend's, on any provider. **It does not change the OCI decision.** It moved with the
app image on 2026-09-11 and is live on the OCI hostname.

| Hosting choice | Cost | When it would make sense |
|---|---|---|
| **Served by the backend at `/` (current, keep)** | 0 extra | One deploy artifact, same origin as the API so no CORS or API-base configuration; the site is only useful with the backend anyway |
| OCI Object Storage static website / Cloudflare Pages | 0–negligible | Only if the site needs a release cadence independent of the backend, a CDN, or a hostname that must survive backend moves. Would need the `?api=` / `ORCHESTRATOR` base to be configured and CORS kept open |

Remaining work is code, not hosting: make the site's API base same-origin on any host, replace the Azure fallback
hostname, and update the Kairos bridge URL row. See [WEBSITE_DEPLOYMENT_PLAN_OCI.md](WEBSITE_DEPLOYMENT_PLAN_OCI.md).

## 2. Next actions

1. ~~Implement the main-based application packaging and staged rollout~~ Done 2026-09-11; the OCI endpoint serves main on `ai_pin_db` ([app plan](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md)).
2. Fix the website's API base and the Kairos URL row so the OCI deployment can be tested end to end ([website plan](WEBSITE_DEPLOYMENT_PLAN_OCI.md)).
3. Scheduler component: jobs table + worker + Mosquitto on OCI, then device firmware ([app plan, component map](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md#component-map-everything-in-the-audit-moves-to-oci)).
4. Before the device goes live on OCI: off-VM backups, reboot check, reserved IP/hostname ([app plan checklist](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md#before-the-device-goes-live-on-oci)).
5. Retire Azure.

The [validation report](DEPLOYMENT_AUDIT.md#original-document-validation--historical-review) records the findings against the original documents;
its old line numbers are historical references.
