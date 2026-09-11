# Website deployment plan and status — OCI

Written 2026-09-11 after the app backend cutover. Component 6 in the [deployment audit](DEPLOYMENT_AUDIT.md#26-agent-registry-website-component-6--added-2026-09-11).

## What the website is

The Agent Registry site is three static files in `agent_directory/`:

| File | Purpose | API it uses |
|---|---|---|
| `index.html` | Register / edit / delete agents; lists registered agents | `GET/POST /api/agents`, `PUT/DELETE /api/agents/{id}` |
| `test.html` | Browser microphone client: connects to `/ws/developer/{user_id}`, plays Opus replies, lists agents you can "call" | `WS /ws/developer/{user_id}`, `GET /api/agents?active_only=true` |
| `style.css` | Styling | — |

There is no separate hosting resource on Azure or OCI. The backend serves the directory at `/` (`app/main.py`, `_STATIC_DIR`),
so the site ships inside the app image and moved with it. **The site is already live on OCI** at
`https://146-235-229-232.sslip.io/` (verified 2026-09-11: `/`, `/test.html`, `/style.css` all 200; `test.html` opened a
developer WebSocket session against the OCI app).

## Why the OCI site still talks to Azure

`index.html` decides where the API lives with this rule (line 87–95):

```js
const ORCHESTRATOR = "https://websocket-ai-pin-fbbrhfawfkb7ecf3.westus2-01.azurewebsites.net";
// same-origin only for localhost/127.0.0.1 or when the page's hostname is inside ORCHESTRATOR; otherwise ORCHESTRATOR
```

Loaded from `146-235-229-232.sslip.io`, neither condition holds, so **every registry call from the OCI Register page goes to
Azure**: registrations land in the Azure database and the list shown is Azure's. `test.html` in `main` had the same rule
plus a hardcoded Azure `wss://` base in its input field (an earlier note that it was already correct came from the stale VM
stack's copy of the page, not from `main`).

Separately, the Kairos row in the `agents` table (copied from Azure into `ai_pin_db`) has
`agent_url = wss://websocket-ai-pin-….azurewebsites.net/ws/{user_id}`. When the OCI orchestrator hands a call to Kairos,
it dials Azure. Any other rows containing the Azure hostname: none (`agent_registry` has 0 matches).

Third-party agents self-register their own public `wss://` URLs; those are not affected.

## Decision

Keep the site served by the backend at `/` (see [options §1.8](DEPLOYMENT_OPTIONS.md#18-website-added-2026-09-11)). No new
hosting. The work is three small changes: two in the site, one in the data.

## Implementation steps

### 1. Make the site host-agnostic (repo change on `codex/oci-application-main`)

- [x] `index.html`: use the same rule as `test.html` — same-origin whenever the page is served over `http(s)`, `?api=` override
  first, `ORCHESTRATOR` only for `file://`. Replace the `ORCHESTRATOR` constant with the OCI hostname (it becomes the real DNS
  name once one exists; see the [before-go-live checklist](APP_BACKEND_DEPLOYMENT_PLAN_OCI.md#before-the-device-goes-live-on-oci)).
- [x] `test.html`: replace the `ORCHESTRATOR` fallback constant the same way. Logic unchanged.
- [x] Remove the now-wrong comment "same-origin when the orchestrator itself serves us (localhost dev or the Azure app)".
- [x] Local check: open both pages via `?api=http://localhost:8000`-style override and via same-origin; confirm `API_BASE` and
  `WS_BASE` resolve as expected (a two-line `console.log` while testing is fine, remove before commit).

**Complete when:** neither file contains `azurewebsites.net`, and the same-origin rule is identical in both pages.

### 2. Rebuild and redeploy the app image (the site is baked into it)

- [x] Sync `agent_directory/` to the VM build directory `/home/ubuntu/releases/app-backend-step2-20260911/agent_directory/`
  (or rebuild from a fresh checkout of the branch), rebuild with the same Dockerfile, tag `codex-app-backend:step3-<tree-sha>`.
- [x] `docker compose -p app-backend -f docker-compose.oci.yml up -d` with `APP_IMAGE` set to the new tag. Downtime is the
  container restart (about 10 s to healthy; open WebSocket sessions drop and reconnect).
- [x] Verify `curl -s https://146-235-229-232.sslip.io/ | grep -c azurewebsites` returns 0, `/healthz` 200.

**Complete when:** the OCI site no longer references Azure and the app is healthy on the new image.

### 3. Repoint the Kairos bridge URL (data change in `ai_pin_db`)

Kairos is the `/ws/{user_id}` server inside the same backend, so on OCI its bridge URL is the OCI hostname:

```sql
-- run as postgres on the VM, inside a transaction, after checking the row
UPDATE agents SET agent_url = 'wss://146-235-229-232.sslip.io/ws/{user_id}'
 WHERE agent_id = '1af8ac73-794d-456c-87c4-738ba12162a9' AND agent_url LIKE '%azurewebsites.net%';
```

- [x] Take a dump first (there is a pre-cutover dump; a fresh one is 1 s).
- [x] Apply, `SELECT` to confirm exactly one row changed, commit.
- [ ] Redo this when the hostname changes at the stable-address step; better, make the Kairos URL relative or derived from
  the request host in code so the row never needs editing (candidate for the scheduler/registry cleanup, not required now).

**Complete when:** no row in `agents` or `agent_registry` contains `azurewebsites.net`.

### 4. End-to-end test of the OCI deployment from the website

- [x] Open `https://146-235-229-232.sslip.io/` in Chrome/Edge (WebCodecs needed for audio playback).
- [ ] Register page: add a throwaway agent, see it in the list, edit it, delete it. Confirm the rows appear in `ai_pin_db`
  (`SELECT` on the VM) and not in Azure.
- [ ] Test orchestrator page: connect with the microphone, ask a question, hear the reply. Say "call Kairos" (or whatever
  the routing phrase is) and confirm the bridge dials the OCI hostname (app log shows the dial target) and audio returns.
- [ ] Record results here; anything failing in the bridge path is a backend issue to log in the app plan's Step 4 open items.

**Complete when:** register/edit/delete and a voice session including a Kairos handoff all work from the OCI site.

## Status

- [x] Site served on OCI (moved with the app image, 2026-09-11).
- [x] Step 1 (2026-09-11 UTC): both pages now use same-origin for any `http(s)` page, `?api=` override first, `ORCHESTRATOR`
  fallback = `https://146-235-229-232.sslip.io` (file:// only). `test.html` no longer hardcodes the WSS base: it derives
  `WS_BASE` from `API_BASE`/`location.host` and pre-fills the input. No `azurewebsites.net` left under `agent_directory/`.
  Changes are on branch `codex/oci-application-main`, uncommitted.
- [x] Step 2 (2026-09-11 UTC): rebuilt `codex-app-backend:step3-0c9b6894a43f` (image `sha256:0780c84c8f38…`,
  labels `revision=4b1fc89…-worktree`, `source-tree-sha256=0c9b6894…`), redeployed; healthy in 12 s; public `/` and
  `/test.html` contain no Azure hostname. Current tag recorded in `/home/ubuntu/releases/app-backend-step2-20260911/CURRENT_IMAGE.env`.
- [x] Step 3 (2026-09-11 UTC): dump `ai_pin_db-prekairos-<ts>.dump` taken; `UPDATE 1` in a transaction; Kairos row now
  `wss://146-235-229-232.sslip.io/ws/{user_id}`; zero `azurewebsites` references left in `agents`. `/api/agents` reflects it.
- [x] Step 4, automated part: loaded both pages in a real browser from the OCI hostname: no console errors; `wsbase`
  pre-filled with `wss://146-235-229-232.sslip.io`; `GET /api/agents` and `/api/agents?active_only=true` went to the OCI origin (200).
- [x] Step 4, manual part (user, 2026-09-11 06:18 UTC): "worked perfectly". App log confirms a Kairos handoff through
  `wss://146-235-229-232.sslip.io/ws/{user_id}`: hello sent, ACK accepted (`service_id=kairos`), 123 frames sent / 21 received,
  clean close. Hairpin from the container to the public hostname works (contrary to the `oracle-deploy` branch's note).

## Rollback

Steps 1–2: redeploy the previous image tag `codex-app-backend:step2-e4cd5e194c70`. Step 3: restore the previous `agent_url`
value (recorded above) with the same `UPDATE`. Nothing on Azure changes in this plan.
