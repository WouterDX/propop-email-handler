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
    # Use override=True so a newly edited .env is reapplied on each fresh
    # process start, even if parent processes exported older values.
    load_dotenv(override=True)
except ImportError:
    pass

SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent

# --- Gmail ---
# We need more than just "readonly" from the test script, because the
# handler must also apply labels and create draft replies.
# gmail.compose covers drafts + sending, gmail.modify covers labels/archiving.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", str(PROJECT_ROOT / "credentials.json"))
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", str(PROJECT_ROOT / "token.json"))

# Query used to determine which emails are "new to process".
# Default: all non-handled emails except system folders (sent/spam/trash/drafts)
# that contain one of the configured subject keywords.
GMAIL_LABEL_HANDLED = os.getenv("GMAIL_LABEL_HANDLED", "Processed")
GMAIL_LABEL_NEEDS_HUMAN = os.getenv("GMAIL_LABEL_NEEDS_HUMAN", "NeedsReview")

GMAIL_SUBJECT_KEYWORDS = [
    keyword.strip()
    for keyword in os.getenv("GMAIL_SUBJECT_KEYWORDS", "reservatie,reserveren").split(",")
    if keyword.strip()
]

#This setting could also be set to "in:inbox" to only process new incoming emails.
INCOMING_MAIL_FOLDERS = os.getenv(
    "INCOMING_MAIL_FOLDERS",
    "-in:sent -in:spam -in:trash -in:drafts",
)

_incoming_mail_scope = INCOMING_MAIL_FOLDERS.strip()
_gmail_base_query = (
    f"-label:{GMAIL_LABEL_HANDLED.replace('/', '-')} {_incoming_mail_scope}"
).strip()
if GMAIL_SUBJECT_KEYWORDS:
    _gmail_subject_filter = " OR ".join(
        f"subject:{keyword}" for keyword in GMAIL_SUBJECT_KEYWORDS
    )
    _gmail_default_query = f"{_gmail_base_query} ({_gmail_subject_filter})"
else:
    _gmail_default_query = _gmail_base_query

GMAIL_QUERY = os.getenv(
    "GMAIL_QUERY",
    _gmail_default_query,
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

# --- Reservation list (currently a local JSON mock, see reservation_list.py) ---
RESERVATION_LIST_FILE = os.getenv("RESERVATION_LIST_FILE", str(PROJECT_ROOT / "data" / "reservations.json"))
RESERVATION_LIST_STUB = os.getenv("RESERVATION_LIST_STUB", "false").lower() == "true"
PIPELINE_DATA_FILE = os.getenv(
    "PIPELINE_DATA_FILE",
    str(PROJECT_ROOT / "data" / "mail_pipeline_data.json"),
)
OPENROUTER_JUDGE_MODEL = os.getenv("OPENROUTER_JUDGE_MODEL", OPENROUTER_MODEL)

# --- Company data (JSON file, not hardcoded in source code) ---
# Copy data/company_data_example.json to data/company_data.json and fill in
# your real business data.
COMPANY_DATA_FILE = os.getenv(
    "COMPANY_DATA_FILE",
    str(PROJECT_ROOT / "data" / "company_data.json"),
)
COMPANY_DATA_EXAMPLE_FILE = str(PROJECT_ROOT / "data" / "company_data_example.json")

INSTRUCTIONS_FILE = os.getenv("INSTRUCTIONS_FILE", str(PROJECT_ROOT / "data" / "instructies_agent.md"))

# --- main.py CLI defaults ---
# These values control defaults when flags are omitted.
MAIN_DEFAULT_DRY_RUN = os.getenv("MAIN_DEFAULT_DRY_RUN", "false").lower() == "true"
_main_default_max_threads = os.getenv("MAIN_DEFAULT_MAX_THREADS", "").strip()
MAIN_DEFAULT_MAX_THREADS = int(_main_default_max_threads) if _main_default_max_threads else None
MAIN_DEFAULT_DROP_LAST_ORG_REPLY = (
    os.getenv("MAIN_DEFAULT_DROP_LAST_ORG_REPLY", "false").lower() == "true"
)
MAIN_DEFAULT_RESERVATION_LIST_STUB = (
    os.getenv("MAIN_DEFAULT_RESERVATION_LIST_STUB", str(RESERVATION_LIST_STUB)).lower()
    == "true"
)
