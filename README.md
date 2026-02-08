# Onboarding

## Initial Setup
Clone the repo to whatever IDE you choose. Cursor AI is suggested.
Create a top level .env file. The contents can be copy pasted from the internal doc (ask Rishi)

## Create Virtual Environments
All folders (features for lack of a better term) in the root repo should have their own venv.
Ideally, all development is done in the venv and scoped into the particular feature that is being worked on.

```python -m venv .venv``` if python is not installed, can use py (windows only)

Start up the venv with ```.\.venv\Scripts\Activate.ps1```

If this is first time using the venv run:
```pip install -r requirements.txt``` for python reqs.

Deactivate with...```deactivate```

## Agent Developmet
The Agent logic lives in the app dir. *This should eventually be renamed
To run a local websocket server, run ```python main.py```

To test (local vs deployed can be configured in the test), run ```python -m app.test_ws``` from the test directory

Run ```python -m app.test_proactive_messaging``` from the test directory too to make sure that proactive messaging works

Instructions for deploying the app to Azure:
Mac Instructions:
Run ```bash azure-deploy-simple.sh ``` to deploy container

Windows Instructions:
Ensure you have git bash. All instructions will be assuming you are in bash.
Ensure you have docker desktop running. Try ```docker ps``` and see that it prints a table (empty is fine)
Run ```bash azure-deploy-simple.sh``` to deploy container

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


