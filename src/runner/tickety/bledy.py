"""Bledy integracji z dostawca ticketow i ponawianie przy limicie zapytan (HAR-53).

Komunikat bledu jest dla czlowieka: nigdy nie niesie tokenu, naglowkow ani tresci
odpowiedzi dostawcy.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from cockpit.services.errors import BladDziedziny

T = TypeVar("T")

KODY = ("brak_tokenu", "token_odrzucony", "limit", "nie_znaleziono", "blad_dostawcy")


class BladIntegracji(BladDziedziny):
    """Dostawca ticketow nie dal wyniku; `kod` mowi dlaczego (jeden z KODY)."""

    def __init__(self, dostawca: str, kod: str, komunikat: str) -> None:
        if kod not in KODY:
            raise ValueError(f"nieznany kod bledu integracji: {kod!r}")
        super().__init__(komunikat)
        self.dostawca = dostawca
        self.kod = kod
        self.komunikat = komunikat


class PrzekroczonyLimit(Exception):
    """Sygnal od wywolania do `z_ponowieniem`: dostawca kazal zwolnic."""

    def __init__(self, odczekanie_s: float | None = None) -> None:
        super().__init__(odczekanie_s)
        self.odczekanie_s = odczekanie_s


async def z_ponowieniem(
    wywolanie: Callable[[], Awaitable[T]],
    proby: int = 3,
    *,
    dostawca: str,
    odczekaj: Callable[[float], Awaitable[None]] = asyncio.sleep,
    domyslne_odczekanie_s: float = 1.0,
    maks_odczekanie_s: float = 60.0,
) -> T:
    """Wola `wywolanie` najwyzej `proby` razy; ponawia tylko po `PrzekroczonyLimit`.

    Miedzy probami czeka tyle, ile zazadal dostawca (ucinane do `maks_odczekanie_s`),
    albo `domyslne_odczekanie_s`. Po ostatniej probie nie czeka: zglasza `limit`.
    Kazdy inny wyjatek przechodzi od razu.
    """
    if proby < 1:
        raise ValueError("liczba prob musi byc co najmniej 1")
    for _ in range(proby - 1):
        try:
            return await wywolanie()
        except PrzekroczonyLimit as limit:
            zadane = limit.odczekanie_s
            sekundy = (
                domyslne_odczekanie_s if zadane is None else min(zadane, maks_odczekanie_s)
            )
        await odczekaj(sekundy)
    try:
        return await wywolanie()
    except PrzekroczonyLimit:
        raise BladIntegracji(
            dostawca,
            "limit",
            f"{dostawca}: przekroczony limit zapytań po {proby} próbach; spróbuj później.",
        ) from None
