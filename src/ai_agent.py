"""
De AI-agent: 1 LLM-aanroep per e-mail(gesprek) via OpenRouter, die zowel
(a) de mail classificeert, (b) gestructureerde gegevens extraheert, als
(c) een natuurlijk klinkend Nederlands antwoord opstelt -- rekening houdend
met de volledige gespreksgeschiedenis (tot config.THREAD_CONTEXT_LIMIT
berichten terug).

We gebruiken bewust EEN gecombineerde aanroep (i.p.v. classificeren en dan
apart een antwoord genereren): het antwoord hangt sowieso af van de
classificatie en de ontbrekende velden, dus dat scheiden zou enkel extra
API-kosten en latency toevoegen zonder voordeel -- en het aantal mails is
sowieso laag.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import requests
from pydantic import ValidationError

import config
from email_parser import ParsedEmail
from models import AgentResult, Reservation

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_CACHE: Optional[str] = None
_COMPANY_DATA_CACHE: Optional[dict[str, Any]] = None


def _load_instructions() -> str:
    path = Path(config.INSTRUCTIONS_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"Kan de instructietekst niet vinden op {path}. "
            "Zorg dat instructions_email_handler.md naast main.py staat, "
            "of stel INSTRUCTIONS_FILE in via .env."
        )
    return path.read_text(encoding="utf-8")


def _load_company_data() -> dict[str, Any]:
    global _COMPANY_DATA_CACHE
    if _COMPANY_DATA_CACHE is not None:
        return _COMPANY_DATA_CACHE

    path = Path(config.COMPANY_DATA_FILE)
    if not path.exists():
        raise FileNotFoundError(
            "Company data JSON was not found at "
            f"{path}. Copy {config.COMPANY_DATA_EXAMPLE_FILE} to this path "
            "and fill in your real company data."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in company data file {path}: {e}") from e

    required_keys = {"company_name", "shows", "reservation_urls", "gift_voucher"}
    missing_keys = required_keys - set(data.keys())
    if missing_keys:
        raise ValueError(
            f"Company data file {path} is missing required keys: {sorted(missing_keys)}"
        )

    _COMPANY_DATA_CACHE = data
    return _COMPANY_DATA_CACHE


def build_system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE:
        return _SYSTEM_PROMPT_CACHE

    instructions = _load_instructions()
    company_data = _load_company_data()
    company_name = company_data["company_name"]
    shows_data = company_data["shows"]
    reservation_urls = company_data["reservation_urls"]
    gift_voucher = company_data["gift_voucher"]

    shows = "\n".join(
        f"- {s['title']} ({s['audience_age']}) [aliassen: {', '.join(s['aliases'])}]"
        for s in shows_data
    )

    prompt = f"""
Je bent de e-mailassistent van {company_name}, een kindertheater in Vlaanderen.
Je helpt bij het verwerken van binnenkomende e-mails volgens onderstaande
bedrijfsinstructies. Deze instructies zijn leidend -- verzin nooit
prijzen, adressen, data of regels die er niet in staan.

=== BEDRIJFSINSTRUCTIES (bron van waarheid) ===
{instructions}
=== EINDE BEDRIJFSINSTRUCTIES ===

Volledige lijst voorstellingen met doelgroepleeftijd en veelgebruikte aliassen:
{shows}

Nuttige links:
- Reserveren familievoorstelling poppenzaal: {reservation_urls['family_puppet_theater']}
- Reserveren schoolvoorstelling poppenzaal: {reservation_urls['school_puppet_theater']}
- Reserveren voorstelling op verplaatsing: {reservation_urls['touring']}
- Cadeaubon: {gift_voucher['url']}

Cadeaubon-betaalgegevens (gebruik EXACT deze gegevens, verzin niets):
- IBAN: {gift_voucher['iban']}
- BIC: {gift_voucher['bic']}
- t.a.v.: {gift_voucher['account_name']}
- Verzendkosten: {float(gift_voucher['shipping_cost_eur']):.2f} euro extra bovenop het bedrag van de bon(nen).

