"""Konta: rejestracja z wlasnym zespolem, logowanie argon2id, zmiana hasla (HAR-44).

BRO-115: cienka warstwa, argon2id. BRO-118: kazdy dostaje wlasny zespol.
D18: zajety adres przy rejestracji ujawniamy. Haslo nie trafia do logow ani do
komunikatow wyjatkow.
"""
from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Membership, RolaWZespole, Sesja, Tenant, User
from cockpit.services import sesje
from cockpit.services.errors import BrakDostepu, StanNiemozliwy

KOMUNIKAT_LOGOWANIA = "nieprawidłowy adres lub hasło"
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


async def zaloguj(s: AsyncSession, email: str, haslo: str) -> User:
    user = (
        await s.execute(select(User).where(User.email == _normalizuj(email)))
    ).scalar_one_or_none()
    if user is None:
        _zgodne(_SKROT_ATRAPY, haslo)
        raise BrakDostepu(KOMUNIKAT_LOGOWANIA)
    if not _zgodne(user.haslo_skrot, haslo):
        raise BrakDostepu(KOMUNIKAT_LOGOWANIA)
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
