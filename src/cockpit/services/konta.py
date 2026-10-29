"""Konta: rejestracja z wlasnym zespolem, logowanie argon2id, zmiana hasla (HAR-44).

BRO-115: cienka warstwa, argon2id. BRO-118: kazdy dostaje wlasny zespol.
D18: zajety adres przy rejestracji ujawniamy. Haslo nie trafia do logow ani do
komunikatow wyjatkow.
"""
from __future__ import annotations

import math
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import BlokadaLogowania, Membership, RolaWZespole, Sesja, Tenant, User
from cockpit.services import sesje
from cockpit.services.errors import BrakDostepu, StanNiemozliwy, ZbytWieleProb
from cockpit.settings import settings

KOMUNIKAT_LOGOWANIA = "nieprawidłowy adres lub hasło"
KOMUNIKAT_BLOKADY = "zbyt wiele nieudanych prób, spróbuj za {minuty} min"
MIN_DLUGOSC_HASLA = 8
_ZA_KROTKIE = "hasło musi mieć co najmniej 8 znaków"
_ZAJETY_ADRES = "adres jest już zajęty"

_HASHER = PasswordHasher()
# Skrot atrapy dla nieistniejacego adresu: wyrownuje czas odpowiedzi z istniejacym
_SKROT_ATRAPY = _HASHER.hash(secrets.token_urlsafe(32))


def _normalizuj(email: str) -> str:
    # walidator modelu dziala tylko przez ORM, a tu szukamy zapytaniem
    return email.strip().lower()


def _zgodne(skrot: str, haslo: str) -> bool:
    try:
        _HASHER.verify(skrot, haslo)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def _dotyczy_adresu(blad: IntegrityError) -> bool:
    # PostgreSQL podaje nazwe ograniczenia, SQLite kolumne
    opis = str(blad.orig)
    return "uq_user_email" in opis or "user.email" in opis


async def zarejestruj(
    s: AsyncSession, email: str, haslo: str, nazwa_zespolu: str | None = None
) -> User:
    adres = _normalizuj(email)
    if len(haslo) < MIN_DLUGOSC_HASLA:
        raise StanNiemozliwy(_ZA_KROTKIE)
    nazwa = ((nazwa_zespolu or "").strip() or adres)[:200]
    tenant = Tenant(name=nazwa)
    user = User(email=adres, haslo_skrot=_HASHER.hash(haslo))
    s.add(tenant)
    s.add(user)
    try:
        await s.flush()
        # czlonkostwo dopiero po flush: id zespolu i uzytkownika musza juz istniec
        s.add(Membership(tenant_id=tenant.id, user_id=user.id, rola=RolaWZespole.owner))
        await s.commit()
    except IntegrityError as blad:
        await s.rollback()
        if _dotyczy_adresu(blad):
            raise StanNiemozliwy(_ZAJETY_ADRES) from None
        raise
    except Exception:
        await s.rollback()
        raise
    return user


def klucz_pary(email: str, ip: str) -> str:
    # w bazie tylko skrot: ani adres, ani IP nie leza jawnie
    return sesje.skrot("para\0" + _normalizuj(email) + "\0" + ip)


def klucz_adresu(email: str) -> str:
    return sesje.skrot("adres\0" + _normalizuj(email))


def _aktywna(wiersz: BlokadaLogowania | None, teraz: datetime) -> bool:
    # scisle: dokladnie w chwili zablokowane_do blokada juz nie dziala
    return (
        wiersz is not None
        and wiersz.zablokowane_do is not None
        and sesje._utc(wiersz.zablokowane_do) > teraz
    )


