"""Settings, read from the environment (and an optional .env file in the repo root)."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DB_PATH = Path(os.environ.get("KANAKU_DB", ROOT / "kanaku.db"))
# The sandbox bank lives in its own database on purpose: it stands in for an
# external system that Kanaku Pulla does not control.
BANK_DB_PATH = Path(os.environ.get("KANAKU_BANK_DB", ROOT / "sandbox_bank.db"))
VENDOR_REGISTER = Path(os.environ.get("KANAKU_VENDORS", DATA_DIR / "vendors.json"))
PURCHASE_ORDERS = Path(os.environ.get("KANAKU_POS", DATA_DIR / "purchase_orders.json"))
# Public data goes stale: cached Crustdata answers older than this are fetched again.
CACHE_MAX_AGE_HOURS = float(os.environ.get("KANAKU_CACHE_MAX_AGE_HOURS", "24"))

MODEL = os.environ.get("KANAKU_MODEL", "claude-opus-5-5")
AUTO_PAY_LIMIT_INR = float(os.environ.get("KANAKU_AUTO_PAY_LIMIT_INR", "500000"))

# Hackathon rule: agents only talk to our own team. Every outgoing message is
# checked against this list before it is written to the outbox.
TEAM_EMAILS = [
    e.strip().lower()
    for e in os.environ.get("KANAKU_TEAM_EMAILS", "ap-lead@kanaku.local,ops-lead@kanaku.local").split(",")
    if e.strip()
]

# Crash simulation for the demo: name a checkpoint and the process dies there.
CRASH_AT = os.environ.get("KANAKU_CRASH_AT", "").strip()


# Test switches: OFFLINE means cache only (no live Crustdata calls); NO_LLM forces the regex reader
# and template explanations even if an Anthropic key is present.
OFFLINE = os.environ.get("KANAKU_OFFLINE", "") == "1"
NO_LLM = os.environ.get("KANAKU_NO_LLM", "") == "1"


def crustdata_key() -> str:
    if OFFLINE:
        return ""
    key = os.environ.get("CRUSTDATA_API_KEY", "").strip()
    if key:
        return key
    # Fallback for local dev: the token stored by the Crustdata Claude Code plugin.
    creds = Path.home() / ".crustdata" / "credentials.json"
    try:
        return json.loads(creds.read_text(encoding="utf-8"))["access_token"]
    except Exception:
        return ""


def anthropic_available() -> bool:
    if NO_LLM:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
