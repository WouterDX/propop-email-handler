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
import time
from typing import Any, Optional
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import requests
from pydantic import ValidationError

import config
from email_parser import ParsedEmail
from models import AgentResult, Reservation

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_CACHE: Optional[str] = None
_COMPANY_DATA_CACHE: Optional[dict[str, Any]] = None
_OPENROUTER_MODEL_INFO_CACHE: Optional[dict[str, Any]] = None
_OPENROUTER_MAX_ATTEMPTS = 3
_OPENROUTER_BASE_RETRY_DELAY_SECONDS = 1.5
_OPENROUTER_MAX_RETRY_DELAY_SECONDS = 60.0


class _TransientOpenRouterError(RuntimeError):
    pass


def _parse_retry_after_seconds(retry_after_value: str | None) -> float | None:
    if not retry_after_value:
        return None

    raw = retry_after_value.strip()
    if not raw:
        return None

    try:
        seconds = float(raw)
        return seconds if seconds > 0 else None
    except ValueError:
        pass

    try:
        retry_dt = parsedate_to_datetime(raw)
    except Exception:
        return None

    if retry_dt.tzinfo is None:
        retry_dt = retry_dt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    delta = (retry_dt - now).total_seconds()
    return delta if delta > 0 else None


def _extract_openrouter_error_payload(resp: requests.Response) -> dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError:
        return {}

    if not isinstance(payload, dict):
        return {}

    error = payload.get("error")
    if not isinstance(error, dict):
        return {}

    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    return {
        "code": error.get("code"),
        "message": error.get("message"),
        "error_type": metadata.get("error_type"),
        "provider_code": metadata.get("provider_code"),
    }


def _compute_retry_delay_seconds(attempt: int, retry_after_seconds: float | None) -> float:
    if retry_after_seconds is not None:
        return min(retry_after_seconds, _OPENROUTER_MAX_RETRY_DELAY_SECONDS)

    backoff_delay = _OPENROUTER_BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
    return min(backoff_delay, _OPENROUTER_MAX_RETRY_DELAY_SECONDS)


def _estimate_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, char_count // 4)


