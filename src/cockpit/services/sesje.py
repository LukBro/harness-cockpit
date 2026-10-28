"""Sesje w bazie: utworzenie, rozstrzygniecie, wylogowanie, token CSRF (HAR-44, BRO-115).

W bazie leza wylacznie skroty identyfikatora i tokenu CSRF; jawne wartosci zna tylko
przegladarka. Sesje da sie uniewaznic przed terminem, dlatego nie JWT.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Membership, Sesja, User
from cockpit.services.dostep import Kontekst
from cockpit.services.errors import BrakDostepu, StanNiemozliwy
from cockpit.settings import settings

KOMUNIKAT_SESJI = "sesja wygasła, zaloguj się"


def skrot(napis: str) -> str:
    return hashlib.sha256(napis.encode("utf-8")).hexdigest()


def teraz() -> datetime:
    # jeden zegar UTC dla kont i sesji; aware datetime, bez czasu lokalnego
    return datetime.now(timezone.utc)


def _utc(dt: datetime) -> datetime:
    # SQLite oddaje naive datetime z kolumn DateTime(timezone=True); to zawsze UTC
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _aktywna(s: AsyncSession, identyfikator: str) -> Sesja:
    sesja = (
        await s.execute(select(Sesja).where(Sesja.skrot == skrot(identyfikator)))
    ).scalar_one_or_none()
    if sesja is None or sesja.uniewazniona_at is not None or _utc(sesja.wygasa_at) <= teraz():
        raise BrakDostepu(KOMUNIKAT_SESJI)
    return sesja


async def utworz(s: AsyncSession, user: User) -> tuple[str, str]:
    user_id = user.id
    tenant_id = (
        await s.execute(
            select(Membership.tenant_id)
            .where(Membership.user_id == user_id)
            .order_by(Membership.created_at, Membership.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if tenant_id is None:
        raise StanNiemozliwy("użytkownik nie należy do żadnego zespołu")
    identyfikator = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    s.add(
        Sesja(
            skrot=skrot(identyfikator),
            user_id=user_id,
            tenant_id=tenant_id,
            csrf_skrot=skrot(csrf),
            wygasa_at=teraz() + timedelta(days=settings.sesja_wazna_dni),
        )
    )
    await s.commit()
    return identyfikator, csrf


async def rozstrzygnij(s: AsyncSession, identyfikator: str) -> Kontekst:
    sesja = await _aktywna(s, identyfikator)
    user_id = sesja.user_id
    tenant_id = sesja.tenant_id
    rola = (
        await s.execute(
            select(Membership.rola).where(
                Membership.user_id == user_id, Membership.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if rola is None:
        raise BrakDostepu(KOMUNIKAT_SESJI)
    sesja.ostatnio_at = teraz()
    await s.commit()
    return Kontekst(user_id=user_id, tenant_id=tenant_id, rola=rola)


async def wyloguj(s: AsyncSession, identyfikator: str) -> None:
    await s.execute(
        update(Sesja)
        .where(Sesja.skrot == skrot(identyfikator), Sesja.uniewazniona_at.is_(None))
        .values(uniewazniona_at=teraz())
    )
    await s.commit()


async def wyloguj_wszedzie(s: AsyncSession, user_id: str) -> None:
    await s.execute(
        update(Sesja)
        .where(Sesja.user_id == user_id, Sesja.uniewazniona_at.is_(None))
        .values(uniewazniona_at=teraz())
    )
    await s.commit()


async def odnow_csrf(s: AsyncSession, identyfikator: str) -> str:
    sesja = await _aktywna(s, identyfikator)
    token = secrets.token_urlsafe(32)
    sesja.csrf_skrot = skrot(token)
    await s.commit()
    return token


async def sprawdz_csrf(s: AsyncSession, identyfikator: str, token: str) -> None:
    sesja = await _aktywna(s, identyfikator)
    if not hmac.compare_digest(skrot(token), sesja.csrf_skrot):
        raise BrakDostepu("nieprawidłowy token CSRF")