def _zapisz_nieudana(
    s: AsyncSession,
    wiersz: BlokadaLogowania | None,
    klucz: str,
    teraz: datetime,
    limit: int,
    okno_s: int,
) -> None:
    if wiersz is None:
        wiersz = BlokadaLogowania(klucz=klucz, nieudane=1, pierwsza_at=teraz)
        s.add(wiersz)
    elif (
        sesje._utc(wiersz.pierwsza_at) + timedelta(seconds=okno_s) <= teraz
        or wiersz.zablokowane_do is not None
    ):
        # nowe liczenie: okno minelo albo wygasla blokada (aktywnej tu nie ma)
        wiersz.nieudane = 1
        wiersz.pierwsza_at = teraz
        wiersz.zablokowane_do = None
    else:
        wiersz.nieudane += 1
    if wiersz.nieudane >= limit:
        wiersz.zablokowane_do = teraz + timedelta(seconds=settings.logowanie_blokada_s)


async def _wiersz(s: AsyncSession, klucz: str) -> BlokadaLogowania | None:
    # FOR UPDATE: rownolegle proby tej samej pary czekaja na siebie (SQLite to ignoruje)
    return (
        await s.execute(
            select(BlokadaLogowania).where(BlokadaLogowania.klucz == klucz).with_for_update()
        )
    ).scalar_one_or_none()


async def zaloguj(s: AsyncSession, email: str, haslo: str, ip: str) -> User:
    teraz = sesje.teraz()
    k_pary = klucz_pary(email, ip)
    k_adresu = klucz_adresu(email)
    para = await _wiersz(s, k_pary)
    adres = await _wiersz(s, k_adresu)

    koniec = [sesje._utc(w.zablokowane_do) for w in (para, adres) if _aktywna(w, teraz)]
    if koniec:
        # bez weryfikacji hasla i bez zmiany licznikow, takze przy poprawnym hasle
        minuty = max(1, math.ceil((max(koniec) - teraz).total_seconds() / 60))
        raise ZbytWieleProb(KOMUNIKAT_BLOKADY.format(minuty=minuty))

    user = (
        await s.execute(select(User).where(User.email == _normalizuj(email)))
    ).scalar_one_or_none()
    if user is None:
        _zgodne(_SKROT_ATRAPY, haslo)
    if user is None or not _zgodne(user.haslo_skrot, haslo):
        _zapisz_nieudana(
            s, para, k_pary, teraz, settings.logowanie_limit_prob, settings.logowanie_okno_s
        )
        _zapisz_nieudana(
            s,
            adres,
            k_adresu,
            teraz,
            settings.logowanie_limit_globalny,
            settings.logowanie_okno_globalne_s,
        )
        # commit przed wyjatkiem: rollback wolajacego nie moze skasowac licznika
        await s.commit()
        raise BrakDostepu(KOMUNIKAT_LOGOWANIA)
    # sukces zeruje licznik pary; licznik adresu zostaje, inaczej omijalby limit globalny
    if para is not None:
        await s.delete(para)
    if _HASHER.check_needs_rehash(user.haslo_skrot):
        user.haslo_skrot = _HASHER.hash(haslo)
    await s.commit()
    return user


async def zmien_haslo(
    s: AsyncSession, user: User, stare: str, nowe: str, biezaca_sesja_id: str
) -> None:
    # user moze pochodzic z innej sesji SQLAlchemy, wiec bierzemy go z tej
    zapisany = await s.get(User, user.id)
    if not _zgodne(zapisany.haslo_skrot, stare):
        raise BrakDostepu("nieprawidłowe hasło")
    if len(nowe) < MIN_DLUGOSC_HASLA:
        raise StanNiemozliwy(_ZA_KROTKIE)
    teraz = sesje.teraz()
    zapisany.haslo_skrot = _HASHER.hash(nowe)
    zapisany.haslo_zmienione_at = teraz
    await s.execute(
        update(Sesja)
        .where(
            Sesja.user_id == zapisany.id,
            Sesja.skrot != sesje.skrot(biezaca_sesja_id),
            Sesja.uniewazniona_at.is_(None),
        )
        .values(uniewazniona_at=teraz)
    )
    await s.commit()
