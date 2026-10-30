"""Adapter Jira: Cloud i Data Center z konfiguracji, JQL, pole kryteriow (HAR-63).

Runner pyta REST Jiry bezposrednio. Tryb (Cloud albo Data Center) pochodzi wylacznie
z konfiguracji, nigdy z adresu. Lista idzie z JQL z konfiguracji, a kryteria akceptacji
z pola wskazanego w konfiguracji; nieustawione pole to pusta lista i ostrzezenie, bez
zgadywania z opisu. Token (i e-mail dla Cloud) tylko z `runner.secrets`; nie trafia do
komunikatow, logow ani na wyjscie. Brak dowodu (zla struktura, niepelna lista) to blad.
"""
from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

import httpx
import pydantic

from cockpit.services.errors import BladDziedziny
from runner import secrets
from runner.tickety.adf import adf_na_tekst
from runner.tickety.bledy import BladIntegracji, PrzekroczonyLimit, z_ponowieniem
from runner.tickety.kontrakt import Komentarz, TicketJson

DOSTAWCA = "Jira"
TIMEOUT_S = 30.0
MAKS_STRON = 20  # listuj: gorny limit stron
NA_STRONE = 50  # listuj: parametr maxResults
OSTRZEZENIE_BRAK_POLA = "pole kryteriów nieustawione w project.yaml"

_BAZA_API = {"cloud": "/rest/api/3", "data_center": "/rest/api/2"}
_SZUKAJ = {"cloud": "/search/jql", "data_center": "/search"}

_GDZIE_SEKRETY = "~/.config/cockpit/credentials.json albo w zmiennej COCKPIT_<NAZWA>_TOKEN"


class BlednaKonfiguracja(BladDziedziny):
    """Konfiguracja Jiry jest niepoprawna albo niepelna; komunikat wymienia pola."""


