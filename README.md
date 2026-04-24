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

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open: `http://localhost:5000`
# dokploy-helper
