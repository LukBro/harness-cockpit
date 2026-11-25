"""Reset hasla jednorazowym tokenem z listu (HAR-105).

Token to `secrets.token_urlsafe(32)`; w bazie tylko jego sha256, jawny token jest wylacznie
w tresci listu (link z fragmentem `#reset=`, ktory nie trafia do serwera ani do logu dostepu)
i nie trafia do logow; wraca do serwera tylko w tresci POST /api/konto/reset/potwierdz. Prosba nie zdradza, czy adres ma konto. Uzycie jest
atomowe: warunek (nieuzyty, niewygasly) siedzi w samym UPDATE, a zmiana hasla, uniewaznienie
sesji i pozostalych tokenow uzytkownika to jedna transakcja.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Sesja, TokenResetu, User
from cockpit.services import konta, poczta, sesje
from cockpit.services.errors import BladPoczty, BrakDostepu, StanNiemozliwy
from cockpit.settings import settings

logger = logging.getLogger(__name__)

KOMUNIKAT_TOKENU = "link do zmiany hasła jest nieprawidłowy albo wygasł"
_TEMAT = "Reset hasła w Harness Cockpit"
_BRAK_ADRESU = "reset hasła wyłączony: ustaw zmienną COCKPIT_ADRES_PUBLICZNY"

Wysylka = Callable[[str, str, str], Awaitable[None]]


def _powod_wylaczenia() -> str | None:
    """Dlaczego reset nie dziala; None, gdy jest wlaczony."""
    stan = poczta.stan()
    if not stan.skonfigurowana:
        return stan.powod
    if not _adres_publiczny():
        return _BRAK_ADRESU
    return None


def _adres_publiczny() -> str:
    # bez bialych znakow i koncowego "/": link nie dostaje "//"; pusty wynik = reset wylaczony
    return (settings.adres_publiczny or "").strip().rstrip("/")


def wlaczony() -> bool:
    return _powod_wylaczenia() is None


def _tresc(adres_publiczny: str, token: str) -> str:
    link = f"{adres_publiczny}/#reset={token}"
    minuty = max(1, settings.reset_wazny_s // 60)
    return (
        "Ktoś poprosił o zmianę hasła do Harness Cockpit.\n\n"
        f"Ustaw nowe hasło tym linkiem (jednorazowy, ważny {minuty} min):\n{link}\n\n"
        "Jeśli to nie Ty, zignoruj ten list: hasło się nie zmieni.\n"
    )


async def popros(s: AsyncSession, email: str, wyslij: Wysylka = poczta.wyslij) -> None:
    """Wysyla list z linkiem, gdy adres ma konto; wynik jest ten sam dla kazdego adresu."""
    powod = _powod_wylaczenia()
    if powod is not None:
        raise StanNiemozliwy(powod)
    adres_publiczny = _adres_publiczny()
    adres = konta._normalizuj(email)
    try:
        adres.encode("utf-8")   # samotny surogat nie da sie zapisac w parametrze zapytania
    except UnicodeEncodeError:
        return   # taki adres nie ma konta: wynik jak dla nieznanego adresu
    user = (await s.execute(select(User).where(User.email == adres))).scalar_one_or_none()
    if user is None:
        return
    token = secrets.token_urlsafe(32)
    teraz = sesje.teraz()
    s.add(
        TokenResetu(
            user_id=user.id,
            skrot=sesje.skrot(token),
            utworzony_at=teraz,
            wygasa_at=teraz + timedelta(seconds=settings.reset_wazny_s),
        )
    )
    await s.commit()
    try:
        await wyslij(user.email, _TEMAT, _tresc(adres_publiczny, token))
    except BladPoczty:
        # bez tokenu i tresci w logu; odpowiedz taka sama jak przy sukcesie (brak wyroczni adresow)
        logger.warning("wysylka listu resetu hasla nie powiodla sie")


async def uzyj(s: AsyncSession, token: str, nowe_haslo: str) -> None:
    teraz = sesje.teraz()
    try:
        skrot = sesje.skrot(token)
    except UnicodeEncodeError:   # samotny surogat: taki token nie mogl wyjsc z kokpitu
        raise BrakDostepu(KOMUNIKAT_TOKENU) from None
    user_id = (
        await s.execute(select(TokenResetu.user_id).where(TokenResetu.skrot == skrot))
    ).scalar_one_or_none()
    if user_id is None:
        raise BrakDostepu(KOMUNIKAT_TOKENU)
    # kolejnosc blokad: najpierw konto, potem token; resety jednego konta ida po kolei
    user = (
        await s.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one()   # user_id pochodzi z klucza obcego, wiec wiersz istnieje
    # warunek w samym zapisie: dwa rownolegle uzycia nie przejda oba
    zuzyty = await s.execute(
        update(TokenResetu)
        .where(
            TokenResetu.skrot == skrot,
            TokenResetu.uzyty_at.is_(None),
            TokenResetu.wygasa_at > teraz,
        )
        .values(uzyty_at=teraz)
    )
    if zuzyty.rowcount != 1:
        await s.rollback()
        raise BrakDostepu(KOMUNIKAT_TOKENU)
    try:
        await konta.ustaw_haslo(s, user, nowe_haslo)
    except StanNiemozliwy:
        await s.rollback()   # za krotkie haslo nie zuzywa tokenu
        raise
    # uniewaznienie wprost (bez sesje.wyloguj_wszedzie, ktore commituje); wczesniejszy czas zostaje
    await s.execute(
        update(Sesja)
        .where(Sesja.user_id == user_id, Sesja.uniewazniona_at.is_(None))
        .values(uniewazniona_at=teraz)
    )
    await s.execute(
        update(TokenResetu)
        .where(TokenResetu.user_id == user_id, TokenResetu.uzyty_at.is_(None))
        .values(uzyty_at=teraz)
    )
    await s.commit()
