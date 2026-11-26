"""Tokeny runnera: wydanie, lista, odwolanie (HAR-119).

Token jawny istnieje tylko w wartosci zwroconej z `wydaj_token_runnera`; w bazie jest
jego sha256 i osiem pierwszych znakow (prefiks, do rozpoznania na liscie). Token nie trafia
do logow ani do wyjatkow.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Runner

from .dostep import Kontekst, Uprawnienie, wymagaj
from .errors import NieZnaleziono
from .runs import hash_token
from .sesje import na_utc

PREFIKS_TOKENU = "ckr_"
DLUGOSC_PREFIKSU_NA_LISCIE = 8


def na_iso(chwila: datetime | None) -> str | None:
    """Chwila jako ISO 8601 w UTC; brak chwili to None, nigdy zero."""
    return None if chwila is None else na_utc(chwila).astimezone(timezone.utc).isoformat()


async def wydaj_token_runnera(
    s: AsyncSession, kontekst: Kontekst, nazwa: str, wazny_do: datetime | None = None,
) -> tuple[Runner, str]:
    """Tworzy runnera zespolu z nowym tokenem; zwraca `(runner, token jawny)`."""
    wymagaj(kontekst, Uprawnienie.podlaczanie_projektow)
    token = PREFIKS_TOKENU + secrets.token_urlsafe(32)
    runner = Runner(
        tenant_id=kontekst.tenant_id, name=nazwa, token_hash=hash_token(token),
        token_prefiks=token[:DLUGOSC_PREFIKSU_NA_LISCIE],
        token_utworzony_at=datetime.now(timezone.utc),
        token_wazny_do=None if wazny_do is None else na_utc(wazny_do),   # bez strefy to UTC
    )
    s.add(runner)
    await s.commit()
    return runner, token


async def tokeny_runnerow(s: AsyncSession, kontekst: Kontekst) -> list[Runner]:
    """Runnery zespolu (takze z odwolanym tokenem), najnowszy token pierwszy."""
    wiersze = (
        await s.execute(
            select(Runner).where(Runner.tenant_id == kontekst.tenant_id)
            .order_by(Runner.token_utworzony_at.desc().nulls_last(),Runner.id)
        )
    ).scalars().all()
    return list(wiersze)


async def odwolaj(s: AsyncSession, kontekst: Kontekst, runner_id: str) -> Runner:
    """Odwoluje token runnera zespolu. Ponowne odwolanie zachowuje pierwsza chwile.

    UPDATE jest warunkowy (`odwolany_at IS NULL`), wiec dwa rownolegle odwolania
    nie nadpisza chwili odwolania."""
    wymagaj(kontekst, Uprawnienie.podlaczanie_projektow)
    istnieje = (
        await s.execute(
            select(Runner.id).where(
                Runner.id == runner_id, Runner.tenant_id == kontekst.tenant_id)
        )
    ).scalar_one_or_none()
    if istnieje is None:
        raise NieZnaleziono("runner nieznany")
    await s.execute(
        update(Runner)
        .where(Runner.id == runner_id, Runner.tenant_id == kontekst.tenant_id,
               Runner.odwolany_at.is_(None))
        .values(odwolany_at=datetime.now(timezone.utc))
    )
    await s.commit()
    runner = (
        await s.execute(
            select(Runner).where(
                Runner.id == runner_id, Runner.tenant_id == kontekst.tenant_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return runner