=== JOUW TAAK ===
Je krijgt:
1. De volledige e-mailwisseling van dit gesprek (oudste eerst), inclusief wie
   de afzender is van elk bericht (klant of propop).
2. Eventuele kandidaat-reservaties uit de reservatielijst die al bestaan voor
   het e-mailadres van deze klant (relevant bij annulering/wijziging).

Je antwoordt UITSLUITEND met een geldig JSON-object (geen uitleg errond, geen
markdown-codeblok), met exact deze velden:

{{
  "category": een van "nieuwe_reservatie_volledig" | "nieuwe_reservatie_onduidelijk" | "annulering" | "wijziging" | "cadeaubon" | "bijwonen_voorstelling" | "maatwerk_overig" | "vervolg_overig",
  "reservation_type": een van "verplaatsing" | "school_poppenzaal" | "familie_poppenzaal" | null,
  "extracted": {{ ... zoveel mogelijk van de velden uit het Reservation-schema hieronder, enkel wat je met vertrouwen uit de mail(len) kan afleiden ... }},
  "matched_reservation_id": "<id uit de kandidatenlijst, of null>",
  "ready_for_action": true/false,
  "reservatielijst_action": "create" | "update" | "cancel" | "none",
  "needs_human": true/false,
  "needs_human_reason": "<korte reden, of null>",
  "no_reply_needed": true/false,
  "reply_email_nl": "<volledige mailtekst in het Nederlands, enkel de body, geen onderwerpregel>",
  "interne_notitie": "<korte interne toelichting voor de medewerker, niet zichtbaar voor de klant>"
}}

Reservation-schema (gebruik deze veldnamen exact in "extracted" waar van toepassing):
{{
  "type": "verplaatsing" | "school_poppenzaal" | "familie_poppenzaal",
  "contact": {{"naam": str, "email": str, "telefoon": str, "gsm": str, "bereikbaar_op_speeldag": bool}},
  "speellocatie": {{"type": "poppenzaal" | "op_verplaatsing", "adres": str}},
  "voorstelling_titels": [str, ...],
  "doelgroep_leeftijd": str,
  "speeldatum": {{"vaste_datum": str (ISO), "voorkeurdatums": [str, ...], "periode": str, "vage_aanduiding": str}},
  "aantal_kinderen": int,
  "leeftijd_kinderen": str,
  "aantal_volwassenen": int,
  "pauze": bool,
  "verduistering_mogelijk": bool,
  "cafe_drankje": {{"type": "standaard" | "vrije_keuze", "omschrijving": str}},
  "cafe_hapje": {{"type": "standaard" | "vrije_keuze", "omschrijving": str}},
  "nieuwsbrief": bool,
  "factuurgegevens": {{"organisatie": str, "factuuradres": str, "ondernemingsnummer": str, "leveringswijze": "peppol" | "pdf_mail"}},
  "opmerkingen": str
}}

=== BELANGRIJKE REGELS ===
- Schrijf antwoorden vriendelijk, professioneel en bondig, in het Nederlands,
  in de "je"-vorm (zoals de bedrijfsinstructies zelf ook doen). Onderteken
  met "Team Propop".
- Houd altijd rekening met de VOLLEDIGE gespreksgeschiedenis: stel geen
  vragen die al eerder in het gesprek beantwoord zijn.
- Zet "ready_for_action" enkel op true als je ZEKER genoeg bent om een
  reservatielijst-actie uit te voeren. Bij twijfel: vraag via
  "reply_email_nl" om verduidelijking, en zet ready_for_action op false.
- "reservatielijst_action" mag enkel "create"/"update"/"cancel" zijn als
  ready_for_action ook true is. Anders altijd "none".
- Voor annulering/wijziging: gebruik de meegegeven kandidaat-reservaties om
  te bepalen welke reservatie bedoeld wordt (via naam/datum/voorstelling in
  het gesprek). Vul "matched_reservation_id" in zodra je voldoende zeker
  bent. Is er geen duidelijke match, vraag ernaar in "reply_email_nl" i.p.v.
  te gokken.
