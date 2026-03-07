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
## 2. Create the schema by dumping from Azure

Run this in your terminal:

pg_dump -h ai-pin-server.postgres.database.azure.com -U sssdddaaaa -d postgres --schema-only -f ~/schema.sql
It will prompt for the password: Jymeisit1234

Note -> if you get server version mismatch (on mac), run this instead:
/opt/homebrew/opt/postgresql@17/bin/pg_dump -h ai-pin-server.postgres.database.azure.com -U sssdddaaaa -d postgres --schema-only -f ~/schema.sql

## 3. Import Schema
/opt/homebrew/opt/postgresql@17/bin/psql -h localhost -U postgres -d postgres < ~/schema.sql
(password is your computer password)

## 4. Confirm Tables Created
You may use this to verify that tables were created:
/opt/homebrew/opt/postgresql@17/bin/psql -h localhost -U postgres -d postgres -c "\dt"
(password is your computer password)

## 5. Set up Variables
View the .env file in the root folder with local database variables:
LOCAL_DB_HOST=localhost
LOCAL_DB_PORT=5432
LOCAL_DB_NAME=postgres
LOCAL_DB_USER=postgres
LOCAL_DB_PASSWORD=[your computer password]

## 6. Virtual Environment
If no virtual environment exists, create a .venv in the test folder, then run:
source .venv/bin/activate
Install dependencies with:
pip install -r requirements.txt (mac)

## 7. 