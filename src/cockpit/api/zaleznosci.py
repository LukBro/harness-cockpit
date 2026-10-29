"""Zaleznosci tras: sesja z ciasteczka i token CSRF (HAR-48).

Brak ciasteczka albo sesja niewazna to 401 (BrakDostepu); brak lub zly naglowek
CSRF przy operacji zmieniajacej stan to 403 (Zabronione). Kolejnosc: najpierw sesja.
"""
from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.session import get_session
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
