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
    python main.py --dry-run --drop-last-org-reply
                        # debug comparison mode: exclude all
                        # organisation-sent messages that were sent
                        # after the customer's last message
    python main.py --dry-run --reservation-list-stub
                        # debug mode: treat reservation candidates as
                        # unreliable stub data and ask missing details
    (set RESERVATION_LIST_STUB=true in .env)
                        # same behavior via environment default

Default CLI behavior can be configured via env vars:
    MAIN_DEFAULT_DRY_RUN=true|false
    MAIN_DEFAULT_MAX_THREADS=<int>      # empty/unset means no max
    MAIN_DEFAULT_DROP_LAST_ORG_REPLY=true|false
    MAIN_DEFAULT_RESERVATION_LIST_STUB=true|false
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import re
import sys
from email.utils import getaddresses
from pathlib import Path

import config
import gmail_client
import reservation_list as reservation_list_module
from ai_agent import analyze_email
from models import AgentResult, Reservation

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("propop")

_QUOTE_MARKERS = (
    re.compile(r"^Op .{0,80} schreef .{0,80}:\s*$", re.IGNORECASE),
    re.compile(r"^On .{0,80} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^Van:\s.+$", re.IGNORECASE),
    re.compile(r"^From:\s.+$", re.IGNORECASE),
)

_QUOTE_HEADER_LINES = (
    re.compile(r"^(from|van|sent|verzonden|to|aan|subject|onderwerp|date|datum|cc):\s*", re.IGNORECASE),
    re.compile(r"^op .{0,80} schreef .{0,80}:\s*$", re.IGNORECASE),
    re.compile(r"^on .{0,80} wrote:\s*$", re.IGNORECASE),
)


def _safe_console_print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_text)