- Categorie "maatwerk_overig" (workshops, meewerken aan evenementen,
  poppen maken, of alles wat niet in de andere categorieen past): zet
  ALTIJD needs_human=true en laat "reply_email_nl" leeg (""). Dit wordt
  nooit automatisch beantwoord.
- Als iets duidelijk niet met vertrouwen kan worden afgehandeld (onduidelijke
  of tegenstrijdige info, twijfel over identiteit van de klant, een gevoelige
  klacht, of gewoon een vraag die niet in de instructies past): zet
  needs_human=true, geef een korte "needs_human_reason", en laat
  "reply_email_nl" leeg.
- Als de klant aangeeft toch liever het website-formulier te gebruiken i.p.v.
  verder te mailen: zet "no_reply_needed"=true en laat "reply_email_nl" leeg
  (geen verder antwoord nodig, conform de instructies).
- Verzin nooit gegevens (prijzen, rekeningnummers, data, adressen) die niet
  letterlijk in de bedrijfsinstructies hierboven staan.
- Geef ALLEEN het JSON-object terug, niets ervoor of erna.
"""
    _SYSTEM_PROMPT_CACHE = prompt.strip()
    return _SYSTEM_PROMPT_CACHE


def _format_thread_for_prompt(thread: list[ParsedEmail], own_email_hint: str = "") -> str:
    lines = []
    for i, m in enumerate(thread, 1):
        afzender = "KLANT" if m.from_email.lower() != own_email_hint.lower() else "PROPOP (eerder antwoord)"
        lines.append(
            f"--- Bericht {i} ({afzender}, {m.date or 'onbekende datum'}) ---\n"
            f"Van: {m.from_name} <{m.from_email}>\n"
            f"Onderwerp: {m.subject}\n"
            f"{m.body_text}\n"
        )
    return "\n".join(lines)


def _format_candidates(candidates: list[Reservation]) -> str:
    if not candidates:
        return "Geen bestaande reservaties gevonden voor dit e-mailadres."
    return json.dumps(
        [json.loads(c.model_dump_json()) for c in candidates], indent=2, ensure_ascii=False
    )


def _call_openrouter(messages: list[dict]) -> str:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is niet ingesteld. Zie SETUP.md om een API-key "
            "aan te maken op https://openrouter.ai/ en in je .env te zetten."
        )

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if config.OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = config.OPENROUTER_SITE_URL
    if config.OPENROUTER_APP_NAME:
        headers["X-Title"] = config.OPENROUTER_APP_NAME

    resp = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers=headers,
        json={
            "model": config.OPENROUTER_MODEL,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _extract_json(raw_text: str) -> dict:
    text = raw_text.strip()
    # Sommige modellen verpakken JSON toch nog in ```json ... ``` -- vang dit op.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def analyze_email(
    thread: list[ParsedEmail],
    candidates: list[Reservation],
    own_email_hint: str = "",
) -> AgentResult:
    """Hoofdfunctie: geef het volledige gesprek + reservatielijst-kandidaten
    mee aan het taalmodel en krijg een gevalideerd AgentResult terug."""
    system_prompt = build_system_prompt()
    user_prompt = (
        "GESPREK (oudste bericht eerst):\n\n"
        f"{_format_thread_for_prompt(thread, own_email_hint)}\n\n"
        "KANDIDAAT-RESERVATIES VOOR DIT E-MAILADRES:\n"
        f"{_format_candidates(candidates)}\n\n"
        "Geef nu het JSON-antwoord volgens het afgesproken schema."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error = None
    for attempt in range(2):  # 1 herkansing als het model geen geldige JSON teruggeeft
        raw = _call_openrouter(messages)
        try:
            parsed = _extract_json(raw)
            return AgentResult.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            log.warning("Ongeldige AI-respons (poging %d): %s", attempt + 1, e)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Dat was geen geldig JSON-object volgens het schema. "
                        "Antwoord opnieuw, ENKEL met geldig JSON, niets anders."
                    ),
                }
            )

    raise RuntimeError(f"AI gaf geen geldige JSON terug na 2 pogingen: {last_error}")
