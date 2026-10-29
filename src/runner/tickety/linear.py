"""Adapter Linear: pobranie zgloszenia i lista po typie stanu (HAR-53).

Runner pyta API GraphQL Lineara bezposrednio (bez modelu w petli, nie przez MCP).
Token tylko z `runner.secrets`; nie trafia do komunikatow, logow ani na wyjscie.
Brak dowodu (zla struktura, niepelna lista) to blad, nigdy cichy wynik.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pydantic

from runner import secrets
from runner.tickety.bledy import BladIntegracji, PrzekroczonyLimit, z_ponowieniem
from runner.tickety.kontrakt import Komentarz, TicketJson

DOSTAWCA = "Linear"
URL = "https://api.linear.app/graphql"
TIMEOUT_S = 30.0
MAKS_STRON = 20  # listuj: gorny limit stron po 100

_GDZIE_TOKEN = (
    '~/.config/cockpit/credentials.json (klucz "linear") '
    "albo w zmiennej COCKPIT_LINEAR_TOKEN"
)

_ZAPYTANIE_ZGLOSZENIA = """
query Zgloszenie($id: String!) {
  issue(id: $id) {
    id identifier title description url priority priorityLabel
    state { name type }
    labels(first: 100) { nodes { name } }
    comments(first: 100) { nodes { body createdAt user { name } } }
  }
}
"""

_ZAPYTANIE_LISTY = """
query Zgloszenia($typy: [String!], $po: String) {
  issues(first: 100, after: $po, filter: { state: { type: { in: $typy } } }) {
    nodes { identifier title state { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _blad(kod: str, komunikat: str) -> BladIntegracji:
    return BladIntegracji(DOSTAWCA, kod, komunikat)


def _token() -> str:
    """Token Lineara albo `brak_tokenu`; tresc pliku sekretow nie trafia do komunikatu."""
    try:
        token = secrets.get_token("linear")
    except (OSError, ValueError, AttributeError):
        # ValueError obejmuje UnicodeDecodeError i json.JSONDecodeError; AttributeError -
        # plik bedacy poprawnym JSON-em, ale nie obiektem.
        raise _blad(
            "brak_tokenu",
            f"{DOSTAWCA}: plik sekretów jest nieczytelny. "
            f"Popraw {_GDZIE_TOKEN} i spróbuj ponownie.",
        ) from None
    if not isinstance(token, str) or not token:
        raise _blad("brak_tokenu", f"{DOSTAWCA}: brak tokenu. Ustaw go w {_GDZIE_TOKEN}.")
    return token


def _token_odrzucony() -> BladIntegracji:
    return _blad(
        "token_odrzucony",
        f"{DOSTAWCA}: token odrzucony. Popraw go w {_GDZIE_TOKEN}.",
    )


def _retry_after(odpowiedz: httpx.Response) -> float | None:
    """Sekundy z `Retry-After`; nieliczbowe, ujemne i nieskonczone dane to brak."""
    surowe = odpowiedz.headers.get("retry-after")
    if surowe is None:
        return None
    try:
        sekundy = float(surowe)
    except ValueError:
        return None
    if not math.isfinite(sekundy) or sekundy < 0:
        return None
    return sekundy


def _kody_bledow(bledy: list[Any]) -> set[str]:
    kody: set[str] = set()
    for blad in bledy:
        if isinstance(blad, dict):
            rozszerzenia = blad.get("extensions")
            if isinstance(rozszerzenia, dict) and isinstance(rozszerzenia.get("code"), str):
                kody.add(rozszerzenia["code"])
    return kody


def _brak_zgloszenia(bledy: list[Any]) -> bool:
    return any(
        isinstance(b, dict)
        and isinstance(b.get("message"), str)
        and b["message"].startswith("Entity not found")
        for b in bledy
    )


def _interpretuj(odpowiedz: httpx.Response, id_zgloszenia: str | None) -> dict[str, Any]:
    """Zamienia odpowiedz HTTP na obiekt JSON albo blad/sygnal limitu (kolejnosc z kontraktu)."""
    if odpowiedz.status_code == 401:
        raise _token_odrzucony()
    if odpowiedz.status_code == 429:
        raise PrzekroczonyLimit(_retry_after(odpowiedz))
    try:
        cialo = odpowiedz.json()
    except ValueError:
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź nie jest poprawnym JSON-em.") from None
    if not isinstance(cialo, dict):
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź nie jest obiektem JSON.")
    bledy = cialo.get("errors")
    if bledy:
        if not isinstance(bledy, list):
            raise _blad("blad_dostawcy", f"{DOSTAWCA}: błąd zapytania GraphQL.")
        kody = _kody_bledow(bledy)
        if "RATELIMITED" in kody:
            raise PrzekroczonyLimit(_retry_after(odpowiedz))
        if "AUTHENTICATION_ERROR" in kody:
            raise _token_odrzucony()
        if id_zgloszenia is not None and _brak_zgloszenia(bledy):
            raise _blad("nie_znaleziono", f"{DOSTAWCA}: nie znaleziono zgłoszenia {id_zgloszenia}.")
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: błąd zapytania GraphQL.")
    if odpowiedz.status_code != 200:
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: błąd HTTP {odpowiedz.status_code}.")
    return cialo


async def _zapytaj(
    klient: httpx.AsyncClient,
    token: str,
    zapytanie: str,
    zmienne: dict[str, Any],
    odczekaj: Callable[[float], Awaitable[None]],
    id_zgloszenia: str | None = None,
) -> dict[str, Any]:
    """Jedno zapytanie GraphQL z ponawianiem przy limicie; zwraca obiekt odpowiedzi."""

    async def wywolanie() -> dict[str, Any]:
        try:
            odpowiedz = await klient.post(
                URL,
                json={"query": zapytanie, "variables": zmienne},
                headers={"Authorization": token},
            )
        except httpx.TransportError as wyjatek:
            # Tresc wyjatku moze niesc naglowki zadania, wiec tylko nazwa klasy.
            raise _blad(
                "blad_dostawcy",
                f"{DOSTAWCA}: błąd połączenia ({type(wyjatek).__name__})",
            ) from None
        return _interpretuj(odpowiedz, id_zgloszenia)

    return await z_ponowieniem(wywolanie, dostawca=DOSTAWCA, odczekaj=odczekaj)


def _zla_struktura() -> BladIntegracji:
    return _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź ma nieoczekiwaną strukturę.")


def _na_ticket(issue: dict[str, Any], teraz: datetime) -> TicketJson:
    stan = issue["state"]
    return TicketJson(
        source="linear",
        id=issue["identifier"],
        url=issue["url"],
        title=issue["title"],
        description=issue["description"] or "",
        acceptance_criteria=[],
        status=None if stan is None else stan["name"],
        priority=issue["priorityLabel"],
        labels=[etykieta["name"] for etykieta in issue["labels"]["nodes"]],
        comments=[
            Komentarz(
                author="nieznany" if k["user"] is None else k["user"]["name"],
                body=k["body"],
                at=k["createdAt"],
            )
            for k in issue["comments"]["nodes"]
        ],
        fetched_at=teraz,
        raw=issue,
    )


async def pobierz(
    id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    odczekaj: Callable[[float], Awaitable[None]] = asyncio.sleep,
    teraz: datetime | None = None,
) -> TicketJson:
    """Pobiera zgloszenie z Lineara do `TicketJson`; `acceptance_criteria` zawsze puste."""
    token = _token()
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as klient:
        cialo = await _zapytaj(
            klient, token, _ZAPYTANIE_ZGLOSZENIA, {"id": id}, odczekaj, id_zgloszenia=id
        )
    try:
        issue = cialo["data"]["issue"]
    except (KeyError, TypeError):
        raise _zla_struktura() from None
    if issue is None:
        raise _blad("nie_znaleziono", f"{DOSTAWCA}: nie znaleziono zgłoszenia {id}.")
    try:
        return _na_ticket(issue, teraz if teraz is not None else datetime.now(timezone.utc))
    except (KeyError, TypeError, pydantic.ValidationError):
        raise _zla_struktura() from None


async def listuj(
    typy_stanu: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    odczekaj: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[dict[str, str]]:
    """Lista zgloszen o stanie wskazanego TYPU (np. `unstarted`), wszystkie strony.

    Wiecej stron niz `MAKS_STRON` to blad: ucieta lista bez ostrzezenia byloby falszywie
    kompletna.
    """
    if not typy_stanu:
        return []
    token = _token()
    wynik: list[dict[str, str]] = []
    po: str | None = None
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as klient:
        for _ in range(MAKS_STRON):
            cialo = await _zapytaj(
                klient,
                token,
                _ZAPYTANIE_LISTY,
                {"typy": typy_stanu, "po": po},
                odczekaj,
            )
            try:
                strona = cialo["data"]["issues"]
                wynik.extend(
                    {"id": w["identifier"], "tytul": w["title"], "stan": w["state"]["name"]}
                    for w in strona["nodes"]
                )
                jest_nastepna = strona["pageInfo"]["hasNextPage"]
                po = strona["pageInfo"]["endCursor"]
            except (KeyError, TypeError):
                raise _zla_struktura() from None
            if not jest_nastepna:
                return wynik
    raise _blad(
        "blad_dostawcy",
        f"{DOSTAWCA}: lista dłuższa niż {MAKS_STRON} stron; nie zwracam niepełnej listy.",
    )
