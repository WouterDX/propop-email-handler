"""
E-mailparsing met de Python-standaardbibliotheek (email.*).

Geen extra dependencies nodig om een ruwe RFC822-mail om te zetten naar
bruikbare tekst: afzender, onderwerp, datum, en de "leesbare" body
(platte tekst bij voorkeur, anders HTML omgezet naar tekst).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional


class _HTMLToText(HTMLParser):
    """Kleine, dependency-vrije HTML -> tekst omzetter (best effort)."""

    _SKIP_TAGS = {"script", "style", "head"}
    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self._chunks)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html)
    except Exception:
        # Val terug op een ruwe tag-strip als de HTML echt kapot is.
        return re.sub(r"<[^>]+>", " ", html).strip()
    return parser.get_text()


@dataclass
class ParsedEmail:
    message_id: Optional[str]
    in_reply_to: Optional[str]
    references: list[str]
    from_name: str
    from_email: str
    to: str
    subject: str
    date: Optional[str]
    body_text: str
    body_text_full: Optional[str] = None
    thread_id: Optional[str] = None  # wordt door gmail_client ingevuld (Gmail-specifiek)
    gmail_msg_id: Optional[str] = None


def parse_raw_email(raw_bytes: bytes) -> ParsedEmail:
    """Parseer ruwe RFC822-bytes (zoals Gmail's messages.get(format='raw') teruggeeft)."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    return _parsed_from_message(msg)


def _parsed_from_message(msg: EmailMessage) -> ParsedEmail:
    from_name, from_email = parseaddr(msg.get("From", ""))

    body_text = ""
    try:
        body_part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        body_part = None

    if body_part is not None:
        content = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body_text = html_to_text(content)
        else:
            body_text = content
    else:
        # Zeldzaam fallback-pad voor niet-multipart, niet-standaard mails.
        try:
            body_text = msg.get_content()
        except Exception:
            body_text = ""

    references_raw = msg.get("References", "") or ""
    references = references_raw.split() if references_raw else []

    date_hdr = msg.get("Date")
    date_iso = None
    if date_hdr:
        try:
            date_iso = parsedate_to_datetime(date_hdr).isoformat()
        except Exception:
            date_iso = date_hdr

    full_text = body_text.strip()
    return ParsedEmail(
        message_id=msg.get("Message-ID"),
        in_reply_to=msg.get("In-Reply-To"),
        references=references,
        from_name=from_name,
        from_email=from_email,
        to=msg.get("To", ""),
        subject=msg.get("Subject", "(geen onderwerp)"),
        date=date_iso,
        body_text=strip_quoted_reply(full_text),
        body_text_full=full_text,
    )


# Herkent typische "op <datum> schreef <naam>:" / "-----Original Message-----"
# aanhef van een eerder bericht in de mailketen, zodat we niet telkens de
# hele geschiedenis dubbel doorsturen naar het taalmodel (die geschiedenis
# wordt sowieso apart, netjes per bericht, meegegeven -- zie gmail_client).
_QUOTE_MARKERS = [
    re.compile(r"^Op .{0,80} schreef .{0,80}:\s*$", re.IGNORECASE),
    re.compile(r"^On .{0,80} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^Van:\s.+$", re.IGNORECASE),
    re.compile(r"^From:\s.+$", re.IGNORECASE),
]


def strip_quoted_reply(text: str) -> str:
    lines = text.splitlines()
    cutoff = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(pat.match(stripped) for pat in _QUOTE_MARKERS):
            cutoff = i
            break
    trimmed = "\n".join(lines[:cutoff]).strip()
    # Verwijder resterende ">"-geciteerde regels.
    trimmed = "\n".join(
        l for l in trimmed.splitlines() if not l.strip().startswith(">")
    )
    return trimmed.strip() or text.strip()
