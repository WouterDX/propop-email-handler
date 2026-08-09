"""
Centrale configuratie voor de Propop e-mailhandler.

Alle instellingen worden bij voorkeur via een `.env` bestand aangeleverd
(zie `.env.example`). Niets hier moet je normaal gezien hardcoded aanpassen,
behalve als je de winkel-/prijsinformatie uit de instructietekst wijzigt.
"""
import os
from pathlib import Path

# --- .env laden (optioneel, alleen als python-dotenv geinstalleerd is) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent

# --- Gmail ---
# We hebben meer nodig dan alleen "readonly" uit het testscript, want de
# handler moet ook labels zetten en concept-antwoorden (drafts) aanmaken.
# gmail.compose dekt drafts + verzenden, gmail.modify dekt labels/archiveren.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "token.json"))

# Query om te bepalen welke mails er "nieuw te verwerken" zijn.
# Standaard: alles in de inbox dat nog niet ons eigen "verwerkt"-label draagt.
GMAIL_LABEL_HANDLED = os.getenv("GMAIL_LABEL_HANDLED", "Propop/Verwerkt")
GMAIL_LABEL_NEEDS_HUMAN = os.getenv("GMAIL_LABEL_NEEDS_HUMAN", "Propop/NaZien")
GMAIL_QUERY = os.getenv(
    "GMAIL_QUERY",
    f"in:inbox -label:{GMAIL_LABEL_HANDLED.replace('/', '-')}",
)
# Hoeveel berichten er per run maximaal opgehaald worden.
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "10"))
# Hoeveel berichten van eenzelfde gesprek (thread) er als context meegegeven worden.
THREAD_CONTEXT_LIMIT = int(os.getenv("THREAD_CONTEXT_LIMIT", "10"))

# Veiligheidsklep: standaard maakt de app enkel CONCEPTEN (drafts) aan, en
# verstuurt ze NOOIT automatisch. Pas dit pas aan naar "true" nadat je een
# tijdje de concepten hebt nagelezen en vertrouwt.
AUTO_SEND = os.getenv("AUTO_SEND", "false").lower() == "true"

# --- OpenRouter (AI-agent) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Aanbevolen model: goede prijs/kwaliteit voor NL-tekst -> structuur + korte
# vriendelijke antwoorden, betrouwbare JSON-output. Zie SETUP.md voor
# alternatieven en hoe je dit kan wijzigen zonder code aan te passen.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
# Deze twee headers zijn niet verplicht maar aangeraden door OpenRouter zodat
# jouw app correct herkend wordt in hun dashboard/rate-limits.
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "https://www.propop.be")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "Propop e-mailhandler")

# --- Reservatielijst (momenteel een lokale JSON-mock, zie reservatielijst.py) ---
RESERVATIELIJST_FILE = os.getenv("RESERVATIELIJST_FILE", str(BASE_DIR / "data" / "reservations.json"))

# --- Bedrijfsinformatie die de AI-agent letterlijk moet gebruiken ---
# (Overgenomen uit instructions_email_handler.md zodat bedragen/rekeningnummers
# nooit door het taalmodel "verzonnen" worden.)
CADEAUBON_IBAN = "BE41 0010 8855 4410"
CADEAUBON_BIC = "GEBABEBB"
CADEAUBON_REKENING_NAAM = "Propop vzw"
CADEAUBON_VERZENDKOSTEN = 1.00

VOORSTELLINGEN = [
    {"titel": "Het Waait", "leeftijd": "0.5-3j", "aliassen": ["het waait", "waait"]},
    {"titel": "De Zandman", "leeftijd": "2-5j", "aliassen": ["zandman", "de zandman"]},
    {"titel": "BeestIG", "leeftijd": "2-5j", "aliassen": ["beestig", "beest ig"]},
    {"titel": "Stapel", "leeftijd": "2.5-5j", "aliassen": ["stapel"]},
    {"titel": "Bouwstenen", "leeftijd": "2.5-5j", "aliassen": ["bouwstenen"]},
    {"titel": "Onderonsje", "leeftijd": "2.5-5j", "aliassen": ["onderonsje"]},
    {"titel": "De Taartendief", "leeftijd": "2.5-5j", "aliassen": ["taartendief", "de taartendief"]},
    {"titel": "Kijkdoos", "leeftijd": "2.5-6j", "aliassen": ["kijkdoos"]},
    {"titel": "Bip", "leeftijd": "3-7j", "aliassen": ["bip"]},
    {"titel": "Graaf", "leeftijd": "3-8j", "aliassen": ["graaf"]},
    {"titel": "Sinterklaas Kapoentje", "leeftijd": "3-8j", "aliassen": ["sinterklaas kapoentje", "sinterklaas", "sint"]},
    {"titel": "Bo kan alles", "leeftijd": "4-7j", "aliassen": ["bo kan alles", "bo"]},
    {"titel": "Hoofd vol..#?!.", "leeftijd": "4-10j", "aliassen": ["hoofd vol", "hoofdvol"]},
    {"titel": "Kleine Held", "leeftijd": "4-10j", "aliassen": ["kleine held"]},
    {"titel": "Het lelijke eendje", "leeftijd": "4-10j", "aliassen": ["het lelijke eendje", "lelijke eendje"]},
    {"titel": "De Kakmadam", "leeftijd": "familie", "aliassen": ["de kakmadam", "kakmadam"]},
    {"titel": "Het meisje met de zwavelstokjes", "leeftijd": "4-10j", "aliassen": ["het meisje met de zwavelstokjes", "zwavelstokjes"]},
    {"titel": "De Stoefpotloden", "leeftijd": "4-10j", "aliassen": ["de stoefpotloden", "stoefpotloden"]},
    {"titel": "Control X", "leeftijd": "8-12j", "aliassen": ["control x", "control-x", "controlx"]},
    {"titel": "Hee man!", "leeftijd": "6+ & volwassenen", "aliassen": ["hee man", "heeman"]},
]

RESERVEREN_URLS = {
    "familie_poppenzaal": "https://www.propop.be/de-poppenzaal/familievoorstellingen/reserveren.html",
    "school_poppenzaal": "https://www.propop.be/de-poppenzaal/schoolvoorstellingen/reserveren-schoolvoorstellingen.html",
    "verplaatsing": "https://www.propop.be/theater-propop/reserveren-op-verplaatsing.html",
}

CADEAUBON_URL = "https://www.propop.be/de-poppenzaal/cadeaubon-voor-familievoorstellingen.html"

INSTRUCTIONS_FILE = os.getenv("INSTRUCTIONS_FILE", str(BASE_DIR / "instructions_email_handler.md"))
