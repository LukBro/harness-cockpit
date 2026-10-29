"""Zasiew bazy deweloperskiej: zespol i runner z tokenem w pliku 0600 (HAR-2).

Uzycie: COCKPIT_DATABASE_URL=... python tools/seed_dev.py
Token trafia wylacznie do pliku runnera (runner.secrets.set_token) - nigdy na
ekran, do logu ani do argv. W bazie zostaje tylko jego hash (BRO-98).

Wlasciciel zespolu (HAR-50): adres z COCKPIT_DEV_EMAIL, haslo wylacznie z
COCKPIT_DEV_PASSWORD (nigdy argv, nigdy ekran; w bazie tylko skrot argon2id).
Brak ktorejkolwiek zmiennej -> zasiew bez uzytkownika i komunikat. Zmienne sa
czytane z os.environ, nie z .env, zeby brak zmiennej byl widoczny.
"""
from __future__ import annotations

import asyncio
import os
import secrets as stdlib_secrets
from pathlib import Path

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cockpit.db.models import Membership, RolaWZespole, Runner, Tenant, User
from cockpit.services.runs import hash_token
from cockpit.settings import settings
from runner import secrets as sekrety

NAZWA_ZESPOLU = "Zespol deweloperski"
NAZWA_RUNNERA = "runner-dev"
BRAK_WLASCICIELA = "Brak COCKPIT_DEV_EMAIL lub COCKPIT_DEV_PASSWORD - zasiew bez uzytkownika"


def dane_wlasciciela() -> tuple[str, str] | None:
    """Adres (znormalizowany) i haslo z os.environ; None, gdy ktorekolwiek puste."""
    email = os.environ.get("COCKPIT_DEV_EMAIL", "").strip().lower()
    haslo = os.environ.get("COCKPIT_DEV_PASSWORD", "")
    if not email or not haslo:
        return None
    return email, haslo


async def _zespol(s: AsyncSession) -> Tenant:
    t = (await s.execute(select(Tenant).order_by(Tenant.created_at, Tenant.id).limit(1))
         ).scalar_one_or_none()
    if t is None:
        t = Tenant(name=NAZWA_ZESPOLU)
        s.add(t)
        await s.flush()
    return t


async def _wlasciciel(s: AsyncSession, tenant_id: str, email: str, haslo: str) -> None:
    """Uzytkownik i czlonkostwo owner w zespole; istniejacych nie dubluje ani nie zmienia."""
    u = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if u is None:
        u = User(email=email, haslo_skrot=PasswordHasher().hash(haslo))
        s.add(u)
        await s.flush()
    c = (await s.execute(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == u.id)
    )).scalar_one_or_none()
    if c is None:
        s.add(Membership(tenant_id=tenant_id, user_id=u.id, rola=RolaWZespole.owner))


async def zasiej(maker: async_sessionmaker) -> Path:
    """Zaklada zespol, runnera i (gdy jest w env) wlasciciela; rotuje token."""
    token = stdlib_secrets.token_urlsafe(32)
    wlasciciel = dane_wlasciciela()
    async with maker() as s:
        t = await _zespol(s)
        r = (await s.execute(
            select(Runner).where(Runner.tenant_id == t.id).order_by(Runner.id).limit(1)
        )).scalar_one_or_none()
        if r is None:
            r = Runner(tenant_id=t.id, name=NAZWA_RUNNERA, token_hash=hash_token(token))
            s.add(r)
        else:
            r.token_hash = hash_token(token)   # inaczej token z pliku przestalby dzialac
        if wlasciciel is not None:
            await _wlasciciel(s, t.id, *wlasciciel)
        await s.commit()
    sekrety.set_token("runner", token)
    return sekrety.CONFIG


async def _glowna() -> tuple[Path, str, str]:
    engine = create_async_engine(settings.database_url)   # bez echo=True
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        sciezka = await zasiej(maker)
        async with maker() as s:
            t = await _zespol(s)
            r = (await s.execute(
                select(Runner).where(Runner.tenant_id == t.id).order_by(Runner.id).limit(1)
            )).scalar_one()
            return sciezka, t.name, r.name
    finally:
        await engine.dispose()


def main() -> None:
    sciezka, zespol, runner = asyncio.run(_glowna())
    wlasciciel = dane_wlasciciela()
    print(f"Zespol: {zespol}")
    print(f"Runner: {runner}")
    print(f"Wlasciciel: {wlasciciel[0]}" if wlasciciel else BRAK_WLASCICIELA)
    print(f"Token runnera zapisany w: {sciezka}")


if __name__ == "__main__":
    main()
