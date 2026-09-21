# Onboarding

## Initial Setup
Clone the repo to whatever IDE you choose. Cursor AI is suggested.
Create a top level ```.env``` file. Contents can be copy-pasted from the internal doc (ask Rishi).

## Local Setup

This project needs a Python venv plus three binary assets that are **gitignored** (Vosk STT model, Piper TTS voice, Opus native lib on Windows). Pip alone won't get you a working stack — do all five steps below.

### 1. Python venv + deps

Each top-level "feature" folder has its own venv. Set up both the **root** venv and the **test** venv (```test/.venv```).

```powershell
# Root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Test
python -m venv test\.venv
.\test\.venv\Scripts\Activate.ps1
pip install -r test\requirements.txt
deactivate
```

Bash equivalent: ```. .venv/Scripts/activate``` (Git Bash on Windows) or ```source .venv/bin/activate``` (macOS/Linux).

### 2. Vosk STT model (~68 MB, gitignored)

```bash
python scripts/setup_vosk_model.py
```

The script idempotently downloads and unpacks ```vosk-model-small-en-us-0.15``` at the repo root, so no env var change is needed locally. Pick a different model with ```--model <name>``` (see [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)). Sanity check: ```vosk-model-small-en-us-0.15/am/final.mdl``` must exist.

### 3. Piper TTS voice (~60 MB, gitignored)

Cross-platform neural TTS. Same ```.onnx``` voice file is used by the local server **and** the deployed server, so both sound identical.

```bash
python scripts/setup_piper_voice.py
```

The script idempotently downloads ```en_US-amy-medium``` into ```piper_voices/``` — matches the default in ```app/developer_ws/tts.py``` so no env var change is needed locally. Pick a different voice with ```--voice <id>``` (see [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)).

### 4. Windows: install Opus native library

```opuslib``` is a Python binding that loads the native ```opus.dll```. Pip does not install the DLL.

Root venv:

```powershell
.\.venv\Scripts\python.exe scripts\install_opus_windows.py
```

Test venv (only if you have ```test\.venv```):

```powershell
.\test\.venv\Scripts\python.exe scripts\install_opus_windows.py
```

macOS/Linux: install via your package manager (```brew install opus``` / ```apt-get install libopus0```). No extra script needed.

### 5. ```.env``` at repo root

Pulled from the internal doc — ask Rishi. Contains ```AZURE_OPENAI_*```, ```DB_*```, ```GOOGLE_API_KEY```, ```AZURE_SERVICEBUS_CONNECTION_STRING```, etc.

## Agent Development
The Agent logic lives in the app dir. *This should eventually be renamed*

To run a local websocket server, run ```python app/main.py```

To test (local vs deployed can be configured in the test), run ```python test/app/test_ws.py``` from the repo root (or `cd test/app` then ```python test_ws.py```)

Run ```python -m app.test_proactive_messaging``` from the test directory too to make sure that proactive messaging works

### developer_ws bridge end-to-end test
Default brings up main, the mic client, and the echo relay (each in its own console) without auto-pinging — say "call the service" to open the bridge: ```python app/developer_ws/testing/run_full_test.py --no-ping``` (drop ```--no-ping``` to have the echo server auto-call main on startup).
To run manually instead: ```python app/main.py```, then ```python test/app/developer/test_developer_ws.py```, then (from `app/`) ```python developer_ws/testing/echo_server.py``` (append ```--ping <user_id>``` for the auto-call variant).
See ```app/developer_ws/DESIGN.md``` and ```BRIDGE_PROTOCOL.md``` for architecture and wire protocol.

## Deployment (OCI)

The backend runs on a self-hosted **Oracle Cloud Always Free** VM (migrated off Azure in September 2026; Azure retired). It
is deployed as Docker containers behind Caddy, not via App Service. Full, per-component deployment docs live at the repo root:

- [OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md) — the VM (specs, SSH access), networking, Docker layout, secrets, build/release conventions, and the hardening TODOs. **Start here.**
- [BACKEND.md](BACKEND.md) — the FastAPI app + Caddy TLS front door; image build; deploy/rollback.
- [DATABASE.md](DATABASE.md) — host PostgreSQL `ai_pin_db` and how to connect (SSH tunnel).
- [SCHEDULER.md](SCHEDULER.md) — reminders: jobs table + worker + Mosquitto + the device firmware wake path.
- [WEBSITE.md](WEBSITE.md) — the Agent Registry site served by the app.

Public app: `https://146-235-229-232.sslip.io` (health at `/healthz`). Live logs on the VM:
`docker logs app-backend-app-1` / `docker logs app-backend-worker-1`.

## Git Rules
Create a new branch: ```git checkout -b <name>```
Write code :)
Stage Changes, Commit and publish branch on cursor.
On Github UI make PR.
Get approved, merge + delete branch.

