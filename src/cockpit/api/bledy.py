"""Jeden punkt tlumaczenia bledow dziedziny na HTTP (HAR-30).

Trasy nie rzucaja `HTTPException` - rzucaja bledy z cockpit/services/errors.py,
a ten modul zamienia je na kod i komunikat. Slad stosu idzie do logu, nigdy
do odpowiedzi. Podklasy z `runner.*` sa poza tabela: API nie importuje runnera.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cockpit.services.errors import (
    BladDziedziny,
    BlednyPlikLekcji,
    BlednySkill,
    BrakDostepu,
    NaruszonyNiezmiennik,
    NiespojneDane,
    NieprawidloweObecneHaslo,
    NieZnaleziono,
    PlikPozaZakresem,
    RunnerOffline,
    StanNiemozliwy,
    WymagaPotwierdzenia,
    Zabronione,
    ZbytWieleProb,
)

logger = logging.getLogger(__name__)

KOMUNIKAT_WEWNETRZNY = "błąd wewnętrzny kokpitu"

# Zwykly dict (nie kopia przy rejestracji): handler czyta go przy kazdym zadaniu.
KODY_BLEDOW: Mapping[type[BladDziedziny], int] = {
    BrakDostepu: 401,
    Zabronione: 403,
    NieZnaleziono: 404,
    StanNiemozliwy: 409,
    NaruszonyNiezmiennik: 422,
    BlednySkill: 422,
    BlednyPlikLekcji: 422,
    NiespojneDane: 422,
    NieprawidloweObecneHaslo: 422,
    ZbytWieleProb: 429,
    PlikPozaZakresem: 403,
    RunnerOffline: 409,
    WymagaPotwierdzenia: 409,
}


def _blad_wewnetrzny(exc: BaseException) -> JSONResponse:
    logger.error("nieobsluzony blad w zadaniu", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": KOMUNIKAT_WEWNETRZNY})


async def _obsluz_blad_dziedziny(_: Request, exc: BladDziedziny) -> JSONResponse:
    # Najblizsza klasa w MRO, nie pierwsza pasujaca w kolejnosci tabeli.
    kod = next((KODY_BLEDOW[k] for k in type(exc).__mro__ if k in KODY_BLEDOW), None)
    if kod is None:
        return _blad_wewnetrzny(exc)  # blad bez wpisu: nigdy str(exc), nigdy 400
    tresc = {"detail": str(exc)}
    aktywny_run_id = getattr(exc, "aktywny_run_id", None)
    if aktywny_run_id is not None:
        tresc["aktywny_run_id"] = aktywny_run_id  # HAR-58: front pokazuje, ktory run trwa
    return JSONResponse(status_code=kod, content=tresc)


async def _obsluz_nieoczekiwany_blad(_: Request, exc: Exception) -> JSONResponse:
    return _blad_wewnetrzny(exc)


def zarejestruj_obsluge_bledow(app: FastAPI) -> None:
    app.add_exception_handler(BladDziedziny, _obsluz_blad_dziedziny)
    app.add_exception_handler(Exception, _obsluz_nieoczekiwany_blad)