class KonfiguracjaJira(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    tryb: Literal["cloud", "data_center"]
    adres: str
    pole_kryteriow: str | None = None
    jql: str | None = None

    @pydantic.field_validator("adres")
    @classmethod
    def _bez_koncowego_ukosnika(cls, adres: str) -> str:
        return adres.rstrip("/")


def konfiguracja(dane: object) -> KonfiguracjaJira:
    """Waliduje konfiguracje; komunikat niesie nazwy pol, nigdy wartosci wejscia."""
    try:
        return KonfiguracjaJira.model_validate(dane)
    except pydantic.ValidationError as wyjatek:
        pola = dict.fromkeys(
            ".".join(str(czesc) for czesc in blad["loc"]) or "konfiguracja"
            for blad in wyjatek.errors()
        )
        raise BlednaKonfiguracja(
            f"{DOSTAWCA}: niepoprawna konfiguracja, błędne pola: {', '.join(pola)}."
        ) from None


def _blad(kod: str, komunikat: str) -> BladIntegracji:
    return BladIntegracji(DOSTAWCA, kod, komunikat)


def _sekret(nazwa: str) -> str:
    """Wartosc sekretu `nazwa` albo `brak_tokenu`; tresc pliku sekretow nie trafia do komunikatu."""
    try:
        wartosc = secrets.get_token(nazwa)
    except (OSError, ValueError, AttributeError):
        # ValueError obejmuje UnicodeDecodeError i json.JSONDecodeError; AttributeError -
        # plik bedacy poprawnym JSON-em, ale nie obiektem.
        raise _blad(
            "brak_tokenu",
            f'{DOSTAWCA}: plik sekretów jest nieczytelny (klucz "{nazwa}"). '
            f"Popraw {_GDZIE_SEKRETY} i spróbuj ponownie.",
        ) from None
    if not isinstance(wartosc, str) or not wartosc:
        raise _blad(
            "brak_tokenu",
            f'{DOSTAWCA}: brak sekretu "{nazwa}". Ustaw go w {_GDZIE_SEKRETY}.',
        )
    return wartosc


def _naglowek_autoryzacji(konf: KonfiguracjaJira) -> str:
    """Cloud: Basic z e-maila i tokenu; Data Center: Bearer z samego tokenu."""
    token = _sekret("jira")
    if konf.tryb == "data_center":
        return f"Bearer {token}"
    email = _sekret("jira_email")
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


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


def _interpretuj(odpowiedz: httpx.Response, klucz: str | None) -> dict[str, Any]:
    """Zamienia odpowiedz HTTP na obiekt JSON albo blad/sygnal limitu.

    `klucz` podany tylko przy pobraniu: dopiero wtedy 404 znaczy `nie_znaleziono`.
    """
    if odpowiedz.status_code == 401:
        raise _blad(
            "token_odrzucony",
            f"{DOSTAWCA}: dane logowania odrzucone. Popraw je w {_GDZIE_SEKRETY}.",
        )
    if odpowiedz.status_code == 404 and klucz is not None:
        raise _blad("nie_znaleziono", f"{DOSTAWCA}: nie znaleziono zgłoszenia {klucz}.")
    if odpowiedz.status_code == 429:
        raise PrzekroczonyLimit(_retry_after(odpowiedz))
    if odpowiedz.status_code != 200:
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: błąd HTTP {odpowiedz.status_code}.")
    try:
        cialo = odpowiedz.json()
    except ValueError:
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź nie jest poprawnym JSON-em.") from None
    if not isinstance(cialo, dict):
        raise _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź nie jest obiektem JSON.")
    return cialo


async def _zapytaj(
    klient: httpx.AsyncClient,
    url: str,
    naglowek: str,
    params: dict[str, str] | None,
    odczekaj: Callable[[float], Awaitable[None]],
    klucz: str | None = None,
) -> dict[str, Any]:
    """Jedno zapytanie GET z ponawianiem przy limicie; zwraca obiekt odpowiedzi."""

    async def wywolanie() -> dict[str, Any]:
        try:
            odpowiedz = await klient.get(
                url,
                params=params,
                headers={"Authorization": naglowek, "Accept": "application/json"},
            )
        except httpx.TransportError as wyjatek:
            # Tresc wyjatku moze niesc naglowki zadania, wiec tylko nazwa klasy.
            raise _blad(
                "blad_dostawcy",
                f"{DOSTAWCA}: błąd połączenia ({type(wyjatek).__name__})",
            ) from None
        return _interpretuj(odpowiedz, klucz)

    return await z_ponowieniem(wywolanie, dostawca=DOSTAWCA, odczekaj=odczekaj)


def _zla_struktura() -> BladIntegracji:
    return _blad("blad_dostawcy", f"{DOSTAWCA}: odpowiedź ma nieoczekiwaną strukturę.")


def _napis(wartosc: object) -> str:
    """Data Center oddaje wiki-markup jako napis; cokolwiek innego to zla struktura."""
    if not isinstance(wartosc, str):
        raise _zla_struktura()
    return wartosc


def _tekst(wartosc: Any, konf: KonfiguracjaJira) -> str:
    """Opis albo tresc komentarza: Cloud to ADF (splaszczany), Data Center to napis."""
    if konf.tryb == "cloud":
        return adf_na_tekst(wartosc)
    return _napis(wartosc)


def _kryteria(wartosc: Any) -> list[str]:
    """Kryteria z wartosci pola: napis, lista napisow albo ADF; inny typ to zla struktura."""
    if wartosc is None:
        return []
    if isinstance(wartosc, str):
        linie = wartosc.splitlines()
    elif isinstance(wartosc, dict):
        # ADF: znacznik punktu "- " nie jest trescia kryterium.
        linie = [
            linia.strip().removeprefix("- ") for linia in adf_na_tekst(wartosc).splitlines()
        ]
    elif isinstance(wartosc, list):
        if not all(isinstance(element, str) for element in wartosc):
            raise _zla_struktura()
        linie = wartosc
    else:
        raise _zla_struktura()
    return [linia.strip() for linia in linie if linia.strip()]


def _na_ticket(issue: dict[str, Any], konf: KonfiguracjaJira, teraz: datetime) -> TicketJson:
    fields = issue["fields"]
    tytul = fields["summary"]  # pierwszy dostep: `fields` niebedace obiektem pada tu na TypeError
    priorytet = fields.get("priority")
    surowe_komentarze = fields["comment"]["comments"] if "comment" in fields else []
    if not isinstance(surowe_komentarze, list):
        # `{}` albo `""` przeszloby petle jako "brak komentarzy" zamiast bledu struktury.
        raise _zla_struktura()
    komentarze = []
    for k in surowe_komentarze:
        # Tresc przed autorem: komentarz niebedacy obiektem pada na `k["body"]`
        # (TypeError), a nie na `k.get` (AttributeError spoza tabeli wyjatkow).
        tresc = _tekst(k["body"], konf)
        autor = k.get("author")
        utworzono = k.get("created")
        komentarze.append(
            Komentarz(
                author="nieznany" if autor is None else autor["displayName"],
                body=tresc,
                at=None if utworzono is None else datetime.fromisoformat(utworzono),
            )
        )
    return TicketJson(
        source="jira",
        id=issue["key"],
        url=f"{konf.adres}/browse/{issue['key']}",
        title=tytul,
        description="" if fields["description"] is None else _tekst(fields["description"], konf),
        acceptance_criteria=(
            [] if konf.pole_kryteriow is None else _kryteria(fields.get(konf.pole_kryteriow))
        ),
        status=fields["status"]["name"],
        priority=None if priorytet is None else priorytet["name"],
        labels=fields.get("labels") or [],
        comments=komentarze,
        fetched_at=teraz,
        raw=issue,
    )


async def pobierz(
    klucz: str,
    konf: KonfiguracjaJira,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    odczekaj: Callable[[float], Awaitable[None]] = asyncio.sleep,
    teraz: datetime | None = None,
) -> tuple[TicketJson, list[str]]:
    """Pobiera zgloszenie do `TicketJson` i liste ostrzezen (nic nie jest drukowane)."""
    naglowek = _naglowek_autoryzacji(konf)
    # Klucz z `?` albo `/` zmienilby zapytanie do Jiry i oddal inne zgloszenie jako sukces.
    adres = f"{konf.adres}{_BAZA_API[konf.tryb]}/issue/{quote(klucz, safe='')}"
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as klient:
        issue = await _zapytaj(klient, adres, naglowek, None, odczekaj, klucz=klucz)
    chwila = teraz if teraz is not None else datetime.now(timezone.utc)
    try:
        ticket = _na_ticket(issue, konf, chwila)
    except (KeyError, TypeError, ValueError, pydantic.ValidationError):
        raise _zla_struktura() from None
    ostrzezenia = [OSTRZEZENIE_BRAK_POLA] if konf.pole_kryteriow is None else []
    return ticket, ostrzezenia


async def listuj(
    konf: KonfiguracjaJira,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    odczekaj: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[dict[str, str]]:
    """Lista zgloszen z JQL z konfiguracji, wszystkie strony.

    Wiecej stron niz `MAKS_STRON` to blad: ucieta lista bez ostrzezenia byloby falszywie
    kompletna.
    """
    if konf.jql is None or not konf.jql.strip():
        raise BlednaKonfiguracja(f"{DOSTAWCA}: brak jql w konfiguracji, nie ma czego listować.")
    naglowek = _naglowek_autoryzacji(konf)
    adres = f"{konf.adres}{_BAZA_API[konf.tryb]}{_SZUKAJ[konf.tryb]}"
    wynik: list[dict[str, str]] = []
    params = {"jql": konf.jql, "maxResults": str(NA_STRONE), "fields": "summary,status"}
    if konf.tryb == "data_center":
        params["startAt"] = "0"
    start = 0
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as klient:
        for _ in range(MAKS_STRON):
            strona = await _zapytaj(klient, adres, naglowek, params, odczekaj)
            try:
                issues = strona["issues"]
                if not isinstance(issues, list):
                    # `{}` albo `""` dalo by "brak zgloszen" zamiast bledu struktury.
                    raise _zla_struktura()
                wynik.extend(
                    {
                        "id": w["key"],
                        "tytul": w["fields"]["summary"],
                        "stan": w["fields"]["status"]["name"],
                    }
                    for w in issues
                )
                if konf.tryb == "cloud":
                    nastepny = strona.get("nextPageToken")
                    if not nastepny:
                        return wynik
                    params["nextPageToken"] = nastepny
                else:
                    total = strona["total"]
                    start += len(issues)
                    if not (len(issues) > 0 and start < total):
                        return wynik
                    params["startAt"] = str(start)
            except (KeyError, TypeError):
                raise _zla_struktura() from None
    raise _blad(
        "blad_dostawcy",
        f"{DOSTAWCA}: lista dłuższa niż {MAKS_STRON} stron; nie zwracam niepełnej listy.",
    )
