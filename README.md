## Run Function Locally
```func start```

## Deploy function to Azure:
```func azure functionapp publish aipin```

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

## Run Python Code Locally
Start up the venv with ```.\.venv\Scripts\Activate.ps1```
Deactivate with...```deactivate```