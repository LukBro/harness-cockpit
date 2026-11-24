"""Podglad artefaktu runu (HAR-182): tresc pliku z katalogu runu, czytana wylacznie przez runnera runu.

Kokpit nie widzi dysku uzytkownika. Sciezka jest wzgledna wobec katalogu runu; jej ksztalt
sprawdzamy tu PRZED wyslaniem (runner sprawdza ja jeszcze raz, to nie jest jedyna zapora).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import runs, zapytania_runnera
from .errors import NiespojneDane, StanNiemozliwy
from .zapytania_runnera import Wyslij

OPERACJA = "read_artifact"


def _zly_ksztalt(powod: str) -> StanNiemozliwy:
    return StanNiemozliwy(f"odpowiedz runnera ma zly ksztalt ({powod})")


def _sprawdz_sciezke(sciezka: str) -> None:
    if not sciezka or "\0" in sciezka:
        raise NiespojneDane("sciezka artefaktu jest pusta albo zawiera znak NUL")
    if sciezka.startswith("/") or ".." in sciezka.replace("\\", "/").split("/"):
        raise NiespojneDane("sciezka artefaktu musi byc wzgledna i bez `..`")


def _ksztalt_artefaktu(wynik: Any) -> tuple[str, bool, int]:
    """(text, truncated, full_len); zly ksztalt to StanNiemozliwy, nigdy 500 ani zgadywanie."""
    if not isinstance(wynik, dict):
        raise _zly_ksztalt("wynik nie jest slownikiem")
    tekst = wynik.get("text")
    if not isinstance(tekst, str):
        raise _zly_ksztalt("pole text nie jest napisem")
    obciety = wynik.get("truncated")
    if not isinstance(obciety, bool):
        raise _zly_ksztalt("pole truncated nie jest wartoscia logiczna")
    pelna = wynik.get("full_len")
    if not isinstance(pelna, int) or isinstance(pelna, bool) or pelna < 0:
        raise _zly_ksztalt("pole full_len nie jest nieujemna liczba calkowita")
    try:
        bajty = len(tekst.encode("utf-8"))
    except UnicodeEncodeError:   # pojedynczy surogat z JSON-a runnera
        raise _zly_ksztalt("pole text nie jest poprawnym tekstem UTF-8") from None
    if pelna < bajty:
        raise _zly_ksztalt("pole full_len jest krotsze niz przyslany tekst")
    return tekst, obciety, pelna


async def artefakt_runu(
    s: AsyncSession, tenant_id: str, run_id: str, sciezka: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Artefakt runu zespolu. Run cudzy albo nieznany to NieZnaleziono (bez pytania runnera),
    runner runu offline to RunnerOffline (bez zastepstwa innym runnerem)."""
    run = await runs.run_dla_najemcy(s, run_id, tenant_id)
    _sprawdz_sciezke(sciezka)
    wynik = await zapytania_runnera.zapytaj_runnera(
        s, tenant_id, run.runner_id, OPERACJA, {"run_id": run.id, "sciezka": sciezka},
        polaczone, wyslij)
    tekst, obciety, pelna = _ksztalt_artefaktu(wynik)
    return {"path": sciezka, "text": tekst, "truncated": obciety, "full_len": pelna}
