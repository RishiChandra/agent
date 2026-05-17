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
curl -L -o vosk-model.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
python -c "import zipfile; zipfile.ZipFile('vosk-model.zip').extractall('.')"
rm vosk-model.zip
```

Sanity check: ```vosk-model-small-en-us-0.15/am/final.mdl``` must exist.

### 3. Piper TTS voice (~60 MB, gitignored)

Cross-platform neural TTS. Same ```.onnx``` voice file is used by the local server **and** the deployed server, so both sound identical.

```bash
mkdir -p piper_voices
curl -L -o piper_voices/en_US-amy-medium.onnx       https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx
curl -L -o piper_voices/en_US-amy-medium.onnx.json  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
```

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

## Deploy to Azure App Service

Zip deploy, no Docker. Done by ```azure-deploy.sh```. Target: ```ai-pin``` resource group, ```websocket-ai-pin``` Linux App Service (Python 3.12, B1).

### Prereqs

- Azure CLI installed and ```az login``` complete (refresh token expires after 90d — re-login if you get ```AADSTS700082```).
- Python 3.12 venv active. Script invokes ```$PYTHON_BIN``` to build the zip via ```zipfile``` stdlib.
- ```.env``` populated. ```azure-deploy.sh``` reads ```.env``` and pushes the values as App Settings.
- Local Vosk model + Piper voice exist (steps 2–3 above). Preflight checks for ```vosk-model-small-en-us-0.15/am/final.mdl``` and ```piper_voices/en_US-amy-medium.onnx```.
- **Windows**: run via **Git Bash**, not WSL stub. PowerShell ```bash azure-deploy.sh``` defaults to WSL on Windows — use ```& "C:\Program Files\Git\bin\bash.exe" azure-deploy.sh``` instead, or open Git Bash directly.

### One-time bootstrap: upload heavy assets to ```/home/data/```

Vosk model + Piper voice live on App Service's persistent ```/home``` volume — not in every deploy zip. Upload them **once** so future deploys ship just app code (~MB, not ~100 MB):

```bash
# Build a bundle of both data dirs
python -c "
import zipfile, os
EXCLUDE = {'__pycache__', '.pytest_cache'}
def walk(zf, root):
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE]
        for f in files:
            if f.endswith('.pyc'): continue
            p = os.path.join(r, f)
            zf.write(p, arcname=p.replace(os.sep, '/'))
with zipfile.ZipFile('data-bundle.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    walk(zf, 'vosk-model-small-en-us-0.15')
    walk(zf, 'piper_voices')
"

# Upload + extract to /home/data via Kudu (uses ARM bearer auth — no publishing creds)
TOKEN=$(az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv)
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/zip" \
     --data-binary "@data-bundle.zip" \
     "https://websocket-ai-pin-fbbrhfawfkb7ecf3.scm.westus2-01.azurewebsites.net/api/zip/data/"
rm data-bundle.zip
```

This step only repeats when the Vosk model or Piper voice file changes.

### Deploy

```bash
# Git Bash (Windows) or any bash (macOS/Linux)
bash azure-deploy.sh
```

What it does:

1. Pushes app settings from ```.env``` (```VOSK_MODEL_PATH=/home/data/...```, ```PIPER_MODEL_PATH=/home/data/...```, secrets).
2. Sets the startup file: ```bash -c "apt-get update -qq && apt-get install -y -qq libopus0 && cd app && python -m uvicorn main:app --host 0.0.0.0 --port 8000 --ws websockets"``` — installs libopus on every cold start.
3. Builds a slim zip (app code + ```requirements.txt```, no data assets).
4. Pushes the zip via ```az webapp deploy```. Oryx repacks into ```output.tar.zst``` and extracts at runtime.
5. Polls deployment status for up to 15 min, then prints URLs.

### URLs

- App:    ```https://websocket-ai-pin-fbbrhfawfkb7ecf3.westus2-01.azurewebsites.net```
- WS:     ```wss://websocket-ai-pin-fbbrhfawfkb7ecf3.westus2-01.azurewebsites.net/ws```
- Health: ```/healthz```

### Viewing logs

```bash
# Live tail (Ctrl+C to stop)
az webapp log tail --name websocket-ai-pin --resource-group ai-pin

# Or via Kudu in a browser
# https://websocket-ai-pin-fbbrhfawfkb7ecf3.scm.westus2-01.azurewebsites.net/newui/fileManager → /home/LogFiles/
```

If logging stops working: ```az webapp log config --name websocket-ai-pin --resource-group ai-pin --application-logging filesystem --level information --docker-container-logging filesystem```.

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
View here
<img width="1715" height="855" alt="Screenshot 2026-02-07 at 14 32 28" src="https://github.com/user-attachments/assets/d03fd709-93c7-4aae-9b97-8adeb63688e1" />

## Mobile App (Flutter)
Prereqs: Flutter SDK installed and a device/simulator available.
From ```mobile_app```: run ```flutter pub get``` then ```flutter run```.
List devices with ```flutter devices```. Run ```flutter clean``` if builds get stuck.

## Database
We host a postgres sql server in our Azure resource group.
Use pgAdmin4 (or other sql client of choice) to connect to the db.
Credentials can be found in internal docs (ask Rishi) or in the env vars of the web app / app service.

## Listener Function app
Deploy with ```func azure functionapp publish listener --python``` in listener dir

View Listener Logs Here:
<img width="1672" height="879" alt="Screenshot 2026-02-07 at 15 19 02" src="https://github.com/user-attachments/assets/b93e9b30-8652-46f1-b970-29a239fd7f46" />

Note when publishing:
- Make sure your func is up to date, and azure core tools
- Ask cursor to view the deployment logs via Azure CLI 

Testing:
You can quickly create a task with ```python testing/quick_enqueue.py 1```
You can also test the task reminder feature by running  ```python test/app/test_task_reminder.py```, which will start a websocket connection with an initial message if the user is not in session or defer by 1 minute.

You can see the current Task Queue for the Service Bus on the Azure Portal:
<img width="2560" height="1271" alt="screencapture-portal-azure-2025-12-07-17_27_29" src="https://github.com/user-attachments/assets/2d820d6c-1b2e-470c-ae72-aa097f54bb2a" />