def _collect_non_null_field_paths(data: dict, prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in data.items():
        if value is None:
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            nested = _collect_non_null_field_paths(value, path)
            if nested:
                paths.extend(nested)
            else:
                paths.append(path)
        else:
            paths.append(path)
    return paths


def _collect_effectively_changed_field_paths(
    existing_data: dict,
    extracted_data: dict,
    prefix: str = "",
) -> list[str]:
    paths: list[str] = []
    for key, value in extracted_data.items():
        if value is None:
            continue

        path = f"{prefix}.{key}" if prefix else key
        existing_value = existing_data.get(key) if isinstance(existing_data, dict) else None

        if isinstance(value, dict):
            nested_existing = existing_value if isinstance(existing_value, dict) else {}
            nested = _collect_effectively_changed_field_paths(nested_existing, value, path)
            paths.extend(nested)
            continue

        if existing_value != value:
            paths.append(path)

    return paths


def _build_update_note(
    action: str,
    result: AgentResult,
    changed_fields: list[str],
    dry_run: bool,
) -> str:
    mode = "DRY-RUN" if dry_run else "LIVE"
    if action == "create":
        basis = f"[{mode}] Nieuwe reservatie aangemaakt op basis van extracted velden"
    elif action == "update":
        basis = f"[{mode}] Reservatie geüpdatet op velden"
    elif action == "cancel":
        basis = f"[{mode}] Reservatie geannuleerd (status -> canceled)"
    else:
        basis = f"[{mode}] Reservatieactie uitgevoerd"
    if changed_fields:
        basis = f"{basis}: {', '.join(changed_fields)}"
    if result.interne_notitie:
        return f"{basis} | AI-notitie: {result.interne_notitie}"
    return basis


def _reservations_update_path(reservation_list) -> Path:
    source_path = Path(getattr(reservation_list, "path", config.RESERVATION_LIST_FILE))
    return source_path.parent / "reservations_update.json"


def _write_reservations_update_file(reservation_list, updated_items: list[dict]):
    if not updated_items:
        return
    output_path = _reservations_update_path(reservation_list)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(updated_items, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Wrote %d updated reservation item(s) to %s", len(updated_items), output_path)


def _review_queue_path() -> Path:
    return Path(config.REVIEW_QUEUE_FILE)


def _build_review_id(thread_id: str, message_id: str) -> str:
    return f"{thread_id}:{message_id}"


def _write_review_queue_file(review_items: list[dict]):
    if not review_items:
        return

    output_path = _review_queue_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing_items: list[dict] = []
    if output_path.exists():
        try:
            existing_items = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            existing_items = []

    existing_by_id = {
        item.get("review_id"): item
        for item in existing_items
        if isinstance(item, dict) and item.get("review_id")
    }

    for review_item in review_items:
        review_id = review_item.get("review_id")
        previous = existing_by_id.get(review_id)
        if previous and previous.get("status") in {"approved", "rejected"}:
            review_item["status"] = previous.get("status")
            review_item["decision_reason"] = previous.get("decision_reason")
            review_item["decided_at"] = previous.get("decided_at")
        existing_by_id[review_id] = review_item

    merged = sorted(
        existing_by_id.values(),
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    )
    output_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("Wrote %d review queue item(s) to %s", len(review_items), output_path)


def _build_review_item(
    thread_id: str,
    latest_msg_id: str,
    latest,
    analyzed_message,
    result: AgentResult | None,
    dry_run: bool,
    reservation_proposal: dict | None,
    error: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    proposal = {
        "category": result.category if result else "vervolg_overig",
        "ready_for_action": result.ready_for_action if result else False,
        "reservation_action": result.reservatielijst_action if result else "none",
        "matched_reservation_id": result.matched_reservation_id if result else None,
        "needs_human": result.needs_human if result else True,
        "needs_human_reason": result.needs_human_reason if result else "AI analysis failed",
        "no_reply_needed": result.no_reply_needed if result else False,
        "reply_email_nl": result.reply_email_nl if result else "",
        "interne_notitie": result.interne_notitie if result else None,
        "reservation_change": reservation_proposal,
    }
    return {
        "review_id": _build_review_id(thread_id, latest_msg_id),
        "status": "pending",
        "decision_reason": None,
        "decided_at": None,
        "created_at": now,
        "updated_at": now,
        "run_mode": "dry-run" if dry_run else "live",
        "error": error,
        "mail": {
            "thread_id": thread_id,
            "gmail_message_id": latest_msg_id,
            "from_email": analyzed_message.from_email,
            "subject": analyzed_message.subject,
            "date": analyzed_message.date,
            "body_preview": (analyzed_message.body_text or "")[:2000],
        },
        "proposal": proposal,
    }


def _build_reply_only_update_item(
    thread_id: str,
    result: AgentResult,
    reply_mode: str,
) -> dict:
    note = f"[LIVE] Geen reservatie-update; reply {reply_mode}"
    if result.interne_notitie:
        note = f"{note} | AI-notitie: {result.interne_notitie}"
    return {
        "email_thread_id": thread_id,
        "interne_notitie": note,
        "reply_email_nl": result.reply_email_nl,
    }


def _thread_with_full_text(thread: list) -> list:
    full_thread = []
    for message in thread:
        full_text = getattr(message, "body_text_full", None)
        if full_text and full_text.strip() and full_text.strip() != (message.body_text or "").strip():
            full_thread.append(replace(message, body_text=full_text))
        else:
            full_thread.append(message)
    return full_thread


def _has_more_full_context(thread: list) -> bool:
    for message in thread:
        full_text = getattr(message, "body_text_full", None)
        stripped = (message.body_text or "").strip()
        if full_text and full_text.strip() and full_text.strip() != stripped:
            return True
    return False


def _extract_quoted_text(full_text: str) -> str:
    lines = full_text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if any(pattern.match(stripped) for pattern in _QUOTE_MARKERS):
            return "\n".join(lines[index:]).strip()
    return ""


def _normalize_quoted_for_compare(text: str) -> str:
    normalized_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^>+\s*", "", stripped)
        if not stripped:
            continue
        if any(pattern.match(stripped) for pattern in _QUOTE_MARKERS):
            continue
        if any(pattern.match(stripped) for pattern in _QUOTE_HEADER_LINES):
            continue
        normalized_lines.append(stripped.lower())

    compact = " ".join(normalized_lines)
    return re.sub(r"\s+", " ", compact).strip()


def _quoted_text_differs_from_previous_messages(
    quoted_text: str,
    previous_messages: list,
    min_length: int = 40,
) -> bool:
    quoted_normalized = _normalize_quoted_for_compare(quoted_text)
    if len(quoted_normalized) < min_length:
        return False

    for message in previous_messages:
        baseline = getattr(message, "body_text_full", None) or message.body_text or ""
        previous_normalized = _normalize_quoted_for_compare(baseline)
        if len(previous_normalized) < min_length:
            continue
        if previous_normalized == quoted_normalized:
            return False
        if previous_normalized in quoted_normalized:
            return False
        if quoted_normalized in previous_normalized:
            return False

    return True


def _should_retry_with_full_context(thread: list) -> bool:
    for idx, message in enumerate(thread):
        full_text = getattr(message, "body_text_full", None) or ""
        body_text = message.body_text or ""
        if not full_text.strip() or full_text.strip() == body_text.strip():
            continue

        quoted_text = _extract_quoted_text(full_text)
        if not quoted_text:
            continue

        if _quoted_text_differs_from_previous_messages(quoted_text, thread[:idx]):
            return True
    return False


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


def _extract_email_addresses(header_value: str) -> set[str]:
    if not header_value:
        return set()
    return {
        email.strip().lower()
        for _, email in getaddresses([header_value])
        if email and email.strip()
    }


def _drop_org_messages_after_last_customer(
    thread: list,
    organisation_emails: set[str],
    customer_email: str,
):
    if not organisation_emails or not customer_email:
        return thread, []

    customer_email_normalized = customer_email.strip().lower()
    last_customer_idx = None
    for idx in range(len(thread) - 1, -1, -1):
        sender = (thread[idx].from_email or "").strip().lower()
        if sender == customer_email_normalized:
            last_customer_idx = idx
            break

    if last_customer_idx is None:
        return thread, []

    removed_messages = []
    filtered = []
    for idx, message in enumerate(thread):
        sender = (message.from_email or "").strip().lower()
        if idx > last_customer_idx and sender in organisation_emails:
            removed_messages.append(message)
            continue
        filtered.append(message)

    return filtered, removed_messages


def process_thread(
    service,
    reservation_list,
    thread_id: str,
    latest_msg_id: str,
    trigger_message,
    dry_run: bool,
    drop_last_org_reply: bool = False,
    reservation_list_stub: bool = False,
) -> tuple[list[dict], dict | None]:
    reservation_updates: list[dict] = []
    reservation_proposal: dict | None = None
    reply_dispatched = False
    reply_mode = ""

    thread = gmail_client.get_thread_messages(service, thread_id)
    if not thread:
        log.warning("Empty conversation for thread %s, skipping.", thread_id)
        return reservation_updates, None

    latest = thread[-1]
    customer_email = trigger_message.from_email or latest.from_email
    log.info("Processing conversation with %s | subject: %s", customer_email, latest.subject)

    # We try to automatically determine which email address is the customer and which is our own address.    
    own_email_hint = trigger_message.to.split(",")[0].strip() if trigger_message.to else ""

    thread_for_analysis = thread
    if drop_last_org_reply:
        organisation_emails = _extract_email_addresses(trigger_message.to)
        filtered_thread, removed_messages = _drop_org_messages_after_last_customer(
            thread,
            organisation_emails,
            customer_email,
        )
        if removed_messages:
            thread_for_analysis = filtered_thread
            log.info(
                "  -> dropped %d organisation response(s) after last customer message",
                len(removed_messages),
            )
        else:
            log.info("  -> no organisation responses found after last customer message")

    candidates = reservation_list.search(email=customer_email)

    if (
        _has_more_full_context(thread_for_analysis)
        and _should_retry_with_full_context(thread_for_analysis)
    ):
        log.info("  -> quoted older-email text differs from thread history; analyzing with full quoted context")
        thread_for_analysis = _thread_with_full_text(thread_for_analysis)
    else:
        log.info("  -> quoted context matches thread history; analyzing with trimmed body context")

    analyzed_message = thread_for_analysis[-1]

    try:
        result: AgentResult = analyze_email(
            thread_for_analysis,
            candidates,
            own_email_hint,
            reservation_list_stub=reservation_list_stub,
        )
    except Exception as e:
        log.error("AI analysis failed for thread %s: %s", thread_id, e)        
        if not dry_run:
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_NEEDS_HUMAN)
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_HANDLED)
        review_item = _build_review_item(
            thread_id,
            latest_msg_id,
            latest,
            analyzed_message,
            result=None,
            dry_run=dry_run,
            reservation_proposal=None,
            error=str(e),
        )
        return reservation_updates, review_item

    log.info(
        "  -> category=%s | ready_for_action=%s | action=%s | matched_reservation_id=%s | needs_human=%s | no_reply=%s",
        result.category,
        result.ready_for_action,
        result.reservatielijst_action,
        result.matched_reservation_id,
        result.needs_human,
        result.no_reply_needed,
    )    
    

    # --- Execute reservation-list action (if applicable) ---
    if result.ready_for_action and result.reservatielijst_action != "none":
        try:
            if result.reservatielijst_action == "create":
                data = _apply_extracted_to_reservation({}, result.extracted)
                data["email_thread_id"] = thread_id
                if result.reservation_type:
                    data["type"] = result.reservation_type
                reservation = Reservation.model_validate(data)
                if not dry_run:
                    reservation_list.create(reservation)
                    log.info("  -> new reservation created: %s", reservation.id)
                else:
                    log.info("  -> dry-run: reservation create prepared")

                created_item = json.loads(reservation.model_dump_json())
                changed_fields = _collect_non_null_field_paths(result.extracted)
                created_item["interne_notitie"] = _build_update_note(
                    "create", result, changed_fields, dry_run
                )
                created_item["reply_email_nl"] = result.reply_email_nl
                reservation_updates.append(created_item)
                reservation_proposal = {
                    "action": "create",
                    "reservation": created_item,
                    "changed_fields": changed_fields,
                }

            elif result.reservatielijst_action == "update" and result.matched_reservation_id:
                existing = reservation_list.get(result.matched_reservation_id)
                if existing:
                    existing_data = existing.model_dump()
                    merged = _apply_extracted_to_reservation(
                        existing_data, result.extracted
                    )
                    if not dry_run:
                        reservation_list.update(result.matched_reservation_id, merged)
                        log.info("  -> reservation updated: %s", result.matched_reservation_id)
                    else:
                        log.info("  -> dry-run: reservation update prepared: %s", result.matched_reservation_id)

                    merged_reservation = Reservation.model_validate(merged)
                    updated_item = json.loads(merged_reservation.model_dump_json())
                    changed_fields = _collect_effectively_changed_field_paths(
                        existing_data,
                        result.extracted,
                    )
                    updated_item["interne_notitie"] = _build_update_note(
                        "update", result, changed_fields, dry_run
                    )
                    updated_item["reply_email_nl"] = result.reply_email_nl
                    reservation_updates.append(updated_item)
                    reservation_proposal = {
                        "action": "update",
                        "reservation": updated_item,
                        "changed_fields": changed_fields,
                    }
                else:
                    log.warning("  -> matched_reservation_id not found, no update executed.")

            elif result.reservatielijst_action == "cancel" and result.matched_reservation_id:
                existing = reservation_list.get(result.matched_reservation_id)
                if existing:
                    if not dry_run:
                        ok = reservation_list.cancel(result.matched_reservation_id)
                        log.info("  -> reservation canceled: %s (found=%s)", result.matched_reservation_id, ok)
                    else:
                        log.info("  -> dry-run: reservation cancel prepared: %s", result.matched_reservation_id)

                    canceled_item = existing.model_dump()
                    canceled_item["status"] = "canceled"
                    canceled_item["interne_notitie"] = _build_update_note(
                        "cancel", result, ["status"], dry_run
                    )
                    canceled_item["reply_email_nl"] = result.reply_email_nl
                    reservation_updates.append(canceled_item)
                    reservation_proposal = {
                        "action": "cancel",
                        "reservation": canceled_item,
                        "changed_fields": ["status"],
                    }
                else:
                    log.warning("  -> matched_reservation_id not found, no cancel executed.")

        except Exception as e:
            log.error("  -> error while updating reservation list: %s", e)
            result.needs_human = True
            result.needs_human_reason = f"Reservation-list action failed: {e}"

    # --- Compose reply ---
    if dry_run:
        _safe_console_print("\n" + "=" * 70)
        _safe_console_print(
            f"THREAD: {thread_id}  |  FROM: {latest.from_email}  |  SUBJECT: {latest.subject}"
        )
        _safe_console_print("-" * 70)
        _safe_console_print(result.model_dump_json(indent=2))
        _safe_console_print("=" * 70 + "\n")
        review_item = _build_review_item(
            thread_id,
            latest_msg_id,
            latest,
            analyzed_message,
            result,
            dry_run,
            reservation_proposal,
        )
        return reservation_updates, review_item

    if result.no_reply_needed or result.needs_human or not result.reply_email_nl.strip():
        if result.needs_human:
            gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_NEEDS_HUMAN)
            log.info("  -> marked for manual follow-up: %s", result.needs_human_reason)
    else:
        if config.AUTO_SEND:
            gmail_client.send_reply(service, latest, result.reply_email_nl)
            log.info("  -> reply SENT (AUTO_SEND=true)")
            reply_dispatched = True
            reply_mode = "verstuurd"
        else:
            gmail_client.create_draft_reply(service, latest, result.reply_email_nl)
            log.info("  -> draft reply created (not sent yet)")
            reply_dispatched = True
            reply_mode = "als draft aangemaakt"

    if reply_dispatched and not reservation_updates:
        reply_only_item = _build_reply_only_update_item(
            thread_id,
            result,
            reply_mode,
        )
        reservation_updates.append(reply_only_item)
        reservation_proposal = {
            "action": "reply_only",
            "reservation": reply_only_item,
            "changed_fields": [],
        }

    gmail_client.add_label(service, latest_msg_id, config.GMAIL_LABEL_HANDLED)
    review_item = _build_review_item(
        thread_id,
        latest_msg_id,
        latest,
        analyzed_message,
        result,
        dry_run,
        reservation_proposal,
    )
    return reservation_updates, review_item


