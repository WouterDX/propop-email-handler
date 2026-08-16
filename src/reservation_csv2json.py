"""
csv_naar_json.py

Zet de FormIt CSV-exports van de website om naar:
  - reservations.json   -> lijst van Reservation-objecten (models.py schema),
                            direct bruikbaar door reservation_list.JsonFileReservationList
  - contact_berichten.json -> losse lijst met de vrije-tekst berichten uit het
                            algemene contactformulier (dit ZIJN GEEN reservaties,
                            zie toelichting onderaan dit bestand)

Gebruik:
    python3 csv_naar_json.py \
        --familie formit_form__familievoorstelling_.csv \
        --school  formit_form__schoolvoorstelling_.csv \
        --verplaatsing formit_form__verplaatsing_.csv \
        --contact formit_form__contact_.csv \
        --out-dir ./output

Waarom het contactformulier apart blijft
-----------------------------------------
De vier CSV's komen uit verschillende FormIt-formulieren op de website:
  1. "familievoorstelling" -> reservatieformulier (gestructureerd)         -> Reservation
  2. "schoolvoorstelling"  -> reservatieformulier (gestructureerd)         -> Reservation
  3. "verplaatsing"        -> reservatieformulier (gestructureerd)         -> Reservation
  4. "contact"              -> algemeen contactformulier (vrije tekst:
                                naam/email/onderwerp/bericht)               -> GEEN reservatie

De "contact"-CSV bevat vragen zoals "waar vind ik het reservatieformulier",
vragen over prijzen, cadeaubonnen, of bevestigingen die nog niet toekwamen.
Dat past niet in het Reservation-schema (er is geen voorstelling/speeldatum/
type gekozen) en zou als reservatie-record in de reservatielijst enkel
lege/foute records opleveren. Dit script schrijft die berichten daarom naar
een apart bestand (contact_berichten.json), zodat een medewerker (of een
volgende verwerkingsstap, zie ook instructies_agent.md onder
"Andere vragen dan reservaties") ze met de hand kan bekijken.

Datakwaliteit
-------------
Dit zijn historische exports van echte klantinvoer via vrije tekstvelden
("Aantal kinderen": "ongeveer 190", "Voorkeur datum": "Vr 4/12", ...). Waar
een veld niet betrouwbaar naar een getal/datum kan worden omgezet, laat het
script het gestructureerde veld leeg (None) en bewaart het de originele tekst:
  - als extra veld op de reservatie (dankzij `extra="allow"` in models.py,
    bv. "aantal_kinderen_ruwe_tekst"), en
  - in `opmerkingen`, met een "[TE CONTROLEREN]"-prefix, zodat niets verloren
    gaat en een medewerker het snel kan terugvinden.

Zowel de familie- als de schoolvoorstelling-CSV worden generiek verwerkt via
kolomnaam-matching (substring, case-insensitive) i.p.v. harde kolomposities.
Dat is nodig omdat het schoolvoorstelling-export-bestand op dit moment leeg
is (geen enkele inzending tot nu toe) -- we kennen de exacte kolomnamen dus
niet met zekerheid, en dit maakt de code robuust zodra er wel data binnenkomt
(en ook bestand tegen kleine kolomnaam-varianten in de andere bestanden).
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import Reservation  # noqa: E402


# ---------------------------------------------------------------------------
# Referentielijst voorstellingen (uit instructies_agent.md), gebruikt
# om titels te normaliseren naar de officiële naam.
# ---------------------------------------------------------------------------
VOORSTELLINGEN_ALIASSEN: dict[str, str] = {}
_VOORSTELLINGEN_RAW = [
    ("Het Waait", ["het waait", "waait"]),
    ("De Zandman", ["zandman", "de zandman"]),
    ("BeestIG", ["beestig", "beest ig"]),
    ("Stapel", ["stapel"]),
    ("Bouwstenen", ["bouwstenen"]),
    ("Onderonsje", ["onderonsje"]),
    ("De Taartendief", ["taartendief", "de taartendief"]),
    ("Kijkdoos", ["kijkdoos"]),
    ("Bip", ["bip"]),
    ("Graaf", ["graaf"]),
    ("Sinterklaas Kapoentje", ["sinterklaas kapoentje", "sinterklaas", "sint"]),
    ("Bo kan alles", ["bo kan alles", "bo"]),
    ("Hoofd vol..#?!.", ["hoofd vol", "hoofdvol"]),
    ("Kleine Held", ["kleine held"]),
    ("Het lelijke eendje", ["het lelijke eendje", "lelijke eendje"]),
    ("De Kakmadam", ["de kakmadam", "kakmadam"]),
    ("Het meisje met de zwavelstokjes", ["het meisje met de zwavelstokjes", "zwavelstokjes"]),
    ("De Stoefpotloden", ["de stoefpotloden", "stoefpotloden"]),
    ("Control X", ["control x", "control-x", "controlx"]),
    ("Hee man!", ["hee man", "heeman"]),
]
for _naam, _aliassen in _VOORSTELLINGEN_RAW:
    for _a in _aliassen + [_naam]:
        VOORSTELLINGEN_ALIASSEN[_a.lower()] = _naam


def normaliseer_titel(ruwe_titel: str) -> str:
    """Probeer een vrij ingetypte titel te matchen met de officiële naam.
    Als er geen match is, wordt de opgeschoonde ruwe titel teruggegeven."""
    schoon = ruwe_titel.strip()
    sleutel = schoon.lower()
    if sleutel in VOORSTELLINGEN_ALIASSEN:
        return VOORSTELLINGEN_ALIASSEN[sleutel]
    for alias, canoniek in VOORSTELLINGEN_ALIASSEN.items():
        if alias in sleutel:
            return canoniek
    return schoon


# ---------------------------------------------------------------------------
# Generieke helpers
# ---------------------------------------------------------------------------
MAANDEN_NL = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10,
    "november": 11, "december": 12,
}

FAMILIE_KIES_PATRONEN = re.compile(
    r"^(?P<weekdag>[a-zA-Zé]+)\s+(?P<dag>\d{1,2})\s+(?P<maand>[a-zA-Zé]+)\s+"
    r"(?P<jaar>\d{4})\s+(?P<uur>\d{1,2})[.:](?P<minuut>\d{2})\s*u\.?\s*"
    r"['\u2018\u2019](?P<titel>.+?)['\u2018\u2019]\s*\((?P<leeftijd>.*?)\)\s*$"
)

VERPLAATSING_KIES_PATROON = re.compile(r"^(?P<titel>.+?)\s*\((?P<leeftijd>[^)]*)\)\s*$")


def clean(value: Optional[str]) -> Optional[str]:
    """Unescape HTML-entiteiten, trim whitespace, zet lege string om naar None."""
    if value is None:
        return None
    value = html.unescape(value).replace("\xa0", " ").replace("\r\n", "\n").strip()
    return value or None


def get(row: dict, *keywords: str) -> Optional[str]:
    """Vind de waarde van de eerste kolom waarvan de naam (case-insensitive)
    een van de gegeven keywords bevat. Keywords worden na elkaar geprobeerd."""
    lower_map = {k.lower(): v for k, v in row.items()}
    for kw in keywords:
        kw = kw.lower()
        for col_lower, val in lower_map.items():
            if kw in col_lower:
                cleaned = clean(val)
                if cleaned is not None:
                    return cleaned
    return None


def parse_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", text.replace(".", ""))
    return int(m.group()) if m else None


def parse_bool_ja_nee(text: Optional[str]) -> Optional[bool]:
    if not text:
        return None
    t = text.strip().lower()
    if t in ("ja", "yes", "y", "true"):
        return True
    if t in ("nee", "neen", "no", "n", "false"):
        return False
    return None


def parse_ingediend_op(raw_date: Optional[str]) -> Optional[str]:
    """FormIt datumformaat '2025-08-03, 11:09 am' -> ISO '2025-08-03T11:09:00'."""
    if not raw_date:
        return None
    from datetime import datetime
    try:
        dt = datetime.strptime(raw_date.strip(), "%Y-%m-%d, %I:%M %p")
        return dt.isoformat()
    except ValueError:
        return None


def parse_familie_school_kies_veld(ruw: Optional[str]) -> dict:
    """Parseer het 'Kies een voorstelling'-veld van familie-/schoolvoorstelling:
    bv. "zondag 21 september 2025 10.30 u 'Stapel' (2.5-5j)".
    Geeft dict terug met vaste_datum (ISO of None), titel, leeftijd, en of het
    parsen gelukt is."""
    resultaat = {"vaste_datum": None, "titel": None, "leeftijd": None, "geparsed": False}
    if not ruw:
        return resultaat
    tekst = html.unescape(ruw).replace("\u2019", "'").replace("\u2018", "'")
    m = FAMILIE_KIES_PATRONEN.match(tekst)
    if m:
        maand_nr = MAANDEN_NL.get(m.group("maand").lower())
        if maand_nr:
            dag = int(m.group("dag"))
            jaar = int(m.group("jaar"))
            uur = int(m.group("uur"))
            minuut = int(m.group("minuut"))
            resultaat["vaste_datum"] = f"{jaar:04d}-{maand_nr:02d}-{dag:02d}T{uur:02d}:{minuut:02d}"
            resultaat["titel"] = normaliseer_titel(m.group("titel"))
            resultaat["leeftijd"] = m.group("leeftijd").strip()
            resultaat["geparsed"] = True
            return resultaat
    # Niet gelukt te parsen volgens het verwachte patroon: probeer minstens
    # een titel + leeftijd te vinden zoals bij verplaatsing, anders raw bewaren.
    m2 = VERPLAATSING_KIES_PATROON.match(tekst)
    if m2:
        resultaat["titel"] = normaliseer_titel(m2.group("titel"))
        resultaat["leeftijd"] = m2.group("leeftijd").strip()
    return resultaat


def parse_verplaatsing_kies_veld(ruw: Optional[str]) -> dict:
    """Parseer 'Kies uw voorstelling' bij verplaatsing: bv. "De Zandman (2-5 jaar)"."""
    resultaat = {"titel": None, "leeftijd": None, "geparsed": False}
    if not ruw:
        return resultaat
    tekst = html.unescape(ruw)
    m = VERPLAATSING_KIES_PATROON.match(tekst)
    if m:
        resultaat["titel"] = normaliseer_titel(m.group("titel"))
        resultaat["leeftijd"] = m.group("leeftijd").strip()
        resultaat["geparsed"] = True
    else:
        resultaat["titel"] = normaliseer_titel(tekst)
    return resultaat


def leveringswijze_van(ruw: Optional[str]) -> Optional[str]:
    if not ruw:
        return None
    t = ruw.lower()
    if "peppol" in t:
        return "peppol"
    if "pdf" in t:
        return "pdf_mail"
    return None


# ---------------------------------------------------------------------------
# CSV inlezen
# ---------------------------------------------------------------------------
def lees_csv(pad: Path) -> list[dict]:
    with open(pad, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        return [row for row in reader if any((v or "").strip() for v in row.values())]


# ---------------------------------------------------------------------------
# Rij -> Reservation-dict
# ---------------------------------------------------------------------------
def rij_naar_reservation_dict(row: dict, reservation_type: str) -> dict:
    opmerkingen_delen: list[str] = []
    extra_velden: dict = {}

    ruwe_opmerkingen = get(row, "opmerkingen")
    if ruwe_opmerkingen:
        opmerkingen_delen.append(ruwe_opmerkingen)

    # --- contact ---
    naam = get(row, "naam contactpersoon", "naam")
    email = get(row, "emailadres", "email")
    telefoon = get(row, "telefoon")
    gsm = get(row, "gsm")
    contact = {
        "naam": naam,
        "email": email,
        "telefoon": telefoon,
        "gsm": gsm,
        "bereikbaar_op_speeldag": None,  # niet gevraagd in het formulier
    }

    # --- voorstelling + speeldatum ---
    voorstelling_titels: list[str] = []
    doelgroep_leeftijd = None
    speeldatum = {"vaste_datum": None, "voorkeurdatums": None, "periode": None, "vage_aanduiding": None}

    kies_ruw = get(row, "kies een voorstelling", "kies uw voorstelling", "kies")

    if reservation_type == "verplaatsing":
        geparsed = parse_verplaatsing_kies_veld(kies_ruw)
        if geparsed["titel"]:
            voorstelling_titels = [geparsed["titel"]]
        doelgroep_leeftijd = geparsed["leeftijd"]

        datum1 = get(row, "voorkeur datum")
        datum2 = get(row, "tweede keus")
        datum3 = get(row, "derde keus")
        uur = get(row, "voorkeur uur", "uur van de voorstelling")

        voorkeurdatums = []
        if datum1:
            voorkeurdatums.append(f"{datum1} ({uur})" if uur else datum1)
        for d in (datum2, datum3):
            if d and d not in ("/", "-"):
                voorkeurdatums.append(d)
        speeldatum["voorkeurdatums"] = voorkeurdatums or None
        if uur and datum1:
            extra_velden["voorkeur_uur_ruwe_tekst"] = uur

    elif reservation_type == "school_lokaal":
        geparsed = parse_verplaatsing_kies_veld(kies_ruw)
        if geparsed["titel"]:
            voorstelling_titels = [geparsed["titel"]]
        doelgroep_leeftijd = geparsed["leeftijd"]

        datum1 = get(row, "voorkeur datum")
        datum2 = get(row, "datum tweede keuze", "tweede keuze")
        datum3 = get(row, "datum derde keuze", "derde keuze")
        uur = get(row, "voorkeur uur van de voorstelling", "voorkeur uur", "uur van de voorstelling")

        voorkeurdatums = []
        if datum1:
            voorkeurdatums.append(f"{datum1} ({uur})" if uur else datum1)
        for d in (datum2, datum3):
            if d and d not in ("/", "-"):
                voorkeurdatums.append(d)
        speeldatum["voorkeurdatums"] = voorkeurdatums or None
        if uur and datum1:
            extra_velden["voorkeur_uur_ruwe_tekst"] = uur

    else:  # familie_lokaal (poppenzaal)
        geparsed = parse_familie_school_kies_veld(kies_ruw)
        if geparsed["geparsed"]:
            speeldatum["vaste_datum"] = geparsed["vaste_datum"]
        elif kies_ruw:
            speeldatum["vage_aanduiding"] = kies_ruw
            opmerkingen_delen.append(f"[TE CONTROLEREN] niet-geparsede voorstelling/datum: {kies_ruw}")
        if geparsed["titel"]:
            voorstelling_titels = [geparsed["titel"]]
        doelgroep_leeftijd = geparsed["leeftijd"]

    # --- speellocatie ---
    if reservation_type == "verplaatsing":
        speeladres = get(row, "speeladres")
        straat = get(row, "straat + huisnummer", "straat")
        postcode_plaats = get(row, "postcode + plaatsnaam", "postcode")
        adres = speeladres or " ".join(x for x in (straat, postcode_plaats) if x) or None
        speellocatie = {"type": "op_verplaatsing", "adres": adres}
    else:
        speellocatie = {"type": "poppenzaal", "adres": None}
        # Straatnaam/postcode in familie-formulier is het thuisadres van de
        # klant (niet de speellocatie); apart bewaren i.p.v. verliezen.
        klant_straat = get(row, "straatnaam", "straat + huisnummer")
        klant_postcode = get(row, "postcode + woonplaats")
        if klant_straat or klant_postcode:
            extra_velden["klant_adres"] = " ".join(x for x in (klant_straat, klant_postcode) if x)

    # --- aantallen ---
    aantal_kinderen_ruw = get(row, "aantal kinderen")
    aantal_kinderen = parse_int(aantal_kinderen_ruw)
    if aantal_kinderen_ruw and aantal_kinderen is None:
        extra_velden["aantal_kinderen_ruwe_tekst"] = aantal_kinderen_ruw
        opmerkingen_delen.append(f"[TE CONTROLEREN] aantal kinderen niet eenduidig: {aantal_kinderen_ruw}")
    elif aantal_kinderen_ruw and str(aantal_kinderen) != aantal_kinderen_ruw.strip():
        # tekst bevatte meer dan enkel een getal (bv. "ongeveer 190")
        extra_velden["aantal_kinderen_ruwe_tekst"] = aantal_kinderen_ruw

    aantal_volwassenen_ruw = get(row, "aantal volwassenen")
    aantal_volwassenen = parse_int(aantal_volwassenen_ruw)
    if aantal_volwassenen_ruw and aantal_volwassenen is None:
        extra_velden["aantal_volwassenen_ruwe_tekst"] = aantal_volwassenen_ruw
        opmerkingen_delen.append(f"[TE CONTROLEREN] aantal volwassenen niet eenduidig: {aantal_volwassenen_ruw}")
    elif aantal_volwassenen_ruw and str(aantal_volwassenen) != aantal_volwassenen_ruw.strip():
        extra_velden["aantal_volwassenen_ruwe_tekst"] = aantal_volwassenen_ruw

    leeftijd_kinderen = get(row, "leeftijd kinderen")

    # --- pauze / verduistering (verplaatsing + school) ---
    pauze = parse_bool_ja_nee(get(row, "pauze"))
    verduistering = parse_bool_ja_nee(get(row, "verduistering"))

    # --- cafe (schoolvoorstelling poppenzaal) ---
    cafe_drankje = None
    cafe_hapje = None
    drankje_ruw = get(row, "drankje")
    hapje_ruw = get(row, "hapje")
    if drankje_ruw:
        cafe_drankje = {
            "type": "vrije_keuze" if "vrij" in drankje_ruw.lower() else "standaard",
            "omschrijving": drankje_ruw,
        }
    if hapje_ruw:
        cafe_hapje = {
            "type": "vrije_keuze" if "vrij" in hapje_ruw.lower() else "standaard",
            "omschrijving": hapje_ruw,
        }

    # --- nieuwsbrief (familievoorstelling) ---
    nieuwsbrief_ruw = get(row, "nieuwsbrief")
    nieuwsbrief = bool(nieuwsbrief_ruw) if reservation_type == "familie_lokaal" else None

    # --- factuurgegevens ---
    organisatie = get(row, "organisatie of school", "organisatie")
    factuuradres = get(row, "factuuradres")
    ondernemingsnummer = get(row, "ondernemingsnummer")
    leveringswijze = leveringswijze_van(get(row, "peppol", "pdf"))
    factuurgegevens = None
    if organisatie or factuuradres or ondernemingsnummer or leveringswijze:
        factuurgegevens = {
            "organisatie": organisatie,
            "factuuradres": factuuradres,
            "ondernemingsnummer": ondernemingsnummer,
            "leveringswijze": leveringswijze,
        }

    # --- metadata van de inzending zelf ---
    ingediend_op = parse_ingediend_op(row.get("Date"))
    extra_velden["bron_formulier"] = row.get("Form")
    if row.get("Date"):
        extra_velden["ingediend_op_ruw"] = row.get("Date")
    if row.get("IP"):
        extra_velden["form_ip"] = row.get("IP")

    data = {
        "status": "nieuw",
        "type": reservation_type,
        "contact": contact,
        "speellocatie": speellocatie,
        "voorstelling_titels": voorstelling_titels,
        "doelgroep_leeftijd": doelgroep_leeftijd,
        "speeldatum": speeldatum,
        "aantal_kinderen": aantal_kinderen,
        "leeftijd_kinderen": leeftijd_kinderen,
        "aantal_volwassenen": aantal_volwassenen,
        "pauze": pauze,
        "verduistering_mogelijk": verduistering,
        "cafe_drankje": cafe_drankje,
        "cafe_hapje": cafe_hapje,
        "nieuwsbrief": nieuwsbrief,
        "factuurgegevens": factuurgegevens,
        "opmerkingen": "\n".join(opmerkingen_delen) or None,
        "email_thread_id": None,
        **extra_velden,
    }
    if ingediend_op:
        data["created_at"] = ingediend_op
        data["updated_at"] = ingediend_op
    return data


def verwerk_reservatie_csv(pad: Path, reservation_type: str) -> list[dict]:
    rijen = lees_csv(pad)
    resultaten = []
    for row in rijen:
        ruw = rij_naar_reservation_dict(row, reservation_type)
        reservering = Reservation.model_validate(ruw)  # valideert + genereert id
        resultaten.append(json.loads(reservering.model_dump_json()))
    return resultaten


def verwerk_contact_csv(pad: Path) -> list[dict]:
    """Het algemene contactformulier bevat GEEN reservaties (zie module-docstring
    hierboven) -- we bewaren de berichten als eenvoudige, aparte records."""
    rijen = lees_csv(pad)
    resultaten = []
    for row in rijen:
        resultaten.append({
            "ingediend_op": parse_ingediend_op(row.get("Date")) or row.get("Date"),
            "naam": clean(row.get("Naam")),
            "email": clean(row.get("Email")),
            "onderwerp": clean(row.get("Onderwerp")),
            "bericht": html.unescape(row.get("Bericht") or "").replace("\r\n", "\n").strip() or None,
            "form_ip": row.get("IP"),
        })
    return resultaten


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--familie", type=Path, help="CSV-export familievoorstelling")
    parser.add_argument("--school", type=Path, help="CSV-export schoolvoorstelling (poppenzaal)")
    parser.add_argument("--verplaatsing", type=Path, help="CSV-export voorstelling op verplaatsing")
    parser.add_argument("--contact", type=Path, help="CSV-export algemeen contactformulier")
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="Map om de json-bestanden in weg te schrijven")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    alle_reservaties: list[dict] = []

    if args.familie and args.familie.exists():
        rijen = verwerk_reservatie_csv(args.familie, "familie_lokaal")
        print(f"familievoorstelling: {len(rijen)} reservaties ingelezen")
        alle_reservaties += rijen

    if args.school and args.school.exists():
        rijen = verwerk_reservatie_csv(args.school, "school_lokaal")
        print(f"schoolvoorstelling: {len(rijen)} reservaties ingelezen")
        alle_reservaties += rijen

    if args.verplaatsing and args.verplaatsing.exists():
        rijen = verwerk_reservatie_csv(args.verplaatsing, "verplaatsing")
        print(f"verplaatsing: {len(rijen)} reservaties ingelezen")
        alle_reservaties += rijen

    reservations_pad = args.out_dir / "reservations.json"
    reservations_pad.write_text(json.dumps(alle_reservaties, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"-> {reservations_pad} geschreven ({len(alle_reservaties)} reservaties totaal)")

    if args.contact and args.contact.exists():
        contact_berichten = verwerk_contact_csv(args.contact)
        contact_pad = args.out_dir / "contact_berichten.json"
        contact_pad.write_text(json.dumps(contact_berichten, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"-> {contact_pad} geschreven ({len(contact_berichten)} berichten, GEEN reservaties)")


if __name__ == "__main__":
    main()