def _fetch_openrouter_model_info() -> dict[str, Any]:
    global _OPENROUTER_MODEL_INFO_CACHE
    if _OPENROUTER_MODEL_INFO_CACHE is not None:
        return _OPENROUTER_MODEL_INFO_CACHE

    headers = {"Content-Type": "application/json"}
    if config.OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {config.OPENROUTER_API_KEY}"

    response = requests.get(
        f"{config.OPENROUTER_BASE_URL}/models",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    model_entries = payload.get("data") or []
    for entry in model_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == config.OPENROUTER_MODEL:
            _OPENROUTER_MODEL_INFO_CACHE = entry
            return entry

    raise RuntimeError(
        f"Configured model '{config.OPENROUTER_MODEL}' not found in OpenRouter /models response."
    )


def _extract_model_context_limit(model_info: dict[str, Any]) -> int | None:
    direct = model_info.get("context_length")
    if isinstance(direct, int) and direct > 0:
        return direct

    top_level_max = model_info.get("max_context_length")
    if isinstance(top_level_max, int) and top_level_max > 0:
        return top_level_max

    architecture = model_info.get("architecture")
    if isinstance(architecture, dict):
        context_length = architecture.get("context_length")
        if isinstance(context_length, int) and context_length > 0:
            return context_length

    return None


def _log_prompt_context_diagnostic(
    messages: list[dict],
    thread: list[ParsedEmail]
):
    system_chars = 0
    user_chars = 0
    assistant_chars = 0
    for message in messages:
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        role = message.get("role")
        if role == "system":
            system_chars += len(text)
        elif role == "user":
            user_chars += len(text)
        elif role == "assistant":
            assistant_chars += len(text)

    total_chars = system_chars + user_chars + assistant_chars
    estimated_tokens = _estimate_tokens_from_chars(total_chars)

    full_thread_chars = 0
    trimmed_thread_chars = 0
    for item in thread:
        full_thread_chars += len((getattr(item, "body_text_full", None) or ""))
        trimmed_thread_chars += len(item.body_text or "")

    log.info(
        "AI context diagnostic | model=%s | messages=%d | est_tokens~%d | chars_total=%d (system=%d user=%d assistant=%d) | thread_msgs=%d | thread_chars_trimmed=%d | thread_chars_full=%d",
        config.OPENROUTER_MODEL,
        len(messages),
        estimated_tokens,
        total_chars,
        system_chars,
        user_chars,
        assistant_chars,
        len(thread),
        trimmed_thread_chars,
        full_thread_chars,
    )    

    try:
        model_info = _fetch_openrouter_model_info()
        context_limit = _extract_model_context_limit(model_info)
        if context_limit is None:
            log.info(
                "AI context diagnostic | model limit unavailable from OpenRouter /models for %s",
                config.OPENROUTER_MODEL,
            )
            return

        usage_pct = (estimated_tokens / context_limit) * 100 if context_limit > 0 else 0.0
        log.info(
            "AI context diagnostic | model_context_limit=%d | est_usage=%.1f%%",
            context_limit,
            usage_pct,
        )
    except Exception as e:
        log.warning("AI context diagnostic | failed to fetch model limits: %s", e)


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
    company_building=company_data["company_building_name"]
    shows_data = company_data["shows"]
    reservation_urls = company_data["reservation_urls"]
    gift_voucher = company_data["gift_voucher"]

    shows = "\n".join(
        f"- {s['title']} ({s['audience_age']}) [aliassen: {', '.join(s['aliases'])}]"
        for s in shows_data
    )

    prompt = f"""
Je bent de e-mailassistent van {company_name}, een kindertheater.
Je helpt bij het verwerken van binnenkomende e-mails volgens onderstaande
bedrijfsinstructies. Deze instructies zijn leidend -- verzin nooit
prijzen, adressen, data of regels die er niet in staan.

=== BEDRIJFSINSTRUCTIES (bron van waarheid) ===
{instructions}
=== EINDE BEDRIJFSINSTRUCTIES ===

Volledige lijst voorstellingen met doelgroepleeftijd en veelgebruikte aliassen:
{shows}

Nuttige links:
- Reserveren familievoorstelling {company_building}: {reservation_urls['family_puppet_theater']}
- Reserveren schoolvoorstelling {company_building}: {reservation_urls['school_puppet_theater']}
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
  "reservation_type": een van "verplaatsing" | "school_lokaal" | "familie_lokaal" | null,
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
  "type": "verplaatsing" | "school_lokaal" | "familie_lokaal",
  "contact": {{"naam": str, "email": str, "telefoon": str, "gsm": str, "bereikbaar_op_speeldag": bool}},
  "speellocatie": {{"type": "{company_building}" | "op_verplaatsing", "adres": str}},
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
  met "Team {company_name}".
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

    last_error: Exception | None = None

    for attempt in range(1, _OPENROUTER_MAX_ATTEMPTS + 1):
        try:
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
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"OpenRouter response contains no choices: {data}")

            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            choice_error = first_choice.get("error") if isinstance(first_choice.get("error"), dict) else None
            if choice_error:
                error_code = choice_error.get("code")
                metadata = choice_error.get("metadata") if isinstance(choice_error.get("metadata"), dict) else {}
                error_type = metadata.get("error_type")
                provider_code = metadata.get("provider_code")
                error_message = choice_error.get("message") or "OpenRouter returned provider error in choice"
                retryable = bool(
                    error_code in (408, 409, 425, 429, 502, 503, 504)
                    or error_type in {
                        "rate_limit_exceeded",
                        "provider_overloaded",
                        "provider_unavailable",
                        "timeout",
                        "server",
                        "unmapped",
                    }
                )
                detail = (
                    f"{error_message} "
                    f"(code={error_code}, error_type={error_type}, provider_code={provider_code})"
                )
                if retryable:
                    raise _TransientOpenRouterError(detail)
                raise RuntimeError(detail)

            message = first_choice.get("message") or {}
            content = message.get("content")
            finish_reason = first_choice.get("finish_reason")

            if content is None:
                raise _TransientOpenRouterError(
                    "OpenRouter returned empty assistant content "
                    f"(finish_reason={finish_reason}). "
                    "This can happen with provider throttling, quota issues, or non-text responses."
                )

            if isinstance(content, str):
                return content

            return json.dumps(content, ensure_ascii=False)

        except (requests.Timeout, requests.ConnectionError, _TransientOpenRouterError) as e:
            last_error = e
            if attempt >= _OPENROUTER_MAX_ATTEMPTS:
                break
            delay = _compute_retry_delay_seconds(attempt, None)
            log.warning(
                "OpenRouter transient failure (attempt %d/%d): %s. Retrying in %.1fs...",
                attempt,
                _OPENROUTER_MAX_ATTEMPTS,
                e,
                delay,
            )
            time.sleep(delay)

        except requests.HTTPError as e:
            last_error = e
            status_code = e.response.status_code if e.response is not None else None
            error_payload = _extract_openrouter_error_payload(e.response) if e.response is not None else {}
            error_type = error_payload.get("error_type")
            provider_code = error_payload.get("provider_code")
            error_message = error_payload.get("message")
            retryable = bool(
                status_code in (408, 409, 425, 429)
                or (status_code is not None and 500 <= status_code <= 599)
                or error_type in {
                    "rate_limit_exceeded",
                    "provider_overloaded",
                    "provider_unavailable",
                    "timeout",
                    "server",
                    "unmapped",
                }
            )
            if not retryable or attempt >= _OPENROUTER_MAX_ATTEMPTS:
                break

            retry_after_seconds = _parse_retry_after_seconds(
                (e.response.headers.get("Retry-After") if e.response is not None else None)
            )
            delay = _compute_retry_delay_seconds(attempt, retry_after_seconds)
            retry_after_hint = (
                f" | retry_after={retry_after_seconds:.1f}s"
                if retry_after_seconds is not None
                else ""
            )
            error_type_hint = f" | error_type={error_type}" if error_type else ""
            provider_code_hint = f" | provider_code={provider_code}" if provider_code else ""
            message_hint = f" | message={error_message}" if error_message else ""
            log.warning(
                "OpenRouter HTTP %s (attempt %d/%d). Retrying in %.1fs...%s%s%s%s",
                status_code,
                attempt,
                _OPENROUTER_MAX_ATTEMPTS,
                delay,
                retry_after_hint,
                error_type_hint,
                provider_code_hint,
                message_hint,
            )
            time.sleep(delay)

    if isinstance(last_error, requests.HTTPError):
        status_code = last_error.response.status_code if last_error.response is not None else "unknown"
        error_payload = _extract_openrouter_error_payload(last_error.response) if last_error.response is not None else {}
        error_type = error_payload.get("error_type")
        provider_code = error_payload.get("provider_code")
        error_message = error_payload.get("message")

        if status_code == 429 or error_type == "rate_limit_exceeded":
            retry_after_seconds = _parse_retry_after_seconds(
                (last_error.response.headers.get("Retry-After") if last_error.response is not None else None)
            )
            retry_hint = (
                f" Retry-After={retry_after_seconds:.1f}s."
                if retry_after_seconds is not None
                else ""
            )
            raise RuntimeError(
                "OpenRouter rate limit reached (429/rate_limit_exceeded)."
                f" Retries exhausted after {_OPENROUTER_MAX_ATTEMPTS} attempts.{retry_hint}"
                " Consider lowering request burst/concurrency or waiting before retrying."
            ) from last_error

        detail = (
            f"error_type={error_type}, provider_code={provider_code}, message={error_message}"
            if (error_type or provider_code or error_message)
            else "no structured error details"
        )
        raise RuntimeError(
            f"OpenRouter request failed after {_OPENROUTER_MAX_ATTEMPTS} attempts (HTTP {status_code}; {detail})."
        ) from last_error

    if last_error is not None:
        raise RuntimeError(
            f"OpenRouter request failed after {_OPENROUTER_MAX_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    raise RuntimeError("OpenRouter request failed for an unknown reason.")


def _extract_json(raw_text: str) -> dict:
    if raw_text is None:
        raise ValueError("AI response content is empty (None)")
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)
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
    reservation_list_stub: bool = False,
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
    ]

    if reservation_list_stub:
        messages.append(
            {
                "role": "system",
                "content": (
                    "RUN MODE OVERRIDE: de reservatielijst is momenteel een stub en niet betrouwbaar. "
                    "Gebruik kandidaat-reservaties NIET als basis om de klant eerst een bestaande reservatie te laten bevestigen. "
                    "Vraag in plaats daarvan meteen gericht naar ontbrekende noodzakelijke gegevens om de vraag correct af te handelen. "
                    "Behoud alle andere regels ongewijzigd."
                ),
            }
        )

    messages.append({"role": "user", "content": user_prompt})

    
    _log_prompt_context_diagnostic(messages,thread)

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
