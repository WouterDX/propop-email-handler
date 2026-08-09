"""
Propop email handler -- main script.

What this does per run:
  1. Fetches new/unprocessed emails from Gmail (config.GMAIL_QUERY).
  2. Groups by conversation (thread) and processes each conversation once.
  3. Fetches full conversation history (up to THREAD_CONTEXT_LIMIT
      messages) as context.
  4. Looks up existing reservations for the sender's email address
      (relevant for cancellations/changes).
  5. Calls the AI agent (OpenRouter) to classify, extract data,
      and draft a reply.
  6. Executes a reservation-list action if needed
      (create/update/cancel).
  7. Creates a DRAFT reply in Gmail -- sends NOTHING
      automatically unless AUTO_SEND=true is set in .env.
  8. Labels the email as handled (and optionally "NeedsReview" if a staff
      member should check it), so it is not picked up again.

Usage:
     python main.py                 # normal run
     python main.py --dry-run       # analyzes and prints everything, creates NO
                                                # drafts and does not modify the reservation list
                                                # -- ideal for initial testing
     python main.py --max 3         # process at most 3 conversations this run
"""
from __future__ import annotations

import argparse
import logging
import sys

import config
import gmail_client
import reservatielijst as reservation_list_module
from ai_agent import analyze_email
from models import AgentResult, Reservation

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("propop")


def _apply_extracted_to_reservation(existing: dict, extracted: dict) -> dict:
    """Merge extracted fields on top of an existing (or empty) reservation dict."""
    merged = dict(existing)
    for key, value in extracted.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def process_thread(service, reservation_list, thread_id: str, latest_msg_id: str, dry_run: bool):
    thread = gmail_client.get_thread_messages(service, thread_id)
    if not thread:
        log.warning("Empty conversation for thread %s, skipping.", thread_id)
        return

    latest = thread[-1]
    log.info("Processing conversation with %s | subject: %s", latest.from_email, latest.subject)

    # We try to automatically determine which email address is "Propop itself"
    # (the "To" address from the latest customer email) so we can indicate
    # in the prompt who is the customer and who is Propop.
    own_email_hint = latest.to.split(",")[0].strip() if latest.to else ""

    candidates = reservation_list.search(email=latest.from_email)

    try:
        result: AgentResult = analyze_email(thread, candidates, own_email_hint)
    except Exception as e:
        log.error("AI analysis failed for thread %s: %s", thread_id, e)
        if not dry_run:
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_NEEDS_HUMAN)
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_HANDLED)
        return

    log.info(
        "  -> category=%s | ready_for_action=%s | action=%s | needs_human=%s | no_reply=%s",
        result.category,
        result.ready_for_action,
        result.reservatielijst_action,
        result.needs_human,
        result.no_reply_needed,
    )

    # --- Execute reservation-list action (if applicable) ---
    if not dry_run and result.ready_for_action and result.reservatielijst_action != "none":
        try:
            if result.reservatielijst_action == "create":
                data = _apply_extracted_to_reservation({}, result.extracted)
                data["email_thread_id"] = thread_id
                if result.reservation_type:
                    data["type"] = result.reservation_type
                reservation = Reservation.model_validate(data)
                reservation_list.create(reservation)
                log.info("  -> new reservation created: %s", reservation.id)

            elif result.reservatielijst_action == "update" and result.matched_reservation_id:
                existing = reservation_list.get(result.matched_reservation_id)
                if existing:
                    merged = _apply_extracted_to_reservation(
                        existing.model_dump(), result.extracted
                    )
                    reservation_list.update(result.matched_reservation_id, merged)
                    log.info("  -> reservation updated: %s", result.matched_reservation_id)
                else:
                    log.warning("  -> matched_reservation_id not found, no update executed.")

            elif result.reservatielijst_action == "cancel" and result.matched_reservation_id:
                ok = reservation_list.cancel(result.matched_reservation_id)
                log.info("  -> reservation canceled: %s (found=%s)", result.matched_reservation_id, ok)

        except Exception as e:
            log.error("  -> error while updating reservation list: %s", e)
            result.needs_human = True
            result.needs_human_reason = f"Reservation-list action failed: {e}"

    # --- Compose reply ---
    if dry_run:
        print("\n" + "=" * 70)
        print(f"THREAD: {thread_id}  |  FROM: {latest.from_email}  |  SUBJECT: {latest.subject}")
        print("-" * 70)
        print(result.model_dump_json(indent=2))
        print("=" * 70 + "\n")
        return

    if result.no_reply_needed or result.needs_human or not result.reply_email_nl.strip():
        if result.needs_human:
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_NEEDS_HUMAN)
            log.info("  -> marked for manual follow-up: %s", result.needs_human_reason)
    else:
        if config.AUTO_SEND:
            gmail_client.send_reply(service, latest, result.reply_email_nl)
            log.info("  -> reply SENT (AUTO_SEND=true)")
        else:
            gmail_client.create_draft_reply(service, latest, result.reply_email_nl)
            log.info("  -> draft reply created (not sent yet)")

    gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_HANDLED)


def run(dry_run: bool = False, max_threads: int = None):
    if not config.OPENROUTER_API_KEY and not dry_run:
        log.warning(
            "OPENROUTER_API_KEY is missing. Add it to your .env -- see SETUP.md. "
            "(In dry-run without a key, the AI call will also fail.)"
        )

    service = gmail_client.get_gmail_service()
    reservation_list = reservation_list_module.JsonFileReservatieLijst()

    msg_ids = gmail_client.list_new_message_ids(service)
    log.info("Found: %d new/unprocessed messages.", len(msg_ids))

    seen_threads = set()
    processed = 0
    for msg_id in msg_ids:
        if max_threads is not None and processed >= max_threads:
            break
        parsed = gmail_client.get_parsed_message(service, msg_id)
        if parsed.thread_id in seen_threads:
            continue
        seen_threads.add(parsed.thread_id)
        process_thread(service, reservation_list, parsed.thread_id, msg_id, dry_run)
        processed += 1

    log.info("Done. %d conversation(s) processed.", processed)


def main():
    parser = argparse.ArgumentParser(description="Gmail business handler")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print results, but do not change anything in Gmail or the reservation list.",
    )
    parser.add_argument(
        "--max", type=int, default=None, help="Process at most this number of conversations."
    )
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, max_threads=args.max)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