Local Cleanup:
Go to main branch
Delete remote branches: ```git fetch --prune```
Delete all local branches (except main/master and current):
- **Git Bash**: ```git branch | grep -v "^\*\|main\|master" | xargs git branch -D```
- **PowerShell**: ```git branch | Where-Object { $_ -notmatch '^\*|main|master' } | ForEach-Object { git branch -D $_.Trim() }```
Or delete a specific branch: ```git branch -D branch-name```

In general keep PRs as small as feasible. Minimize commit and branch complexity for everyone's sake.

## Server Logs
On the VM: `docker logs -f app-backend-app-1` (backend) or `docker logs -f app-backend-worker-1` (reminder worker). See
[OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

## Mobile App (Flutter)
Prereqs: Flutter SDK installed and a device/simulator available.
From ```mobile_app```: run ```flutter pub get``` then ```flutter run```.
List devices with ```flutter devices```. Run ```flutter clean``` if builds get stuck.

## Database
PostgreSQL `ai_pin_db`, hosted on the OCI VM (moved off Azure). Connect with any SQL client over an SSH tunnel — full
settings, the SSH-tunnel steps, and how to read the password are in [DATABASE.md](DATABASE.md).

## Reminders / scheduler
The Azure Service Bus queue + Function App `listener` + IoT Hub were replaced by a PostgreSQL `jobs` table, a worker
container, and the Mosquitto broker on the VM — see [SCHEDULER.md](SCHEDULER.md) for the full flow and the device wake path.

Testing:
- Create a task quickly: `python testing/quick_enqueue.py 1`.
- Exercise the reminder path: `python test/app/test_task_reminder.py` (opens a WebSocket with an initial message if the user
  is not in session, or defers by 1 minute).
- Watch the queue drain: `docker logs -f app-backend-worker-1` on the VM, or query the `jobs` table (`SELECT * FROM jobs`).


## Improvements Needed

Three known latency wins for the deployed voice loop. Baseline (measured ```2026-05-22``` against B1, ```"hello can you hear me"```): ~9 s from stopped-talking to first audio out. Breakdown: 2.0 s silence timer + 3.3 s Vosk STT + 1.7 s Gemini + 1.6 s Piper TTS first chunk + ~200 ms downlink coalesce.

### 1. More compute

The old Azure baseline above was measured on a throttled App Service B1. On OCI the app runs on the Always-Free A1 VM
(2 ARM OCPUs / 12 GB, **shared** with the worker and host PostgreSQL); a first OCI measurement was ~3.6 s end-of-speech →
first reply. If inference CPU becomes the bottleneck, the lever is a larger (paid) OCI shape or moving Vosk/Piper to a
dedicated VM, rather than an Azure SKU change. See [OCI_INFRASTRUCTURE.md](OCI_INFRASTRUCTURE.md).

### 2. Lower the end-of-utterance silence timer

```DEVELOPER_WS_END_SILENCE_SEC``` currently defaults to ```2.0```. Set it to ```1.0``` (or even ```0.7```) in the app's env file (`/home/ubuntu/app-backend-config/backend.env` on the VM) to shave that off every turn.

Expected impact: ~1.0 s shaved.

Trade-off: slow speakers or natural mid-thought pauses get cut off. Easy to A/B — change the value and restart the app container (`docker compose -p app-backend -f docker-compose.oci.yml up -d`).

### 3. Streaming Gemini (token / sentence)

Today [```CustomGeminiLLMService._call_gemini```](app/developer_ws/pipecat_llm.py) does a blocking ```generate_content``` and waits for the full response before any token reaches Piper. Switching to ```generate_content_stream``` lets us aggregate text per sentence and start Piper synthesis on sentence 1 while Gemini is still generating sentence 2/3.

Expected impact: ~500–1500 ms shaved on multi-sentence replies (compounds with the streaming TTS we already shipped — see [```tts.py```](app/developer_ws/tts.py) ```synthesize_speech_pcm24_stream```).

Implementation:
1. Replace the ```call_gemini``` thread call in ```_call_gemini``` with an async iterator over ```client.aio.models.generate_content_stream(...)```.
2. Buffer streaming text until a sentence boundary (```.```, ```?```, ```!```), then push an ```LLMTextFrame``` for that sentence. ```PiperTTSProcessor``` already handles one-text-frame-per-sentence cleanly.
3. Function calls still need to be aggregated to completion before dispatch (they can arrive across multiple chunks).

This is the most invasive of the three but the only one that can take per-reply latency below P1 v3's CPU floor.

### Stacking estimate

With all three: ~9 s → **~3 s** stopped-talking → first audio. Closer to feeling conversational.


