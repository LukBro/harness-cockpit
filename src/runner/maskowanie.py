"""Maskowanie sekretow w logach runnera i kokpitu (HAR-86, BRO-98).

Kazdy rekord `logging` przechodzi przez filtr, ktory zamienia znane sekrety na
prefiks + gwiazdki (`mask()` z `runner.secrets`).
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping

from runner.secrets import mask

_SEKRETY: set[str] = set()
# Glob COCKPIT_*_TOKEN z ticketu obejmuje pusty środek, a get_token("") czyta
# zmienną COCKPIT__TOKEN; szerszy wzorzec maskuje więcej sekretów, więc jest bezpieczniejszy.
_ZMIENNA_TOKENU = re.compile(r"COCKPIT_.*_TOKEN")
_BEARER = re.compile(r"(Bearer\s+)(\S+)")


def zarejestruj_sekret(wartosc: object) -> None:
    """Dodaje sekret do rejestru maskowanych wartosci; pusty napis i nie-napis ignoruje.

    WHY: plik sekretow bywa uszkodzony (lista, dict) - rejestracja nie moze rzucic
    TypeError; odrzucenie takiej wartosci to sprawa adapterow.
    """
    if isinstance(wartosc, str) and wartosc:
        _SEKRETY.add(wartosc)


class FiltrSekretow(logging.Filter):
    """Maskuje sekrety w tresci i argumentach rekordu; nigdy nie odrzuca rekordu."""

    def maskuj(self, tekst: str) -> str:
        sekrety = set(_SEKRETY)
        for nazwa, wartosc in os.environ.items():
            if wartosc and _ZMIENNA_TOKENU.fullmatch(nazwa):
                sekrety.add(wartosc)
        for sekret in sorted(sekrety, key=len, reverse=True):
            tekst = tekst.replace(sekret, mask(sekret))
        return _BEARER.sub(lambda m: m.group(1) + mask(m.group(2)), tekst)

    def _maskuj_wartosc(self, wartosc: object) -> object:
        return self.maskuj(wartosc) if isinstance(wartosc, str) else wartosc

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.maskuj(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._maskuj_wartosc(a) for a in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {k: self._maskuj_wartosc(v) for k, v in record.args.items()}
        return True


_LOGGERY_MASKOWANE = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx")
_FILTR = FiltrSekretow()


def zainstaluj_maskowanie() -> None:
    """Zaklada filtr na loggerach i ich handlerach (idempotentnie).

    WHY: dictConfig uvicorna wymienia handlery, a filtry loggera przezywaja;
    filtr loggera nie lapie rekordow z loggerow potomnych, wiec filtr idzie tez
    na handlery (korzenia i wymienionych loggerow); korzen bez handlerow
    wypisuje rekordy przez `logging.lastResort`, wiec filtr idzie i tam.
    """
    for logger in (logging.getLogger(), *(logging.getLogger(n) for n in _LOGGERY_MASKOWANE)):
        logger.addFilter(_FILTR)
        for handler in logger.handlers:
            handler.addFilter(_FILTR)
    if logging.lastResort is not None:
        logging.lastResort.addFilter(_FILTR)
