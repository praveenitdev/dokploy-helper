# Dokploy Helper Dashboard

A modern Flask dashboard for managing Dokploy helper operations.

## Features

- Microsoft Entra ID (Azure AD) login
- Professional dashboard layout with:
  - top header
  - profile dropdown on top right
  - collapsible left menu with submenu items
- DNS management for AWS Route53 hosted zone
- CRUD operations for CNAME records

## Environment variables

Copy `env_sample` to `.env` and fill values:

- `CLIENT_ID`
- `CLIENT_SECRET`
- `TENANT_ID`
- `APP_SECRET_KEY`
- `AWS_REGION`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_SESSION_TOKEN` (optional for temporary credentials)
- `IAM_ROLE_ARN` (optional, preferred; if set, app assumes this role for Route53 calls)
- `MONGODB_URI` (required for DNS metadata audit storage)
- `MONGODB_DB_NAME` (default: `dokploy`)
- `HOSTED_ZONE_ID` (optional if `HOSTED_ZONE_NAME` is set and discoverable)
- `HOSTED_ZONE_NAME` (default: `apps.poc.darwinbox.io`)
- `DOKPLOY_BASE_URL` (Dokploy server URL, for example `https://dokploy.example.com`)
- `DOKPLOY_API_KEY` (Dokploy API key sent as `x-api-key`)
- `DOKPLOY_API_TIMEOUT_SECONDS` (default: `20`)
- `DOKPLOY_AUTO_SYNC_ENABLED` (`true` to run continuous sync worker)
- `DOKPLOY_SYNC_INTERVAL_SECONDS` (default: `30`)
- `DOKPLOY_SYNC_ACTOR` (default: `System` for created_by/updated_by on auto-sync)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open: `http://localhost:5000`
# dokploy-helper

## Auto sync every 30 seconds

1. Configure these in `.env`:

```dotenv
DOKPLOY_AUTO_SYNC_ENABLED=true
DOKPLOY_SYNC_INTERVAL_SECONDS=30
DOKPLOY_SYNC_ACTOR=System
```

2. Run the worker in a separate process:

```bash
python dokploy_sync_worker.py
```

Keep it running under a process manager (systemd/supervisor/pm2) in production.

## Container Runtime

The Docker image now uses Supervisor to run both processes together:

- Web app process: `python app.py`
- Auto sync worker: `python dokploy_sync_worker.py`

If `DOKPLOY_AUTO_SYNC_ENABLED=false`, the worker exits with code 0 and Supervisor keeps the web process running.
