# AI-pin backend — deployment docs

The backend runs on a self-hosted **Oracle Cloud Always Free** VM (migrated off Azure, September 2026; Azure retired). These
docs describe the live system by component: what each is, how and where it's deployed, and its open TODOs.

- **[OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md)** — the VM (specs, access), networking, Docker layout, secrets, build/release
  conventions, and the **hardening items** that stand between "working" and "production-safe" (off-VM backups, reboot check,
  stable address). Start here.
- **[BACKEND.md](BACKEND.md)** — the FastAPI app, its Caddy TLS front door, image build, deploy/rollback.
- **[DATABASE.md](DATABASE.md)** — host PostgreSQL `ai_pin_db`, the container→host bridge route, and how to connect (SSH tunnel).
- **[SCHEDULER.md](SCHEDULER.md)** — reminders: the `jobs` table, worker, Mosquitto broker, and the device firmware wake path.
- **[WEBSITE.md](WEBSITE.md)** — the Agent Registry static site served by the app.

Quick facts: VM `ai-assistant-server` (A1 ARM64, 2 OCPU/12 GB, us-sanjose-1); public `146.235.229.232` /
`2603:c024:c020:3700:0:537e:9221:8587` (sslip.io hostnames); `ssh -i /Users/rishi/projects/keys/agent/id_ed25519
ubuntu@146.235.229.232`. Repo onboarding / local dev is in the top-level [README](../README.md).

> Superseded: the earlier `DEPLOYMENT_AUDIT.md`, `DEPLOYMENT_OPTIONS.md`, `*_DEPLOYMENT_PLAN_OCI.md`, `POSTGRES_MIGRATION_PLAN.md`
> and `DB_CONNECT_BEEKEEPER.md` (the migration audit + step-by-step plans) were consolidated into these component docs. Their
> git history holds the blow-by-blow migration record if ever needed.
