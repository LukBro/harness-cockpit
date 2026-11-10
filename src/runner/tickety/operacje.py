"""Operacje runnera na tickety (HAR-104, BRO-111): lista i pobranie przez dostawce z `project.yaml`.

Kokpit nie rozmawia z dostawca ani nie ma jego tokenu: pyta runnera o katalog projektu, a ten
czyta sekcje `tickets` i wola adapter. Zapis wyniku do dostawcy jest poza zakresem: operacja
`ticket_zapisz_wynik` zawsze odmawia i nie wysyla zadania (zapis wlaczony osobnym ticketem).
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from cockpit.services.projekt import PLIK_PROJEKTU, wczytaj_projekt
from cockpit.services.workflow import Poziom
from runner.operacje import BladOperacji, _rozwiaz, _tekst
from runner.tickety import github, jira, linear
from runner.tickety.bledy import BladIntegracji
from runner.zakres import Zakres

# Podmiana w testach: adaptery pytaja ten transport zamiast sieci.
TRANSPORT: httpx.AsyncBaseTransport | None = None

ZRODLA = ("linear", "jira", "github")
TYPY_STANU_DOMYSLNE = ["unstarted", "started"]
BRAK_SEKCJI = "brak sekcji tickets w .cockpit/project.yaml — wybór ticketu wymaga konfiguracji dostawcy"
ZLE_ZRODLO = "tickets.source musi być jednym z: linear, jira, github"
BRAK_REPO = "tickets.repo jest wymagane dla dostawcy github"
STANY_GITHUB = ("open", "closed", "all")
ZLE_TYPY_STANU = "tickets.typy_stanu musi być listą napisów"
ZLE_ETYKIETY = "tickets.etykiety musi być listą napisów"
ZLY_STAN = "tickets.stan musi być jednym z: open, closed, all"
ZAPIS_WYLACZONY = (
    "zapis wyniku do systemu ticketów wyłączony — włącz tickets.post_result: true "
    "w .cockpit/project.yaml"
)
ZAPIS_NIEDOSTEPNY = "zapis wyniku do systemu ticketów nie jest jeszcze dostępny"


def _filtr(dane: dict) -> str | None:
    if dane.get("filtr") is None:
        return None
    return _tekst(dane, "filtr")


def _elementy(filtr: str | None) -> list[str]:
    """Elementy filtra po przecinku, przyciete; puste odpadaja."""
    return [e.strip() for e in (filtr or "").split(",") if e.strip()]


def _lista_napisow(wartosc: Any, komunikat: str) -> list[str]:
    """Wartosc z konfiguracji musi byc lista napisow; inaczej blad konfiguracji."""
    if not isinstance(wartosc, list) or not all(isinstance(e, str) for e in wartosc):
        raise BladOperacji("zla_konfiguracja", komunikat)
    return wartosc


def _sekcja_tickets(projekt: str) -> dict[str, Any]:
    """Sekcja `tickets` projektu w zakresie runnera, ze sprawdzonym `source`."""
    katalog = _rozwiaz(Zakres.z_konfiguracji(), projekt)
    wczytany, problemy = wczytaj_projekt(katalog)
    bledy = [p for p in problemy if p.poziom == Poziom.blad]
    if bledy:
        raise BladOperacji("zla_konfiguracja", f"{PLIK_PROJEKTU.as_posix()}: {bledy[0].komunikat}")
    tickets = wczytany.tickets
    if tickets is None:
        raise BladOperacji("brak_konfiguracji", BRAK_SEKCJI)
    if tickets.get("source") not in ZRODLA:
        raise BladOperacji("zla_konfiguracja", ZLE_ZRODLO)
    return tickets


@contextmanager
def _bledy_dostawcy() -> Iterator[None]:
    try:
        yield
    except BladIntegracji as e:
        raise BladOperacji(e.kod, e.komunikat) from None
    except jira.BlednaKonfiguracja as e:
        raise BladOperacji("zla_konfiguracja", str(e)) from None


def _konfiguracja_jiry(tickets: dict[str, Any], **dodatki: Any) -> jira.KonfiguracjaJira:
    pola = {k: v for k, v in tickets.items() if k not in ("source", "post_result")}
    return jira.konfiguracja({**pola, **dodatki})


async def ticket_lista(dane: dict) -> list[dict[str, str]]:
    projekt = _tekst(dane, "projekt")
    filtr = _filtr(dane)
    elementy = _elementy(filtr)
    tickets = await asyncio.to_thread(_sekcja_tickets, projekt)
    with _bledy_dostawcy():
        if tickets["source"] == "linear":
            typy_stanu = elementy or _lista_napisow(
                tickets.get("typy_stanu", TYPY_STANU_DOMYSLNE), ZLE_TYPY_STANU)
            return await linear.listuj(typy_stanu, transport=TRANSPORT)
        if tickets["source"] == "github":
            repo = tickets.get("repo")
            if not isinstance(repo, str) or not repo:
                raise BladOperacji("zla_konfiguracja", BRAK_REPO)
            etykiety = elementy or _lista_napisow(tickets.get("etykiety", []), ZLE_ETYKIETY)
            stan = tickets.get("stan", "open")
            if stan not in STANY_GITHUB:
                raise BladOperacji("zla_konfiguracja", ZLY_STAN)
            return await github.listuj(repo, etykiety, stan, transport=TRANSPORT)
        jql = (filtr or "").strip()
        dodatki = {"jql": jql} if jql else {}
        return await jira.listuj(_konfiguracja_jiry(tickets, **dodatki), transport=TRANSPORT)


async def ticket_pobierz(dane: dict) -> dict[str, Any]:
    projekt = _tekst(dane, "projekt")
    id_ticketu = _tekst(dane, "id")
    tickets = await asyncio.to_thread(_sekcja_tickets, projekt)
    ostrzezenia: list[str] = []
    with _bledy_dostawcy():
        if tickets["source"] == "linear":
            ticket = await linear.pobierz(id_ticketu, transport=TRANSPORT)
        elif tickets["source"] == "github":
            ticket = await github.pobierz(id_ticketu, transport=TRANSPORT)
        else:
            ticket, ostrzezenia = await jira.pobierz(
                id_ticketu, _konfiguracja_jiry(tickets), transport=TRANSPORT)
    return {"ticket": ticket.model_dump(mode="json"), "ostrzezenia": ostrzezenia}


async def ticket_zapisz_wynik(dane: dict) -> dict[str, Any]:
    """Zawsze odmowa: nawet przy `post_result: true` zapis do dostawcy nie jest jeszcze dostepny."""
    projekt = _tekst(dane, "projekt")
    _tekst(dane, "id")
    tickets = await asyncio.to_thread(_sekcja_tickets, projekt)
    if tickets.get("post_result") is not True:
        raise BladOperacji("zapis_wylaczony", ZAPIS_WYLACZONY)
    raise BladOperacji("zapis_niedostepny", ZAPIS_NIEDOSTEPNY)
