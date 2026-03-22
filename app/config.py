import logging
import subprocess
from pathlib import Path

from dotenv import load_dotenv
import os

# Load .env from the project root (~/jri/.env)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# GitHub OAuth
GITHUB_CLIENT_ID: str = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET: str = os.environ.get("GITHUB_CLIENT_SECRET", "")

# App secret key (for signing sessions/tokens)
SECRET_KEY: str = os.environ.get("SECRET_KEY", "")

# Stripe
STRIPE_SECRET_KEY: str = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY: str = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

X402_PAY_TO_ADDRESS: str = os.environ.get("X402_PAY_TO_ADDRESS", "")
X402_FACILITATOR_URL: str = os.environ.get(
    "X402_FACILITATOR_URL", "https://x402.org/facilitator"
)
X402_NETWORK: str = os.environ.get("X402_NETWORK", "eip155:84532")
X402_RALPH_PRICE_USD: str = os.environ.get("X402_RALPH_PRICE_USD", "$20.00")

# Base URL (used for OAuth callbacks, Stripe redirects, etc.)
BASE_URL: str = os.environ.get("BASE_URL", "https://justralph.it")

# Data directory for persistent storage
DATA_DIR: Path = Path.home() / "jri" / "data"

# Ralph bot GitHub token – read from gh CLI at import time and cached
def _get_ralph_bot_github_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

RALPH_BOT_GITHUB_TOKEN: str = _get_ralph_bot_github_token()

# ── Startup validation ──
_REQUIRED = {
    "GITHUB_CLIENT_ID": GITHUB_CLIENT_ID,
    "GITHUB_CLIENT_SECRET": GITHUB_CLIENT_SECRET,
    "SECRET_KEY": SECRET_KEY,
}
_missing = [name for name, val in _REQUIRED.items() if not val]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_missing)}"
    )

if not STRIPE_SECRET_KEY:
    logging.getLogger(__name__).warning(
        "STRIPE_SECRET_KEY not set — Stripe payments will not work"
    )

if not X402_PAY_TO_ADDRESS:
    logging.getLogger(__name__).info(
        "X402_PAY_TO_ADDRESS not set — x402 payments are disabled"
    )
