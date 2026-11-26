"""Ustawianie hasla z konsoli: `cockpit ustaw-haslo <email>` (HAR-111).

Wyszukanie uzytkownika po adresie i zmiana hasla leza w `services/`, bo `__main__` nie dotyka bazy.
Zmiana hasla i uniewaznienie wszystkich sesji to jedna transakcja na zablokowanym wierszu konta.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import User
from cockpit.services import konta, sesje


async def ustaw_haslo_z_konsoli(s: AsyncSession, email: str, nowe: str) -> bool:
    """True po zmianie hasla, False dla nieznanego adresu; za krotkie haslo to `StanNiemozliwy`."""
    adres = konta._normalizuj(email)
    try:
        adres.encode("utf-8")   # samotny surogat nie da sie zapisac w parametrze zapytania
    except UnicodeEncodeError:
        return False   # taki adres nie ma konta
    user = (
        await s.execute(select(User).where(User.email == adres).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        return False
    await konta.ustaw_haslo(s, user, nowe)
    # wyloguj_wszedzie commituje: zmiana hasla i uniewaznienie sesji ida jednym commitem
    await sesje.wyloguj_wszedzie(s, user.id)
    return True
