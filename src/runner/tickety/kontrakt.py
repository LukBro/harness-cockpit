"""Kontrakt ticket.json i tryb wklejenia recznego (HAR-41, BRO-111).

Jedno wyjscie niezalezne od dostawcy: skille nie wiedza, skad ticket pochodzi.
Normalizujemy ksztalt, nie slownictwo - `status` i `priority` to napisy ze zrodla,
`acceptance_criteria` jest puste, gdy dostawca nie ma osobnego pola (bez zgadywania
z tresci), a `raw` zachowuje oryginal.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from cockpit.services.errors import BladDziedziny

# Jawna klasa ASCII zamiast \w (w Pythonie unikodowe); fullmatch zamiast $, ktore dopuszcza koncowy \n.
_ZRODLO = re.compile(r"linear|jira|github|manual|mcp:[A-Za-z0-9_.-]+")


class PusteWklejenie(BladDziedziny):
    """Wklejony tekst nie zawiera zadnej niepustej linii, wiec nie ma tytulu."""

    def __init__(self) -> None:
        super().__init__("wklejony tekst nie zawiera zadnej niepustej linii")


class Komentarz(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str
    body: str
    at: AwareDatetime | None


class TicketJson(BaseModel):
    # Kolejnosc deklaracji = kolejnosc kluczy w pliku.
    model_config = ConfigDict(extra="forbid")

    source: str
    id: str
    url: str | None
    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: str | None
    priority: str | None
    labels: list[str]
    comments: list[Komentarz]
    fetched_at: AwareDatetime
    raw: dict[str, Any]

    @field_validator("source")
    @classmethod
    def _zrodlo_z_listy(cls, wartosc: str) -> str:
        if _ZRODLO.fullmatch(wartosc) is None:
            raise ValueError(
                "source musi byc: linear, jira, github, manual albo mcp:<nazwa>"
            )
        return wartosc


def do_pliku(ticket: TicketJson) -> bytes:
    """Serializuje ticket do ticket.json: UTF-8, klucze w kolejnosci kontraktu."""
    tekst = json.dumps(ticket.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return tekst.encode("utf-8")


def z_wklejenia(
    tekst: str, url: str | None = None, *, teraz: datetime | None = None
) -> TicketJson:
    """Sklada ticket `manual` z wklejonego tekstu; opis i `raw` to tekst bez zmian."""
    tytul = next((l.strip() for l in tekst.splitlines() if l.strip()), None)
    if tytul is None:
        raise PusteWklejenie()
    skrot = hashlib.sha256(tekst.encode("utf-8")).hexdigest()[:8]
    return TicketJson(
        source="manual",
        id=f"manual-{skrot}",
        url=url,
        title=tytul,
        description=tekst,
        acceptance_criteria=[],
        status=None,
        priority=None,
        labels=[],
        comments=[],
        fetched_at=teraz if teraz is not None else datetime.now(timezone.utc),
        raw={"tekst": tekst},
    )
