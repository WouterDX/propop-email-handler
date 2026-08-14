from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlparse

import config


REVIEW_QUEUE_PATH = Path(config.REVIEW_QUEUE_FILE)
REVIEW_DECISIONS_PATH = Path(config.REVIEW_DECISIONS_FILE)
STATIC_DIR = Path(__file__).resolve().parent / "review_ui"

_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _review_counts(items: list[dict]) -> dict:
    pending = sum(1 for item in items if item.get("status") == "pending")
    approved = sum(1 for item in items if item.get("status") == "approved")
    rejected = sum(1 for item in items if item.get("status") == "rejected")
    return {
        "total": len(items),
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
    }


class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args):
        return

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND.value, "Not found")
            return
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._serve_index()
            return

        if parsed.path == "/app.js":
            self._serve_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
            return

        if parsed.path == "/styles.css":
            self._serve_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            return

        if parsed.path == "/api/reviews":
            with _LOCK:
                items = _read_json_list(REVIEW_QUEUE_PATH)
            self._send_json({"items": items, "counts": _review_counts(items)})
            return

        self.send_error(HTTPStatus.NOT_FOUND.value, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]

        if len(path_parts) == 4 and path_parts[0] == "api" and path_parts[1] == "reviews" and path_parts[3] == "decision":
            self._handle_decision(unquote(path_parts[2]))
            return

        self.send_error(HTTPStatus.NOT_FOUND.value, "Not found")

    def _handle_decision(self, review_id: str) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json({"error": "Invalid JSON payload"}, status=HTTPStatus.BAD_REQUEST)
            return

        decision = (payload.get("decision") or "").strip().lower()
        if decision not in {"approved", "rejected"}:
            self._send_json(
                {"error": "decision must be 'approved' or 'rejected'"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        reason = (payload.get("reason") or "").strip()
        now = _now_iso()

        with _LOCK:
            items = _read_json_list(REVIEW_QUEUE_PATH)
            target = next((item for item in items if item.get("review_id") == review_id), None)
            if not target:
                self._send_json({"error": "Review item not found"}, status=HTTPStatus.NOT_FOUND)
                return

            target["status"] = decision
            target["decision_reason"] = reason or None
            target["decided_at"] = now
            target["updated_at"] = now
            _write_json(REVIEW_QUEUE_PATH, items)

            decisions = _read_json_list(REVIEW_DECISIONS_PATH)
            decisions.append(
                {
                    "review_id": review_id,
                    "decision": decision,
                    "reason": reason or None,
                    "decided_at": now,
                }
            )
            _write_json(REVIEW_DECISIONS_PATH, decisions)

        self._send_json({"ok": True, "review_id": review_id, "decision": decision})


def run_server(host: str = "127.0.0.1", port: int = 8787):
    REVIEW_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not REVIEW_QUEUE_PATH.exists():
        _write_json(REVIEW_QUEUE_PATH, [])

    server = ThreadingHTTPServer((host, port), ReviewHandler)
    print(f"Review frontend running at http://{host}:{port}")
    print(f"Queue file: {REVIEW_QUEUE_PATH}")
    print(f"Decisions file: {REVIEW_DECISIONS_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
