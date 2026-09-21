# Website (Agent Registry site)

A small static site served by the [backend](BACKEND.md) itself — there is no separate hosting resource on OCI (or, before,
on Azure). Infrastructure basics: [OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

## What / where

Three files in `agent_directory/`, served at `/` by the app (`app/main.py`, `_STATIC_DIR`), so they ship **inside the app
image** and deploy with it:

| File | Purpose |
|---|---|
| `index.html` | Register / edit / delete agents; lists registered agents. Calls `GET/POST /api/agents`, `PUT/DELETE /api/agents/{id}` |
| `test.html` | Browser-mic client for the orchestrator: connects to `/ws/developer/{user_id}`, plays Opus replies, lists callable agents |
| `style.css` | Styling |

Live at `https://146-235-229-232.sslip.io/` and `/test.html`.

## Host-agnostic API base (fixed during migration)

Originally both pages hard-coded the Azure hostname and only treated `localhost` or that Azure host as same-origin — so when
served from any other host, every API/registry call went back to Azure. Now both pages use **same-origin whenever served over
`http(s)`**, honor a `?api=` override, and fall back to the compiled `ORCHESTRATOR` constant (the OCI hostname) only for
`file://`. `test.html` derives its WebSocket base from the API base instead of hard-coding a `wss://` URL. No `azurewebsites`
reference remains under `agent_directory/`. Changing the page's API target is now just editing the `ORCHESTRATOR` fallback
constant (only used for `file://`), or passing `?api=`.

## Kairos bridge URL (data, in `ai_pin_db`)

Kairos is the `/ws/{user_id}` server inside the same backend, so its bridge `agent_url` in the `agents` table points at the
OCI host: `wss://146-235-229-232.sslip.io/ws/{user_id}` (repointed from the old Azure URL during migration). Any agent row
containing an Azure hostname would dial Azure; there are none left.

## Deploying a change

The site is baked into the app image, so a change means a rebuild + redeploy (see [BACKEND.md](BACKEND.md)). Verify with
`curl -s https://146-235-229-232.sslip.io/ | grep -c azurewebsites` → `0`.

## TODOs

- [ ] **Kairos URL is hostname-coupled.** The `agents` row holds an absolute `wss://…sslip.io/ws/{user_id}`, so it must be
  re-`UPDATE`d whenever the public hostname changes (the [stable-address](OCI_INFRASTRUCTURE.md#hardening--pending-before-this-is-production-safe)
  item). Better: derive the Kairos URL from the request host in code so the row never needs editing.
- [ ] Optional: a manual browser pass of register → edit → delete from the site and a mic voice test with a Kairos handoff
  (the underlying paths are proven via API and the automated tests, but a hands-on click-through hasn't been recorded here).
