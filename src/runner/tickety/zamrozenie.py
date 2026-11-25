"""Ticket zamrozony w katalogu runu na starcie (HAR-117, BRO-111).

Runner zapisuje `<katalog_runu>/ticket.json` raz, przed pierwszym krokiem, z prawami `0444`.
Istniejacy plik zostaje bez zmian: zmiana opisu u dostawcy w trakcie runu nie zmienia tego,
co widza kroki. Plik lezy w katalogu runu, poza repozytorium uzytkownika (`run_dir_outside_repo`).

Zapis: tresc idzie do pliku tymczasowego (`O_EXCL`), a ten do `ticket.json` przez `os.link`,
ktory zawodzi, gdy cel istnieje. Czytelnik i wspolbiezny zapis widza wiec caly plik albo
zaden, a przerwany zapis nie zostawia obcietego `ticket.json`.
"""
from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cockpit.services.errors import BladDziedziny
from runner.operacje import BladOperacji
from runner.tickety import operacje as operacje_ticketow
from runner.tickety.kontrakt import TicketJson, do_pliku, z_wklejenia

PLIK_TICKETU = "ticket.json"
PRAWA_TICKETU = 0o444
KLUCZE_DOSTAWCY = frozenset({"projekt", "id"})
KLUCZE_WKLEJENIA = frozenset({"wklejony", "url"})


class BladTicketu(BladDziedziny):
    """Ticket runu nie da sie pobrac, zlozyc albo zapisac; tekst to powod dla czlowieka."""


def zamroz(katalog_runu: Path, ticket: TicketJson) -> Path:
    """Zapisuje `ticket.json` w katalogu runu, jesli jeszcze go nie ma; zwraca jego sciezke.

    Istniejacy plik (takze o innej tresci) zostaje. Blad dysku to `BladTicketu`."""
    cel = katalog_runu / PLIK_TICKETU
    tymczasowy = katalog_runu / f".{PLIK_TICKETU}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        deskryptor = os.open(tymczasowy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRAWA_TICKETU)
        try:
            with os.fdopen(deskryptor, "wb") as plik:
                os.fchmod(plik.fileno(), PRAWA_TICKETU)     # umask procesu nie zmienia praw
                plik.write(do_pliku(ticket))
            try:
                os.link(tymczasowy, cel)
            except FileExistsError:
                pass                    # zamrozone wczesniej: pierwszy zapis wygrywa
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tymczasowy)
    except OSError as e:
        raise BladTicketu(
            f"nie zapisano {PLIK_TICKETU} w katalogu runu: {type(e).__name__}") from e
    return cel


def _tekst_pola(zlecenie: Mapping[str, Any], pole: str) -> str:
    wartosc = zlecenie.get(pole)
    if not isinstance(wartosc, str):
        raise BladTicketu(f"polecenie runu: ticket.{pole} musi byc napisem")
    return wartosc


async def ticket_ze_zlecenia(zlecenie: object, sciezka_projektu: object) -> TicketJson:
    """Sklada ticket z pola `ticket` polecenia `run_workflow`.

    Dwa ksztalty: `{"projekt", "id"}` (pobranie od dostawcy przez `ticket_pobierz`) albo
    `{"wklejony", "url"?}` (tryb reczny, bez sieci). Dostawce pytamy o `sciezka_projektu`
    (`polecenie["sciezka"]`, ta sama co worktree), bo `ticket.projekt` to id projektu w kokpicie,
    a runner rozwiazuje tylko sciezki. Inny ksztalt, zly typ, pusty wklejony tekst i kazdy blad
    dostawcy to `BladTicketu` albo `BladIntegracji`; anulowanie przechodzi."""
    if not isinstance(zlecenie, Mapping):
        raise BladTicketu("polecenie runu: ticket musi byc obiektem")
    klucze = set(zlecenie)
    if klucze == KLUCZE_DOSTAWCY:
        _tekst_pola(zlecenie, "projekt")
        if not isinstance(sciezka_projektu, str) or not sciezka_projektu:
            raise BladTicketu("polecenie runu: brak sciezki projektu dla ticketu")
        return await _pobierz_od_dostawcy(
            {"projekt": sciezka_projektu, "id": _tekst_pola(zlecenie, "id")})
    if "wklejony" in klucze and klucze <= KLUCZE_WKLEJENIA:
        url = zlecenie.get("url")
        if url is not None and not isinstance(url, str):
            raise BladTicketu("polecenie runu: ticket.url musi byc napisem")
        return z_wklejenia(_tekst_pola(zlecenie, "wklejony"), url)     # PusteWklejenie: blad dziedziny
    raise BladTicketu("polecenie runu: ticket ma nieznany ksztalt (projekt+id albo wklejony[+url])")


async def _pobierz_od_dostawcy(dane: dict[str, str]) -> TicketJson:
    try:
        odpowiedz = await operacje_ticketow.ticket_pobierz(dane)
    except BladOperacji as e:
        raise BladTicketu(f"pobranie ticketu {dane['id']}: {e.komunikat}") from e
    except Exception as e:  # noqa: BLE001
        # WHY szeroki: granica sieci (httpx, parsowanie odpowiedzi, kod adaptera) - kazdy wyjatek
        # dostawcy poza anulowaniem (`CancelledError` to `BaseException`) i poza bledem dziedziny
        # konczy run `step_failed` + `run_finished`, a nie zostawia go w `running`. Tekst niesie
        # sama nazwe typu: tresc wyjatku moze niesc token albo odpowiedz dostawcy.
        if isinstance(e, BladDziedziny):
            raise                       # `BladIntegracji` niesie komunikat dla czlowieka
        raise BladTicketu(
            f"pobranie ticketu {dane['id']}: nieoczekiwany wyjątek dostawcy ({type(e).__name__})"
        ) from e
    try:
        surowy = odpowiedz["ticket"]
    except (KeyError, TypeError) as e:
        raise BladTicketu(
            f"odpowiedz dostawcy dla ticketu {dane['id']} nie zawiera ticketu") from e
    try:
        return TicketJson.model_validate(surowy)
    except ValidationError as e:
        raise BladTicketu(
            f"odpowiedz dostawcy dla ticketu {dane['id']} ma zly ksztalt "
            f"({e.error_count()} bledow)") from e


def ostrzezenie_o_opisie(ticket: TicketJson) -> str | None:
    """Tekst ostrzezenia, gdy ticket nie ma opisu (pusty albo same biale znaki); inaczej `None`."""
    if ticket.description.strip():
        return None
    return f"ticket {ticket.id} nie ma opisu"
