# Onboarding

## Initial Setup
Clone the repo to whatever IDE you choose. Cursor AI is suggested.
Create a top level .env file. The contents can be copy pasted from the internal doc (ask Rishi)

## Create Virtual Environment
Follow these steps in the root directory and in the test directory

```python -m venv .venv``` if python is not installed, can use py (windows only)

Start up the venv with ```.\.venv\Scripts\Activate.ps1```

If this is first time using the venv run:
```pip install -r requirements.txt``` for python reqs.

Deactivate with...```deactivate```

## Git Rules
Create a new branch: ```git checkout -b <name>```
Write code :)
Stage Changes, Commit and publish branch on cursor.
On Github UI make PR.
Get approved, merge + delete branch.

Local Cleanup:
Go to main branch
Delete remote branches: ```git fetch --prune```
Delete local branches: ```git branch -D branch-name```

In general keep PRs as small as feasible. Minimize commit and branch complexity for everyone's sake.

## Database
We host a postgress sql server in our Azure resource group.
Use pgAdmin4 (or other sql client of choice) to connect to the db.
Credentials can be found in internal docs (ask Rishi) or in the env vars of the web app / app service.