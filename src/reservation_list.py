"""
Reservation list interface.

IMPORTANT: this file is a PLACEHOLDER. In production, reservations live on
the website (filled through reservation forms). This project currently has no
active connection to that system.

`ReservationList` below is the interface used by the rest of the app.
`JsonFileReservationList` is a simple local implementation (a JSON file on
disk) so you can already test the full email flow end to end.

To connect this to the real website reservation list later:
  1. Write a new class inheriting from `ReservationList` and implement the
      methods (search, create, update, cancel) against the real website
      database/API.
  2. Replace `reservation_list_module.JsonFileReservationList()` in main.py
      with your new class.
The rest of the app (email parsing, AI agent, Gmail) can stay unchanged.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import config
from models import Reservation


class ReservationList(ABC):
    @abstractmethod
    def search(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        play_date_contains: Optional[str] = None,
    ) -> list[Reservation]:
        """Find candidate reservations, e.g. to match a cancellation/update."""

    @abstractmethod
    def get(self, reservation_id: str) -> Optional[Reservation]:
        ...

    @abstractmethod
    def create(self, reservation: Reservation) -> Reservation:
        ...

    @abstractmethod
    def update(self, reservation_id: str, updates: dict) -> Optional[Reservation]:
        ...

    @abstractmethod
    def cancel(self, reservation_id: str) -> bool:
        ...


class JsonFileReservationList(ReservationList):
    """Local mock implementation: stores reservations as JSON on disk.
    Great for local testing; replace with a real integration for production."""

    def __init__(self, path: str = None):
        self.path = Path(path or config.RESERVATION_LIST_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, items: list[dict]):
        self.path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    def search(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        play_date_contains: Optional[str] = None,
    ) -> list[Reservation]:
        items = self._load()
        results = []
        for item in items:
            if item.get("status") in {"geannuleerd", "canceled"}:
                continue
            contact = item.get("contact", {}) or {}
            match = True
            if email and contact.get("email", "").lower() != email.lower():
                match = False
            if name and match:
                if name.lower() not in (contact.get("naam") or "").lower():
                    match = False
            if play_date_contains and match:
                sd = json.dumps(item.get("speeldatum", {}), ensure_ascii=False).lower()
                if play_date_contains.lower() not in sd:
                    match = False
            if match:
                results.append(Reservation.model_validate(item))
        return results

    def get(self, reservation_id: str) -> Optional[Reservation]:
        for item in self._load():
            if item.get("id") == reservation_id:
                return Reservation.model_validate(item)
        return None

    def create(self, reservation: Reservation) -> Reservation:
        items = self._load()
        items.append(json.loads(reservation.model_dump_json()))
        self._save(items)
        return reservation

    def update(self, reservation_id: str, updates: dict) -> Optional[Reservation]:
        items = self._load()
        for item in items:
            if item.get("id") == reservation_id:
                item.update(updates)
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                item["status"] = "updated"
                self._save(items)
                return Reservation.model_validate(item)
        return None

    def cancel(self, reservation_id: str) -> bool:
        items = self._load()
        for item in items:
            if item.get("id") == reservation_id:
                item["status"] = "canceled"
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._save(items)
                return True
        return False


ReservationList = ReservationList
JsonFileReservationList = JsonFileReservationList
