from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

import requests

import config
import pipeline_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(__name__)


def _extract_json(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _call_openrouter(messages: list[dict[str, str]]) -> str:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is niet ingesteld. De judge gebruikt OpenRouter en kan zonder key niet draaien."
        )

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if config.OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = config.OPENROUTER_SITE_URL
    if config.OPENROUTER_APP_NAME:
        headers["X-Title"] = f"{config.OPENROUTER_APP_NAME} (judge)"

    resp = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers=headers,
        json={
            "model": config.OPENROUTER_JUDGE_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter judge response has no choices: {data}")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError("OpenRouter judge response has empty content.")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _build_judge_messages(
    reference: dict[str, Any],
    include_instruction_review: bool,
    instructions_text: str | None,
) -> list[dict[str, str]]:
    system_prompt = (
        "Je bent een strenge maar eerlijke evaluator van e-mailantwoorden. "
        "Je vergelijkt een AI-antwoord met het echte menselijke antwoord in dezelfde context. "
        "Antwoord uitsluitend met geldig JSON."
    )

    review_id = reference.get("review_id")
    ai_input = reference.get("ai_input") or {}
    ai_output = reference.get("ai_output") or {}
    human_reference = reference.get("human_reference") or {}

    payload = {
        "review_id": review_id,
        "conversation_presented_to_ai": ai_input.get("thread_messages") or [],
        "ai_user_prompt": ai_input.get("user_prompt") or "",
        "ai_extracted_facts": ai_output.get("extracted") or {},
        "ai_reply": ai_output.get("reply_email_nl") or "",
        "human_reply": human_reference.get("answer_text") or "",
    }

    instruction_block = ""
    if include_instruction_review and instructions_text is not None:
        instruction_block = (
            "\n\nINSTRUCTIETEKST DIE DE AI GEBRUIKTE:\n"
            f"{instructions_text}"
        )

    user_prompt = (
        "Beoordeel dit paar AI-vs-mens antwoord.\n"
        "Doelen:\n"
        "1) Controleer of de feiten in ai_extracted_facts ook terug te vinden zijn in human_reply.\n"
        "2) Beoordeel of ai_reply qua toon en aanpak overeenkomt met het menselijke antwoord.\n"
        "   - Te veel vragen stellen is negatief.\n"
        "   - Irrelevante details geven is negatief.\n"
        "3) Geef een compacte, concrete evaluatie met scores.\n"
        "4) Als instructiereview gevraagd is, geef verbetersuggesties voor de instructietekst die het verschil kunnen verklaren.\n\n"
        "Geef exact dit JSON-schema terug:\n"
        "{\n"
        "  \"review_id\": \"...\",\n"
        "  \"facts_alignment\": {\n"
        "    \"score_0_10\": 0,\n"
        "    \"recognized_facts\": [\"...\"],\n"
        "    \"missing_or_conflicting_facts\": [\"...\"],\n"
        "    \"notes\": \"...\"\n"
        "  },\n"
        "  \"tone_and_response_quality\": {\n"
        "    \"score_0_10\": 0,\n"
        "    \"asks_too_much\": true,\n"
        "    \"irrelevant_details\": true,\n"
        "    \"issues\": [\"...\"],\n"
        "    \"notes\": \"...\"\n"
        "  },\n"
        "  \"overall\": {\n"
        "    \"score_0_10\": 0,\n"
        "    \"verdict\": \"good|mixed|bad\",\n"
        "    \"summary\": \"...\"\n"
        "  },\n"
        "  \"instruction_feedback\": {\n"
        "    \"suggested_changes\": [\"...\"],\n"
        "    \"rationale\": \"...\"\n"
        "  }\n"
        "}\n"
        "Als instruction_feedback niet gevraagd is: zet suggested_changes als [] en rationale als lege string.\n\n"
        "INPUT:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        f"{instruction_block}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _judge_single_reference(
    reference: dict[str, Any],
    include_instruction_review: bool,
    instructions_text: str | None,
) -> dict[str, Any]:
    messages = _build_judge_messages(
        reference,
        include_instruction_review=include_instruction_review,
        instructions_text=instructions_text,
    )
    raw = _call_openrouter(messages)
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Judge output is not a JSON object.")

    parsed["review_id"] = reference.get("review_id")
    parsed["judged_at"] = datetime.now(timezone.utc).isoformat()
    parsed["judge_model"] = config.OPENROUTER_JUDGE_MODEL
    parsed["include_instruction_review"] = include_instruction_review
    return parsed


def run_judge(
    input_file: str | None,
    max_items: int | None,
    review_id: str | None,
    include_instruction_review: bool,
    only_missing_results: bool,
) -> None:
    pipeline_path = Path(input_file) if input_file else Path(config.PIPELINE_DATA_FILE)
    pipeline_items = pipeline_store.read_pipeline_items(pipeline_path)
    if not pipeline_items:
        log.info("No pipeline items found in %s", pipeline_path)
        return

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in pipeline_items:
        item_review_id = item.get("review_id")
        if not item_review_id:
            continue
        if review_id and item_review_id != review_id:
            continue

        judge_reference = item.get("judge_reference") or {}
        if not isinstance(judge_reference, dict):
            continue

        if only_missing_results and isinstance(item.get("judge_result"), dict):
            continue

        human_answer = ((judge_reference.get("human_reference") or {}).get("answer_text") or "").strip()
        ai_reply = ((judge_reference.get("ai_output") or {}).get("reply_email_nl") or "").strip()
        if not human_answer or not ai_reply:
            continue

        selected.append((item, judge_reference))

    if max_items is not None:
        selected = selected[:max_items]

    if not selected:
        log.info("No eligible references selected for judging.")
        return

    instructions_text = None
    if include_instruction_review:
        instruction_path = Path(config.INSTRUCTIONS_FILE)
        instructions_text = instruction_path.read_text(encoding="utf-8")

    log.info("Judging %d reference item(s) with model %s", len(selected), config.OPENROUTER_JUDGE_MODEL)

    for idx, (pipeline_item, judge_reference) in enumerate(selected, start=1):
        rid = pipeline_item.get("review_id")
        try:
            judgement = _judge_single_reference(
                judge_reference,
                include_instruction_review=include_instruction_review,
                instructions_text=instructions_text,
            )
            pipeline_item["judge_result"] = judgement
            pipeline_item["updated_at"] = datetime.now(timezone.utc).isoformat()
            log.info("[%d/%d] Judged %s", idx, len(selected), rid)
        except Exception as e:
            log.error("[%d/%d] Failed judging %s: %s", idx, len(selected), rid, e)

    pipeline_store.write_pipeline_items(pipeline_items, pipeline_path)
    log.info("Wrote judge results into unified data file %s", pipeline_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone LLM judge for AI vs human email answers")
    parser.add_argument(
        "--input-file",
        type=str,
        default=None,
        help=(
            "Path to the JSON data file to judge. "
            "Defaults to PIPELINE_DATA_FILE from config when omitted."
        ),
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Maximum number of references to judge in this run.",
    )
    parser.add_argument(
        "--review-id",
        type=str,
        default=None,
        help="Judge only this review_id.",
    )
    parser.add_argument(
        "--include-instruction-review",
        action="store_true",
        help="Also review instruction text and propose edits that explain AI vs human differences.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-judge all selected references, including ones that already have a result.",
    )
    args = parser.parse_args()

    run_judge(
        input_file=args.input_file,
        max_items=args.max,
        review_id=args.review_id,
        include_instruction_review=args.include_instruction_review,
        only_missing_results=not args.all,
    )


if __name__ == "__main__":
    main()
