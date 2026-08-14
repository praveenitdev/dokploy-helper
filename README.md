# Dokploy Helper Dashboard

A modern Flask dashboard for managing Dokploy helper operations.

## Features

- Microsoft Entra ID (Azure AD) login
- Professional dashboard layout with:
  - top header
  - profile dropdown on top right
  - collapsible left menu with submenu items
- DNS management for AWS Route53 hosted zone
- ECR repository auto-create for Dokploy apps (`dokploy/<appName>`)
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
- `ECR_AUTO_CREATE_ENABLED` (`true` to ensure ECR repos for Dokploy apps)
- `ECR_REGISTRY_ID` (AWS account id, e.g. `816930190089`)
- `ECR_REPO_PREFIX` (default: `dokploy` → repos named `dokploy/<appName>`)
- `ECR_SCAN_ON_PUSH` (default: `true`)
- `ECR_LIFECYCLE_KEEP_COUNT` (default: `30`)
- `PUBLIC_BASE_URL` / `PREFERRED_URL_SCHEME` (OAuth redirect behind proxy)

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

## ECR auto-create

When `ECR_AUTO_CREATE_ENABLED=true`, each sync cycle:

1. Lists Dokploy applications/compose services via `project.all`
2. Ensures ECR repository `{ECR_REPO_PREFIX}/{appName}` exists
3. Stores metadata in Mongo collection `ecr` and writes audit events

UI: **Infrastructure → ECR**

- **Sync Dokploy Apps** — ensure repos for all discovered apps
- **Ensure Repo** — create a single `dokploy/<appName>` repository

Image path used by Dokploy registry swarm mode:

`{account}.dkr.ecr.{region}.amazonaws.com/dokploy/<appName>:latest`
