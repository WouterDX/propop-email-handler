"""
Interface naar de "reservatielijst".

BELANGRIJK: dit bestand is een PLACEHOLDER. In de echte situatie staat de
reservatielijst op de website (gevuld via de reservatieformulieren). Dit
project heeft daar momenteel geen actieve verbinding mee.

`ReservatieLijst` hieronder is de interface die de rest van de app gebruikt.
`JsonFileReservatieLijst` is een eenvoudige, lokale implementatie (een JSON-
bestand op schijf) zodat je de hele e-mailflow nu al end-to-end kan testen.

Om dit later te koppelen aan de echte reservatielijst van de website:
  1. Schrijf een nieuwe klasse die van `ReservatieLijst` erft en de vier
     methodes (search, create, update, cancel) implementeert tegen de echte
     database/API van de website.
  2. Vervang in main.py de regel `reservatielijst.JsonFileReservatieLijst()`
     door jouw nieuwe klasse.
Al de rest van de app (e-mailparsing, AI-agent, Gmail) blijft ongewijzigd.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import config
from models import Reservation


class ReservatieLijst(ABC):
    @abstractmethod
    def search(
        self,
        naam: Optional[str] = None,
        email: Optional[str] = None,
        speeldatum_bevat: Optional[str] = None,
    ) -> list[Reservation]:
        """Zoek kandidaat-reservaties, bv. om een annulering/wijziging te matchen."""

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


class JsonFileReservatieLijst(ReservatieLijst):
    """Lokale mock-implementatie: bewaart reservaties als JSON op schijf.
    Prima om lokaal te testen; vervang door een echte koppeling voor productie."""

    def __init__(self, path: str = None):
        self.path = Path(path or config.RESERVATIELIJST_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, items: list[dict]):
        self.path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    def search(
        self,
        naam: Optional[str] = None,
        email: Optional[str] = None,
        speeldatum_bevat: Optional[str] = None,
    ) -> list[Reservation]:
        items = self._load()
        results = []
        for item in items:
            if item.get("status") == "geannuleerd":
                continue
            contact = item.get("contact", {}) or {}
            match = True
            if email and contact.get("email", "").lower() != email.lower():
                match = False
            if naam and match:
                if naam.lower() not in (contact.get("naam") or "").lower():
                    match = False
            if speeldatum_bevat and match:
                sd = json.dumps(item.get("speeldatum", {}), ensure_ascii=False).lower()
                if speeldatum_bevat.lower() not in sd:
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
                item["status"] = "gewijzigd"
                self._save(items)
                return Reservation.model_validate(item)
        return None

    def cancel(self, reservation_id: str) -> bool:
        items = self._load()
        for item in items:
            if item.get("id") == reservation_id:
                item["status"] = "geannuleerd"
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._save(items)
                return True
        return False
