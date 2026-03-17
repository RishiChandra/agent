# README for setting up Local Postgres Server
## 1. Connect pgAdmin to your local server

In pgAdmin, right-click Servers → Register → Server
General tab: Name it something like local
Connection tab:
Host: localhost
Port: 5432
Username: postgres
Password: [use your computers password]
Click Save

### Windows instructions
You need to first have a local postgress server installed from postgresql.org/download/windows.
Confimr by presseing Windows Key + r and search for services.msc. Find and esnure postgress is running. 
## 2. Create the schema by dumping from Azure

Run this in your terminal:

Mac: pg_dump -h ai-pin-server.postgres.database.azure.com -U sssdddaaaa -d postgres --schema-only -f ~/schema.sql
Windows: & "C:\Program Files\PostgreSQL\17\bin\pg_dump.exe" -d "host=ai-pin-server.postgres.database.azure.com user=sssdddaaaa dbname=postgres sslmode=require" --schema-only -f "$HOME\schema.sql"
It will prompt for the password: Jymeisit1234 *manually type the pw, atleast for windows the powershell terminal didnot accept a pasted pw

Note -> if you get server version mismatch (on mac), run this instead:
/opt/homebrew/opt/postgresql@17/bin/pg_dump -h ai-pin-server.postgres.database.azure.com -U sssdddaaaa -d postgres --schema-only -f ~/schema.sql

## 3. Import Schema
Mac: /opt/homebrew/opt/postgresql@17/bin/psql -h localhost -U postgres -d postgres < ~/schema.sql
Windows: Remove-Item Env:PGSSLMODE
>>
>> & "C:\Program Files\PostgreSQL\17\bin\psql.exe" -h localhost -U postgres -d postgres -f "$HOME\schema.sql"
(password is your computer password)

You may see errors here, should be fine just confirm the tables exist in local now

## 4. Confirm Tables Created
You may use this to verify that tables were created:
Mac: /opt/homebrew/opt/postgresql@17/bin/psql -h localhost -U postgres -d postgres -c "\dt"
Windows: & "C:\Program Files\PostgreSQL\17\bin\psql.exe" -h localhost -U postgres -d postgres -c "\dt"
(password is your computer password)

## 5. Set up Variables
View the .env file in the root folder with local database variables:
LOCAL_DB_HOST=localhost
LOCAL_DB_PORT=5432
LOCAL_DB_NAME=local
LOCAL_DB_USER=postgres
LOCAL_DB_PASSWORD=[your computer password]

## 6. Virtual Environment
If no virtual environment exists, create a .venv in the test folder, then run:
Mac: source .venv/bin/activate
Windows: & c:/Users/Rishi/agent/.venv/Scripts/Activate.ps1
Install dependencies with:
pip install -r requirements.txt

## 7. Run Test
python integration/integration_test.py 