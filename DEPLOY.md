# Deploying the NIFTY 50 dashboard (Fly.io)

## Before you start: is this actually "free"?

Mostly, but be honest with yourself about this: Fly.io requires a card on
file, and today it's a small monthly usage credit (their "Hobby" plan) rather
than an unconditional free tier. One always-on 512MB machine for a
single-user, low-traffic app like this should comfortably stay inside that
free credit - but it is not a hard guarantee of $0.00 forever the way, say,
Oracle Cloud's "Always Free" VM tier is. If you want zero risk of ever being
charged a cent, say so and I'll walk you through an Oracle Cloud VM instead
(more setup work, but genuinely free-forever by policy). Fly.io is the path
below because it's dramatically less setup.

## What you're deploying

A single Docker container running the FastAPI app + its in-process
scheduler (live poll every 60s during market hours, the 17:00 IST daily
report job). It needs to **never sleep** (the scheduler must keep ticking)
and needs a **persistent disk** (SQLite DB + chart snapshots + Excel
reports survive restarts). `fly.toml` in this repo is already configured
for both.

## 1. Install the Fly CLI

PowerShell:
```powershell
iwr https://fly.io/install.ps1 -useb | iex
```
Close and reopen your terminal afterward so `fly` is on PATH.

## 2. Sign up / log in

```
fly auth login
```
Opens a browser to sign up (or log in) and link a card. You will not be
charged unless you exceed the free usage credit.

## 3. Pick a unique app name and edit fly.toml

Fly app names are global across every Fly user, so `nifty50-dashboard` is
almost certainly taken. Open `fly.toml` and change:
```
app = "REPLACE-WITH-YOUR-UNIQUE-APP-NAME"
```
to something unique, e.g. `app = "vijay-nifty50-ict"`.

## 4. Create the app (registers the name, doesn't deploy yet)

From the project root (where `fly.toml` and `Dockerfile` live):
```
fly apps create <your-app-name>
```

## 5. Create the persistent volume

This is what keeps your trade history/snapshots/reports across restarts
and deploys - skipping this means every redeploy wipes your data.
```
fly volumes create nifty_data --app <your-app-name> --region bom --size 1
```
(`bom` = Mumbai; `--size 1` is 1GB, plenty for SQLite + a few thousand PNG
chart snapshots. Bump it later with `fly volumes extend` if you ever need to.)

## 6. Set your real login credentials as secrets

Do **not** deploy with the default `vijay` / `changeme123` - anyone who
finds your URL would have full access, including the trade-log delete
buttons and the backtest trigger.
```
fly secrets set NIFTY_AUTH_USERNAME=your_username NIFTY_AUTH_PASSWORD=a_real_password --app <your-app-name>
```

## 7. Deploy

```
fly deploy --app <your-app-name>
```
This builds the Docker image on Fly's remote builder (you don't need Docker
installed locally) and ships it. First deploy takes a few minutes.

## 8. Verify

```
fly status --app <your-app-name>
fly logs --app <your-app-name>
```
You should see the same startup lines you see locally: "Application
starting up", "Scheduler started: live_poll...". Then open
`https://<your-app-name>.fly.dev` - you should hit the login page.

## Ongoing

- **Logs**: `fly logs --app <your-app-name>` (or `fly logs -a <name> -f` to
  follow live - useful for watching the 09:15 IST session start or the
  17:00 IST report job actually fire).
- **Redeploying after code changes**: just `fly deploy --app <your-app-name>`
  again - the persistent volume (your data) is untouched by redeploys.
- **Checking it's really not sleeping**: `fly status` should always show
  1 machine in the `started` state, never `stopped`.
- **Backups**: the SQLite DB and snapshots live entirely on the Fly volume.
  There's no automatic off-Fly backup - if you care about not losing trade
  history, periodically `fly ssh console -a <name>` and copy
  `/app/data/nifty_strategy.db` out, or ask me to wire up a scheduled
  export.
