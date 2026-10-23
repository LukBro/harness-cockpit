"""Zasiew bazy deweloperskiej: zespol i runner z tokenem w pliku 0600 (HAR-2).

Uzycie: COCKPIT_DATABASE_URL=... python tools/seed_dev.py
Token trafia wylacznie do pliku runnera (runner.secrets.set_token) - nigdy na
ekran, do logu ani do argv. W bazie zostaje tylko jego hash (BRO-98).
"""
from __future__ import annotations

import asyncio
import secrets as stdlib_secrets
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cockpit.db.models import Runner, Tenant
from cockpit.services.runs import hash_token
from cockpit.settings import settings
from runner import secrets as sekrety

NAZWA_ZESPOLU = "Zespol deweloperski"
NAZWA_RUNNERA = "runner-dev"


async def _zespol(s: AsyncSession) -> Tenant:
    t = (await s.execute(select(Tenant).order_by(Tenant.created_at, Tenant.id).limit(1))
         ).scalar_one_or_none()
    if t is None:
        t = Tenant(name=NAZWA_ZESPOLU)
        s.add(t)
        await s.flush()
    return t


async def zasiej(maker: async_sessionmaker) -> Path:
    """Zaklada zespol i runnera (albo uzywa istniejacych), rotuje token."""
    token = stdlib_secrets.token_urlsafe(32)
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
    print(f"Zespol: {zespol}")
    print(f"Runner: {runner}")
    print(f"Token runnera zapisany w: {sciezka}")


if __name__ == "__main__":
    main()
