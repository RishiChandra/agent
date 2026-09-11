# Connecting to the OCI PostgreSQL database with Beekeeper Studio

The database runs on the OCI VM `ai-assistant-server` as a host PostgreSQL 16 install. It only listens on
`127.0.0.1` and the private Docker bridge, so every connection from a laptop goes through an SSH tunnel.
No public database port exists and none should be opened.

## 1. Get the password

The `appuser` password is generated on the VM and stored only in the private backend env file. Read it there:

```sh
ssh -i /Users/rishi/projects/keys/agent/id_ed25519 ubuntu@146.235.229.232 'grep ^DB_PASSWORD= /home/ubuntu/app-backend-config/backend.env'
```

Paste it into Beekeeper's password field and nowhere else (not into chat, docs or Git).

## 2. Beekeeper connection settings

New connection → **PostgreSQL**.

| Field | Value |
|---|---|
| Host | `127.0.0.1` |
| Port | `5432` |
| User | `appuser` |
| Password | from step 1 |
| Default database | `ai_pin_db` |
| SSL | off (or "prefer"); the tunnel already encrypts the hop |

Enable **SSH Tunnel** on the same connection:

| Field | Value |
|---|---|
| SSH host | `146.235.229.232` |
| SSH port | `22` |
| SSH user | `ubuntu` |
| Auth | Private key: `/Users/rishi/projects/keys/agent/id_ed25519` (no passphrase prompt if the key has none) |

Test, then save as **`OCI — live (ai_pin_db)`**.

Duplicate the connection, change the database to `agent_rehearsal`, save as **`OCI — rehearsal`**. That copy is for
experiments; the app does not use it.

## 3. What you are looking at

| Database | Purpose | Edit freely? |
|---|---|---|
| `ai_pin_db` | **Live.** The OCI app reads and writes here | No. Every change is immediately visible to the app |
| `agent_rehearsal` | Staging clone of `ai_pin_db` taken 2026-09-11 | Yes |
| `postgres` | PostgreSQL's own default DB | `appuser` is not allowed in; ignore |

Tables in both application databases: `agents`, `agent_registry`, `agent_tasks`, `chat_members`, `chats`, `messages`,
`pending_text_message_jobs`, `relationships`, `sessions`, `tasks`, `users`.

## 4. If it doesn't connect

- **Tunnel fails**: check the key path, and that `ssh -i <key> ubuntu@146.235.229.232` works in a terminal.
- **Password rejected**: re-run step 1; the password was rotated on 2026-09-11 and may be rotated again (the env file is always current).
- **`FATAL: no pg_hba.conf entry`**: you connected to a database other than `ai_pin_db`/`agent_rehearsal`, or not via the tunnel.
- **Admin work** (create DB, extensions, roles): not possible as `appuser`. Use `ssh` + `sudo -u postgres psql` on the VM.

## 5. Other tools

Same settings work for TablePlus, DBeaver and pgAdmin (all support SSH tunnels), or from a terminal:

```sh
ssh -i /Users/rishi/projects/keys/agent/id_ed25519 -L 15432:127.0.0.1:5432 ubuntu@146.235.229.232 -N &
psql "host=127.0.0.1 port=15432 dbname=ai_pin_db user=appuser"
```
