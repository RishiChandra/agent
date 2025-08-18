# Onboarding

## Initial Setup
Clone the repo to whatever IDE you choose. Cursor AI is suggested.
Create a top level .env file. The contents can be copy pasted from the internal doc (ask Rishi)

## Create Virtual Environment
```python -m venv .venv``` if python is not installed, can use py (windows only)

Start up the venv with ```.\.venv\Scripts\Activate.ps1```

If this is first time using the venv run:
```pip install -r requirements.txt``` for python reqs.
Install Azure Functions Core Tools and CLI with
```
npm install -g azure-functions-core-tools@4 --unsafe-perm true
pip install azure-cli
pip install azure-functions
pip install azure-identity
```

Deactivate with...```deactivate```

## Function Development & Deployment
Virtual environment IS required unless you have installed the azure libraries locally

### Local Testing
This is a good way to test compilation/behavior before actual deployment
```func start```

### Azure Deployment
If this is the first time, follow these steps to get setup:
Consult with Jason on getting connected to the Azure account. Once setup run:
```az login```
When prompted choose the right subscription: Microsoft Azure Sponsorship

Deploy with:
```func azure functionapp publish aipin```

If you notice the deployment is suprisingly large (>50kb), cancel asap.
There are probably some uneeded packages being installed. Update the .funcignore file.

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