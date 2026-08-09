"""
Propop e-mailhandler -- hoofdscript.

Wat dit doet, per run:
  1. Haalt nieuwe/onverwerkte mails op uit Gmail (config.GMAIL_QUERY).
  2. Groepeert per gesprek (thread) en verwerkt elk gesprek 1x.
  3. Haalt de volledige gespreksgeschiedenis op (tot THREAD_CONTEXT_LIMIT
     berichten) als context.
  4. Zoekt bestaande reservaties op voor het e-mailadres van de afzender
     (relevant bij annuleringen/wijzigingen).
  5. Roept de AI-agent (OpenRouter) op om te classificeren, gegevens te
     extraheren en een antwoord op te stellen.
  6. Voert indien nodig een actie uit op de reservatielijst
     (aanmaken/wijzigen/annuleren).
  7. Maakt een CONCEPT-antwoord (draft) aan in Gmail -- verstuurt NIETS
     automatisch, tenzij je AUTO_SEND=true zet in .env.
  8. Labelt de mail als verwerkt (en evt. "NaZien" als een medewerker moet
     kijken), zodat ze niet opnieuw wordt opgepikt.

Gebruik:
    python main.py                 # normale run
    python main.py --dry-run       # analyseert en print alles, maakt GEEN
                                    # drafts aan en wijzigt de reservatielijst
                                    # niet -- ideaal om eerst te testen
    python main.py --max 3         # verwerk hoogstens 3 gesprekken deze run
"""
from __future__ import annotations

import argparse
import logging
import sys

import config
import gmail_client
import reservatielijst as reservatielijst_module
from ai_agent import analyze_email
from models import AgentResult, Reservation

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("propop")


def _apply_extracted_to_reservation(existing: dict, extracted: dict) -> dict:
    """Merge geëxtraheerde velden bovenop een bestaande (of lege) reservatie-dict."""
    merged = dict(existing)
    for key, value in extracted.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def process_thread(service, reslijst, thread_id: str, latest_msg_id: str, dry_run: bool):
    thread = gmail_client.get_thread_messages(service, thread_id)
    if not thread:
        log.warning("Leeg gesprek voor thread %s, overslaan.", thread_id)
        return

    latest = thread[-1]
    log.info("Verwerk gesprek met %s | onderwerp: %s", latest.from_email, latest.subject)

    # We proberen automatisch te bepalen welk e-mailadres "propop zelf" is
    # (het "To"-adres van het laatste klantbericht) zodat we in de prompt
    # kunnen aanduiden wie klant is en wie propop.
    own_email_hint = latest.to.split(",")[0].strip() if latest.to else ""

    candidates = reslijst.search(email=latest.from_email)

    try:
        result: AgentResult = analyze_email(thread, candidates, own_email_hint)
    except Exception as e:
        log.error("AI-analyse mislukt voor thread %s: %s", thread_id, e)
        if not dry_run:
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_NEEDS_HUMAN)
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_HANDLED)
        return

    log.info(
        "  -> categorie=%s | ready_for_action=%s | actie=%s | needs_human=%s | no_reply=%s",
        result.category,
        result.ready_for_action,
        result.reservatielijst_action,
        result.needs_human,
        result.no_reply_needed,
    )

    # --- Reservatielijst-actie uitvoeren (indien van toepassing) ---
    if not dry_run and result.ready_for_action and result.reservatielijst_action != "none":
        try:
            if result.reservatielijst_action == "create":
                data = _apply_extracted_to_reservation({}, result.extracted)
                data["email_thread_id"] = thread_id
                if result.reservation_type:
                    data["type"] = result.reservation_type
                reservation = Reservation.model_validate(data)
                reslijst.create(reservation)
                log.info("  -> nieuwe reservatie aangemaakt: %s", reservation.id)

            elif result.reservatielijst_action == "update" and result.matched_reservation_id:
                existing = reslijst.get(result.matched_reservation_id)
                if existing:
                    merged = _apply_extracted_to_reservation(
                        existing.model_dump(), result.extracted
                    )
                    reslijst.update(result.matched_reservation_id, merged)
                    log.info("  -> reservatie gewijzigd: %s", result.matched_reservation_id)
                else:
                    log.warning("  -> matched_reservation_id niet gevonden, geen wijziging uitgevoerd.")

            elif result.reservatielijst_action == "cancel" and result.matched_reservation_id:
                ok = reslijst.cancel(result.matched_reservation_id)
                log.info("  -> reservatie geannuleerd: %s (gevonden=%s)", result.matched_reservation_id, ok)

        except Exception as e:
            log.error("  -> fout bij bijwerken reservatielijst: %s", e)
            result.needs_human = True
            result.needs_human_reason = f"Reservatielijst-actie mislukt: {e}"

    # --- Antwoord opstellen ---
    if dry_run:
        print("\n" + "=" * 70)
        print(f"THREAD: {thread_id}  |  VAN: {latest.from_email}  |  ONDERWERP: {latest.subject}")
        print("-" * 70)
        print(result.model_dump_json(indent=2))
        print("=" * 70 + "\n")
        return

    if result.no_reply_needed or result.needs_human or not result.reply_email_nl.strip():
        if result.needs_human:
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_NEEDS_HUMAN)
            log.info("  -> gemarkeerd voor manuele opvolging: %s", result.needs_human_reason)
    else:
        if config.AUTO_SEND:
            gmail_client.send_reply(service, latest, result.reply_email_nl)
            log.info("  -> antwoord VERSTUURD (AUTO_SEND=true)")
        else:
            gmail_client.create_draft_reply(service, latest, result.reply_email_nl)
            log.info("  -> conceptantwoord aangemaakt (nog niet verstuurd)")

    gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_HANDLED)


def run(dry_run: bool = False, max_threads: int = None):
    if not config.OPENROUTER_API_KEY and not dry_run:
        log.warning(
            "OPENROUTER_API_KEY ontbreekt. Zet 'm in je .env -- zie SETUP.md. "
            "(In dry-run zonder key zal de AI-aanroep ook falen.)"
        )

    service = gmail_client.get_gmail_service()
    reslijst = reservatielijst_module.JsonFileReservatieLijst()

    msg_ids = gmail_client.list_new_message_ids(service)
    log.info("Gevonden: %d nieuwe/onverwerkte berichten.", len(msg_ids))

    seen_threads = set()
    processed = 0
    for msg_id in msg_ids:
        if max_threads is not None and processed >= max_threads:
            break
        parsed = gmail_client.get_parsed_message(service, msg_id)
        if parsed.thread_id in seen_threads:
            continue
        seen_threads.add(parsed.thread_id)
        process_thread(service, reslijst, parsed.thread_id, msg_id, dry_run)
        processed += 1

    log.info("Klaar. %d gesprek(ken) verwerkt.", processed)


def main():
    parser = argparse.ArgumentParser(description="Propop e-mailhandler")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyseer en print resultaten, maar wijzig niets in Gmail of de reservatielijst.",
    )
    parser.add_argument(
        "--max", type=int, default=None, help="Verwerk hoogstens dit aantal gesprekken."
    )
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, max_threads=args.max)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
