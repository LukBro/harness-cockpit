"""Zaleznosci tras: sesja z ciasteczka i token CSRF (HAR-48).

Brak ciasteczka albo sesja niewazna to 401 (BrakDostepu); brak lub zly naglowek
CSRF przy operacji zmieniajacej stan to 403 (Zabronione). Kolejnosc: najpierw sesja.
"""
from __future__ import annotations

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.session import get_session
from cockpit.services import runs as uslugi_runow
from cockpit.services import sesje
from cockpit.services.dostep import Kontekst
from cockpit.services.errors import BrakDostepu, Zabronione

CIASTECZKO_SESJI = "cockpit_sesja"
NAGLOWEK_CSRF = "X-CSRF-Token"
METODY_BEZPIECZNE = frozenset({"GET", "HEAD"})


def identyfikator_sesji(request: Request) -> str:
    identyfikator = request.cookies.get(CIASTECZKO_SESJI)
    if not identyfikator:
        raise BrakDostepu(sesje.KOMUNIKAT_SESJI)
    return identyfikator


async def kontekst_sesji(
    request: Request,
    identyfikator: str = Depends(identyfikator_sesji),
    s: AsyncSession = Depends(get_session),
) -> Kontekst:
    kontekst = await sesje.rozstrzygnij(s, identyfikator)
    if request.method not in METODY_BEZPIECZNE:
        token = request.headers.get(NAGLOWEK_CSRF)
        if not token:
            raise Zabronione("brak tokenu CSRF")
        try:
            await sesje.sprawdz_csrf(s, identyfikator, token)
        except BrakDostepu:
            raise Zabronione("nieprawidłowy token CSRF") from None
    return kontekst


async def runner_z_tokenu(
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
):
    """Runner uwierzytelniony tokenem z naglowka Authorization; brak lub zly token to 401."""
    return await uslugi_runow.runner_po_tokenie(s, authorization)


def trasa_publiczna() -> None:
    """Znacznik: ta trasa swiadomie dziala bez sesji i bez tokenu runnera.

    Nic nie robi w czasie zadania. Dzieki niemu kazda trasa /api/ ma jawnie
    nazwane zabezpieczenie (`kontekst_sesji`, `runner_z_tokenu` albo ten znacznik),
    a brak zabezpieczenia da sie wykryc mechanicznie (HAR-110).
    """
