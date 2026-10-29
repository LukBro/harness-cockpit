"""Adapter GitHub Issues: pobranie zgloszenia i lista po etykietach i stanie (HAR-65).

Runner pyta REST API GitHuba bezposrednio (bez modelu w petli, nie przez `gh`).
Token tylko z `runner.secrets`; nie trafia do komunikatow, logow ani na wyjscie.
Brak dowodu (zla struktura, niepelna lista) to blad, nigdy cichy wynik.
"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pydantic

from runner import secrets
from runner.tickety.bledy import BladIntegracji, PrzekroczonyLimit, z_ponowieniem
from runner.tickety.kontrakt import Komentarz, TicketJson

DOSTAWCA = "GitHub"
API = "https://api.github.com"
WERSJA_API = "2022-11-28"
TIMEOUT_S = 30.0
NA_STRONE = 100
MAKS_STRON = 20  # gorny limit stron po NA_STRONE

_GDZIE_TOKEN = (
    '~/.config/cockpit/credentials.json (klucz "github") '
    "albo w zmiennej COCKPIT_GITHUB_TOKEN"
)

_ID_ZGLOSZENIA = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)")
_REPOZYTORIUM = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_STANY = ("open", "closed", "all")


def _blad(kod: str, komunikat: str) -> BladIntegracji:
    return BladIntegracji(DOSTAWCA, kod, komunikat)


def _token() -> str:
    """Token GitHuba albo `brak_tokenu`; tresc pliku sekretow nie trafia do komunikatu."""
    try:
        token = secrets.get_token("github")
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


def _zla_struktura() -> BladIntegracji:
    return _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź ma nieoczekiwaną strukturę.")


def _interpretuj(odpowiedz: httpx.Response, opis: str) -> Any:
    """Zamienia odpowiedz HTTP na JSON albo blad/sygnal limitu (kolejnosc z kontraktu)."""
    status = odpowiedz.status_code
    if status == 401:
        raise _token_odrzucony()
    if status == 429:
        raise PrzekroczonyLimit(_retry_after(odpowiedz))
    if status == 403:
        # Primary i secondary rate limit GitHuba przychodza jako 403.
        if (
            odpowiedz.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in odpowiedz.headers
        ):
            raise PrzekroczonyLimit(_retry_after(odpowiedz))
        raise _blad(
            "token_odrzucony",
            f"{DOSTAWCA}: token nie ma uprawnień do tego zasobu. Popraw go w {_GDZIE_TOKEN}.",
        )
    if status == 404:
        raise _blad("nie_znaleziono", f"{DOSTAWCA}: nie znaleziono {opis}.")
    if status != 200:
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: błąd HTTP {status}.")
    try:
        return odpowiedz.json()
    except ValueError:
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź nie jest poprawnym JSON-em.") from None


async def _zapytaj(
    klient: httpx.AsyncClient,
    token: str,
    url: str,
    parametry: dict[str, str] | None,
    odczekaj: Callable[[float], Awaitable[None]],
    opis: str,
) -> tuple[Any, str | None]:
    """Jedno GET z ponawianiem przy limicie; zwraca JSON i adres nastepnej strony."""

    async def wywolanie() -> tuple[Any, str | None]:
        try:
            odpowiedz = await klient.get(
                url,
                params=parametry,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": WERSJA_API,
                    "User-Agent": "harness-cockpit",
                },
            )
        except httpx.TransportError as wyjatek:
            # Tresc wyjatku moze niesc naglowki zadania, wiec tylko nazwa klasy.
            raise _blad(
                "blad_dostawcy",
                f"{DOSTAWCA}: błąd połączenia ({type(wyjatek).__name__})",
            ) from None
        cialo = _interpretuj(odpowiedz, opis)
        return cialo, odpowiedz.links.get("next", {}).get("url")

    return await z_ponowieniem(wywolanie, dostawca=DOSTAWCA, odczekaj=odczekaj)


async def _wszystkie_strony(
    klient: httpx.AsyncClient,
    token: str,
    url: str,
    parametry: dict[str, str],
    odczekaj: Callable[[float], Awaitable[None]],
    opis: str,
) -> list[Any]:
    """Elementy wszystkich stron (kolejne adresy dosłownie z `Link`); ucieta lista to blad."""
    elementy: list[Any] = []
    kolejny: str | None = url
    for numer in range(MAKS_STRON):
        cialo, kolejny = await _zapytaj(
            klient, token, kolejny, parametry if numer == 0 else None, odczekaj, opis
        )
        if not isinstance(cialo, list):
            raise _zla_struktura()
        elementy.extend(cialo)
        if kolejny is None:
            return elementy
    raise _blad(
        "blad_dostawcy",
        f"{DOSTAWCA}: lista dłuższa niż {MAKS_STRON} stron; nie zwracam niepełnej listy.",
    )


def _na_ticket(
    id: str, issue: dict[str, Any], komentarze: list[Any], teraz: datetime
) -> TicketJson:
    return TicketJson(
        source="github",
        id=id,
        url=issue["html_url"],
        title=issue["title"],
        description=issue["body"] or "",
        acceptance_criteria=[],
        status=issue["state"],
        priority=None,
        labels=[etykieta["name"] for etykieta in issue["labels"]],
        comments=[
            Komentarz(
                author="nieznany" if k["user"] is None else k["user"]["login"],
                body=k["body"],
                at=k["created_at"],
            )
            for k in komentarze
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
    """Pobiera zgloszenie (`owner/repo#numer`) do `TicketJson`; kryteria zawsze puste."""
    dopasowanie = _ID_ZGLOSZENIA.fullmatch(id)
    if dopasowanie is None:
        raise _blad(
            "nie_znaleziono",
            f"{DOSTAWCA}: niepoprawny identyfikator zgłoszenia {id!r}; "
            "oczekiwano owner/repo#numer.",
        )
    wlasciciel, repo, numer = dopasowanie.groups()
    token = _token()
    url = f"{API}/repos/{wlasciciel}/{repo}/issues/{numer}"
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as klient:
        issue, _ = await _zapytaj(klient, token, url, None, odczekaj, id)
        if not isinstance(issue, dict):
            raise _zla_struktura()
        if "pull_request" in issue:
            raise _blad(
                "nie_znaleziono", f"{DOSTAWCA}: {id} to pull request, nie zgłoszenie."
            )
        komentarze = await _wszystkie_strony(
            klient, token, f"{url}/comments", {"per_page": str(NA_STRONE)}, odczekaj, id
        )
    zlozono = teraz if teraz is not None else datetime.now(timezone.utc)
    try:
        return _na_ticket(id, issue, komentarze, zlozono)
    except (KeyError, TypeError, pydantic.ValidationError):
        raise _zla_struktura() from None


async def listuj(
    repo: str,
    etykiety: list[str],
    stan: str = "open",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    odczekaj: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[dict[str, str]]:
    """Lista zgloszen repozytorium `owner/repo` po etykietach i stanie, wszystkie strony.

    Pusta lista etykiet to brak filtra. Pull requesty sa pomijane. Wiecej stron niz
    `MAKS_STRON` to blad: ucieta lista bez ostrzezenia byloby falszywie kompletna.
    """
    if stan not in _STANY:
        raise ValueError(f"stan musi byc jednym z {_STANY}, a jest {stan!r}")
    if _REPOZYTORIUM.fullmatch(repo) is None:
        raise _blad(
            "nie_znaleziono",
            f"{DOSTAWCA}: niepoprawne repozytorium {repo!r}; oczekiwano owner/repo.",
        )
    token = _token()
    parametry = {"state": stan, "per_page": str(NA_STRONE)}
    if etykiety:
        parametry["labels"] = ",".join(etykiety)
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as klient:
        wpisy = await _wszystkie_strony(
            klient, token, f"{API}/repos/{repo}/issues", parametry, odczekaj, repo
        )
    try:
        return [
            {"id": f"{repo}#{w['number']}", "tytul": w["title"], "stan": w["state"]}
            for w in wpisy
            if not (isinstance(w, dict) and "pull_request" in w)
        ]
    except (KeyError, TypeError):
        raise _zla_struktura() from None
