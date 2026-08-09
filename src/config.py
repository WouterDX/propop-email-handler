"""
Central configuration for the Propop email handler.

All settings are preferably provided via a `.env` file
(see `.env.example`). Normally, nothing here needs to be hardcoded,
except when you change store/pricing information from the instruction text.
"""
import os
from pathlib import Path

# --- Load .env (optional, only if python-dotenv is installed) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

# --- Gmail ---
# We need more than just "readonly" from the test script, because the
# handler must also apply labels and create draft replies.
# gmail.compose covers drafts + sending, gmail.modify covers labels/archiving.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "token.json"))

# Query used to determine which emails are "new to process".
# Default: everything in inbox that does not yet have our own "handled" label.
GMAIL_LABEL_HANDLED = os.getenv("GMAIL_LABEL_HANDLED", "Processed")
GMAIL_LABEL_NEEDS_HUMAN = os.getenv("GMAIL_LABEL_NEEDS_HUMAN", "NeedsReview")
GMAIL_QUERY = os.getenv(
    "GMAIL_QUERY",
    f"in:inbox -label:{GMAIL_LABEL_HANDLED.replace('/', '-')}",
)
# Maximum number of messages fetched per run.
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "10"))
# Number of messages from the same conversation (thread) included as context.
THREAD_CONTEXT_LIMIT = int(os.getenv("THREAD_CONTEXT_LIMIT", "10"))

# Safety guard: by default, the app only creates DRAFTS and
# NEVER sends automatically. Set this to "true" only after you have
# reviewed drafts for a while and trust the behavior.
AUTO_SEND = os.getenv("AUTO_SEND", "false").lower() == "true"

# --- OpenRouter (AI agent) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Recommended model: good price/quality for Dutch text -> structure + short,
# friendly replies and reliable JSON output. See SETUP.md for
# alternatives and how to change this without code changes.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
# These headers are optional; for local/dev usage, leaving SITE_URL empty is fine.
# If set, it is sent as HTTP-Referer to OpenRouter.
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "myorg e-mailhandler")

# --- Reservation list (currently a local JSON mock, see reservatielijst.py) ---
RESERVATIELIJST_FILE = os.getenv("RESERVATIELIJST_FILE", str(BASE_DIR / "data" / "reservations.json"))

# --- Company data (JSON file, not hardcoded in source code) ---
# Copy data/company_data_example.json to data/company_data.json and fill in
# your real business data.
COMPANY_DATA_FILE = os.getenv(
    "COMPANY_DATA_FILE",
    str(PROJECT_ROOT / "data" / "company_data.json"),
)
COMPANY_DATA_EXAMPLE_FILE = str(PROJECT_ROOT / "data" / "company_data_example.json")

INSTRUCTIONS_FILE = os.getenv("INSTRUCTIONS_FILE", str(BASE_DIR / "instructions_email_handler.md"))
