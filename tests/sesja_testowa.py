"""Wspolny helper testow: wlasciciel zespolu, czlonkowie, runner i zalogowany klient (HAR-57).

Nie jest fixture'em z conftest.py: kazdy plik sam sklada swiat z tych klockow.
Hasla hashuje argon2, wiec zakladaj jednego wlasciciela na fixture, a pozostalych
czlonkow przez `zaloz_czlonka` (bez hasla, sesja i tak powstaje przez `sesje.utworz`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from cockpit.api.zaleznosci import CIASTECZKO_SESJI, NAGLOWEK_CSRF
from cockpit.db.models import Membership, RolaWZespole, Runner, User
from cockpit.services import konta, sesje
from cockpit.services.runs import hash_token

HASLO_TESTOWE = "haslo-testowe-123"


async def zaloz_wlasciciela(
    maker, email: str = "wlasciciel@example.com", haslo: str = HASLO_TESTOWE,
    nazwa_zespolu: str | None = None,
) -> tuple[User, str]:
    """Rejestruje konto z nowym zespolem (rola owner); zwraca uzytkownika i id zespolu."""
    async with maker() as s:
        user = await konta.zarejestruj(s, email, haslo, nazwa_zespolu)
    async with maker() as s:
        tenant_id = (
            await s.execute(select(Membership.tenant_id).where(Membership.user_id == user.id))
        ).scalar_one()
    return user, tenant_id


async def zaloz_czlonka(maker, tenant_id: str, rola: RolaWZespole, email: str) -> User:
    """Uzytkownik z jednym czlonkostwem w danym zespole i podanej roli."""
    async with maker() as s:
        user = User(email=email, haslo_skrot="argon2-skrot-testowy")
        s.add(user)
        await s.flush()
        s.add(Membership(tenant_id=tenant_id, user_id=user.id, rola=rola))
        await s.commit()
        return user


async def zaloz_runnera(maker, tenant_id: str, nazwa: str, token: str) -> str:
    """Runner zespolu swiezo zameldowany (heartbeat teraz); zwraca jego id.
    Polaczenie ze strumieniem to osobna sprawa (`bus.mark_runner_connected`)."""
    async with maker() as s:
        r = Runner(tenant_id=tenant_id, name=nazwa, token_hash=hash_token(token),
                   last_seen_at=datetime.now(timezone.utc))
        s.add(r)
        await s.commit()
        return r.id


async def zaloguj_klienta(klient, maker, user: User) -> None:
    """Sesja uzytkownika jak po logowaniu: ciasteczko oraz naglowek CSRF na kliencie."""
    async with maker() as s:
        identyfikator, csrf = await sesje.utworz(s, user)
    klient.cookies.set(CIASTECZKO_SESJI, identyfikator)
    klient.headers[NAGLOWEK_CSRF] = csrf
