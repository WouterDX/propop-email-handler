"""
Datastructuren die door de rest van de app gebruikt worden.

We gebruiken pydantic om de JSON die het taalmodel teruggeeft te valideren.
Als het model iets teruggeeft dat niet aan dit schema voldoet, faalt de
validatie op een duidelijke manier in plaats van dat er stilletjes foute
data in de reservatielijst terechtkomt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict

Category = Literal[
    "nieuwe_reservatie_volledig",   # via website-formulier, gestructureerd
    "nieuwe_reservatie_onduidelijk",  # vrije tekst, info nog niet compleet
    "annulering",
    "wijziging",
    "cadeaubon",
    "bijwonen_voorstelling",
    "maatwerk_overig",
    "vervolg_overig",               # vervolgvraag die in geen enkele categorie hierboven past
]

ReservationType = Literal["verplaatsing", "school_lokaal", "familie_lokaal"]

ReservationStatus = Literal["nieuw", "bevestigd", "gewijzigd", "geannuleerd"]


class Factuurgegevens(BaseModel):
    organisatie: Optional[str] = None
    factuuradres: Optional[str] = None
    ondernemingsnummer: Optional[str] = None
    leveringswijze: Optional[Literal["peppol", "pdf_mail"]] = None


class Contact(BaseModel):
    naam: Optional[str] = None
    email: Optional[str] = None
    telefoon: Optional[str] = None
    gsm: Optional[str] = None
    # Mag propop deze gegevens gebruiken om op de speeldag zelf te bellen?
    bereikbaar_op_speeldag: Optional[bool] = None


class Speellocatie(BaseModel):
    type: Optional[Literal["poppenzaal", "op_verplaatsing"]] = None
    adres: Optional[str] = None  # verplicht als type == op_verplaatsing


class Speeldatum(BaseModel):
    vaste_datum: Optional[str] = None                 # ISO datum/tijd, bv "2026-03-14T10:00"
    voorkeurdatums: Optional[List[str]] = None         # lijst van (max 3) datum+uur strings
    periode: Optional[str] = None                      # bv "maart 2027"
    vage_aanduiding: Optional[str] = None               # bv "eerstvolgende donderdag die past"


class CafeKeuze(BaseModel):
    type: Optional[Literal["standaard", "vrije_keuze"]] = None
    omschrijving: Optional[str] = None


class Reservation(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    status: ReservationStatus = "nieuw"
    type: Optional[ReservationType] = None

    contact: Contact = Field(default_factory=Contact)
    speellocatie: Speellocatie = Field(default_factory=Speellocatie)
    voorstelling_titels: List[str] = Field(default_factory=list)
    doelgroep_leeftijd: Optional[str] = None
    speeldatum: Speeldatum = Field(default_factory=Speeldatum)

    aantal_kinderen: Optional[int] = None
    leeftijd_kinderen: Optional[str] = None
    aantal_volwassenen: Optional[int] = None

    pauze: Optional[bool] = None
    verduistering_mogelijk: Optional[bool] = None

    cafe_drankje: Optional[CafeKeuze] = None
    cafe_hapje: Optional[CafeKeuze] = None

    nieuwsbrief: Optional[bool] = None

    factuurgegevens: Optional[Factuurgegevens] = None

    opmerkingen: Optional[str] = None

    email_thread_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AgentResult(BaseModel):
    """Wat we van het taalmodel verwachten terug te krijgen, per e-mail."""
    model_config = ConfigDict(extra="ignore")

    category: Category
    reservation_type: Optional[ReservationType] = None

    # Structured extraction (kan gedeeltelijk zijn ingevuld)
    extracted: dict = Field(default_factory=dict)

    # Voor annulering/wijziging: welke bestaande reservatie (uit de
    # meegegeven kandidatenlijst) het model denkt dat bedoeld wordt.
    matched_reservation_id: Optional[str] = None

    # Is alle noodzakelijke info aanwezig om een actie uit te voeren
    # (aanmaken / wijzigen / annuleren in de reservatielijst)?
    ready_for_action: bool = False

    # Welke actie moet er (indien ready_for_action) op de reservatielijst gebeuren.
    reservatielijst_action: Optional[Literal["create", "update", "cancel", "none"]] = "none"

    # Moet een medewerker dit met de hand bekijken i.p.v. automatisch te antwoorden?
    needs_human: bool = False
    needs_human_reason: Optional[str] = None

    # Sommige berichten in een gesprek vragen gewoon geen antwoord meer
    # (bv. klant zegt "ok ik gebruik toch de website" -- instructies zeggen
    # dan expliciet te stoppen met verder antwoorden in dit gesprek).
    no_reply_needed: bool = False

    # Het voorgestelde antwoord (volledige mailtekst, in het Nederlands,
    # zonder onderwerp -- enkel de body). Leeg als needs_human=True.
    reply_email_nl: str = ""

    # Korte interne toelichting voor de medewerker (niet naar de klant).
    interne_notitie: Optional[str] = None
