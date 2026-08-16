from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config


def _pipeline_data_path() -> Path:
    return Path(config.PIPELINE_DATA_FILE)


def read_pipeline_items(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _pipeline_data_path()
    if not target.exists():
        return []

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def write_pipeline_items(items: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or _pipeline_data_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_pipeline_items(items: list[dict[str, Any]], path: Path | None = None) -> list[dict[str, Any]]:
    if not items:
        return read_pipeline_items(path)

    existing = read_pipeline_items(path)
    by_id: dict[str, dict[str, Any]] = {
        item.get("review_id"): item
        for item in existing
        if item.get("review_id")
    }

    for incoming in items:
        review_id = incoming.get("review_id")
        if not review_id:
            continue

        previous = by_id.get(review_id)
        merged = dict(previous) if isinstance(previous, dict) else {}
        merged.update(incoming)

        if previous:
            previous_status = previous.get("status")
            incoming_status = incoming.get("status")
            if previous_status in {"approved", "rejected"} and incoming_status == "pending":
                merged["status"] = previous_status
                merged["decision_reason"] = previous.get("decision_reason")
                merged["decided_at"] = previous.get("decided_at")

            if previous.get("judge_result") and not incoming.get("judge_result"):
                merged["judge_result"] = previous.get("judge_result")

            merged["created_at"] = previous.get("created_at") or incoming.get("created_at")

        by_id[review_id] = merged

    merged_items = sorted(
        by_id.values(),
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    )
    write_pipeline_items(merged_items, path)
    return merged_items
