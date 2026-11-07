"""Jeden punkt tlumaczenia bledow dziedziny na HTTP (HAR-30).

Trasy nie rzucaja `HTTPException` - rzucaja bledy z cockpit/services/errors.py,
a ten modul zamienia je na kod i komunikat. Slad stosu idzie do logu, nigdy
do odpowiedzi. Podklasy z `runner.*` sa poza tabela: API nie importuje runnera.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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


def _bezpieczne_dla_json(wartosc: object) -> object:
    """Liczby nieskonczone i NaN (JSON ich nie zna) zamienia na napis, rekurencyjnie."""
    if isinstance(wartosc, float) and not math.isfinite(wartosc):
        return str(wartosc)
    if isinstance(wartosc, dict):
        return {k: _bezpieczne_dla_json(v) for k, v in wartosc.items()}
    if isinstance(wartosc, (list, tuple)):
        return [_bezpieczne_dla_json(v) for v in wartosc]
    return wartosc


async def _obsluz_blad_walidacji(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Ksztalt jak domyslny handler FastAPI; `input: inf` nie moze zrobic z 422 kodu 500.
    szczegoly = jsonable_encoder(_bezpieczne_dla_json(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": szczegoly})


def zarejestruj_obsluge_bledow(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _obsluz_blad_walidacji)
    app.add_exception_handler(BladDziedziny, _obsluz_blad_dziedziny)
    app.add_exception_handler(Exception, _obsluz_nieoczekiwany_blad)