def run(
    dry_run: bool = False,
    max_threads: int = None,
    drop_last_org_reply: bool = False,
    reservation_list_stub: bool = False,
):
    if not config.OPENROUTER_API_KEY and not dry_run:
        log.warning(
            "OPENROUTER_API_KEY is missing. Add it to your .env -- see SETUP.md. "
            "(In dry-run without a key, the AI call will also fail.)"
        )

    service = gmail_client.get_gmail_service()    
    reservation_list = reservation_list_module.JsonFileReservationList()    

    msg_ids = gmail_client.list_new_message_ids(service)
    log.info("Found: %d new/unprocessed messages.", len(msg_ids))    

    seen_threads = set()
    processed = 0
    reservation_updates_for_run: list[dict] = []
    review_items_for_run: list[dict] = []
    for msg_id in msg_ids:
        if max_threads is not None and processed >= max_threads:
            break
        parsed = gmail_client.get_parsed_message(service, msg_id)        
        if parsed.thread_id in seen_threads:
            continue
        seen_threads.add(parsed.thread_id)
        updates, review_item = process_thread(
            service,
            reservation_list,
            parsed.thread_id,
            msg_id,
            parsed,
            dry_run,
            drop_last_org_reply=drop_last_org_reply,
            reservation_list_stub=reservation_list_stub,
        )
        reservation_updates_for_run.extend(updates)
        if review_item:
            review_items_for_run.append(review_item)

        processed += 1        

    _write_reservations_update_file(reservation_list, reservation_updates_for_run)
    _write_review_queue_file(review_items_for_run)
    log.info("Done. %d conversation(s) processed.", processed)    


