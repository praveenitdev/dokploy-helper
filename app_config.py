import os

from dotenv import load_dotenv


load_dotenv(override=True)


def _get_env(name: str) -> str:
    return os.getenv(name, "").strip().strip('"').strip("'")

CLIENT_ID = _get_env("CLIENT_ID")
CLIENT_SECRET = _get_env("CLIENT_SECRET")
TENANT_ID = _get_env("TENANT_ID")

if not CLIENT_ID:
    raise RuntimeError("CLIENT_ID is not defined")
if not CLIENT_SECRET:
    raise RuntimeError("CLIENT_SECRET is not defined")
if not TENANT_ID:
    raise RuntimeError("TENANT_ID is not defined")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_PROFILE_ENDPOINT = "https://graph.microsoft.com/v1.0/me"
SCOPE = ["User.Read"]

SESSION_TYPE = "filesystem"
SESSION_PERMANENT = False
SESSION_USE_SIGNER = True

APP_SECRET_KEY = _get_env("APP_SECRET_KEY") or "change-this-secret-in-env"

AWS_REGION = _get_env("AWS_REGION") or "ap-south-1"
AWS_ACCESS_KEY_ID = _get_env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = _get_env("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN = _get_env("AWS_SESSION_TOKEN")
IAM_ROLE_ARN = _get_env("IAM_ROLE_ARN")
MONGODB_URI = _get_env("MONGODB_URI")
MONGODB_DB_NAME = _get_env("MONGODB_DB_NAME") or "dokploy"
HOSTED_ZONE_ID = _get_env("HOSTED_ZONE_ID")
HOSTED_ZONE_NAME = _get_env("HOSTED_ZONE_NAME") or "apps.poc.darwinbox.io"
DOKPLOY_BASE_URL = _get_env("DOKPLOY_BASE_URL")
DOKPLOY_API_KEY = _get_env("DOKPLOY_API_KEY")
DOKPLOY_API_TIMEOUT_SECONDS = int(_get_env("DOKPLOY_API_TIMEOUT_SECONDS") or "20")
PUBLIC_BASE_URL = _get_env("PUBLIC_BASE_URL")
PREFERRED_URL_SCHEME = _get_env("PREFERRED_URL_SCHEME") or (
    "https" if PUBLIC_BASE_URL.lower().startswith("https://") else "http"
)