def main():
    parser = argparse.ArgumentParser(description="Gmail business handler")
    parser.set_defaults(
        dry_run=config.MAIN_DEFAULT_DRY_RUN,
        drop_last_org_reply=config.MAIN_DEFAULT_DROP_LAST_ORG_REPLY,
        reservation_list_stub=config.MAIN_DEFAULT_RESERVATION_LIST_STUB,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print results, but do not change anything in Gmail or the reservation list.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=config.MAIN_DEFAULT_MAX_THREADS,
        help="Process at most this number of conversations.",
    )
    parser.add_argument(
        "--drop-last-org-reply",
        action="store_true",
        help=(
            "Debug mode: exclude organisation-sent messages after the customer's "
            "last message from AI analysis context."
        ),
    )
    reservation_stub_group = parser.add_mutually_exclusive_group()
    reservation_stub_group.add_argument(
        "--reservation-list-stub",
        dest="reservation_list_stub",
        action="store_true",
        help=(
            "Debug mode: treat reservation-list candidates as unreliable stub data "
            "in AI analysis."
        ),
    )
    reservation_stub_group.add_argument(
        "--no-reservation-list-stub",
        dest="reservation_list_stub",
        action="store_false",
        help="Disable stub mode even when RESERVATION_LIST_STUB=true in the environment.",
    )
    args = parser.parse_args()

    try:
        run(
            dry_run=args.dry_run,
            max_threads=args.max,
            drop_last_org_reply=args.drop_last_org_reply,
            reservation_list_stub=args.reservation_list_stub,
        )
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
